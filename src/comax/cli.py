"""comax: Save and restore tmux + Copilot CLI and Claude Code sessions.

Usage:
  comax              # Scan, display, and save state
  comax --save       # Same as above
  comax --restore    # Restore sessions from saved state

Discovery:
  Copilot: process tree walk -> PID match against ~/.copilot/session-state/*/inuse.<PID>.lock
  Claude:  child PID match against ~/.claude/sessions/<PID>.json
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
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
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
class AgentInstance:
    """A CLI agent (copilot or claude) running in a tmux pane."""
    pane: PaneInfo
    agent_type: str  # "copilot" or "claude"
    agent_pid: int
    agent_command: str
    session_uuid: str | None = None
    cwd: str | None = None
    args: str = ""


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


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def get_child_pids(pid: int) -> list[int]:
    # Use ps instead of pgrep — pgrep on macOS can't find ancestor processes
    raw = run(["ps", "-eo", "pid=,ppid="])
    if not raw:
        return []
    children = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                child_pid, parent_pid = int(parts[0]), int(parts[1])
                if parent_pid == pid:
                    children.append(child_pid)
            except ValueError:
                continue
    return children


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


def pane_has_agent(pane_pid: int, agent_type: str) -> bool:
    """Check if a pane's process tree contains a given agent."""
    if agent_type == "copilot":
        tree = walk_process_tree(pane_pid)
        return any("copilot" in cmd.lower() for _, cmd, _ in tree)
    elif agent_type == "claude":
        # Claude is a direct child of the shell, check children
        for child_pid in get_child_pids(pane_pid):
            cmd = get_process_command(child_pid)
            if cmd:
                binary = cmd.split()[0].split("/")[-1].lower()
                if binary == "claude":
                    return True
        return False
    return False


# ── Copilot discovery ────────────────────────────────────────────────────────


def build_copilot_lock_index() -> dict[int, str]:
    """Build PID -> session UUID from ~/.copilot/session-state/*/inuse.*.lock."""
    index: dict[int, str] = {}
    pattern = str(COPILOT_STATE_DIR / "*" / "inuse.*.lock")
    for lock_path in glob(pattern):
        path = Path(lock_path)
        try:
            pid = int(path.stem.split(".")[1])
        except (IndexError, ValueError):
            continue
        if not pid_is_alive(pid):
            continue
        index[pid] = path.parent.name
    return index


def read_copilot_metadata(uuid: str) -> tuple[str | None, str | None]:
    """Read cwd and branch from workspace.yaml. Returns (cwd, branch)."""
    workspace_path = COPILOT_STATE_DIR / uuid / "workspace.yaml"
    if not workspace_path.exists():
        return None, None
    try:
        with open(workspace_path) as f:
            data = yaml.safe_load(f)
        if not data:
            return None, None
        return data.get("cwd"), data.get("branch")
    except Exception:
        return None, None


def extract_copilot_args(command: str) -> str:
    """Extract copilot flags minus --resume and its value."""
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


def find_copilot_in_pane(pane: PaneInfo, lock_index: dict[int, str]) -> AgentInstance | None:
    tree = walk_process_tree(pane.pane_pid)
    copilot_procs = [(pid, cmd, d) for pid, cmd, d in tree if "copilot" in cmd.lower()]
    if not copilot_procs:
        return None

    main_pid, main_cmd, _ = copilot_procs[0]

    # Match deepest copilot PID against lock index
    session_uuid = None
    for pid, _, _ in reversed(copilot_procs):
        if pid in lock_index:
            session_uuid = lock_index[pid]
            break

    cwd = None
    if session_uuid:
        cwd, _ = read_copilot_metadata(session_uuid)

    return AgentInstance(
        pane=pane,
        agent_type="copilot",
        agent_pid=main_pid,
        agent_command=main_cmd,
        session_uuid=session_uuid,
        cwd=cwd,
        args=extract_copilot_args(main_cmd),
    )


# ── Claude discovery ─────────────────────────────────────────────────────────


def build_claude_session_index() -> dict[int, dict]:
    """Build PID -> session data from ~/.claude/sessions/<PID>.json."""
    index: dict[int, dict] = {}
    if not CLAUDE_SESSIONS_DIR.exists():
        return index
    for f in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        if not pid_is_alive(pid):
            continue
        try:
            index[pid] = json.loads(f.read_text())
        except Exception:
            continue
    return index


def extract_claude_args(command: str) -> str:
    """Extract claude flags minus --resume and its value."""
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
        # Skip the binary name itself
        binary = part.split("/")[-1].lower()
        if binary == "claude":
            continue
        if part.startswith("--") or part.startswith("-"):
            args.append(part)
    return " ".join(args) if args else ""


def find_claude_in_pane(pane: PaneInfo, claude_index: dict[int, dict]) -> AgentInstance | None:
    # Check direct children of the pane shell
    for child_pid in get_child_pids(pane.pane_pid):
        cmd = get_process_command(child_pid)
        if not cmd:
            continue
        binary = cmd.split()[0].split("/")[-1].lower()
        if binary != "claude":
            continue

        # Found a claude process — look it up in the session index
        data = claude_index.get(child_pid, {})
        return AgentInstance(
            pane=pane,
            agent_type="claude",
            agent_pid=child_pid,
            agent_command=cmd,
            session_uuid=data.get("sessionId"),
            cwd=data.get("cwd"),
            args=extract_claude_args(cmd),
        )
    return None


# ── Unified discovery ─────────────────────────────────────────────────────────


