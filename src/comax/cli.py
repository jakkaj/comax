"""comax: Save and restore tmux + Copilot CLI, Claude Code, and pi sessions.

Usage:
  comax                        # Scan the whole machine, display, and save state
  comax --save                 # Same as above
  comax --restore              # Restore this folder's windows into the current
                               #   tmux session
  comax --restore --all        # Restore every saved session under its original
                               #   name (post-reboot)
  comax --dry-run              # Preview any of the above; changes nothing

Save always records the whole machine. Restore is folder-scoped and
session-local by default: it takes the saved windows whose cwd is exactly
$PWD and rehydrates them into the tmux session you're attached to.

Discovery:
  Copilot: process tree walk -> PID match against ~/.copilot/session-state/*/inuse.<PID>.lock
  Claude:  child PID match against ~/.claude/sessions/<PID>.json
  pi:      child PID with basename 'pi' -> lsof for cwd and open session-sql/<uuid>.sqlite
"""

import argparse
import json
import os
import subprocess
import time
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
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PI_DB_DIR = Path.home() / ".pi" / "db" / "session-sql"
PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
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
    """A CLI agent (copilot, claude, or pi) running in a tmux pane."""
    pane: PaneInfo
    agent_type: str  # "copilot", "claude", or "pi"
    agent_pid: int
    agent_command: str
    session_uuid: str | None = None
    cwd: str | None = None
    args: str = ""


# ── Process / tmux helpers ────────────────────────────────────────────────────


def run(cmd: list[str]) -> str:
    return run_full(cmd).stdout

@dataclass
class RunResult:
    """Structured result of an external subprocess call.

    The bare `run()` helper returns just stdout for the discovery path —
    where empty output already signals 'no match'. For tmux mutations
    (`new-session`, `new-window`, `send-keys`) we need the returncode and
    stderr to tell the operator the truth: if the call failed, the restore
    table must say `FAIL: <reason>`, not `OK`. Use `run_full()` in those
    callsites.
    """
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def reason(self) -> str:
        """Short failure reason (first non-empty line of stderr)."""
        for line in self.stderr.splitlines():
            line = line.strip()
            if line:
                return line
        return f"exit {self.returncode}"

def run_full(cmd: list[str]) -> RunResult:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return RunResult(
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
        returncode=result.returncode,
    )


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

def _is_copilot_process(cmd: str) -> bool:
    """True iff the command line represents a copilot agent process.

    Matches:
      * `copilot ...` and `/abs/path/to/copilot ...`
      * `node /abs/path/to/copilot ...` (the npm wrapper invocation pattern)

    Rejects anything where 'copilot' merely appears as a substring of an
    argument (e.g. `lean-ctx -c 'build_copilot_lock_index'`, `gh copilot ...`,
    `grep copilot`). Restricting to argv[0]/argv[1] basenames removes those
    false positives without losing the real npm-wrapper detection.
    """
    toks = cmd.split()
    if not toks:
        return False
    argv0 = toks[0].split("/")[-1].lower()
    if argv0 == "copilot":
        return True
    if argv0 == "node" and len(toks) >= 2:
        return toks[1].split("/")[-1].lower() == "copilot"
    return False

def get_process_cwd(pid: int) -> str | None:
    """Get the working directory of a process via lsof."""
    raw = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None

def get_open_file_paths(pid: int) -> list[str]:
    """Get the list of open file paths for a process via lsof."""
    raw = run(["lsof", "-p", str(pid), "-Fn"])
    if not raw:
        return []
    return [line[1:] for line in raw.splitlines() if line.startswith("n")]


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
        return any(_is_copilot_process(cmd) for _, cmd, _ in tree)
    elif agent_type in ("claude", "pi"):
        # Direct child of the shell; match by binary basename to avoid false positives
        # (e.g. 'python' contains 'pi', 'copilot' contains 'pi').
        for child_pid in get_child_pids(pane_pid):
            cmd = get_process_command(child_pid)
            if cmd:
                binary = cmd.split()[0].split("/")[-1].lower()
                if binary == agent_type:
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


