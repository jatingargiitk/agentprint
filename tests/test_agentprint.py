import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentprint import cli, core  # noqa: E402

# fake commands run with PATH=fakebin only, so use shell builtins instead of cat
CAT = "#!/bin/sh\nwhile IFS= read -r line; do printf '%%s\\n' \"$line\"; done < %s\n"
TOKEN = "npm_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["AGENTPRINT_HOME"] = self.home
        os.environ.pop("AGENTPRINT_STORE", None)
        write(self.h(".bashrc"), "export EDITOR=vim\nalias ll='ls -l'\n")
        write(self.h(".gitconfig"), "[user]\n\tname = Dev\n")
        write(self.h(".ssh/config"), "Host gh\n  HostName github.com\n")
        write(self.h(".ssh/id_ed25519"), "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n", 0o600)
        write(self.h(".npmrc"), "//registry.npmjs.org/:_authToken=%s\n" % TOKEN)
        write(self.h(".local/bin/tool"), "#!/bin/sh\necho tool\n", 0o755)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def h(self, rel):
        return os.path.join(self.home, rel)

    def ap(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def snap(self, *extra):
        rc, out, err = self.ap("snap", "--no-commands", *extra)
        self.assertEqual(rc, 0, err)
        return out


class SnapDiffTest(Base):
    def test_no_changes_exits_zero(self):
        self.snap()
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 0)
        self.assertIn("no changes since snapshot", out)

    def test_modified_added_deleted(self):
        self.snap()
        write(self.h(".bashrc"), read(self.h(".bashrc")) + 'export PATH="$HOME/.agent/bin:$PATH"\n')
        write(self.h(".local/bin/helper"), "#!/bin/sh\n", 0o755)
        os.unlink(self.h(".gitconfig"))
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 1)
        self.assertIn("  M ~/.bashrc", out)
        self.assertIn("  A ~/.local/bin/helper", out)
        self.assertIn("  D ~/.gitconfig", out)
        self.assertIn('+export PATH="$HOME/.agent/bin:$PATH"', out)
        self.assertIn("-\tname = Dev", out)

    def test_missing_watched_path_created_later(self):
        self.snap()
        write(self.h(".zshrc"), "source ~/.agentenv\n")
        write(self.h(".claude/settings.json"), "{}\n")
        rc, out, _ = self.ap("diff", "--stat")
        self.assertEqual(rc, 1)
        self.assertIn("A ~/.zshrc", out)
        self.assertIn("A ~/.claude/settings.json", out)
        self.assertNotIn("+source", out)  # --stat: no patches

    def test_mode_change(self):
        self.snap()
        os.chmod(self.h(".local/bin/tool"), 0o644)
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 1)
        self.assertIn("mode: 755 -> 644", out)

    def test_json(self):
        self.snap()
        write(self.h(".gitconfig"), "[user]\n\tname = Agent\n")
        rc, out, _ = self.ap("diff", "--json")
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertTrue(data["changed"])
        self.assertEqual([(f["path"], f["status"]) for f in data["files"]], [("~/.gitconfig", "modified")])
        self.assertIn("+\tname = Agent", data["files"][0]["patch"])

    def test_extra_path_and_config(self):
        write(self.h("work/notes.txt"), "one\n")
        write(self.h("other.cfg"), "a=1\n")
        os.makedirs(self.h(".agentprint"))
        write(self.h(".agentprint/config.json"), json.dumps({"paths": ["other.cfg"]}))
        self.snap("--path", "work/notes.txt")
        write(self.h("work/notes.txt"), "two\n")
        write(self.h("other.cfg"), "a=2\n")
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 1)
        self.assertIn("M ~/work/notes.txt", out)
        self.assertIn("M ~/other.cfg", out)

    def test_store_and_ignore_patterns_not_watched(self):
        self.snap()
        write(self.h(".config/google-chrome/Default/Prefs"), "x\n")
        write(self.h(".config/tool/__pycache__/x.pyc"), "x\n")
        self.snap()  # writes into ~/.agentprint, which must not show up
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 0, out)

    def test_symlinked_dotfile(self):
        write(self.h("dotfiles/zshrc"), "setopt autocd\n")
        os.symlink(self.h("dotfiles/zshrc"), self.h(".zshrc"))
        self.snap()
        write(self.h("dotfiles/zshrc"), "setopt autocd\nexport AGENT=1\n")
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 1)
        self.assertIn("M ~/.zshrc", out)
        rc, _, _ = self.ap("revert", "--yes")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.islink(self.h(".zshrc")))
        self.assertEqual(read(self.h("dotfiles/zshrc")), "setopt autocd\n")

    def test_list_and_select_snapshot(self):
        self.snap("--name", "first")
        write(self.h(".bashrc"), "changed\n")
        self.snap("--name", "second")
        rc, out, _ = self.ap("list")
        self.assertIn("-first", out)
        self.assertIn("-second", out)
        self.assertEqual(self.ap("diff")[0], 0)  # latest = second
        snaps = core.list_snapshots(core.resolve_store(self.home))
        self.assertEqual(self.ap("diff", snaps[0]["id"])[0], 1)

    def test_no_snapshot_is_error(self):
        rc, _, err = self.ap("diff")
        self.assertEqual(rc, 2)
        self.assertIn("no snapshots", err)


