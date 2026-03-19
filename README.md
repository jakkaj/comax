# comax

Save and restore tmux + Copilot CLI sessions.

If your machine restarts, tmux crashes, or you accidentally kill a pane — `comax` gets you back to exactly where you were. It discovers all running Copilot CLI instances across your tmux sessions, saves their state, and can rehydrate everything with a single command.

## How it works

**Save** scans your tmux sessions and discovers Copilot CLI instances by:

1. Walking each tmux pane's process tree to find copilot native binaries
2. Matching PIDs to `~/.copilot/session-state/*/inuse.<PID>.lock` files to get session UUIDs
3. Reading `workspace.yaml` for each session's metadata (cwd, branch, summary)
4. Writing the full mapping to `~/.config/comax/state.json`

**Restore** reads the saved state and intelligently rehydrates:

- Missing tmux session → creates it with all windows
- Session exists but window was killed → creates just the missing window
- Window exists but copilot stopped → resumes copilot in the existing pane
- Everything already running → skips (no-op)

## Prerequisites

- **tmux** — `brew install tmux`
- **uv** (Python package manager) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **GitHub Copilot CLI** (`@github/copilot`) running in your tmux sessions

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
# Save current tmux/copilot state
comax

# Restore from saved state
comax --restore
```
