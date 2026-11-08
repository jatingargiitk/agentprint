"""Snapshot, diff and revert the machine state a coding agent can touch outside a repo."""

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime

SNAPSHOT_VERSION = 1
MAX_CONTENT = 256 * 1024  # bytes; larger files are hashed only
MAX_FILES_PER_ROOT = 20000
COMMAND_TIMEOUT = 30

# Paths relative to the home directory. Files or directories; missing ones are
# still watched so that creating them later shows up as "added".
DEFAULT_PATHS = [
    # shell startup files
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
    ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".inputrc",
    # git
    ".gitconfig", ".gitignore_global", ".git-credentials",
    # package managers
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc", ".condarc", ".pip",
    ".cargo/config", ".cargo/config.toml",
    # ssh, cloud, containers
    ".ssh", ".netrc", ".aws/config", ".aws/credentials", ".docker/config.json",
    ".kube/config",
    # XDG config (fish, git, pip, gh, ... all live here)
    ".config",
    # PATH directories
    ".local/bin", "bin", ".cargo/bin", "go/bin",
    # coding agent configuration
    ".claude/settings.json", ".claude/CLAUDE.md", ".claude/commands", ".claude/agents",
    ".cursor/mcp.json", ".codex/config.toml", ".codex/AGENTS.md", ".gemini/settings.json",
]

# fnmatch patterns matched against the path relative to home (and the full key).
DEFAULT_IGNORE = [
    # browser / Electron app profiles: large and constantly rewritten
    ".config/google-chrome*", ".config/chromium*", ".config/BraveSoftware",
    ".config/Code*", ".config/Cursor", ".config/Slack", ".config/discord",
    ".config/Microsoft", ".config/libreoffice", ".config/pulse", ".config/dconf",
    "*/__pycache__", "*/Cache", "*/CacheStorage", "*/GPUCache", "*.log",
    "*.sock", "*.lock",
]

# (name, argv) of read-only commands whose output is recorded.
COMMANDS = [
    ("crontab", ["crontab", "-l"]),
    ("npm-global", ["npm", "ls", "-g", "--depth=0"]),
    ("pip", ["pip", "list", "--format=freeze", "--disable-pip-version-check"]),
    ("pipx", ["pipx", "list", "--short"]),
    ("brew", ["brew", "list", "--versions"]),
]

SECRET_NAMES = [
    "id_*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", ".env", ".env.*",
    "*.env", ".netrc", ".git-credentials", ".pypirc", "credentials", "*credentials*.json",
    "*secret*", "*token*",
]
NOT_SECRET_NAMES = ["*.pub", "known_hosts*", "authorized_keys*"]

SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bnpm_[A-Za-z0-9]{20,}"
    r"|(?i:client-key-data|private[_-]?key)[\"']?\s*[:=]"
    r"|(?i:(?:api[_-]?key|secret|token|passw(?:or)?d|auth)[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?[^\s\"'#]{8,})"
)


class AgentprintError(Exception):
    pass


# ---------------------------------------------------------------- paths

def resolve_home(home=None):
    home = home or os.environ.get("AGENTPRINT_HOME") or os.path.expanduser("~")
    return os.path.realpath(os.path.abspath(home))


def resolve_store(home, store=None):
    store = store or os.environ.get("AGENTPRINT_STORE") or os.path.join(home, ".agentprint")
    return os.path.abspath(store)


def to_key(path, home):
    path = os.path.abspath(os.path.expanduser(path)) if not path.startswith("~/") else os.path.join(home, path[2:])
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~/" + os.path.relpath(path, home).replace(os.sep, "/")
    return path


def from_key(key, home):
    if key == "~":
        return home
    if key.startswith("~/"):
        return os.path.join(home, *key[2:].split("/"))
    return key


def user_key(path, home):
    """Turn a user-supplied path (relative to home, ~/..., or absolute) into a key."""
    if path.startswith("~/") or os.path.isabs(path):
        return to_key(path, home)
    return to_key(os.path.join(home, path), home)


def is_ignored(key, patterns):
    rel = key[2:] if key.startswith("~/") else key
    for pat in patterns:
        if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(key, pat):
            return True
    return False


