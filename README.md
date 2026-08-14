# comax

Save and restore tmux sessions running Copilot CLI, Claude Code, pi, and omp (oh-my-pi).

If your machine restarts, tmux crashes, or you accidentally kill a pane — `comax` gets you back to exactly where you were. It discovers all running Copilot CLI, Claude Code, and pi instances across your tmux sessions, saves their state, and can rehydrate everything with a single command.

## How it works

**Save** scans your tmux sessions and discovers agent instances:

- **Copilot CLI**: walks each pane's process tree, matches PIDs to `~/.copilot/session-state/*/inuse.<PID>.lock` files
- **Claude Code**: matches child PIDs to `~/.claude/sessions/<PID>.json` files
- **pi**: matches direct-child PIDs named `pi`, then uses `lsof` to read the open `~/.pi/db/session-sql/<uuid>.sqlite` handle for the session UUID and the process cwd
- **omp** (oh-my-pi): matches `omp` as argv[0] or behind its `bun` wrapper, then reads the open `~/.omp/agent/sessions/<cwd>/<ts>_<uuid>.jsonl` handle for the session UUID

Agents are matched against the pane's own process as well as its children, so an
agent exec'd directly as the pane command — with no shell in between — is still
found. Split panes are discovered and restored individually.

All three are saved to `~/.config/comax/state.json` with session UUIDs, working directories, and CLI flags.

> **Note on pi flags**: pi rewrites `argv[0]` to just `"pi"`, so launch flags (model, thinking level, extensions, etc.) cannot be recovered. pi sessions are restored with `pi --session <uuid>` from the saved cwd, which resumes the conversation but uses pi's default configuration.

> **Note on launch flags**: only value-less switches that are safe to replay are
> carried across a restore — `--yolo` for copilot, `--dangerously-skip-permissions`
> for claude, `--auto-approve` for omp. Options that take a value (`--model`, `--effort`, `--context`) are not
> restored, so an agent comes back on its default configuration. Session-selecting
> flags (`--session-id`, `--continue`) are deliberately dropped too, since the saved
> UUID already supplies the session via `--resume`; carrying them would produce a
> command that fights itself.

**Restore** reads the saved state and intelligently rehydrates:

- Missing tmux session → creates it with all windows
- Session exists but window was killed → creates just the missing window
- Window exists but agent stopped → resumes in the existing pane
- Everything already running → skips (no-op)
- Saved session UUID no longer on disk → warns instead of leaving a broken pane

- Window had a split → the extra panes are recreated with `tmux split-window`

Each agent is resumed with the correct command (`copilot --yolo --resume <uuid>`, `claude --resume <uuid>`, `pi --session <uuid>`, or `omp --resume=<uuid>`) from the right working directory.

## Prerequisites

- **tmux** — `brew install tmux`
- **uv** (Python package manager) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **GitHub Copilot CLI** (`@github/copilot`), **Claude Code**, and/or **pi** (`@earendil-works/pi-coding-agent`) running in your tmux sessions

## Install

```bash
# Install as a global CLI tool (recommended)
uv tool install git+https://github.com/jakkaj/comax.git
```

This puts `comax` on your PATH so you can run it from anywhere.

To update to the latest version:

```bash
uv tool install git+https://github.com/jakkaj/comax.git --force --reinstall
```

> `--force` on its own can reinstall a **cached build** when the version string
> hasn't changed — it reports success while leaving the old code in place. Add
> `--reinstall` (and `--no-cache` when installing from a local checkout) so you
> actually get the new build.

Or run it once without installing:

```bash
uvx --from git+https://github.com/jakkaj/comax.git comax
```

## Usage

```bash
# Save current tmux/agent state (whole machine)
comax

# Restore this folder's windows into the tmux session you're in
comax --restore

# See exactly what a restore would do, without touching tmux
comax --restore --dry-run

# Restore everything, every session under its original name (post-reboot)
comax --restore --all
```

**Save records the whole machine. Restore is scoped to one folder by default.**

Day to day you're in one tmux session working on one project, so plain
`comax --restore` takes only the saved windows whose cwd is exactly your current
directory and rehydrates them into the session you're already attached to —
regardless of which session they were originally saved under. The match is exact,
not recursive, so a worktree (`myrepo-worktrees/feature-x`) restores its own
windows and never its parent's.

After a reboot, when you want the whole machine back, use `--restore --all`. That's
the original behaviour: every saved session recreated under its own name.

`--dry-run` works on any command. On restore it prints the full plan — every window,
its status, and the exact command that would be sent — and makes zero tmux changes.
On save it scans and displays without writing the state file.

### Notes

- `comax --restore` must be run from inside tmux, since it restores into your current
  session. It exits with an error otherwise. `--dry-run` works from anywhere.
- Windows saved without a recorded cwd can't be matched to a folder. They're excluded
  from folder restores, with a note telling you how many; `--all` still restores them.
