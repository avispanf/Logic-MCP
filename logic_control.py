"""Cross-process ownership and mode barriers shared by Logic MCP entry points."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import functools
import json
import os
import uuid
import logic_manual_mode as mode

_depth = ContextVar("logic_operation_depth", default=0)


def epoch():
    path = mode.STATE_DIR / "epoch.json"
    return path.read_text() if path.exists() else "initial"


def require_automatic():
    if mode.STATE_FILE.exists():
        raise RuntimeError("Logic is in manual mode. Enable automation before sending a NEW request.")


@contextmanager
def operation(owner=None, expected_epoch=None):
    require_automatic()
    initial = epoch() if expected_epoch is None else expected_epoch
    if initial != epoch():
        raise RuntimeError("Request cancelled by mode transition. Submit a new request.")
    if _depth.get():
        yield
        return
    mode.STATE_DIR.mkdir(parents=True, exist_ok=True)
    owner = owner or os.environ.get("LOGIC_MCP_OWNER_TOKEN")
    with (mode.STATE_DIR / "operation.lock").open("a") as lock:
        borrowed = False
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (mode.STATE_DIR / "owner.json").unlink(missing_ok=True)
            if owner:
                raise RuntimeError("Audit ownership expired; submit a new audit.")
        except BlockingIOError:
            try:
                lease = json.loads((mode.STATE_DIR / "owner.json").read_text())
                borrowed = bool(owner and owner == lease.get("token"))
            except (OSError, ValueError):
                pass
            if not borrowed:
                raise RuntimeError("Logic is owned by another operation or audit. Try after it finishes.")
        require_automatic()
        if initial != epoch():
            raise RuntimeError("Request cancelled by mode transition.")
        token = _depth.set(1)
        try:
            yield
        finally:
            _depth.reset(token)


@contextmanager
def transaction():
    with operation():
        token = uuid.uuid4().hex
        path = mode.STATE_DIR / "owner.json"
        mode._write(path, {"pid": os.getpid(), "token": token})
        try:
            yield token
        finally:
            path.unlink(missing_ok=True)


def guard_tool(fn):
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        with operation():
            with mode.worker_activity():
                return fn(*args, **kwargs)
    return guarded
