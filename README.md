# comax

Save and restore tmux sessions running Copilot CLI and Claude Code.

If your machine restarts, tmux crashes, or you accidentally kill a pane — `comax` gets you back to exactly where you were. It discovers all running Copilot CLI and Claude Code instances across your tmux sessions, saves their state, and can rehydrate everything with a single command.

## How it works

**Save** scans your tmux sessions and discovers agent instances:

- **Copilot CLI**: walks each pane's process tree, matches PIDs to `~/.copilot/session-state/*/inuse.<PID>.lock` files
- **Claude Code**: matches child PIDs to `~/.claude/sessions/<PID>.json` files

Both are saved to `~/.config/comax/state.json` with session UUIDs, working directories, and CLI flags.

**Restore** reads the saved state and intelligently rehydrates:

- Missing tmux session → creates it with all windows
- Session exists but window was killed → creates just the missing window
- Window exists but agent stopped → resumes in the existing pane
- Everything already running → skips (no-op)

Each agent is resumed with the correct command (`copilot --yolo --resume <uuid>` or `claude --resume <uuid>`) from the right working directory.

## Prerequisites

- **tmux** — `brew install tmux`
- **uv** (Python package manager) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **GitHub Copilot CLI** (`@github/copilot`) and/or **Claude Code** running in your tmux sessions

## Install

```bash
# Install as a global CLI tool (recommended)
uv tool install git+https://github.com/jakkaj/comax.git
```

This puts `comax` on your PATH so you can run it from anywhere.

To update to the latest version:

```bash
uv tool install git+https://github.com/jakkaj/comax.git --force
```

Or run it once without installing:

```bash
uvx --from git+https://github.com/jakkaj/comax.git comax
```

## Usage

```bash
# Save current tmux/agent state
comax

# Restore from saved state
comax --restore
```