# Flags carried across a restore, per agent. Deliberately a short allowlist
# of value-less switches rather than a faithful argv replay.
#
# The old code kept every token starting with "--" and dropped the values
# in between, so `--model gpt-5 --session-id <uuid>` was saved as
# `--model --session-id` and restored into a command that either misparsed
# ("option '--context <tier>' argument '--effort' is invalid") or swallowed
# the resume UUID as a flag value ("too many arguments"). Reconstructing
# values correctly would mean tracking each CLI's arity; carrying only
# value-less switches needs no such knowledge and cannot malform a command.
#
# Session identity is NOT taken from argv — it comes from the saved UUID via
# build_resume_command. Flags that also select a session (--session-id,
# --continue, --resume) must therefore never be carried, or they would fight
# the resume flag.
#
# Trade-off: launch options like --model / --effort / --context are not
# restored; the agent comes back on its default configuration, as already
# documented for pi. Adding one here is a one-line change.
RESTORE_SAFE_FLAGS: dict[str, tuple[str, ...]] = {
    "copilot": ("--yolo",),
    "claude": ("--dangerously-skip-permissions",),
    "pi": (),
}


def extract_agent_args(agent_type: str, command: str) -> str:
    """Flags worth replaying when resuming this agent.

    Emitted in allowlist order, not argv order, so the saved string is
    stable across restarts.
    """
    toks = set(command.split())
    return " ".join(f for f in RESTORE_SAFE_FLAGS.get(agent_type, ()) if f in toks)


def extract_copilot_args(command: str) -> str:
    """Extract the copilot flags that are safe to replay on resume."""
    return extract_agent_args("copilot", command)


def find_copilot_in_pane(pane: PaneInfo, lock_index: dict[int, str]) -> AgentInstance | None:
    tree = walk_process_tree(pane.pane_pid)
    copilot_procs = [(pid, cmd, d) for pid, cmd, d in tree if _is_copilot_process(cmd)]
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
    """Extract the claude flags that are safe to replay on resume."""
    return extract_agent_args("claude", command)


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


# ── pi discovery ──────────────────────────────────────────────────────────────

def find_pi_session_uuid(pid: int) -> str | None:
    """Look at a pi process's open files and pull the session UUID from
    ~/.pi/db/session-sql/<uuid>.sqlite, which pi keeps open for the duration
    of a session."""
    db_prefix = str(PI_DB_DIR) + "/"
    for path in get_open_file_paths(pid):
        if path.startswith(db_prefix) and path.endswith(".sqlite"):
            return Path(path).stem
    return None

