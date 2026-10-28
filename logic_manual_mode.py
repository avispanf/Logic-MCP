"""Verified manual mode and process inventory for Logic MCP (macOS)."""
from __future__ import annotations
import argparse
import ctypes
import fcntl
import json
import os
import plistlib
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOGIC_MCP_STATE_DIR", Path.home() / "Library/Application Support/LogicMCP"))
STATE_FILE = STATE_DIR / "manual-mode.json"
SCRIPT_NAMES = {"logic_plugins_mcp.py", "logic_audio_mcp.py", "logic_pro_http_bridge.py", "run_mix_audit.py"}
CANDIDATE_NAMES = tuple(SCRIPT_NAMES | {Path(name).stem for name in SCRIPT_NAMES} | {"LogicProMCP", "logic-mcp"})
SCRIPTS = tuple(str(ROOT / name) for name in sorted(SCRIPT_NAMES))
BACKENDS = (str(Path.home() / ".local/bin/LogicProMCP-codex"), "/opt/homebrew/bin/LogicProMCP")
DIAGNOSTIC_FLAGS = {"--help", "-h", "--version", "--check"}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_state():
    if not STATE_FILE.exists():
        return None
    state = json.loads(STATE_FILE.read_text())
    if not isinstance(state, dict) or not isinstance(state.get("processes"), list):
        raise RuntimeError("Invalid manual-mode state. Run repair; automation stays blocked.")
    if state.get("version") not in (1, 2):
        raise RuntimeError("Unsupported state version; automation stays blocked.")
    for row in state["processes"]:
        if not isinstance(row, dict) or not isinstance(row.get("pid"), int) or not row.get("birth") or not row.get("command"):
            raise RuntimeError("Invalid process journal. Run repair; automation stays blocked.")
    return state


def save_state(state):
    _write(STATE_FILE, state)


def wait_for_automatic_mode():
    if DIAGNOSTIC_FLAGS.intersection(sys.argv[1:]):
        return
    while STATE_FILE.exists():
        time.sleep(0.25)