class SecretTest(Base):
    def test_secret_contents_never_stored_or_printed(self):
        self.snap()
        store = core.resolve_store(self.home)
        snap = core.load_snapshot(store)
        raw = json.dumps(snap)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("OPENSSH PRIVATE KEY", raw)
        self.assertEqual(snap["files"]["~/.npmrc"]["note"], "secret")
        self.assertEqual(snap["files"]["~/.ssh/id_ed25519"]["note"], "secret")
        self.assertEqual(snap["files"]["~/.ssh/config"]["note"], None)
        write(self.h(".npmrc"), "//registry.npmjs.org/:_authToken=npm_ZZZZZZZZZZZZZZZZZZZZZZZZZZ\n")
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 1)
        self.assertIn("M ~/.npmrc  [secret: hash only]", out)
        self.assertNotIn("npm_ZZZZ", out)
        self.assertNotIn(TOKEN, out)
        rc, out, _ = self.ap("revert", "--yes")
        self.assertIn("skip     ~/.npmrc (secret-looking file", out)

    def test_secret_detection(self):
        self.assertTrue(core.is_secret_name("~/.ssh/id_rsa"))
        self.assertFalse(core.is_secret_name("~/.ssh/id_rsa.pub"))
        self.assertTrue(core.is_secret_name("~/proj/.env"))
        self.assertFalse(core.is_secret_name("~/.ssh/known_hosts"))
        self.assertEqual(core._content_info(b"export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123\n", "~/.zshrc")[1], "secret")
        self.assertEqual(core._content_info(b"alias gs='git status'\n", "~/.zshrc"), ("alias gs='git status'\n", None))
        self.assertEqual(core._content_info(b"\x00\x01", "~/bin/x")[1], "binary")
        self.assertEqual(core._content_info(b"users:\n- user:\n    client-key-data: LS0tLS1\n", "~/.kube/config")[1], "secret")
        self.assertEqual(core._content_info(b"[default]\naws_secret_access_key = abcdEFGH1234\n", "~/.aws/config")[1], "secret")


class RevertTest(Base):
    def test_revert_restores_everything(self):
        before_bashrc = read(self.h(".bashrc"))
        self.snap()
        write(self.h(".bashrc"), before_bashrc + "eval \"$(agent init)\"\n")
        os.unlink(self.h(".gitconfig"))
        os.chmod(self.h(".local/bin/tool"), 0o600)
        write(self.h(".local/bin/helper"), "#!/bin/sh\n", 0o755)
        write(self.h(".config/newtool/sub/settings.ini"), "x=1\n")
        rc, out, _ = self.ap("revert", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        self.assertTrue(os.path.exists(self.h(".local/bin/helper")))
        rc, out, err = self.ap("revert", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(read(self.h(".bashrc")), before_bashrc)
        self.assertEqual(read(self.h(".gitconfig")), "[user]\n\tname = Dev\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.h(".local/bin/tool")).st_mode), 0o755)
        self.assertFalse(os.path.exists(self.h(".local/bin/helper")))
        self.assertFalse(os.path.exists(self.h(".config/newtool")))
        self.assertEqual(self.ap("diff")[0], 0)

    def test_revert_requires_confirmation_without_tty(self):
        self.snap()
        write(self.h(".bashrc"), "x\n")
        stdin = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            rc, _, err = self.ap("revert")
        finally:
            sys.stdin = stdin
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)
        self.assertEqual(read(self.h(".bashrc")), "x\n")

    def test_nothing_to_revert(self):
        self.snap()
        rc, out, _ = self.ap("revert", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to revert", out)


class CommandsTest(Base):
    def test_crontab_and_pip_changes(self):
        fakebin = self.h("fakebin")
        cron = self.h("crontab.txt")
        pipout = self.h("pip.txt")
        write(cron, "0 3 * * * backup\n")
        write(pipout, "requests==2.31.0\n")
        write(os.path.join(fakebin, "crontab"), CAT % cron, 0o755)
        write(os.path.join(fakebin, "pip"), CAT % pipout, 0o755)
        os.environ["PATH"] = fakebin
        rc, out, _ = self.ap("snap")
        self.assertEqual(rc, 0)
        self.assertIn("commands recorded: crontab, pip", out)
        write(cron, "0 3 * * * backup\n*/5 * * * * curl -s http://example.com/beacon\n")
        write(pipout, "agent-helper==0.1\nrequests==2.31.0\n")
        rc, out, _ = self.ap("diff")
        self.assertEqual(rc, 1)
        self.assertIn("~ $ crontab -l", out)
        self.assertIn("+*/5 * * * * curl -s http://example.com/beacon", out)
        rc, out, _ = self.ap("revert", "--dry-run")
        self.assertIn("pip uninstall agent-helper", out)
        self.assertIn("crontab -l changed (not reverted)", out)


class RunTest(Base):
    def test_run_snaps_runs_and_diffs(self):
        script = "open(%r, 'a').write('export FROM_AGENT=1\\n')" % self.h(".bashrc")
        rc, out, err = self.ap("run", "--no-commands", "--", sys.executable, "-c", script)
        self.assertEqual(rc, 0)
        self.assertIn("snapshot", err)
        self.assertIn("M ~/.bashrc", out)
        self.assertIn("+export FROM_AGENT=1", out)
        self.assertIn("agentprint revert", out)

    def test_run_check_and_exit_code(self):
        script = "open(%r, 'w').write('x')" % self.h(".gitconfig")
        rc, _, _ = self.ap("run", "--no-commands", "--check", "--", sys.executable, "-c", script)
        self.assertEqual(rc, 1)
        rc, out, _ = self.ap("run", "--no-commands", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(rc, 7)
        self.assertIn("no changes", out)

    def test_run_without_command(self):
        rc, _, err = self.ap("run")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