def discover_all(panes: list[PaneInfo]) -> list[AgentInstance]:
    """Find all copilot and claude instances across tmux panes."""
    copilot_lock_index = build_copilot_lock_index()
    claude_session_index = build_claude_session_index()

    instances = []
    for pane in panes:
        # Try copilot first
        inst = find_copilot_in_pane(pane, copilot_lock_index)
        if inst:
            instances.append(inst)
            continue

        # Try claude
        inst = find_claude_in_pane(pane, claude_session_index)
        if inst:
            instances.append(inst)

    return instances


# ── Save ──────────────────────────────────────────────────────────────────────


def cmd_save(console: Console):
    console.print(Panel("[bold]comax save[/bold]", style="blue"))

    panes = get_tmux_panes()
    if not panes:
        console.print("[red]No tmux sessions found.[/red]")
        return

    console.print(f"\nFound [bold]{len(panes)}[/bold] tmux panes across sessions.\n")

    instances = discover_all(panes)

    if not instances:
        console.print("[yellow]No Copilot or Claude instances found in any tmux pane.[/yellow]")
        return

    # Group by tmux session
    sessions_map: dict[str, list[AgentInstance]] = {}
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
            windows.append({
                "name": inst.pane.window_name,
                "cwd": inst.cwd,
                "agent_type": inst.agent_type,
                "session_uuid": inst.session_uuid,
                "args": inst.args,
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
    table.add_column("Agent", style="magenta")
    table.add_column("CWD", style="dim", max_width=50)
    table.add_column("Session UUID", style="bold white", max_width=38)
    table.add_column("Args", style="yellow")

    for session in state["sessions"]:
        for win in session["windows"]:
            table.add_row(
                session["name"],
                win["name"],
                win["agent_type"],
                win["cwd"] or "-",
                win["session_uuid"] or "-",
                win["args"] or "-",
            )

    console.print(table)
    console.print(f"\n[green]State saved to {STATE_FILE}[/green]")

    copilot_count = sum(1 for inst in instances if inst.agent_type == "copilot")
    claude_count = sum(1 for inst in instances if inst.agent_type == "claude")
    parts = []
    if copilot_count:
        parts.append(f"{copilot_count} copilot")
    if claude_count:
        parts.append(f"{claude_count} claude")
    console.print(f"[dim]{' + '.join(parts)} instance(s) across {len(sessions_map)} session(s)[/dim]")


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


def build_resume_command(win: dict) -> str:
    """Build the shell command to resume an agent in a pane."""
    agent_type = win.get("agent_type", "copilot")
    uuid = win.get("session_uuid")
    args = win.get("args", "")
    cwd = win.get("cwd")

    if agent_type == "claude":
        binary = "claude"
        resume_cmd = f"{binary} {args} --resume {uuid}".strip() if uuid else f"{binary} {args}".strip()
    else:
        binary = "copilot"
        resume_cmd = f"{binary} {args} --resume {uuid}".strip() if uuid else f"{binary} {args}".strip()

    if cwd:
        return f"cd {_shell_quote(cwd)} && {resume_cmd}"
    return resume_cmd


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

    results = Table(title="Restore Results", show_lines=True)
    results.add_column("Session", style="cyan")
    results.add_column("Window", style="green")
    results.add_column("Agent", style="magenta")
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
            first_win = windows[0]
            win_cwd = first_win.get("cwd") or first_cwd
            agent_type = first_win.get("agent_type", "copilot")
            run(["tmux", "new-session", "-d", "-s", session_name,
                 "-n", first_win["name"], "-c", win_cwd])

            uuid = first_win.get("session_uuid")
            if uuid:
                resume_cmd = build_resume_command(first_win)
                run(["tmux", "send-keys", "-t", f"{session_name}:{first_win['name']}", resume_cmd, "Enter"])
                results.add_row(session_name, first_win["name"], agent_type,
                                "created session + window + resumed", "[green]OK[/green]")
            else:
                results.add_row(session_name, first_win["name"], agent_type,
                                "created session + window (no UUID)", "[yellow]WARN[/yellow]")

            existing_sessions.add(session_name)
            remaining_windows = windows[1:]
        else:
            remaining_windows = windows

        existing_windows = get_existing_windows(session_name)

        for win in remaining_windows:
            win_name = win["name"]
            win_cwd = win.get("cwd") or first_cwd
            uuid = win.get("session_uuid")
            agent_type = win.get("agent_type", "copilot")

            if win_name in existing_windows:
                pane_pid = existing_windows[win_name]
                if pane_has_agent(pane_pid, agent_type):
                    results.add_row(session_name, win_name, agent_type,
                                    f"{agent_type} already running", "[green]SKIP[/green]")
                else:
                    if uuid:
                        resume_cmd = build_resume_command(win)
                        run(["tmux", "send-keys", "-t", f"{session_name}:{win_name}", resume_cmd, "Enter"])
                        results.add_row(session_name, win_name, agent_type,
                                        f"resumed {agent_type} in existing window", "[green]OK[/green]")
                    else:
                        results.add_row(session_name, win_name, agent_type,
                                        "window exists but no UUID to resume", "[yellow]WARN[/yellow]")
            else:
                run(["tmux", "new-window", "-t", session_name, "-n", win_name, "-c", win_cwd])
                if uuid:
                    resume_cmd = build_resume_command(win)
                    run(["tmux", "send-keys", "-t", f"{session_name}:{win_name}", resume_cmd, "Enter"])
                    results.add_row(session_name, win_name, agent_type,
                                    f"created window + resumed {agent_type}", "[green]OK[/green]")
                else:
                    results.add_row(session_name, win_name, agent_type,
                                    "created window (no UUID)", "[yellow]WARN[/yellow]")

    console.print(results)
    console.print("\n[green]Restore complete.[/green]")


def _shell_quote(s: str) -> str:
    if " " in s or "'" in s or '"' in s:
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="comax: save/restore tmux + Copilot CLI and Claude Code sessions")
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
