# agentprint

A coding agent works in your repo, but it can also change things outside it: `~/.bashrc`, `~/.gitconfig`, `~/.npmrc`, `~/.ssh/config`, `~/.config/*`, scripts in `~/.local/bin`, your crontab, globally installed npm/pip packages. `git diff` shows none of that.

`agentprint` takes a snapshot of those paths before a session, shows exactly what changed afterwards (unified diffs for text files), and can put the files back.

Pure Python 3.9+ standard library, no dependencies.

## Install

```sh
pip install git+https://github.com/jatingargiitk/agentprint
```

## Usage

```sh
agentprint snap   [--name LABEL] [--path PATH]... [--no-commands]
agentprint diff   [SNAPSHOT] [--stat] [--json]
agentprint revert [SNAPSHOT] [--dry-run] [--yes]
agentprint run    [--check] [--path PATH]... [--no-commands] -- <command> [args...]
agentprint list
agentprint paths  [--path PATH]...
```

`SNAPSHOT` is an id or id prefix; the default is the latest snapshot. Every command takes `--home DIR` (default `$AGENTPRINT_HOME`, then `~`) and `--store DIR` (default `$AGENTPRINT_STORE`, then `<home>/.agentprint`).

Exit status: `diff` returns `0` for no changes, `1` for changes, and `2` for errors. `run` returns the wrapped command's status; with `--check` it returns `1` when the command succeeded but changed watched state.

## Example

I produced every block below by running the commands in a throwaway home (`export HOME=$(mktemp -d)`). That home starts with a `.bashrc`, a `.gitconfig`, `~/.ssh/config`, an ed25519 key from `ssh-keygen`, and an `.npmrc` with an auth token. The "agent session" is [`examples/agent-session.sh`](examples/agent-session.sh). It appends to `.bashrc`, sets `core.hooksPath` in the global git config, drops a script into `~/.local/bin`, appends a token to `.npmrc`, and deletes `~/.ssh/config`.

### Snapshot, then let the agent work

```
$ agentprint snap
snapshot 20261108-101407: 6 files under 40 watched paths
commands recorded: npm-global, pip
2 secret-looking file(s) stored as hash only

$ sh examples/agent-session.sh
```

The two secret-looking files are the private key and `.npmrc`. agentprint stores only their hashes.

### See what changed

```
$ agentprint diff
changes since snapshot 20261108-101407 (2026-11-08 10:14:07):
  M ~/.bashrc
  M ~/.gitconfig
  A ~/.local/bin/agent-helper
  M ~/.npmrc  [secret: hash only]
  D ~/.ssh/config

=== modified ~/.bashrc
--- a/~/.bashrc
+++ b/~/.bashrc
@@ -1,2 +1,3 @@
 export EDITOR=vim
 alias ll="ls -l"
+export PATH="$HOME/.agent/bin:$PATH"

=== modified ~/.gitconfig
--- a/~/.gitconfig
+++ b/~/.gitconfig
@@ -2,3 +2,5 @@
 	name = Dev
 [init]
 	defaultBranch = main
+[core]
+	hooksPath = /tmp/tmp.3NFZLQ9sQS/.agent/hooks

=== added ~/.local/bin/agent-helper
--- /dev/null
+++ b/~/.local/bin/agent-helper
@@ -0,0 +1,2 @@
+#!/bin/sh
+echo helper

=== modified ~/.npmrc
(secret-looking file: hash only, contents not stored or shown; sha256 47308994091a -> f7ed9b758c6a)

=== deleted ~/.ssh/config
--- a/~/.ssh/config
+++ /dev/null
@@ -1,3 +0,0 @@
-Host gh
-  HostName github.com
-  User git
```

Exit status was 1. `--stat` gives the summary only:

```
$ agentprint diff --stat
changes since snapshot 20261108-101407 (2026-11-08 10:14:07):
  M ~/.bashrc
  M ~/.gitconfig
  A ~/.local/bin/agent-helper
  M ~/.npmrc  [secret: hash only]
  D ~/.ssh/config
```

`--json` gives the same information in machine-readable form, with `changed`, `files[]` (`path`, `status`, `secret`, `patch`), `commands[]` and `truncated`:

```
$ agentprint diff --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["changed"], [(f["path"], f["status"], f["secret"]) for f in d["files"]])'
True [('~/.bashrc', 'modified', False), ('~/.gitconfig', 'modified', False), ('~/.local/bin/agent-helper', 'added', False), ('~/.npmrc', 'modified', True), ('~/.ssh/config', 'deleted', False)]
```

### Revert

```
$ agentprint revert --dry-run
revert to snapshot 20261108-101407:
  restore  ~/.bashrc
  restore  ~/.gitconfig
  remove   ~/.local/bin/agent-helper
  skip     ~/.npmrc (secret-looking file: hash only, contents not stored or shown)
  restore  ~/.ssh/config
dry run: nothing changed

$ agentprint revert --yes
revert to snapshot 20261108-101407:
  restore  ~/.bashrc
  restore  ~/.gitconfig
  remove   ~/.local/bin/agent-helper
  skip     ~/.npmrc (secret-looking file: hash only, contents not stored or shown)
  restore  ~/.ssh/config
reverted 5 change(s), 1 skipped

$ agentprint diff
changes since snapshot 20261108-101407 (2026-11-08 10:14:07):
  M ~/.npmrc  [secret: hash only]

=== modified ~/.npmrc
(secret-looking file: hash only, contents not stored or shown; sha256 47308994091a -> f7ed9b758c6a)
```