def find_pi_in_pane(pane: PaneInfo) -> AgentInstance | None:
    """Find a pi agent running as a direct child of the pane shell.

    pi rewrites argv[0] to just 'pi', so we cannot recover the original
    launch flags (model, thinking level, extensions, etc.). We capture cwd
    and session UUID — enough to resume the conversation in place.
    """
    for child_pid in get_child_pids(pane.pane_pid):
        cmd = get_process_command(child_pid)
        if not cmd:
            continue
        binary = cmd.split()[0].split("/")[-1].lower()
        if binary != "pi":
            continue

        return AgentInstance(
            pane=pane,
            agent_type="pi",
            agent_pid=child_pid,
            agent_command=cmd,
            session_uuid=find_pi_session_uuid(child_pid),
            cwd=get_process_cwd(child_pid),
            args="",
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
            continue

        # Try pi
        inst = find_pi_in_pane(pane)
        if inst:
            instances.append(inst)

    return instances


# ── Save ──────────────────────────────────────────────────────────────────────


def cmd_save(console: Console, dry: bool = False):
    console.print(Panel(
        "[bold]comax save[/bold]" + (" [cyan]- dry run[/cyan]" if dry else ""),
        style="blue"))

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
    if not dry:
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
    if dry:
        console.print(f"\n[cyan]Dry run - state not written to {STATE_FILE}[/cyan]")
    else:
        console.print(f"\n[green]State saved to {STATE_FILE}[/green]")

    copilot_count = sum(1 for inst in instances if inst.agent_type == "copilot")
    claude_count = sum(1 for inst in instances if inst.agent_type == "claude")
    pi_count = sum(1 for inst in instances if inst.agent_type == "pi")
    parts = []
    if copilot_count:
        parts.append(f"{copilot_count} copilot")
    if claude_count:
        parts.append(f"{claude_count} claude")
    if pi_count:
        parts.append(f"{pi_count} pi")
    console.print(f"[dim]{' + '.join(parts)} instance(s) across {len(sessions_map)} session(s)[/dim]")


# ── Restore ───────────────────────────────────────────────────────────────────


def get_existing_tmux_sessions() -> set[str]:
    raw = run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if not raw:
        return set()
    return set(raw.splitlines())


def get_existing_windows(session_name: str) -> list[tuple[str, int]]:
    """Return [(window_name, pane_pid), ...] for a session. Preserves duplicates."""
    raw = run([
        "tmux", "list-windows", "-t", session_name,
        "-F", "#{window_name}|#{pane_pid}"
    ])
    if not raw:
        return []
    result = []
    for line in raw.splitlines():
        parts = line.split("|", 1)
        if len(parts) == 2:
            result.append((parts[0], int(parts[1])))
    return result


def get_existing_windows_with_path(session_name: str) -> list[tuple[str, int, str]]:
    """Like get_existing_windows but also returns each window's current path.

    Folder-scoped restore lands every window in the *current* tmux session,
    where saved window names routinely collide with unrelated live windows
    ('node' and 'prime' are the usual offenders). Claiming a live window on
    name alone would fire a resume into someone else's project, so the folder
    path additionally requires the pane's cwd to match. The whole-machine
    path is scoped to a window's original session and keeps using the
    name-only helper above.
    """
    raw = run([
        "tmux", "list-windows", "-t", session_name,
        "-F", "#{window_name}|#{pane_pid}|#{pane_current_path}"
    ])
    if not raw:
        return []
    result = []
    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            result.append((parts[0], int(parts[1]), parts[2]))
    return result


def get_current_tmux_session() -> str | None:
    """Name of the tmux session we're running inside, or None if not in tmux.

    Resolves through $TMUX_PANE rather than a bare `display-message -p`:
    the bare form asks the *attached client* which session it's showing, so
    it can report the wrong session (or fail) when several clients are
    attached or when comax is run from a detached context. Targeting our own
    pane id is unambiguous.
    """
    if not os.environ.get("TMUX"):
        return None
    pane = os.environ.get("TMUX_PANE")
    if pane:
        res = run_full(["tmux", "display-message", "-pt", pane, "#{session_name}"])
        if res.ok and res.stdout:
            return res.stdout
    res = run_full(["tmux", "display-message", "-p", "#{session_name}"])
    return res.stdout if res.ok and res.stdout else None


def _resolve_folder(path: str) -> Path:
    """Normalise a folder path for comparison (expanduser + resolve).

    resolve() is non-strict on 3.12, so paths that no longer exist normalise
    rather than raise — a saved cwd for a deleted directory simply fails to
    match instead of blowing up the restore.
    """
    return Path(path).expanduser().resolve()


def _same_folder(saved_cwd: str | None, target: Path) -> bool:
    """True iff a saved window's cwd is exactly the target folder.

    Exact match, not subtree: worktrees like `pij` and
    `pij-worktrees/pr14-…` are separate projects that happen to share a
    prefix, and each should restore only its own windows. Component-wise
    Path comparison also means `SecondCrack` never matches
    `SecondCrack-s024`.
    """
    if not saved_cwd:
        return False
    try:
        return _resolve_folder(saved_cwd) == target
    except (OSError, ValueError):
        return False


def select_windows_for_folder(state: dict, folder: Path) -> tuple[list[dict], int]:
    """Pick saved windows whose cwd is `folder`, in state-file order.

    Returns (matches, skipped_without_cwd). Each match is the saved window
    dict plus `_origin_session` — the tmux session it was saved under, kept
    for display only: folder restore always targets the current session.

    Windows saved with cwd=None can't be attributed to any folder (the agent
    was detected but its cwd couldn't be read). They're counted so we can
    tell the operator they exist rather than dropping them silently.
    """
    matches: list[dict] = []
    skipped_without_cwd = 0
    for session in state.get("sessions", []):
        for win in session.get("windows", []):
            if not win.get("cwd"):
                skipped_without_cwd += 1
                continue
            if _same_folder(win.get("cwd"), folder):
                matches.append({**win, "_origin_session": session.get("name", "?")})
    return matches, skipped_without_cwd


def summarize_saved_folders(state: dict, limit: int = 5) -> list[tuple[str, int]]:
    """Folders that do have saved windows, most-populated first.

    Used to make a zero-match restore actionable — otherwise the operator
    just sees 'nothing to do' while the state file is full of other projects.
    """
    counts: dict[str, int] = {}
    for session in state.get("sessions", []):
        for win in session.get("windows", []):
            cwd = win.get("cwd")
            if cwd:
                counts[cwd] = counts.get(cwd, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def is_session_alive(agent_type: str, uuid: str) -> bool:
    """Check whether the agent's on-disk session store still has this UUID.

    Agents rotate / garbage-collect session files (claude in particular
    drops sessions that haven't been touched recently). A `--resume <uuid>`
    against a missing session produces a broken pane that just prints an
    error — the operator sees no agent. Pre-validate before sending keys
    so we can emit a `WARN: uuid stale` row instead (FX001-4).
    """
    if not uuid:
        return False
    if agent_type == "copilot":
        return (COPILOT_STATE_DIR / uuid).is_dir()
    if agent_type == "claude":
        # ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
        return bool(list(CLAUDE_PROJECTS_DIR.glob(f"*/{uuid}.jsonl"))) if CLAUDE_PROJECTS_DIR.exists() else False
    if agent_type == "pi":
        # ~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl
        # Search the encoded-cwd dirs for any file whose stem ends with the uuid.
        if not PI_SESSIONS_DIR.exists():
            return False
        for f in PI_SESSIONS_DIR.glob(f"*/*{uuid}*.jsonl"):
            if f.is_file():
                return True
        return False
    return False

# Modest pause after creating a new tmux pane before sending keys. Modern
# tmux (3.x) buffers send-keys to the pty so a slow shell still receives
# everything; we validated this experimentally with a 600 ms sleep in
# .zshrc and saw zero character loss. The sleep below is a defensive
# margin for pathological cases (network homes, multi-KB rc files that
# could exceed tmux's pty buffer). Tunable but never longer than needed.
NEW_PANE_SETTLE_S = 0.05


def _settle_new_pane() -> None:
    """Brief defensive pause after creating a new tmux pane (FX001-5).

    Validation showed tmux 3.x buffers send-keys reliably even with a 600 ms
    slow .zshrc, so this is paranoia rather than necessity. Keeping it at
    NEW_PANE_SETTLE_S (50 ms) is well below human-perceptible latency and
    defends against pathological cases (network-mounted homes, multi-KB rc
    files that could approach the pty buffer ceiling).
    """
    time.sleep(NEW_PANE_SETTLE_S)


def build_resume_command(win: dict) -> str:
    """Build the shell command to resume an agent in a pane."""
    agent_type = win.get("agent_type", "copilot")
    uuid = win.get("session_uuid")
    args = win.get("args", "")
    cwd = win.get("cwd")

    if agent_type == "claude":
        binary = "claude"
        resume_cmd = f"{binary} {args} --resume {uuid}".strip() if uuid else f"{binary} {args}".strip()
    elif agent_type == "pi":
        binary = "pi"
        # pi uses --session <uuid|partial-uuid> for non-interactive resume.
        # --resume on pi is an interactive picker, so we deliberately avoid it.
        resume_cmd = f"{binary} {args} --session {uuid}".strip() if uuid else f"{binary} {args}".strip()
    else:
        binary = "copilot"
        resume_cmd = f"{binary} {args} --resume {uuid}".strip() if uuid else f"{binary} {args}".strip()

    if cwd:
        return f"cd {_shell_quote(cwd)} && {resume_cmd}"
    return resume_cmd


class RestoreReport:
    """The restore results table, plus a tally for the closing summary.

    In dry-run mode an extra Command column carries the exact string that
    would be sent, so a preview can be read as 'this is what will happen'
    without cross-referencing anything.
    """

    def __init__(self, dry: bool, title: str | None = None):
        self.dry = dry
        self.counts: dict[str, int] = {}
        self.table = Table(
            title=title or ("Restore Plan (dry run)" if dry else "Restore Results"),
            show_lines=True,
        )
        self.table.add_column("Session", style="cyan")
        self.table.add_column("Window", style="green")
        self.table.add_column("Agent", style="magenta")
        self.table.add_column("Action", style="yellow")
        if dry:
            self.table.add_column("Command", style="dim", overflow="fold")
        self.table.add_column("Status", style="bold")

    def add(self, session: str, window: str, agent: str, action: str,
            status: str, command: str = "") -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        row = [session, window, agent, action]
        if self.dry:
            row.append(command or "-")
        row.append(_STATUS_MARKUP[status])
        self.table.add_row(*row)

    def summary(self) -> str:
        if not self.counts:
            return ""
        order = ["OK", "DRY", "SKIP", "WARN", "FAIL"]
        parts = [f"{self.counts[s]} {s}" for s in order if s in self.counts]
        return "  ".join(parts)


_STATUS_MARKUP = {
    "OK": "[green]OK[/green]",
    "SKIP": "[green]SKIP[/green]",
    "WARN": "[yellow]WARN[/yellow]",
    "FAIL": "[red]FAIL[/red]",
    "DRY": "[cyan]DRY[/cyan]",
}


def _stale_note(uuid: str) -> str:
    return f"uuid stale ({uuid[:8]}…), agent not resumed"


def _restore_into_existing_pane(report: RestoreReport, session_name: str, win: dict,
                                pane_pid: int, dry: bool) -> None:
    """Resume an agent in a pane that already exists.

    Shared by folder-scoped and whole-machine restore so the ladder below
    (already-running / stale-uuid / no-uuid / resume) has exactly one
    implementation.
    """
    win_name = win["name"]
    agent_type = win.get("agent_type", "copilot")
    uuid = win.get("session_uuid")

    if pane_has_agent(pane_pid, agent_type):
        report.add(session_name, win_name, agent_type,
                   f"{agent_type} already running", "SKIP")
        return

    if uuid and not is_session_alive(agent_type, uuid):
        report.add(session_name, win_name, agent_type,
                   f"window exists; {_stale_note(uuid)}", "WARN")
        return

    if not uuid:
        report.add(session_name, win_name, agent_type,
                   "window exists but no UUID to resume", "WARN")
        return

    resume_cmd = build_resume_command(win)
    if dry:
        report.add(session_name, win_name, agent_type,
                   f"would resume {agent_type} in existing window", "DRY", resume_cmd)
        return

    # Target by pane PID to avoid ambiguity with duplicate names
    sk = _send_keys_to_pane(session_name, pane_pid, resume_cmd)
    if sk.ok:
        report.add(session_name, win_name, agent_type,
                   f"resumed {agent_type} in existing window", "OK")
    else:
        report.add(session_name, win_name, agent_type,
                   f"send-keys failed: {sk.reason}", "FAIL")


def _create_window_and_resume(report: RestoreReport, session_name: str, win: dict,
                              win_cwd: str, existing_windows: list, dry: bool) -> list:
    """Create a new window in an existing session and resume its agent.

    `existing_windows` is the caller's [(name, pid), ...] snapshot, used to
    identify which window is the newly created one. Returns a refreshed
    snapshot so callers can keep claiming against current reality.
    """
    win_name = win["name"]
    agent_type = win.get("agent_type", "copilot")
    uuid = win.get("session_uuid")

    if uuid and not is_session_alive(agent_type, uuid):
        action = ("would create window; " if dry else "window created; ") + _stale_note(uuid)
        if dry:
            report.add(session_name, win_name, agent_type, action, "WARN")
            return existing_windows
        nw = run_full(["tmux", "new-window", "-t", session_name, "-n", win_name, "-c", win_cwd])
        if not nw.ok:
            report.add(session_name, win_name, agent_type,
                       f"new-window failed: {nw.reason}", "FAIL")
            return existing_windows
        report.add(session_name, win_name, agent_type, action, "WARN")
        return get_existing_windows(session_name)

    if not uuid:
        if dry:
            report.add(session_name, win_name, agent_type,
                       "would create window (no UUID)", "WARN")
            return existing_windows
        nw = run_full(["tmux", "new-window", "-t", session_name, "-n", win_name, "-c", win_cwd])
        if not nw.ok:
            report.add(session_name, win_name, agent_type,
                       f"new-window failed: {nw.reason}", "FAIL")
            return existing_windows
        report.add(session_name, win_name, agent_type, "created window (no UUID)", "WARN")
        return get_existing_windows(session_name)

    resume_cmd = build_resume_command(win)
    if dry:
        report.add(session_name, win_name, agent_type,
                   f"would create window + resume {agent_type}", "DRY", resume_cmd)
        return existing_windows

    nw = run_full(["tmux", "new-window", "-t", session_name, "-n", win_name, "-c", win_cwd])
    if not nw.ok:
        report.add(session_name, win_name, agent_type,
                   f"new-window failed: {nw.reason}", "FAIL")
        return existing_windows

    # Target the newly created window by finding its pane PID: the one that
    # wasn't in our previous snapshot.
    refreshed = get_existing_windows(session_name)
    old_pids = {pid for _, pid in existing_windows}
    new_pane_pid = None
    for name, pid in refreshed:
        if name == win_name and pid not in old_pids:
            new_pane_pid = pid
            break

    if new_pane_pid is None:
        report.add(session_name, win_name, agent_type,
                   "new window not visible after create", "FAIL")
    else:
        _settle_new_pane()
        sk = _send_keys_to_pane(session_name, new_pane_pid, resume_cmd)
        if sk.ok:
            report.add(session_name, win_name, agent_type,
                       f"created window + resumed {agent_type}", "OK")
        else:
            report.add(session_name, win_name, agent_type,
                       f"send-keys failed: {sk.reason}", "FAIL")
    return refreshed


def cmd_restore(console: Console, dry: bool = False) -> int:
    """Whole-machine restore: every saved session, under its original name.

    Reached via `--restore --all`. This is the post-reboot path; the default
    restore is folder-scoped (see cmd_restore_folder).
    """
    console.print(Panel(
        "[bold]comax restore[/bold] [dim](whole machine)[/dim]"
        + (" [cyan]- dry run[/cyan]" if dry else ""),
        style="blue"))

    if not STATE_FILE.exists():
        console.print(f"[red]No saved state found at {STATE_FILE}[/red]")
        console.print("[dim]Run 'comax' or 'comax --save' first to save current state.[/dim]")
        return 1

    with open(STATE_FILE) as f:
        state = json.load(f)

    saved_at = state.get("saved_at", "unknown")
    sessions = state.get("sessions", [])
    console.print(f"\n[dim]State saved at: {saved_at}[/dim]")
    console.print(f"[dim]{len(sessions)} session(s) to restore[/dim]\n")

    if not sessions:
        console.print("[yellow]Nothing to restore.[/yellow]")
        return 0

    existing_sessions = get_existing_tmux_sessions()
    report = RestoreReport(dry)

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
            uuid = first_win.get("session_uuid")

            if dry:
                # Preview only: no session is created, so there is no pane to
                # resolve. Report the intent, then preview the rest of this
                # session's windows as new-window creations.
                if uuid and not is_session_alive(agent_type, uuid):
                    report.add(session_name, first_win["name"], agent_type,
                               "would create session + window; " + _stale_note(uuid), "WARN")
                elif uuid:
                    report.add(session_name, first_win["name"], agent_type,
                               "would create session + window + resume", "DRY",
                               build_resume_command(first_win))
                else:
                    report.add(session_name, first_win["name"], agent_type,
                               "would create session + window (no UUID)", "WARN")
                existing_sessions.add(session_name)
                for win in windows[1:]:
                    _create_window_and_resume(
                        report, session_name, win,
                        win.get("cwd") or first_cwd, [], dry=True)
                continue

            new_sess = run_full(["tmux", "new-session", "-d", "-s", session_name,
                                  "-n", first_win["name"], "-c", win_cwd])
            if not new_sess.ok:
                report.add(session_name, first_win["name"], agent_type,
                           f"new-session failed: {new_sess.reason}", "FAIL")
                # Skip the rest of this tmux session - nothing else will work.
                continue

            if uuid and not is_session_alive(agent_type, uuid):
                report.add(session_name, first_win["name"], agent_type,
                           "window created; " + _stale_note(uuid), "WARN")
            elif uuid:
                resume_cmd = build_resume_command(first_win)
                # Look up the freshly-created pane by PID so we can send-keys
                # via an unambiguous `<session>:<window_idx>.<pane_idx>` target.
                # Name-based targeting (`<session>:<window_name>`) breaks when
                # the window name contains a '.' - tmux mis-parses it as a
                # window/pane index expression (see FX001-3).
                fresh = get_existing_windows(session_name)
                first_pid = next((pid for n, pid in fresh if n == first_win["name"]), None)
                if first_pid is None:
                    report.add(session_name, first_win["name"], agent_type,
                               "new window not visible after create", "FAIL")
                else:
                    _settle_new_pane()
                    sk = _send_keys_to_pane(session_name, first_pid, resume_cmd)
                    if sk.ok:
                        report.add(session_name, first_win["name"], agent_type,
                                   "created session + window + resumed", "OK")
                    else:
                        report.add(session_name, first_win["name"], agent_type,
                                   f"send-keys failed: {sk.reason}", "FAIL")
            else:
                report.add(session_name, first_win["name"], agent_type,
                           "created session + window (no UUID)", "WARN")

            existing_sessions.add(session_name)
            remaining_windows = windows[1:]
        else:
            remaining_windows = windows

        existing_windows = get_existing_windows(session_name)
        # Track which existing windows have been claimed (by list index)
        claimed: set[int] = set()

        for win in remaining_windows:
            win_name = win["name"]
            win_cwd = win.get("cwd") or first_cwd

            # Find an unclaimed existing window with this name
            matched_idx = None
            matched_pane_pid = None
            for idx, (name, pane_pid) in enumerate(existing_windows):
                if name == win_name and idx not in claimed:
                    matched_idx = idx
                    matched_pane_pid = pane_pid
                    break

            if matched_idx is not None:
                claimed.add(matched_idx)
                _restore_into_existing_pane(report, session_name, win,
                                            matched_pane_pid, dry)
            else:
                # No unclaimed window with this name - create a new one
                existing_windows = _create_window_and_resume(
                    report, session_name, win, win_cwd, existing_windows, dry)

    _print_report(console, report, dry)
    return 0


def _print_report(console: Console, report: RestoreReport, dry: bool) -> None:
    console.print(report.table)
    summary = report.summary()
    if summary:
        console.print(f"\n[dim]{summary}[/dim]")
    if dry:
        console.print("[cyan]Dry run - no tmux changes were made.[/cyan]")
    else:
        console.print("\n[green]Restore complete.[/green]")


def cmd_restore_folder(console: Console, dry: bool = False) -> int:
    """Restore the saved windows for the current folder into the current session.

    This is the default restore mode. Whole-machine restore (`--all`) is the
    post-reboot tool; day to day you are sitting in one tmux session working
    on one project and want that project's windows back, not everyone's.
    """
    console.print(Panel(
        "[bold]comax restore[/bold] [dim](this folder)[/dim]"
        + (" [cyan]- dry run[/cyan]" if dry else ""),
        style="blue"))

    if not STATE_FILE.exists():
        console.print(f"[red]No saved state found at {STATE_FILE}[/red]")
        console.print("[dim]Run 'comax' or 'comax --save' first to save current state.[/dim]")
        return 1

    with open(STATE_FILE) as f:
        state = json.load(f)

    folder = _resolve_folder(os.getcwd())
    target_session = get_current_tmux_session()

    # A real restore must land somewhere; a dry run is a preview and stays
    # useful (and safe) from outside tmux.
    if target_session is None and not dry:
        console.print("[red]Error: not inside a tmux session.[/red]\n")
        console.print("comax --restore rehydrates into the session you're attached to. "
                      "Start one first:\n")
        console.print(f"    [bold]tmux new -s {folder.name}[/bold]\n")
        console.print("[dim](or use --dry-run to preview from anywhere, "
                      "or --all for whole-machine restore)[/dim]")
        return 1

    matches, skipped_without_cwd = select_windows_for_folder(state, folder)

    saved_at = state.get("saved_at", "unknown")
    console.print(f"\n[dim]State saved at: {saved_at}[/dim]")
    console.print(f"[dim]Folder:  {folder}[/dim]")
    console.print(f"[dim]Session: {target_session or '<current tmux session>'}[/dim]")
    console.print(f"[dim]{len(matches)} saved window(s) for this folder[/dim]\n")

    if not matches:
        console.print("[yellow]No saved windows for this folder.[/yellow]")
        others = summarize_saved_folders(state)
        if others:
            console.print("\n[dim]Folders with saved windows:[/dim]")
            for path, count in others:
                console.print(f"  [dim]{count:>3}[/dim]  {path}")
            console.print("\n[dim]cd to one of those, or use --all to restore "
                          "everything.[/dim]")
        if skipped_without_cwd:
            console.print(f"\n[dim]note: {skipped_without_cwd} saved window(s) have no "
                          "recorded cwd and can't be folder-matched; use --all to "
                          "restore them.[/dim]")
        return 0

    report = RestoreReport(dry)
    session_name = target_session or "<current>"

    # Claim live windows on name AND path: everything lands in the current
    # session, where saved window names routinely collide with unrelated live
    # ones ('node', 'prime'). Name-only matching would fire a resume into
    # another project's pane.
    live = get_existing_windows_with_path(target_session) if target_session else []
    # Claims are tracked by pane PID, not list index: creating a window
    # re-orders the live list, and PIDs stay stable across refreshes.
    claimed_pids: set[int] = set()
    # [(name, pid), ...] view for the create path's new-window detection.
    existing_windows = [(name, pid) for name, pid, _ in live]

    for win in matches:
        win_name = win["name"]

        matched_pane_pid = None
        for name, pane_pid, pane_path in live:
            if pane_pid in claimed_pids or name != win_name:
                continue
            if not _same_folder(pane_path, folder):
                continue
            claimed_pids.add(pane_pid)
            matched_pane_pid = pane_pid
            break

        if matched_pane_pid is not None:
            _restore_into_existing_pane(report, session_name, win,
                                        matched_pane_pid, dry)
        else:
            before_pids = {pid for _, pid in existing_windows}
            existing_windows = _create_window_and_resume(
                report, session_name, win, str(folder), existing_windows, dry)
            if not dry and target_session:
                # Claim whatever pane we just created, and refresh the
                # path-aware view so a later window cannot re-claim it.
                claimed_pids |= {pid for _, pid in existing_windows} - before_pids
                live = get_existing_windows_with_path(target_session)

    _print_report(console, report, dry)
    if skipped_without_cwd:
        console.print(f"[dim]note: {skipped_without_cwd} saved window(s) have no recorded "
                      "cwd and can't be folder-matched; use --all to restore them.[/dim]")
    return 0


def _send_keys_to_pane(session_name: str, pane_pid: int, keys: str) -> RunResult:
    """Send keys to a specific pane identified by its PID, avoiding name ambiguity.

    Returns the underlying tmux RunResult so callers can distinguish OK/FAIL.
    On lookup failure (no pane with that PID), returns a synthetic non-zero
    RunResult — we never silently fall back to name-based targeting (FX001-3).
    """
    # `list-panes -a` lists every pane across every session; `-t` is ignored
    # alongside `-a`, so we drop it (cosmetic cleanup vs the original code).
    raw = run([
        "tmux", "list-panes", "-a",
        "-F", "#{session_name}:#{window_index}.#{pane_index}|#{pane_pid}"
    ])
    target = None
    for line in (raw or "").splitlines():
        parts = line.split("|", 1)
        if len(parts) == 2 and parts[1].strip() == str(pane_pid):
            target = parts[0]
            break
    if not target:
        return RunResult(
            stdout="",
            stderr=f"no pane with pid {pane_pid} in session {session_name!r}",
            returncode=2,
        )
    return run_full(["tmux", "send-keys", "-t", target, keys, "Enter"])


def _shell_quote(s: str) -> str:
    if " " in s or "'" in s or '"' in s:
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="comax: save/restore tmux + Copilot CLI, Claude Code, and pi sessions",
        epilog=(
            "comax                        save the whole machine\n"
            "comax --restore              restore this folder into the current tmux session\n"
            "comax --restore --dry-run    preview the above\n"
            "comax --restore --all        restore every saved session (post-reboot)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save", action="store_true", default=True, help="Scan and save state (default)")
    group.add_argument("--restore", action="store_true",
                       help="Restore saved windows for the current folder into the current tmux session")
    parser.add_argument("--all", action="store_true",
                        help="With --restore: restore every saved session under its original name")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without changing anything")
    args = parser.parse_args()

    if args.all and not args.restore:
        parser.error("--all only applies to --restore")

    console = Console()

    if args.restore:
        if args.all:
            return cmd_restore(console, dry=args.dry_run)
        return cmd_restore_folder(console, dry=args.dry_run)
    cmd_save(console, dry=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
