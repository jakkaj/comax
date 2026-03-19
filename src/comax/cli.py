"""comax: Save and restore tmux + Copilot CLI sessions.

Usage:
  comax              # Scan, display, and save state
  comax --save       # Same as above
  comax --restore    # Restore sessions from saved state

Discovery mechanism:
  1. Walk tmux panes -> get shell PIDs
  2. Walk each shell's process tree -> find copilot native binary PIDs
  3. Glob ~/.copilot/session-state/*/inuse.<PID>.lock to match PID -> session UUID
  4. Read workspace.yaml in the session dir for metadata (cwd, branch, summary, etc.)
"""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from dataclasses import dataclass, field

import yaml
from rich.console import Console
from rich.table import Table
from rich.panel import Panel


COPILOT_STATE_DIR = Path.home() / ".copilot" / "session-state"
CONFIG_DIR = Path.home() / ".config" / "comax"
STATE_FILE = CONFIG_DIR / "state.json"


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class PaneInfo:
    session_name: str
    session_id: str
    window_index: int
    window_name: str
    pane_index: int
    pane_pid: int
    pane_command: str
    pane_title: str


@dataclass
class SessionMetadata:
    uuid: str
    cwd: str | None = None
    git_root: str | None = None
    repository: str | None = None
    branch: str | None = None
    summary: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class CopilotInstance:
    pane: PaneInfo
    copilot_pid: int
    copilot_command: str
    session: SessionMetadata | None = None
    child_pids: list[int] = field(default_factory=list)


# ── Process / tmux helpers ────────────────────────────────────────────────────


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


def get_tmux_panes() -> list[PaneInfo]:
    raw = run([
        "tmux", "list-panes", "-a",
        "-F", "#{session_name}|#{session_id}|#{window_index}|#{window_name}|#{pane_index}|#{pane_pid}|#{pane_current_command}|#{pane_title}"
    ])
    if not raw:
        return []

    panes = []
    for line in raw.splitlines():
        parts = line.split("|", 7)
        if len(parts) < 8:
            continue
        panes.append(PaneInfo(
            session_name=parts[0],
            session_id=parts[1],
            window_index=int(parts[2]),
            window_name=parts[3],
            pane_index=int(parts[4]),
            pane_pid=int(parts[5]),
            pane_command=parts[6],
            pane_title=parts[7],
        ))
    return panes


def get_child_pids(pid: int) -> list[int]:
    raw = run(["pgrep", "-P", str(pid)])
    if not raw:
        return []
    return [int(p) for p in raw.splitlines() if p.strip()]


def get_process_command(pid: int) -> str:
    return run(["ps", "-o", "command=", "-p", str(pid)])


def walk_process_tree(pid: int, depth: int = 0) -> list[tuple[int, str, int]]:
    results = []
    cmd = get_process_command(pid)
    if cmd:
        results.append((pid, cmd, depth))
    for child in get_child_pids(pid):
        results.extend(walk_process_tree(child, depth + 1))
    return results


def pane_has_copilot(pane_pid: int) -> bool:
    tree = walk_process_tree(pane_pid)
    return any("copilot" in cmd.lower() for _, cmd, _ in tree)


# ── Lock file index ──────────────────────────────────────────────────────────