def is_secret_name(key):
    base = key.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatchcase(base, p) for p in NOT_SECRET_NAMES):
        return False
    return any(fnmatch.fnmatchcase(base, p) for p in SECRET_NAMES)


# ---------------------------------------------------------------- config

def load_config(store):
    path = os.path.join(store, "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        raise AgentprintError("cannot read %s: %s" % (path, exc))
    if not isinstance(cfg, dict):
        raise AgentprintError("%s must contain a JSON object" % path)
    return cfg


# ---------------------------------------------------------------- scanning

def _content_info(data, key):
    """Return (content, note) for raw bytes."""
    if is_secret_name(key):
        return None, "secret"
    if len(data) > MAX_CONTENT:
        return None, "large"
    if b"\0" in data:
        return None, "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "binary"
    if SECRET_CONTENT.search(text):
        return None, "secret"
    return text, None


def _read_file(path, key, entry):
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            if size > MAX_CONTENT:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
                data = None
            else:
                data = fh.read()
                h.update(data)
    except OSError as exc:
        entry.update(sha256=None, size=None, content=None, note="unreadable: %s" % exc.strerror)
        return entry
    entry["sha256"] = h.hexdigest()
    entry["size"] = size
    if data is None:
        entry["content"], entry["note"] = None, ("secret" if is_secret_name(key) else "large")
    else:
        entry["content"], entry["note"] = _content_info(data, key)
    return entry


def scan_path(path, key):
    """Entry for one non-directory path, or None if it is not a regular file/symlink."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        entry = {"type": "symlink", "target": os.readlink(path)}
        try:
            tst = os.stat(path)
        except OSError:
            return entry  # dangling
        if stat.S_ISREG(tst.st_mode):
            entry["mode"] = stat.S_IMODE(tst.st_mode)
            _read_file(path, key, entry)
        return entry
    if stat.S_ISREG(st.st_mode):
        entry = {"type": "file", "mode": stat.S_IMODE(st.st_mode)}
        return _read_file(path, key, entry)
    return None


def scan(home, roots, ignore):
    """Scan watched roots. Returns (files, dirs, truncated)."""
    files, dirs, truncated = {}, set(), []
    for root_key in roots:
        root = from_key(root_key, home)
        if is_ignored(root_key, ignore):
            continue
        if os.path.isdir(root) and not os.path.islink(root):
            count = 0
            dirs.add(root_key)
            for dirpath, dirnames, filenames in os.walk(root):
                keep = []
                for d in sorted(dirnames):
                    full = os.path.join(dirpath, d)
                    k = to_key(full, home)
                    if is_ignored(k, ignore):
                        continue
                    if os.path.islink(full):
                        files[k] = scan_path(full, k) or {"type": "symlink", "target": os.readlink(full)}
                        continue
                    dirs.add(k)
                    keep.append(d)
                dirnames[:] = keep
                for f in sorted(filenames):
                    full = os.path.join(dirpath, f)
                    k = to_key(full, home)
                    if is_ignored(k, ignore):
                        continue
                    if count >= MAX_FILES_PER_ROOT:
                        if root_key not in truncated:
                            truncated.append(root_key)
                        continue
                    entry = scan_path(full, k)
                    if entry is not None:
                        files[k] = entry
                        count += 1
        else:
            entry = scan_path(root, root_key)
            if entry is not None:
                files[root_key] = entry
    return files, sorted(dirs), truncated


def run_commands():
    out = {}
    for name, argv in COMMANDS:
        exe = shutil.which(argv[0])
        if exe is None:
            continue
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            out[name] = {"argv": argv, "error": str(exc), "sha256": None, "output": None, "note": "error"}
            continue
        data = proc.stdout
        text = data.decode("utf-8", "replace")
        secret = bool(SECRET_CONTENT.search(text))
        out[name] = {
            "argv": argv,
            "sha256": hashlib.sha256(data).hexdigest(),
            "output": None if secret else text,
            "note": "secret" if secret else None,
        }
    return out


# ---------------------------------------------------------------- snapshots

def watch_spec(home, store, extra_paths=(), config=None):
    config = config or {}
    roots = [user_key(p, home) for p in DEFAULT_PATHS]
    for p in list(config.get("paths", [])) + list(extra_paths):
        k = user_key(p, home)
        if k not in roots:
            roots.append(k)
    ignore = list(DEFAULT_IGNORE) + list(config.get("ignore", []))
    store_key = to_key(store, home)
    ignore.append(store_key[2:] if store_key.startswith("~/") else store_key)
    return roots, ignore


def take_snapshot(home, store, name=None, extra_paths=(), commands=True):
    config = load_config(store)
    roots, ignore = watch_spec(home, store, extra_paths, config)
    files, dirs, truncated = scan(home, roots, ignore)
    now = datetime.now()
    snap_id = now.strftime("%Y%m%d-%H%M%S")
    if name:
        if not re.match(r"^[A-Za-z0-9._-]+$", name):
            raise AgentprintError("snapshot name may only contain letters, digits, '.', '_' and '-'")
        snap_id += "-" + name
    snapdir = os.path.join(store, "snapshots")
    os.makedirs(store, mode=0o700, exist_ok=True)
    os.makedirs(snapdir, mode=0o700, exist_ok=True)
    base, n = snap_id, 2
    while os.path.exists(os.path.join(snapdir, snap_id + ".json")):
        snap_id = "%s.%d" % (base, n)
        n += 1
    snap = {
        "version": SNAPSHOT_VERSION,
        "id": snap_id,
        "created": now.isoformat(),
        "home": home,
        "roots": roots,
        "ignore": ignore,
        "files": files,
        "dirs": dirs,
        "truncated": truncated,
        "commands": run_commands() if commands else None,
    }
    path = os.path.join(snapdir, snap_id + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    return snap


def list_snapshots(store):
    snapdir = os.path.join(store, "snapshots")
    if not os.path.isdir(snapdir):
        return []
    snaps = []
    for fn in os.listdir(snapdir):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(snapdir, fn), encoding="utf-8") as fh:
                    snaps.append(json.load(fh))
            except (OSError, ValueError):
                continue
    snaps.sort(key=lambda s: s.get("created", ""))
    return snaps


def load_snapshot(store, snap_id=None):
    snaps = list_snapshots(store)
    if not snaps:
        raise AgentprintError("no snapshots in %s (run `agentprint snap` first)" % store)
    if snap_id is None:
        return snaps[-1]
    matches = [s for s in snaps if s["id"] == snap_id] or [s for s in snaps if s["id"].startswith(snap_id)]
    if not matches:
        raise AgentprintError("no snapshot matching %r" % snap_id)
    if len(matches) > 1:
        raise AgentprintError("%r matches %d snapshots; be more specific" % (snap_id, len(matches)))
    return matches[0]


# ---------------------------------------------------------------- diff

def _same(a, b):
    return all(a.get(f) == b.get(f) for f in ("type", "sha256", "mode", "target"))


NOTE_TEXT = {
    "secret": "secret-looking file: hash only, contents not stored or shown",
    "large": "larger than %d KiB: hash only" % (MAX_CONTENT // 1024),
    "binary": "binary: hash only",
}


def _note_text(note):
    return NOTE_TEXT.get(note, note)


def _text_diff(a_text, b_text, a_name, b_name):
    a_lines = a_text.splitlines() if a_text is not None else []
    b_lines = b_text.splitlines() if b_text is not None else []
    return "\n".join(difflib.unified_diff(a_lines, b_lines, a_name, b_name, lineterm=""))


def _file_patch(key, a, b):
    lines = []
    a_name = "a/" + key if a else "/dev/null"
    b_name = "b/" + key if b else "/dev/null"
    if a and b and a.get("type") != b.get("type"):
        lines.append("type: %s -> %s" % (a["type"], b["type"]))
    if a and b and a.get("target") != b.get("target"):
        lines.append("symlink target: %s -> %s" % (a.get("target"), b.get("target")))
    elif b and not a and b.get("type") == "symlink":
        lines.append("symlink -> %s" % b.get("target"))
    elif a and not b and a.get("type") == "symlink":
        lines.append("was symlink -> %s" % a.get("target"))
    if a and b and a.get("mode") != b.get("mode") and a.get("mode") is not None and b.get("mode") is not None:
        lines.append("mode: %o -> %o" % (a["mode"], b["mode"]))
    if (a or {}).get("sha256") != (b or {}).get("sha256"):
        hidden = [e.get("note") for e in (a, b) if e and e.get("sha256") and e.get("content") is None]
        if hidden:
            lines.append("(%s; sha256 %s -> %s)" % (
                _note_text(hidden[0]),
                ((a or {}).get("sha256") or "none")[:12],
                ((b or {}).get("sha256") or "none")[:12]))
        else:
            patch = _text_diff((a or {}).get("content"), (b or {}).get("content"), a_name, b_name)
            if patch:
                lines.append(patch)
    return "\n".join(lines)


def compute_diff(snap, home):
    """Compare a snapshot with the current state of the same watched paths."""
    files, dirs, truncated = scan(home, snap["roots"], snap["ignore"])
    old = snap["files"]
    changes = []
    for key in sorted(set(old) | set(files)):
        a, b = old.get(key), files.get(key)
        if a is not None and b is not None and _same(a, b):
            continue
        status = "added" if a is None else "deleted" if b is None else "modified"
        secret = any(e and e.get("note") == "secret" for e in (a, b))
        changes.append({
            "path": key,
            "status": status,
            "secret": secret,
            "patch": _file_patch(key, a, b),
            "_old": a,
            "_new": b,
        })
    cmd_changes = []
    if snap.get("commands") is not None:
        current = run_commands()
        for name in sorted(set(snap["commands"]) | set(current)):
            a, b = snap["commands"].get(name), current.get(name)
            if a and b and a.get("sha256") == b.get("sha256"):
                continue
            if a is None or b is None:
                status = "unavailable" if b is None else "new"
                patch = ""
            else:
                status = "changed"
                if a.get("output") is None or b.get("output") is None:
                    patch = "(output not shown: contains secret-looking text)"
                else:
                    patch = _text_diff(a["output"], b["output"], "a/" + name, "b/" + name)
            argv = (a or b)["argv"]
            cmd_changes.append({"name": name, "command": " ".join(argv), "status": status,
                                "patch": patch, "_old": a, "_new": b})
    return {"snapshot": snap["id"], "created": snap["created"], "changes": changes,
            "commands": cmd_changes, "current_dirs": dirs, "truncated": truncated}


def diff_has_changes(d):
    return bool(d["changes"] or [c for c in d["commands"] if c["status"] == "changed"])


def diff_to_json(d):
    return {
        "snapshot": d["snapshot"],
        "created": d["created"],
        "changed": diff_has_changes(d),
        "files": [{k: v for k, v in c.items() if not k.startswith("_")} for c in d["changes"]],
        "commands": [{k: v for k, v in c.items() if not k.startswith("_")} for c in d["commands"]],
        "truncated": d["truncated"],
    }


STATUS_LETTER = {"added": "A", "modified": "M", "deleted": "D"}


def format_diff(d, patches=True):
    out = []
    created = d["created"].split(".")[0].replace("T", " ")
    if not diff_has_changes(d):
        out.append("no changes since snapshot %s (%s)" % (d["snapshot"], created))
        return "\n".join(out)
    out.append("changes since snapshot %s (%s):" % (d["snapshot"], created))
    for c in d["changes"]:
        out.append("  %s %s%s" % (STATUS_LETTER[c["status"]], c["path"], "  [secret: hash only]" if c["secret"] else ""))
    for c in d["commands"]:
        if c["status"] == "changed":
            out.append("  ~ $ %s" % c["command"])
    for r in d["truncated"]:
        out.append("  warning: %s has more than %d files; only the first %d are compared" % (r, MAX_FILES_PER_ROOT, MAX_FILES_PER_ROOT))
    if patches:
        for c in d["changes"]:
            if c["patch"]:
                out.append("")
                out.append("=== %s %s" % (c["status"], c["path"]))
                out.append(c["patch"])
        for c in d["commands"]:
            if c["status"] == "changed" and c["patch"]:
                out.append("")
                out.append("=== output of $ %s" % c["command"])
                out.append(c["patch"])
    return "\n".join(out)


# ---------------------------------------------------------------- revert

def plan_revert(d):
    """Return a list of actions (dicts) that restore the snapshot state for files."""
    actions = []
    for c in d["changes"]:
        key, a, b = c["path"], c["_old"], c["_new"]
        if a is None:
            actions.append({"op": "remove", "path": key})
            continue
        if a["type"] == "symlink":
            if b is None or b.get("type") != "symlink" or b.get("target") != a["target"]:
                actions.append({"op": "symlink", "path": key, "target": a["target"]})
                continue
            # same link, content of the target changed: write through the link
        if b is not None and b.get("sha256") == a.get("sha256") and b.get("type") == a["type"]:
            if a.get("mode") is not None:
                actions.append({"op": "chmod", "path": key, "mode": a["mode"]})
            continue
        if a.get("content") is None:
            reason = _note_text(a.get("note")) if a.get("note") else "contents not stored"
            actions.append({"op": "skip", "path": key, "reason": reason})
            continue
        actions.append({"op": "write", "path": key, "content": a["content"], "mode": a.get("mode"),
                        "replace_link": a["type"] == "file" and b is not None and b.get("type") == "symlink"})
    return actions


def _hints(d):
    hints = []
    for c in d["commands"]:
        if c["status"] != "changed" or c["_old"] is None or c["_old"].get("output") is None or c["_new"].get("output") is None:
            if c["status"] == "changed":
                hints.append("$ %s changed (not reverted)" % c["command"])
            continue
        old = set(c["_old"]["output"].splitlines())
        added = [l for l in c["_new"]["output"].splitlines() if l not in old]
        if c["name"] == "pip":
            pkgs = [l.split("==")[0] for l in added if "==" in l]
            if pkgs:
                hints.append("pip packages added (not reverted): pip uninstall %s" % " ".join(pkgs))
                continue
        if c["name"] == "npm-global":
            pkgs = []
            for l in added:
                m = re.search(r"[\u2500-\u257f+`|\\ -]+\s*(@?[^@\s]+)@", l)
                if m:
                    pkgs.append(m.group(1))
            if pkgs:
                hints.append("npm global packages added (not reverted): npm uninstall -g %s" % " ".join(pkgs))
                continue
        hints.append("$ %s changed (not reverted)" % c["command"])
    return hints


def describe_action(a):
    if a["op"] == "remove":
        return "remove   %s" % a["path"]
    if a["op"] == "write":
        return "restore  %s" % a["path"]
    if a["op"] == "chmod":
        return "chmod    %s %o" % (a["path"], a["mode"])
    if a["op"] == "symlink":
        return "relink   %s -> %s" % (a["path"], a["target"])
    return "skip     %s (%s)" % (a["path"], a["reason"])


def apply_revert(actions, d, snap, home):
    """Apply actions. Returns list of (action, error-or-None)."""
    results = []
    for a in actions:
        path = from_key(a["path"], home)
        try:
            if a["op"] == "remove":
                os.unlink(path)
            elif a["op"] == "chmod":
                os.chmod(path, a["mode"])
            elif a["op"] == "symlink":
                if os.path.lexists(path):
                    os.unlink(path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.symlink(a["target"], path)
            elif a["op"] == "write":
                if a.get("replace_link") and os.path.islink(path):
                    os.unlink(path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(a["content"].encode("utf-8"))
                if a.get("mode") is not None:
                    os.chmod(path, a["mode"])
            else:
                results.append((a, None))
                continue
            results.append((a, None))
        except OSError as exc:
            results.append((a, exc.strerror or str(exc)))
    # remove directories created since the snapshot, if they are now empty
    old_dirs = set(snap.get("dirs", []))
    for key in sorted(set(d["current_dirs"]) - old_dirs, key=lambda k: -k.count("/")):
        path = from_key(key, home)
        try:
            os.rmdir(path)
            results.append(({"op": "rmdir", "path": key}, None))
        except OSError:
            pass
    return results
