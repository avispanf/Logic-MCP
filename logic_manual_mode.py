"""Local process pause switch. Never sends events, MIDI, or AppleScript to Logic."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / "Library/Application Support/LogicMCP"
STATE_FILE = STATE_DIR / "manual-mode.json"
SCRIPTS = tuple(str(ROOT / name) for name in (
    "logic_plugins_mcp.py", "logic_audio_mcp.py", "logic_pro_http_bridge.py",
    "run_mix_audit.py",
))
BACKENDS = (
    str(Path.home() / ".local/bin/LogicProMCP-codex"),
    "/opt/homebrew/bin/LogicProMCP",
    str(Path.home() / "dev/logic-pro-mcp-core/.build/release/LogicProMCP"),
    str(Path.home() / "dev/logic-pro-mcp-core/.build/arm64-apple-macosx/release/LogicProMCP"),
)


def wait_for_automatic_mode():
    # New STDIO sessions wait before importing MCP or opening any Logic/MIDI channel.
    while STATE_FILE.exists():
        time.sleep(0.25)


def managed(command):
    if any(command == path for path in BACKENDS):
        return True
    if re.fullmatch(r"/opt/homebrew/Cellar/logic-pro-mcp/[^/]+/bin/LogicProMCP", command):
        return True
    for script in SCRIPTS:
        before, found, after = command.partition(" " + script)
        if found and (not after or after.startswith(" ")):
            # Match the Python executable, not a shell/host mentioning a script.
            if re.fullmatch(r"/[^\s]*/(?:Python|python[0-9.]*)(?: -[uB])*", before):
                return True
    return False


def processes():
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,uid=,stat=,lstart=,command="],
        capture_output=True, text=True, timeout=4, check=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 9)
        if len(parts) != 10 or int(parts[2]) != os.getuid():
            continue
        pid, ppid, _, status, *rest = parts
        rows[int(pid)] = {"pid": int(pid), "ppid": int(ppid), "status": status,
                          "birth": " ".join(rest[:5]), "command": rest[5]}
    return rows


def same_process(record, row):
    return row is not None and all(record.get(key) == row.get(key)
                                  for key in ("pid", "birth", "command"))


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_FILE)


def signal_verified(record, signum):
    current = processes().get(record["pid"])
    if same_process(record, current):
        os.kill(record["pid"], signum)
        return True
    return False


def selected_processes(rows, records):
    selected = {pid for pid, row in rows.items() if managed(row["command"])}
    selected.update(r["pid"] for r in records if same_process(r, rows.get(r["pid"])))
    while True:
        children = {pid for pid, row in rows.items() if row["ppid"] in selected}
        if children.issubset(selected):
            return selected
        selected.update(children)


def operate(command):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "manual-mode.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
        if command == "toggle":
            command = "resume" if state else "pause"
        if command == "pause" and state is None:
            state = {"version": 1, "processes": []}
            save_state(state)  # Block new sessions before stopping existing workers.
        errors = []
        if command == "resume" and state:
            # Keep the gate closed until every process we stopped is resumed.
            for record in reversed(state["processes"]):
                if not record.get("resume", True):
                    continue
                try:
                    signal_verified(record, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    errors.append(str(exc))
            if not errors:
                time.sleep(0.05)
                rows = processes()
                for record in state["processes"]:
                    row = rows.get(record["pid"])
                    if record.get("resume", True) and same_process(record, row) and "T" in row["status"]:
                        errors.append(f"PID {record['pid']} did not resume")
                if not errors:
                    STATE_FILE.unlink()
                    state = None
        if state:
            # Stop roots first; take a second snapshot to catch children created at
            # the first boundary. Never stop the host app or Logic itself.
            for _ in range(3):
                rows = processes()
                targets = selected_processes(rows, state["processes"])
                records = {r["pid"]: r for r in state["processes"]}
                for pid in sorted(targets, key=lambda p: (rows[p]["ppid"] in targets, p)):
                    row = rows[pid]
                    if not same_process(records.get(pid, {}), row):
                        record = {**row, "resume": "T" not in row["status"]}
                        state["processes"] = [r for r in state["processes"] if r["pid"] != pid]
                        state["processes"].append(record)
                        save_state(state)  # Journal before SIGSTOP, including descendants.
                    if "T" not in row["status"]:
                        try:
                            signal_verified(row, signal.SIGSTOP)
                        except ProcessLookupError:
                            pass
                        except OSError as exc:
                            errors.append(str(exc))
                time.sleep(0.03)
            rows = processes()
            targets = selected_processes(rows, state["processes"])
            active = [pid for pid in targets if "T" not in rows[pid]["status"]]
            return {"mode": "manual", "verified": not active and not errors,
                    "paused": len(targets) - len(active), "active": active, "errors": errors}
        return {"mode": "automatic", "verified": not errors, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "pause", "resume", "toggle"))
    args = parser.parse_args()
    try:
        result = operate(args.command)
    except Exception as exc:
        result = {"mode": "unknown", "verified": False, "errors": [str(exc)]}
    print(json.dumps(result))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