`.npmrc` stays changed because agentprint never stored its contents, so you have to fix it yourself. Without `--yes`, `revert` asks before changing anything. If stdin is not a terminal and you didn't pass `--yes`, it refuses.

### Wrap a command

`agentprint run` takes a snapshot, runs the command, and prints the diff. It also records the output of `crontab -l`, `npm ls -g --depth=0`, `pip list`, `pipx list --short` and `brew list --versions` (whichever exist), so package installs show up:

```
$ agentprint run -- pip install -q six
agentprint: snapshot 20261108-101409-run taken; running: pip install -q six
agentprint: command exited with status 0
changes since snapshot 20261108-101409-run (2026-11-08 10:14:09):
  ~ $ pip list --format=freeze --disable-pip-version-check

=== output of $ pip list --format=freeze --disable-pip-version-check
--- a/pip
+++ b/pip
@@ -1,2 +1,3 @@
 agentprint==0.1.0
 pip==25.1.1
+six==1.17.0

$ agentprint revert --dry-run
revert to snapshot 20261108-101409-run:
  note: pip packages added (not reverted): pip uninstall six
dry run: nothing changed
```

agentprint never re-runs package managers itself; for packages it only prints the uninstall command.

To wrap an agent CLI, run `agentprint run -- <agent command>` the same way, or call `agentprint snap` before a session and `agentprint diff` after it.

### Snapshots and watched paths

```
$ agentprint list
20261108-101407              2026-11-08 10:14:07  6 files
20261108-101409-run          2026-11-08 10:14:09  6 files
```

You can add paths with `--path` or in `<store>/config.json`. Paths are relative to home, `~/...`, or absolute. Ignore patterns are `fnmatch` patterns matched against the path relative to home:

```
$ mkdir -p ~/work && echo '{"paths": ["work/tool.cfg"], "ignore": [".config/noisy-app"]}' > ~/.agentprint/config.json && agentprint paths
watched:
  ~/.bashrc
  ~/.bash_profile
  ~/.bash_login
  ~/.bash_logout
  ~/.profile
  ~/.zshrc
  ~/.zprofile
  ~/.zshenv
  ~/.zlogin
  ~/.inputrc
  ~/.gitconfig
  ~/.gitignore_global
  ~/.git-credentials
  ~/.npmrc
  ~/.yarnrc
  ~/.yarnrc.yml
  ~/.pypirc
  ~/.condarc
  ~/.pip
  ~/.cargo/config
  ~/.cargo/config.toml
  ~/.ssh
  ~/.netrc
  ~/.aws/config
  ~/.aws/credentials
  ~/.docker/config.json
  ~/.kube/config
  ~/.config
  ~/.local/bin
  ~/bin
  ~/.cargo/bin
  ~/go/bin
  ~/.claude/settings.json
  ~/.claude/CLAUDE.md
  ~/.claude/commands
  ~/.claude/agents
  ~/.cursor/mcp.json
  ~/.codex/config.toml
  ~/.codex/AGENTS.md
  ~/.gemini/settings.json
  ~/work/tool.cfg
ignored:
  .config/google-chrome*
  .config/chromium*
  .config/BraveSoftware
  .config/Code*
  .config/Cursor
  .config/Slack
  .config/discord
  .config/Microsoft
  .config/libreoffice
  .config/pulse
  .config/dconf
  */__pycache__
  */Cache
  */CacheStorage
  */GPUCache
  *.log
  *.sock
  *.lock
  .config/noisy-app
  .agentprint
```

## Privacy

- A file is **hash only** (contents never written to the snapshot or printed) when its name looks secret (`id_*` except `*.pub`, `*.pem`, `*.key`, `.env*`, `.netrc`, `.git-credentials`, `.pypirc`, `credentials`, `*secret*`, `*token*`, ...) or its contents match key/token patterns. Those patterns include private key headers, GitHub/OpenAI/AWS/Slack/npm token prefixes, and `token=` / `password:` / `api_key=` style assignments. The same rule applies to recorded command output.
- Snapshots go to `~/.agentprint/snapshots/*.json` with mode `0600` in a `0700` directory. They do contain the full text of non-secret watched files such as `.bashrc`.
- Detection is pattern-based and leans toward hiding things. A `.zshrc` that exports a literal `GITHUB_TOKEN=<value>` is treated as secret, so it gets a hash-only diff and can't be reverted.

## Limits

- It only sees the watched paths (`agentprint paths`) and the commands listed above. It does not look at `/etc`, `/usr/local`, system packages, launchd/systemd units, keychains, browser profiles, or anything else outside that list unless you add it with `--path`.
- It is not a sandbox and doesn't prevent anything. It reports changes after they happen. A change made and undone within the session is invisible to it.
- Revert only covers files. It restores modified and deleted files whose contents were stored, resets permission bits and symlinks, removes added files, and removes directories created since the snapshot if they are empty. It can't restore secret-looking files, binaries, or files over 256 KiB, because only their hashes are stored; those show as `skip`. Crontab and package changes are reported but not reverted.
- Changes you or other programs make during the session appear too; agentprint can't tell who made a change.
- Each watched directory is capped at 20,000 files; a warning is printed if the cap is hit. Browser and Electron app profiles under `~/.config` are ignored by default.
- `pip` means the first `pip` on `PATH`, so inside a virtualenv it lists that environment's packages.
- Tested on Linux. macOS should behave the same; Windows isn't supported.

## Development

```sh
python -m unittest discover -s tests -v
```

## License

MIT
