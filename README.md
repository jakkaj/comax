# comax

Save and restore tmux sessions running Copilot CLI, Claude Code, and pi.

If your machine restarts, tmux crashes, or you accidentally kill a pane — `comax` gets you back to exactly where you were. It discovers all running Copilot CLI, Claude Code, and pi instances across your tmux sessions, saves their state, and can rehydrate everything with a single command.

## How it works

**Save** scans your tmux sessions and discovers agent instances:

- **Copilot CLI**: walks each pane's process tree, matches PIDs to `~/.copilot/session-state/*/inuse.<PID>.lock` files
- **Claude Code**: matches child PIDs to `~/.claude/sessions/<PID>.json` files
- **pi**: matches direct-child PIDs named `pi`, then uses `lsof` to read the open `~/.pi/db/session-sql/<uuid>.sqlite` handle for the session UUID and the process cwd

All three are saved to `~/.config/comax/state.json` with session UUIDs, working directories, and CLI flags.

> **Note on pi flags**: pi rewrites `argv[0]` to just `"pi"`, so launch flags (model, thinking level, extensions, etc.) cannot be recovered. pi sessions are restored with `pi --session <uuid>` from the saved cwd, which resumes the conversation but uses pi's default configuration.

**Restore** reads the saved state and intelligently rehydrates:

- Missing tmux session → creates it with all windows
- Session exists but window was killed → creates just the missing window
- Window exists but agent stopped → resumes in the existing pane
- Everything already running → skips (no-op)

Each agent is resumed with the correct command (`copilot --yolo --resume <uuid>`, `claude --resume <uuid>`, or `pi --session <uuid>`) from the right working directory.

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
