import argparse
import json
import os
import subprocess
import sys

from . import __version__
from . import core


def _common(p):
    p.add_argument("--home", help="home directory to watch (default: $AGENTPRINT_HOME or ~)")
    p.add_argument("--store", help="where snapshots live (default: $AGENTPRINT_STORE or <home>/.agentprint)")


def _snap_opts(p):
    p.add_argument("--name", help="label appended to the snapshot id")
    p.add_argument("--path", action="append", default=[], metavar="PATH",
                   help="extra file or directory to watch (relative to home, ~/..., or absolute); repeatable")
    p.add_argument("--no-commands", action="store_true",
                   help="do not record crontab / global package listings")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentprint",
        description="Snapshot the config and PATH state outside your repo, then diff and revert "
                    "what a coding-agent session changed.")
    parser.add_argument("--version", action="version", version="agentprint " + __version__)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("snap", help="record a snapshot of the watched paths")
    _common(p)
    _snap_opts(p)

    p = sub.add_parser("list", help="list snapshots")
    _common(p)

    p = sub.add_parser("paths", help="print the watched paths and ignore patterns")
    _common(p)
    p.add_argument("--path", action="append", default=[], metavar="PATH")

    p = sub.add_parser("diff", help="show what changed since a snapshot (exit 1 if anything did)")
    _common(p)
    p.add_argument("snapshot", nargs="?", help="snapshot id or prefix (default: latest)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--stat", action="store_true", help="list changed paths only, no patches")

    p = sub.add_parser("revert", help="restore watched files to a snapshot")
    _common(p)
    p.add_argument("snapshot", nargs="?", help="snapshot id or prefix (default: latest)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("run", help="snap, run a command, then diff: agentprint run -- <cmd> [args]")
    _common(p)
    _snap_opts(p)
    p.add_argument("--check", action="store_true",
                   help="exit 1 if the command succeeded but changed watched state")
    p.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _ctx(args):
    home = core.resolve_home(args.home)
    if not os.path.isdir(home):
        raise core.AgentprintError("home directory %s does not exist" % home)
    return home, core.resolve_store(home, args.store)


def _snap_summary(snap):
    files = snap["files"]
    hidden = sum(1 for e in files.values() if e.get("note") == "secret")
    parts = ["snapshot %s: %d files under %d watched paths" % (snap["id"], len(files), len(snap["roots"]))]
    if snap["commands"] is not None:
        names = sorted(snap["commands"])
        parts.append("commands recorded: %s" % (", ".join(names) if names else "none available"))
    if hidden:
        parts.append("%d secret-looking file(s) stored as hash only" % hidden)
    return "\n".join(parts)


def cmd_snap(args):
    home, store = _ctx(args)
    snap = core.take_snapshot(home, store, args.name, args.path, not args.no_commands)
    print(_snap_summary(snap))
    for r in snap["truncated"]:
        print("warning: %s has more than %d files; only the first %d were recorded" % (r, core.MAX_FILES_PER_ROOT, core.MAX_FILES_PER_ROOT))
    return 0


def cmd_list(args):
    home, store = _ctx(args)
    snaps = core.list_snapshots(store)
    if not snaps:
        print("no snapshots in %s" % store)
        return 0
    for s in snaps:
        print("%-32s %s  %d files" % (s["id"], s["created"].split(".")[0].replace("T", " "), len(s["files"])))
    return 0


def cmd_paths(args):
    home, store = _ctx(args)
    roots, ignore = core.watch_spec(home, store, args.path, core.load_config(store))
    print("watched:")
    for r in roots:
        print("  " + r)
    print("ignored:")
    for i in ignore:
        print("  " + i)
    return 0


def cmd_diff(args):
    home, store = _ctx(args)
    snap = core.load_snapshot(store, args.snapshot)
    d = core.compute_diff(snap, home)
    if args.json:
        print(json.dumps(core.diff_to_json(d), indent=2))
    else:
        print(core.format_diff(d, patches=not args.stat))
    return 1 if core.diff_has_changes(d) else 0


def cmd_revert(args):
    home, store = _ctx(args)
    snap = core.load_snapshot(store, args.snapshot)
    d = core.compute_diff(snap, home)
    actions = core.plan_revert(d)
    hints = core._hints(d)
    if not actions and not hints:
        print("nothing to revert: watched files match snapshot %s" % snap["id"])
        return 0
    print("revert to snapshot %s:" % snap["id"])
    for a in actions:
        print("  " + core.describe_action(a))
    for h in hints:
        print("  note: " + h)
    todo = [a for a in actions if a["op"] != "skip"]
    if args.dry_run or not todo:
        if args.dry_run:
            print("dry run: nothing changed")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to revert without confirmation; re-run with --yes", file=sys.stderr)
            return 2
        answer = input("apply %d change(s)? [y/N] " % len(todo)).strip().lower()
        if answer not in ("y", "yes"):
            print("aborted")
            return 1
    results = core.apply_revert(actions, d, snap, home)
    failed = [(a, e) for a, e in results if e]
    done = [a for a, e in results if not e and a["op"] != "skip"]
    for a, e in failed:
        print("  failed: %s: %s" % (core.describe_action(a), e), file=sys.stderr)
    skipped = sum(1 for a in actions if a["op"] == "skip")
    print("reverted %d change(s)%s%s" % (
        len(done), ", %d skipped" % skipped if skipped else "", ", %d failed" % len(failed) if failed else ""))
    return 1 if failed else 0


def cmd_run(args):
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise core.AgentprintError("usage: agentprint run [options] -- <command> [args...]")
    home, store = _ctx(args)
    snap = core.take_snapshot(home, store, args.name or "run", args.path, not args.no_commands)
    print("agentprint: snapshot %s taken; running: %s" % (snap["id"], " ".join(command)), file=sys.stderr)
    sys.stderr.flush()
    try:
        rc = subprocess.call(command)
    except OSError as exc:
        raise core.AgentprintError("cannot run %s: %s" % (command[0], exc.strerror))
    except KeyboardInterrupt:
        rc = 130
    sys.stdout.flush()
    d = core.compute_diff(snap, home)
    print("agentprint: command exited with status %d" % rc, file=sys.stderr)
    sys.stderr.flush()
    print(core.format_diff(d))
    if core.diff_has_changes(d):
        print("\nundo file changes with: agentprint revert %s" % snap["id"])
    if rc == 0 and args.check and core.diff_has_changes(d):
        return 1
    return rc


COMMANDS = {"snap": cmd_snap, "list": cmd_list, "paths": cmd_paths, "diff": cmd_diff,
            "revert": cmd_revert, "run": cmd_run}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    try:
        return COMMANDS[args.cmd](args)
    except core.AgentprintError as exc:
        print("agentprint: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