def build_lock_index() -> dict[int, str]:
    """Build a map of PID -> session UUID from lock files on disk."""
    index: dict[int, str] = {}
    pattern = str(COPILOT_STATE_DIR / "*" / "inuse.*.lock")
    for lock_path in glob(pattern):
        path = Path(lock_path)
        try:
            pid = int(path.stem.split(".")[1])
        except (IndexError, ValueError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        session_uuid = path.parent.name
        index[pid] = session_uuid
    return index


def read_session_metadata(uuid: str) -> SessionMetadata | None:
    workspace_path = COPILOT_STATE_DIR / uuid / "workspace.yaml"
    if not workspace_path.exists():
        return SessionMetadata(uuid=uuid)
    try:
        with open(workspace_path) as f:
            data = yaml.safe_load(f)
        if not data:
            return SessionMetadata(uuid=uuid)
        return SessionMetadata(
            uuid=data.get("id", uuid),
            cwd=data.get("cwd"),
            git_root=data.get("git_root"),
            repository=data.get("repository"),
            branch=data.get("branch"),
            summary=data.get("summary"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
    except Exception:
        return SessionMetadata(uuid=uuid)


# ── Discovery ────────────────────────────────────────────────────────────────


def find_copilot_instances(panes: list[PaneInfo], lock_index: dict[int, str]) -> list[CopilotInstance]:
    instances = []
    for pane in panes:
        tree = walk_process_tree(pane.pane_pid)
        copilot_procs = [(pid, cmd, d) for pid, cmd, d in tree if "copilot" in cmd.lower()]
        if not copilot_procs:
            continue

        main_pid, main_cmd, _ = copilot_procs[0]
        instance = CopilotInstance(
            pane=pane,
            copilot_pid=main_pid,
            copilot_command=main_cmd,
            child_pids=[pid for pid, _, _ in copilot_procs[1:]],
        )

        for pid, _, _ in reversed(copilot_procs):
            if pid in lock_index:
                instance.session = read_session_metadata(lock_index[pid])
                break

        instances.append(instance)
    return instances


def extract_copilot_args(command: str) -> str:
    """Extract the copilot flags from the command line (minus --resume and its value)."""
    parts = command.split()
    args = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part == "--resume":
            skip_next = True
            continue
        if part.startswith("--resume="):
            continue
        if part.startswith("--"):
            args.append(part)
    return " ".join(args) if args else "--yolo"


# ── Save ──────────────────────────────────────────────────────────────────────


def cmd_save(console: Console):
    console.print(Panel("[bold]comax save[/bold]", style="blue"))

    panes = get_tmux_panes()
    if not panes:
        console.print("[red]No tmux sessions found.[/red]")
        return

    console.print(f"\nFound [bold]{len(panes)}[/bold] tmux panes across sessions.\n")

    lock_index = build_lock_index()
    instances = find_copilot_instances(panes, lock_index)

    if not instances:
        console.print("[yellow]No Copilot CLI instances found in any tmux pane.[/yellow]")
        return

    # Group instances by tmux session
    sessions_map: dict[str, list[CopilotInstance]] = {}
    for inst in instances:
        sessions_map.setdefault(inst.pane.session_name, []).append(inst)

    # Build state
    state = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "sessions": [],
    }

    for session_name, session_instances in sessions_map.items():
        windows = []
        for inst in session_instances:
            cwd = inst.session.cwd if inst.session and inst.session.cwd else None
            windows.append({
                "name": inst.pane.window_name,
                "cwd": cwd,
                "copilot_session_uuid": inst.session.uuid if inst.session else None,
                "copilot_args": extract_copilot_args(inst.copilot_command),
            })
        state["sessions"].append({
            "name": session_name,
            "windows": windows,
        })

    # Write state file
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    # Display summary
    table = Table(title="Saved State", show_lines=True)
    table.add_column("Tmux Session", style="cyan")
    table.add_column("Window", style="green")
    table.add_column("CWD", style="dim", max_width=50)
    table.add_column("Copilot UUID", style="bold white", max_width=38)
    table.add_column("Args", style="yellow")

    for session in state["sessions"]:
        for win in session["windows"]:
            table.add_row(
                session["name"],
                win["name"],
                win["cwd"] or "-",
                win["copilot_session_uuid"] or "-",
                win["copilot_args"],
            )

    console.print(table)
    console.print(f"\n[green]State saved to {STATE_FILE}[/green]")
    console.print(f"[dim]{len(instances)} copilot instance(s) across {len(sessions_map)} session(s)[/dim]")


# ── Restore ───────────────────────────────────────────────────────────────────


def get_existing_tmux_sessions() -> set[str]:
    raw = run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if not raw:
        return set()
    return set(raw.splitlines())


def get_existing_windows(session_name: str) -> dict[str, int]:
    """Return {window_name: pane_pid} for a session."""
    raw = run([
        "tmux", "list-windows", "-t", session_name,
        "-F", "#{window_name}|#{pane_pid}"
    ])
    if not raw:
        return {}
    result = {}
    for line in raw.splitlines():
        parts = line.split("|", 1)
        if len(parts) == 2:
            result[parts[0]] = int(parts[1])
    return result


def cmd_restore(console: Console):
    console.print(Panel("[bold]comax restore[/bold]", style="blue"))

    if not STATE_FILE.exists():
        console.print(f"[red]No saved state found at {STATE_FILE}[/red]")
        console.print("[dim]Run 'comax' or 'comax --save' first to save current state.[/dim]")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)

    saved_at = state.get("saved_at", "unknown")
    sessions = state.get("sessions", [])
    console.print(f"\n[dim]State saved at: {saved_at}[/dim]")
    console.print(f"[dim]{len(sessions)} session(s) to restore[/dim]\n")

    if not sessions:
        console.print("[yellow]Nothing to restore.[/yellow]")
        return

    existing_sessions = get_existing_tmux_sessions()

    # Results table
    results = Table(title="Restore Results", show_lines=True)
    results.add_column("Session", style="cyan")
    results.add_column("Window", style="green")
    results.add_column("Action", style="yellow")
    results.add_column("Status", style="bold")

    for session in sessions:
        session_name = session["name"]
        windows = session.get("windows", [])
        if not windows:
            continue

        first_cwd = windows[0].get("cwd") or os.path.expanduser("~")

        # Ensure session exists
        if session_name not in existing_sessions:
            # Create session with first window
            first_win = windows[0]
            win_cwd = first_win.get("cwd") or first_cwd
            run(["tmux", "new-session", "-d", "-s", session_name,
                 "-n", first_win["name"], "-c", win_cwd])

            # Resume copilot in the first window
            uuid = first_win.get("copilot_session_uuid")
            args = first_win.get("copilot_args", "--yolo")
            if uuid:
                copilot_cmd = f"cd {_shell_quote(win_cwd)} && copilot {args} --resume {uuid}"
                run(["tmux", "send-keys", "-t", f"{session_name}:{first_win['name']}", copilot_cmd, "Enter"])
                results.add_row(session_name, first_win["name"], "created session + window + resumed copilot", "[green]OK[/green]")
            else:
                results.add_row(session_name, first_win["name"], "created session + window (no UUID)", "[yellow]WARN[/yellow]")

            existing_sessions.add(session_name)
            remaining_windows = windows[1:]
        else:
            remaining_windows = windows

        # Get current windows in this session
        existing_windows = get_existing_windows(session_name)

        for win in remaining_windows:
            win_name = win["name"]
            win_cwd = win.get("cwd") or first_cwd
            uuid = win.get("copilot_session_uuid")
            args = win.get("copilot_args", "--yolo")

            if win_name in existing_windows:
                # Window exists — check if copilot is running
                pane_pid = existing_windows[win_name]
                if pane_has_copilot(pane_pid):
                    results.add_row(session_name, win_name, "copilot already running", "[green]SKIP[/green]")
                else:
                    # Window exists but copilot not running — resume
                    if uuid:
                        copilot_cmd = f"cd {_shell_quote(win_cwd)} && copilot {args} --resume {uuid}"
                        run(["tmux", "send-keys", "-t", f"{session_name}:{win_name}", copilot_cmd, "Enter"])
                        results.add_row(session_name, win_name, "resumed copilot in existing window", "[green]OK[/green]")
                    else:
                        results.add_row(session_name, win_name, "window exists but no UUID to resume", "[yellow]WARN[/yellow]")
            else:
                # Window missing — create it and resume copilot
                run(["tmux", "new-window", "-t", session_name, "-n", win_name, "-c", win_cwd])
                if uuid:
                    copilot_cmd = f"cd {_shell_quote(win_cwd)} && copilot {args} --resume {uuid}"
                    run(["tmux", "send-keys", "-t", f"{session_name}:{win_name}", copilot_cmd, "Enter"])
                    results.add_row(session_name, win_name, "created window + resumed copilot", "[green]OK[/green]")
                else:
                    results.add_row(session_name, win_name, "created window (no UUID)", "[yellow]WARN[/yellow]")

    console.print(results)
    console.print("\n[green]Restore complete.[/green]")


def _shell_quote(s: str) -> str:
    """Simple shell quoting for paths."""
    if " " in s or "'" in s or '"' in s:
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="comax: save/restore tmux + Copilot CLI sessions")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save", action="store_true", default=True, help="Scan and save state (default)")
    group.add_argument("--restore", action="store_true", help="Restore sessions from saved state")
    args = parser.parse_args()

    console = Console()

    if args.restore:
        cmd_restore(console)
    else:
        cmd_save(console)


if __name__ == "__main__":
    main()