def process_arguments(pid, include_environment=False):
    """Read actual argv; ps text cannot distinguish spaces from argument boundaries."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        size = ctypes.c_size_t(1024 * 1024)
        data = ctypes.create_string_buffer(size.value)
        mib = (ctypes.c_int * 3)(1, 49, pid)
        if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
            return (None, {}) if include_environment else None
        raw = data.raw[:size.value]
        argc = int.from_bytes(raw[:4], sys.byteorder)
        offset = raw.find(b"\0", 4) + 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        fields = [s.decode(errors="surrogateescape") for s in raw[offset:].split(b"\0")]
        argv = fields[:argc]
        environment = dict(s.split("=", 1) for s in fields[argc:] if "=" in s)
        return (argv, environment) if include_environment else argv
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        environment = dict(s.split("=", 1) for s in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if "=" in s)
        return (argv, environment) if include_environment else argv
    except OSError:
        return (None, {}) if include_environment else None


def managed(command):
    try:
        return managed_argv(shlex.split(command))
    except ValueError:
        return False


def managed_argv(argv):
    if not argv:
        return False
    executable = Path(argv[0]).name
    if executable == "logic-mcp":
        return any(arg in ("--service", "--plugins", "--audio", "--audit") for arg in argv[1:])
    if executable in ("LogicProMCP", "LogicProMCP-codex"):
        return True
    if executable != "Python" and not executable.lower().startswith("python"):
        return False
    args = iter(argv[1:])
    for arg in args:
        if arg == "-c":
            return False
        if arg == "--":
            return Path(next(args, "")).name in SCRIPT_NAMES
        if arg in ("-X", "-W"):
            next(args, None)
            continue
        if arg == "-m":
            return next(args, "") + ".py" in SCRIPT_NAMES
        if arg.startswith("-"):
            continue
        return Path(arg).name in SCRIPT_NAMES
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
    for row in rows.values():
        if any(name in row["command"] for name in CANDIDATE_NAMES):
            row["argv"], environment = process_arguments(row["pid"], include_environment=True)
            row["state_dir"] = environment.get("LOGIC_MCP_STATE_DIR")
    return rows


def same_process(record, row):
    return row is not None and all(record.get(key) == row.get(key) for key in ("pid", "birth", "command"))


def signal_verified(record, signum):
    if same_process(record, processes().get(record["pid"])):
        os.kill(record["pid"], signum)
        return True
    return False


def supervisors(rows):
    result = set()
    # The installed LaunchAgent identifies the bundled supervisor before Python/SDK
    # imports finish. Otherwise the menu could stop its own service during cold start.
    path = Path.home() / "Library/LaunchAgents/com.avispanf.logic-pro-mcp-bridge.plist"
    try:
        agent = plistlib.loads(path.read_bytes())
        arguments = agent.get("ProgramArguments", [])
        if agent.get("Label") == "com.avispanf.logic-pro-mcp-bridge" and len(arguments) == 2 and arguments[1] == "--service" and Path(arguments[0]).name == "logic-mcp":
            result.update(pid for pid, row in rows.items() if row.get("argv") == arguments)
    except (OSError, ValueError, TypeError):
        pass
    try:
        record = json.loads((STATE_DIR / "supervisor.json").read_text())
        if same_process(record, rows.get(record["pid"])):
            result.add(record["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def idle_workers(rows):
    result = set()
    for path in (STATE_DIR / "workers").glob("*.json"):
        try:
            row = json.loads(path.read_text())
            if row.get("active") is False and same_process(row, rows.get(row["pid"])):
                result.add(row["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result


@contextmanager
def register_worker():
    path = STATE_DIR / "workers" / f"{os.getpid()}.json"
    _write(path, {**processes()[os.getpid()], "active": False})
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def worker_activity():
    path = STATE_DIR / "workers" / f"{os.getpid()}.json"
    # Atomic with the controller's gate-close + inventory: no idle-to-active gap.
    with (STATE_DIR / "manual-mode.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if STATE_FILE.exists():
            raise RuntimeError("Logic is in manual mode")
        if path.exists():
            row = json.loads(path.read_text())
            _write(path, {**row, "active": True})
    try:
        yield
    finally:
        if path.exists():
            row = json.loads(path.read_text())
            _write(path, {**row, "active": False})


@contextmanager
def register_supervisor():
    path = STATE_DIR / "supervisor.json"
    _write(path, processes()[os.getpid()])
    try:
        yield
    finally:
        try:
            if json.loads(path.read_text()).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass


def inventory(rows, records=()):
    service_pids = supervisors(rows)
    supervisors_ = service_pids | idle_workers(rows)
    selected, unknown = set(), []
    def in_scope(row):
        return not row.get("state_dir") or Path(row["state_dir"]).resolve() == STATE_DIR.resolve()
    for pid, row in rows.items():
        argv = row.get("argv")
        if pid in supervisors_ or not in_scope(row):
            continue
        if managed_argv(argv) if argv else managed(row["command"]):
            selected.add(pid)
        elif not argv and "Z" not in row["status"] and any(name in row["command"] for name in CANDIDATE_NAMES):
            if Path(row["command"].split()[0]).name not in ("zsh", "bash", "sh", "rg", "disclaimer"):
                unknown.append(pid)
    selected.update(pid for pid, row in rows.items() if row["ppid"] in service_pids and in_scope(row))
    selected.update(r["pid"] for r in records if same_process(r, rows.get(r["pid"])) and r["pid"] not in supervisors_ and in_scope(rows[r["pid"]]))
    while True:
        children = {p for p, r in rows.items() if r["ppid"] in selected}
        if children.issubset(selected):
            break
        selected.update(children)
    selected = {p for p in selected if not any(name in rows[p]["command"].split(" -", 1)[0]
                for name in ("/Logic Pro.app/", "/Claude.app/", "/ChatGPT.app/", "/Codex.app/"))}
    return selected, unknown


def selected_processes(rows, records):
    return inventory(rows, records)[0]


def health():
    from urllib.request import urlopen
    try:
        with urlopen("http://127.0.0.1:8765/health", timeout=0.5) as response:
            value = json.load(response)
        return value if value.get("service") == "logic-mcp" else {}
    except Exception:
        return {}


def operate(command):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "manual-mode.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "repair":
            if STATE_FILE.exists():
                os.replace(STATE_FILE, STATE_DIR / f"manual-mode.backup-{time.time_ns()}.json")
            save_state({"version": 2, "epoch": uuid.uuid4().hex, "processes": []})
        state = read_state()
        if state is not None:
            rows = processes()
            live = [r for r in state["processes"] if same_process(r, rows.get(r["pid"]))]
            if state["version"] == 1:
                _write(STATE_DIR / f"manual-mode.v1-backup-{time.time_ns()}.json", state)
                state = {**state, "version": 2, "epoch": uuid.uuid4().hex}
            if live != state["processes"] or not STATE_FILE.exists() or read_state().get("version") != state["version"]:
                state["processes"] = live
                save_state(state)
        if command == "toggle":
            command = "resume" if state is not None else "pause"
        if command == "pause" and state is None:
            state = {"version": 2, "epoch": uuid.uuid4().hex, "processes": []}
            save_state(state)
        errors = []
        if command == "resume" and state is not None:
            rows = processes()
            targets, unknown = inventory(rows, state["processes"])
            if unknown:
                errors.append(f"Cannot identify workers: {unknown}")
            ownership = {r["pid"]: r for r in state["processes"]}
            # Cancel old work. Never SIGCONT old automation into a changed project.
            for pid in sorted(targets, reverse=True):
                if not ownership.get(pid, {}).get("resume", True):
                    errors.append(f"PID {pid} was already stopped outside this controller; resolve it before enabling")
                    continue
                try:
                    signal_verified(rows[pid], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    errors.append(str(exc))
            time.sleep(0.05)
            current = processes()
            for pid in targets:
                if ownership.get(pid, {}).get("resume", True) and same_process(rows[pid], current.get(pid)) and "Z" not in current[pid]["status"]:
                    errors.append(f"Old worker {pid} still alive")
            if not errors:
                _write(STATE_DIR / "epoch.json", {"epoch": uuid.uuid4().hex})
                STATE_FILE.unlink()
                state = None
        if state is not None:
            for _ in range(3):
                rows = processes()
                targets, unknown = inventory(rows, state["processes"])
                records = {r["pid"]: r for r in state["processes"]}
                for pid in sorted(targets, key=lambda p: (rows[p]["ppid"] in targets, p)):
                    row = rows[pid]
                    if not same_process(records.get(pid, {}), row):
                        state["processes"] = [r for r in state["processes"] if r["pid"] != pid]
                        state["processes"].append({**row, "resume": "T" not in row["status"]})
                        save_state(state)
                    if "T" not in row["status"] and "Z" not in row["status"]:
                        try:
                            signal_verified(row, signal.SIGSTOP)
                        except ProcessLookupError:
                            pass
                        except OSError as exc:
                            errors.append(str(exc))
                time.sleep(0.03)
            rows = processes()
            targets, unknown = inventory(rows, state["processes"])
            active = [p for p in targets if not any(x in rows[p]["status"] for x in ("T", "Z"))]
            return {"mode": "manual", "verified": not active and not unknown and not errors,
                    "paused": len(targets) - len(active), "active": active, "unknown": unknown,
                    "errors": errors, "old_jobs": "cancelled_on_enable"}
        service = health()
        return {"mode": "automatic", "verified": not errors, "ready": service.get("ready", False),
                "service_state": service.get("state", "offline"), "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "pause", "resume", "toggle", "repair"))
    args = parser.parse_args()
    try:
        result = operate(args.command)
    except Exception as exc:
        result = {"mode": "unknown", "verified": False, "errors": [str(exc)]}
    print(json.dumps(result))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
