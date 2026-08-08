from __future__ import annotations

import json
import os
import pathlib
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_PROCESSES = ("Logic Pro", "Logic Pro Creator Studio")
PREFERENCE_DOMAINS = ("com.apple.logic10", "com.apple.mobilelogic")
CONTROLS_VIEW_KEYS = (
    "PlugInWindowsUseControlsView",
    "OpenPlugInWindowsInControlsView",
    "kPlugInWindowControlsView",
)
SETTINGS_ROOTS = (
    Path.home() / "Music" / "Audio Music Apps" / "Channel Strip Settings",
    Path.home() / "Music" / "Audio Music Apps" / "Plug-In Settings",
)
PLIST_MAGIC = (b"bplist00", b"<?xml")
WRITABLE_ROLES = ("AXSlider", "AXTextField", "AXIncrementor", "AXCheckBox", "AXPopUpButton")

NUMERIC_ROLES = ("AXSlider", "AXIncrementor", "AXValueIndicator", "AXScrollBar")


def as_number(text):
    try:
        return float(str(text).strip().replace(",", "."))
    except (ValueError, AttributeError, TypeError):
        return None


def find_control(name: str, window_index: int, process: str) -> dict:
    matches = [
        e
        for e in walk_window(process, window_index, 6)
        if e["name"] == name or e["description"] == name
    ]
    if not matches:
        raise ProbeError(f"no control named {name!r} in window {window_index}")
    if len(matches) > 1:
        paths = ", ".join(m["path"] for m in matches[:6])
        raise ProbeError(
            f"{len(matches)} controls named {name!r} in window {window_index} "
            f"at paths {paths}, refusing to guess. Use plugin_write_path"
        )
    return matches[0]


TOOLS: list = []
PLANS: dict[str, dict] = {}


def tool(fn):
    TOOLS.append(fn)
    return fn


class ProbeError(RuntimeError):
    pass


def osa(script: str, timeout: float = 40.0) -> str:
    if shutil.which("osascript") is None:
        raise ProbeError("osascript not found, this server requires macOS")
    process = subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise ProbeError(
            f"osascript exceeded {timeout:.0f}s and was killed. The element tree under "
            "this branch is too large for Accessibility. Reduce max_depth, target a "
            "narrower path, or use the MCU channel for mixer-scale data"
        )
    if process.returncode != 0:
        raise ProbeError((err or "").strip() or "osascript failed")
    return (out or "").strip()


def logic_process() -> str | None:
    for name in APP_PROCESSES:
        proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
        if proc.returncode == 0:
            return name
    return None


def require_logic() -> str:
    name = logic_process()
    if name is None:
        raise ProbeError("Logic is not running")
    return name


def split_records(raw: str) -> list[str]:
    return [part.strip() for part in raw.split("|:|") if part.strip()]


def read_preference_flags() -> dict:
    found = {}
    for domain in PREFERENCE_DOMAINS:
        for key in CONTROLS_VIEW_KEYS:
            proc = subprocess.run(
                ["defaults", "read", domain, key], capture_output=True, text=True
            )
            if proc.returncode == 0:
                found[f"{domain}.{key}"] = proc.stdout.strip()
    return found


def inventory_settings() -> list[dict]:
    entries = []
    for root in SETTINGS_ROOTS:
        if not root.is_dir():
            continue
        files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
        by_suffix: dict[str, int] = {}
        for path in files:
            by_suffix[path.suffix.lower() or "(none)"] = by_suffix.get(path.suffix.lower() or "(none)", 0) + 1
        entries.append(
            {
                "root": str(root),
                "files": len(files),
                "by_suffix": dict(sorted(by_suffix.items(), key=lambda kv: -kv[1])),
                "sample": [str(p) for p in files[:8]],
            }
        )
    return entries


@tool
def plugins_probe() -> dict:
    """Report which plugin access channels are actually open on this machine before any
    read or write is attempted: whether Logic runs, whether Accessibility works, whether
    plugin windows default to Controls view, whether saved channel strip settings exist,
    and whether auval is available for parameter dictionaries."""
    report: dict = {"platform_ok": sys.platform == "darwin"}
    name = logic_process()
    report["logic_process"] = name
    report["accessibility"] = {}
    if name:
        try:
            windows = osa(
                f'tell application "System Events" to tell process "{name}" to '
                "return count of windows"
            )
            report["accessibility"] = {"granted": True, "window_count": int(windows or 0)}
        except (ProbeError, ValueError) as exc:
            report["accessibility"] = {"granted": False, "reason": str(exc)}
    report["controls_view_preference"] = read_preference_flags() or {
        "found": False,
        "note": (
            "no known preference key was readable. Set it by hand in Logic: "
            "Settings > Accessibility > Open plug-in windows in Controls view by default"
        ),
    }
    report["settings_files"] = inventory_settings()
    report["auval"] = shutil.which("auval") or "not found"
    report["next_step"] = (
        "run ax_dump on an open plugin window to learn the real element tree "
        "before attempting any parameter read or write"
    )
    return report


@tool
def au_list(filter_text: str | None = None, limit: int = 200) -> dict:
    """List installed Audio Unit plugins with their four-character type, subtype and
    manufacturer codes. Those codes are what au_parameters needs."""
    if shutil.which("auval") is None:
        return {"error": "auval not found"}
    proc = subprocess.run(["auval", "-a"], capture_output=True, text=True, timeout=180)
    units = []
    pattern = re.compile(
        r"^\s*(\w{4})\s+(\w{4})\s+(\w{4})\s*-\s*(.+?):\s*(.+?)\s*$"
    )
    for line in proc.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        kind, subtype, manufacturer, vendor, title = match.groups()
        entry = {
            "type": kind,
            "subtype": subtype,
            "manufacturer": manufacturer,
            "vendor": vendor.strip(),
            "name": title.strip(),
        }
        if filter_text and filter_text.lower() not in json.dumps(entry).lower():
            continue
        units.append(entry)
    return {"count": len(units), "units": units[:limit]}


@tool
def au_parameters(type_code: str, subtype: str, manufacturer: str) -> dict:
    """Read the parameter dictionary of one Audio Unit: index, name, range and default
    for each parameter. Use this before writing any parameter by index, so a numeric
    index is never guessed. Codes come from au_list."""
    if shutil.which("auval") is None:
        return {"error": "auval not found"}
    for code in (type_code, subtype, manufacturer):
        if not re.fullmatch(r"[\w\-\*\+]{4}", code):
            return {"error": f"not a four character code: {code!r}"}
    proc = subprocess.run(
        ["auval", "-v", type_code, subtype, manufacturer],
        capture_output=True,
        text=True,
        timeout=240,
    )
    text = proc.stdout
    parameters = []
    current: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        id_match = re.match(r"^Parameter ID\s*:?\s*(\d+)", stripped, re.I)
        if id_match:
            if current:
                parameters.append(current)
            current = {"index": int(id_match.group(1))}
            continue
        if not current:
            continue
        name_match = re.match(r"^Name\s*:?\s*(.+)$", stripped, re.I)
        if name_match:
            current["name"] = name_match.group(1).strip()
            continue
        values = re.search(
            r"Minimum\s*=\s*(-?[\d.]+).*?Default\s*=\s*(-?[\d.]+).*?Maximum\s*=\s*(-?[\d.]+)",
            stripped,
            re.I,
        )
        if values:
            current["minimum"] = float(values.group(1))
            current["default"] = float(values.group(2))
            current["maximum"] = float(values.group(3))
            continue
        unit_match = re.match(r"^Parameter Type\s*:?\s*(.+)$", stripped, re.I)
        if unit_match:
            current["unit"] = unit_match.group(1).strip()
    if current:
        parameters.append(current)
    result: dict = {
        "unit": f"{type_code} {subtype} {manufacturer}",
        "parameter_count": len(parameters),
        "parameters": parameters,
    }
    if not parameters:
        result["raw_head"] = text[:1200]
        result["note"] = (
            "no parameters parsed. auval output format varies by macOS release; "
            "send raw_head back so the parser can be corrected against real output"
        )
    return result


@tool
def strip_settings_inspect(path: str, harvest_strings: int = 40) -> dict:
    """Probe a saved channel strip or plugin settings file and report what is actually
    inside it: whether it is a property list, whether a property list is embedded, and
    which readable strings it contains. This is a format explorer, not a parser -- it
    exists so the format is learned from real files instead of guessed."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"error": f"file not found: {target}"}
    data = target.read_bytes()
    report: dict = {
        "path": str(target),
        "bytes": len(data),
        "leading_hex": data[:16].hex(),
        "is_plist": data.startswith(PLIST_MAGIC),
    }
    if report["is_plist"]:
        try:
            parsed = plistlib.loads(data)
            report["plist_type"] = type(parsed).__name__
            if isinstance(parsed, dict):
                report["plist_keys"] = sorted(parsed.keys())
                report["scalars"] = {
                    k: v
                    for k, v in parsed.items()
                    if isinstance(v, (str, int, float, bool))
                }
        except Exception as exc:
            report["plist_error"] = f"{type(exc).__name__}: {exc}"
    else:
        offset = data.find(b"bplist00")
        if offset > 0:
            report["embedded_plist_offset"] = offset
            try:
                parsed = plistlib.loads(data[offset:])
                report["embedded_type"] = type(parsed).__name__
                if isinstance(parsed, dict):
                    report["embedded_keys"] = sorted(parsed.keys())
            except Exception as exc:
                report["embedded_error"] = f"{type(exc).__name__}: {exc}"
    strings = re.findall(rb"[ -~]{4,}", data)
    seen: list[str] = []
    for chunk in strings:
        text = chunk.decode("ascii", "replace")
        if text not in seen:
            seen.append(text)
        if len(seen) >= harvest_strings:
            break
    report["strings"] = seen
    return report


@tool
def strip_settings_list(limit: int = 80) -> dict:
    """List saved channel strip and plugin settings files, newest first, so one can be
    handed to strip_settings_inspect."""
    entries = []
    for root in SETTINGS_ROOTS:
        if not root.is_dir():
            continue
        files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:limit]:
            entries.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "modified": int(path.stat().st_mtime),
                }
            )
    return {"count": len(entries), "files": entries[:limit], "roots": [str(r) for r in SETTINGS_ROOTS]}


SKIP_DESCENT = ("AXScrollBar", "AXColumn", "AXImage", "AXValueIndicator", "AXStaticText")
CONTROL_ROLES = (
    "AXSlider",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXTextField",
    "AXIncrementor",
    "AXMenuButton",
    "AXButton",
)


def clean(value: str) -> str:
    return "" if value in ("missing value", "-") else value


def normalise_number(value: str) -> str:
    text = value.strip().replace(",", ".")
    try:
        return f"{float(text):g}"
    except ValueError:
        return value.strip()


def values_match(requested: str, observed: str) -> bool:
    if requested == observed:
        return True
    return normalise_number(requested) == normalise_number(observed)


def walk_script(process: str, window: int, max_depth: int, budget: int, seconds: int, root: str = "") -> str:
    skip = ", ".join(f'"{r}"' for r in SKIP_DESCENT)
    return (
        "global outText\n"
        "global seen\n"
        "global deadline\n"
        "on grab(parent, prefix, depth, maxDepth)\n"
        "if seen > " + str(int(budget)) + " then return\n"
        "if (current date) > deadline then return\n"
        'tell application "System Events"\n'
        "set rs to {}\n"
        "try\n"
        "set rs to role of every UI element of parent\n"
        "end try\n"
        "set n to count of rs\n"
        "if n is 0 then return\n"
        "set ns to {}\n"
        "set ds to {}\n"
        "set vs to {}\n"
        "try\n"
        "set ns to name of every UI element of parent\n"
        "end try\n"
        "try\n"
        "set ds to description of every UI element of parent\n"
        "end try\n"
        "try\n"
        "set vs to value of every UI element of parent\n"
        "end try\n"
        "repeat with i from 1 to n\n"
        'set rr to ""\n'
        'set nn to ""\n'
        'set dd to ""\n'
        'set vv to ""\n'
        "try\n"
        "set rr to (item i of rs) as string\n"
        "end try\n"
        "try\n"
        "set nn to (item i of ns) as string\n"
        "end try\n"
        "try\n"
        "set dd to (item i of ds) as string\n"
        "end try\n"
        "try\n"
        "set vv to (item i of vs) as string\n"
        "end try\n"
        'set outText to outText & prefix & i & "~" & rr & "~" & nn & "~" & dd & "~" & vv & "|:|"\n'
        "set seen to seen + 1\n"
        "end repeat\n"
        "if depth < maxDepth then\n"
        "repeat with i from 1 to n\n"
        'set rr to ""\n'
        "try\n"
        "set rr to (item i of rs) as string\n"
        "end try\n"
        "if rr is not in {" + skip + "} then\n"
        "try\n"
        'my grab(UI element i of parent, prefix & i & ".", depth + 1, maxDepth)\n'
        "end try\n"
        "end if\n"
        "end repeat\n"
        "end if\n"
        "end tell\n"
        "end grab\n"
        'set outText to ""\n'
        "set seen to 0\n"
        "set deadline to (current date) + " + str(int(seconds)) + "\n"
        'tell application "System Events"\n'
        f'tell process "{process}"\n'
        + (
            f"my grab({element_reference(root, window)}, \"{root}.\", 0, {int(max_depth)})\n"
            if root
            else f"my grab(window {int(window)}, \"\", 0, {int(max_depth)})\n"
        )
        +
        "end tell\n"
        "end tell\n"
        "return outText"
    )


def element_reference(path: str, window: int) -> str:
    parts = [int(x) for x in path.split(".") if x.strip()]
    reference = f"window {int(window)}"
    for index in parts:
        reference = f"UI element {index} of {reference}"
    return reference


def walk_window(
    process: str,
    window: int,
    max_depth: int = 6,
    budget: int = 1500,
    seconds: int = 90,
    root: str = "",
) -> list[dict]:
    raw = osa(
        walk_script(process, window, max_depth, budget, seconds, root),
        timeout=seconds + 15,
    )
    elements = []
    for record in split_records(raw):
        parts = (record.split("~") + ["", "", "", "", ""])[:5]
        elements.append(
            {
                "path": parts[0],
                "role": parts[1],
                "name": clean(parts[2]),
                "description": clean(parts[3]),
                "value": clean(parts[4]),
            }
        )
    return elements


def parent_path(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def pair_parameters(elements: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for entry in elements:
        groups.setdefault(parent_path(entry["path"]), []).append(entry)
    parameters = []
    claimed: set[str] = set()
    for parent, siblings in groups.items():
        if not parent:
            continue
        labels = [s for s in siblings if s["role"] == "AXStaticText" and s["value"]]
        if not labels:
            continue
        label = labels[0]["value"].rstrip(":").strip()
        for sibling in siblings:
            if sibling["role"] == "AXStaticText":
                continue
            control = sibling
            display = sibling["value"]
            if sibling["role"] == "AXGroup":
                inner = [
                    c
                    for c in groups.get(sibling["path"], [])
                    if c["role"] in CONTROL_ROLES
                ]
                if not inner:
                    continue
                control = inner[0]
                display = sibling["value"] or control["value"]
            elif sibling["role"] not in CONTROL_ROLES:
                continue
            if control["path"] in claimed:
                continue
            claimed.add(control["path"])
            parameters.append(
                {
                    "label": label,
                    "display": display,
                    "raw_value": control["value"],
                    "role": control["role"],
                    "path": control["path"],
                }
            )
    parameters.sort(key=lambda p: [int(x) for x in p["path"].split(".")])
    return parameters


@tool
def ax_windows() -> dict:
    """List Logic's windows with their titles and subroles. Plugin editors appear as
    separate windows, so this is how a plugin window index is found for ax_dump."""
    name = require_logic()
    raw = osa(
        f'tell application "System Events" to tell process "{name}"\n'
        'set out to ""\n'
        "repeat with w in windows\n"
        'set out to out & (name of w as string) & "~" & (subrole of w as string) & "|:|"\n'
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    windows = []
    for index, record in enumerate(split_records(raw), start=1):
        parts = record.split("~", 1)
        windows.append(
            {
                "index": index,
                "title": parts[0],
                "subrole": parts[1] if len(parts) > 1 else "",
            }
        )
    return {"count": len(windows), "windows": windows}


@tool
def ax_dump(window_index: int = 1, limit: int = 400, max_depth: int = 6) -> dict:
    """Walk the accessibility tree of one Logic window by index recursion and report role,
    name, description and value for every element, each with a dotted path that addresses
    it exactly. The path is what plugin_write_path uses, so no element is ever located by
    an ambiguous name."""
    name = require_logic()
    elements = walk_window(name, int(window_index), int(max_depth))
    roles: dict[str, int] = {}
    for entry in elements:
        roles[entry["role"]] = roles.get(entry["role"], 0) + 1
    named = [e for e in elements if e["name"] or e["description"]]
    return {
        "window_index": window_index,
        "max_depth": max_depth,
        "element_count": len(elements),
        "role_histogram": dict(sorted(roles.items(), key=lambda kv: -kv[1])),
        "writable_candidates": sum(1 for e in elements if e["role"] in WRITABLE_ROLES),
        "named_elements": len(named),
        "elements": elements[:limit],
        "truncated": len(elements) > limit,
    }


@tool
def ax_strategies(window_index: int = 1) -> dict:
    """Try four different ways of walking a window's accessibility tree and report which
    one actually returns element roles on this machine and this Logic build. Run this when
    ax_dump returns elements whose fields are all empty, so the working traversal is
    measured instead of guessed."""
    name = require_logic()
    window = int(window_index)
    attempts = {
        "entire_contents_deref": (
            f'tell application "System Events" to tell process "{name}"\n'
            f"set target to window {window}\n"
            'set out to ""\n'
            "repeat with e in (entire contents of target)\n"
            "set el to contents of e\n"
            "try\n"
            'set out to out & (role of el as string) & "|:|"\n'
            "on error\n"
            'set out to out & "ERR|:|"\n'
            "end try\n"
            "end repeat\n"
            "return out\n"
            "end tell"
        ),
        "entire_contents_direct": (
            f'tell application "System Events" to tell process "{name}"\n'
            f"set target to window {window}\n"
            'set out to ""\n'
            "repeat with e in (entire contents of target)\n"
            "try\n"
            'set out to out & (role of e as string) & "|:|"\n'
            "on error\n"
            'set out to out & "ERR|:|"\n'
            "end try\n"
            "end repeat\n"
            "return out\n"
            "end tell"
        ),
        "bulk_property_query": (
            f'tell application "System Events" to tell process "{name}"\n'
            f"set target to window {window}\n"
            'set out to ""\n'
            "try\n"
            "set rs to role of every UI element of target\n"
            "repeat with r in rs\n"
            'set out to out & (r as string) & "|:|"\n'
            "end repeat\n"
            "on error errText\n"
            'set out to "ERROR: " & errText\n'
            "end try\n"
            "return out\n"
            "end tell"
        ),
        "indexed_top_level": (
            f'tell application "System Events" to tell process "{name}"\n'
            f"set target to window {window}\n"
            'set out to ""\n'
            "set n to count of UI elements of target\n"
            "repeat with i from 1 to n\n"
            "try\n"
            'set out to out & (role of (UI element i of target) as string) & "|:|"\n'
            "on error\n"
            'set out to out & "ERR|:|"\n'
            "end try\n"
            "end repeat\n"
            "return out\n"
            "end tell"
        ),
    }
    results = {}
    for label, script in attempts.items():
        try:
            raw = osa(script, timeout=120)
        except ProbeError as exc:
            results[label] = {"failed": str(exc)}
            continue
        records = split_records(raw)
        good = [r for r in records if r not in ("ERR", "") and not r.startswith("ERROR")]
        results[label] = {
            "returned": len(records),
            "usable": len(good),
            "sample": good[:12] or records[:3],
        }
    winner = max(
        (k for k in results if isinstance(results[k].get("usable"), int)),
        key=lambda k: results[k]["usable"],
        default=None,
    )
    return {
        "window_index": window,
        "results": results,
        "working_strategy": winner if winner and results[winner]["usable"] else None,
        "note": "if every strategy reports zero usable roles, the window is drawn by the "
        "plugin itself and Controls view is not active",
    }


@tool
def plugin_snapshot(
    window_index: int = 1,
    max_depth: int = 6,
    budget: int = 1500,
    seconds: int = 60,
) -> dict:
    """Read one open plugin window as a parameter table: plugin name, whether Controls
    view is active, and every label paired with its control value and exact path. This is
    the read that ax_dump feeds; use it rather than reading the raw tree."""
    name = require_logic()
    elements = walk_window(
        name, int(window_index), int(max_depth), budget=int(budget), seconds=int(seconds)
    )
    if not elements:
        return {"window_index": window_index, "error": "no elements returned"}
    view_mode = next(
        (e["name"] or e["value"] for e in elements if e["role"] == "AXMenuButton" and e["description"] == "view"),
        "",
    )
    titles = [
        e["value"]
        for e in elements
        if e["role"] == "AXStaticText" and "." not in e["path"] and e["value"]
    ]
    parameters = pair_parameters(elements)
    return {
        "window_index": window_index,
        "plugin": titles[0] if titles else "",
        "channel": titles[-1] if len(titles) > 1 else "",
        "view_mode": view_mode,
        "controls_view": view_mode == "Controls",
        "element_count": len(elements),
        "parameter_count": len(parameters),
        "parameters": parameters,
        "note": ""
        if view_mode == "Controls"
        else "view is not Controls, the plugin draws itself and few parameters will be readable",
    }


@tool
def plugin_read(window_index: int = 1) -> dict:
    """Compatibility wrapper around plugin_snapshot."""
    return plugin_snapshot(window_index)


@tool
def plugins_sweep(
    max_depth: int = 6,
    skip_main: bool = True,
    total_seconds: int = 140,
    per_window_seconds: int = 25,
    shallow_limit: int = 400,
) -> dict:
    """Read every currently open plugin window in one pass. Each window is first probed
    cheaply for size, and a window whose shallow element count exceeds shallow_limit is
    skipped before any expensive walk, so a heavy third-party editor cannot consume the
    budget that the readable windows need. The sweep carries both a whole-run deadline and
    a per-window one, and uses the same depth as plugin_snapshot so its values match."""
    process = require_logic()
    listing = ax_windows()
    deadline = time.time() + max(10, min(int(total_seconds), 600))
    results, skipped = [], []
    for entry in listing["windows"]:
        if skip_main and entry["subrole"] == "AXStandardWindow":
            continue
        remaining = deadline - time.time()
        if remaining <= 6:
            skipped.append({**entry, "reason": "sweep deadline reached before this window"})
            continue
        size = probe_window_size(process, entry["index"], seconds=min(8, int(remaining)))
        if not size.get("readable"):
            skipped.append({**entry, "reason": f"size probe failed: {size.get('reason')}"})
            continue
        if size["shallow_count"] > int(shallow_limit):
            skipped.append(
                {
                    **entry,
                    "shallow_count": size["shallow_count"],
                    "reason": f"shallow element count {size['shallow_count']} exceeds "
                    f"{shallow_limit}; this editor draws itself and is not worth walking",
                }
            )
            continue
        remaining = deadline - time.time()
        allowance = int(min(max(5, remaining - 3), int(per_window_seconds)))
        try:
            snapshot = plugin_snapshot(entry["index"], max_depth, budget=1200, seconds=allowance)
        except ProbeError as exc:
            skipped.append({**entry, "reason": str(exc)})
            continue
        snapshot["title"] = entry["title"]
        snapshot["shallow_count"] = size["shallow_count"]
        results.append(snapshot)
    return {
        "windows_examined": len(results),
        "windows_with_parameters": sum(1 for r in results if r.get("parameter_count")),
        "windows_skipped": len(skipped),
        "total_parameters": sum(r.get("parameter_count", 0) for r in results),
        "seconds_used": round(time.time() - (deadline - max(10, min(int(total_seconds), 600))), 1),
        "plugins": results,
        "skipped": skipped,
    }


@tool
def plugin_write_path(
    path: str,
    value: str,
    window_index: int = 1,
    dry_run: bool = True,
    allow_stepping: bool = True,
    max_steps: int = 64,
) -> dict:
    """Write one control addressed by the dotted path from plugin_snapshot, and verify by
    reading it back. An absolute set is attempted first; measured behaviour on Logic sliders
    is that it is ignored and the control instead moves one step toward the requested value
    per call, so a directed stepping loop follows. Stepping stops as soon as the target is
    reached, progress stalls, or the value moves away from the target. Runs as a dry run
    unless dry_run is set to false, and never reports success on an unverified write."""
    process = require_logic()
    if not all(part.strip().isdigit() for part in path.split(".") if part.strip()):
        return {"ok": False, "error": f"path must be dotted integers, got {path!r}"}
    if '"' in value:
        return {"ok": False, "error": "double quotes are not accepted in value"}
    reference = element_reference(path, int(window_index))
    try:
        role, current = read_role_and_value(process, reference)
    except ProbeError as exc:
        return {"ok": False, "error": f"path does not resolve: {exc}"}
    if role not in WRITABLE_ROLES and role not in NUMERIC_ROLES:
        return {
            "ok": False,
            "error": f"role {role} is not writable",
            "path": path,
            "current_value": current,
        }
    target_number = as_number(value)
    numeric = target_number is not None and role in NUMERIC_ROLES
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "path": path,
            "role": role,
            "current_value": current,
            "requested_value": value,
            "write_mode": "absolute number" if numeric else "string",
            "note": "nothing was changed. Re-run with dry_run false to apply",
        }
    try:
        send_value(process, reference, value, numeric)
        observed = read_value(process, reference)
    except ProbeError as exc:
        return {"ok": False, "verified": False, "error": str(exc), "before": current}
    if values_match(value, observed):
        return {
            "ok": True,
            "verified": True,
            "method": "absolute",
            "path": path,
            "before": current,
            "requested": value,
            "after": observed,
            "steps": 1,
        }
    if not (allow_stepping and numeric):
        return {
            "ok": False,
            "verified": False,
            "method": "absolute",
            "path": path,
            "before": current,
            "requested": value,
            "after": observed,
            "note": "value read back differs, treat as not applied",
        }
    steps = 1
    trail = [current, observed]
    reason = "step budget exhausted"
    while steps < int(max_steps):
        previous = observed
        try:
            send_value(process, reference, value, numeric)
            observed = read_value(process, reference)
        except ProbeError as exc:
            reason = f"stepping failed: {exc}"
            break
        steps += 1
        trail.append(observed)
        if values_match(value, observed):
            return {
                "ok": True,
                "verified": True,
                "method": "stepped",
                "path": path,
                "before": current,
                "requested": value,
                "after": observed,
                "steps": steps,
                "trail": trail[:12],
            }
        previous_number, observed_number = as_number(previous), as_number(observed)
        if previous_number is None or observed_number is None:
            reason = "value stopped being numeric"
            break
        if observed_number == previous_number:
            reason = "no further movement, control is at its limit or ignores writes"
            break
        if abs(observed_number - target_number) >= abs(previous_number - target_number):
            reason = "value started moving away from the target"
            break
    return {
        "ok": False,
        "verified": False,
        "method": "stepped",
        "path": path,
        "before": current,
        "requested": value,
        "after": observed,
        "steps": steps,
        "trail": trail[:12],
        "note": reason,
    }


@tool
def plugin_write(
    parameter: str,
    value: str,
    window_index: int = 1,
    dry_run: bool = True,
) -> dict:
    """Write one plugin parameter located by its accessibility name. Refuses when the name
    is ambiguous and reports the competing paths so plugin_write_path can be used instead.
    Runs as a dry run unless dry_run is set to false, and verifies every write by read-back."""
    process = require_logic()
    try:
        target = find_control(parameter, int(window_index), process)
    except ProbeError as exc:
        return {"ok": False, "error": str(exc)}
    return plugin_write_path(target["path"], value, window_index, dry_run)


@tool
def plugin_plan(edits: list, note: str = "") -> dict:
    """Register a batch of parameter edits as a named plan without touching Logic.
    Each edit is an object with parameter, value and optional window_index. Returns a
    plan id and the full list for review. Nothing is applied until plugin_apply is
    called with that id and confirm set to true."""
    if not isinstance(edits, list) or not edits:
        return {"error": "edits must be a non-empty list"}
    normalised = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {"error": f"edit {index} is not an object"}
        if "value" not in edit or ("parameter" not in edit and "path" not in edit):
            return {"error": f"edit {index} needs value plus either parameter or path"}
        normalised.append(
            {
                "parameter": str(edit.get("parameter", "")),
                "path": str(edit["path"]) if edit.get("path") else "",
                "value": str(edit["value"]),
                "window_index": int(edit.get("window_index", 1)),
            }
        )
    plan_id = f"plan-{len(PLANS) + 1}"
    PLANS[plan_id] = {"edits": normalised, "note": note, "applied": False}
    return {
        "plan_id": plan_id,
        "count": len(normalised),
        "edits": normalised,
        "note": note,
        "next_step": (
            "review the list, save the project first, then call plugin_apply with "
            "this plan_id and confirm true"
        ),
    }


@tool
def plugin_apply(plan_id: str, confirm: bool = False, stop_on_failure: bool = True) -> dict:
    """Apply a plan created by plugin_plan. Refuses to run unless confirm is true.
    Each edit is written and verified by read-back; by default the run stops at the first
    edit that cannot be verified, so a partial failure does not cascade."""
    plan = PLANS.get(plan_id)
    if plan is None:
        return {"error": f"unknown plan_id {plan_id!r}", "known": sorted(PLANS)}
    if plan["applied"]:
        return {"error": f"plan {plan_id} was already applied"}
    if not confirm:
        return {
            "confirmation_required": True,
            "plan_id": plan_id,
            "count": len(plan["edits"]),
            "edits": plan["edits"],
            "reason": "re-call with confirm true to write these values into Logic",
        }
    results = []
    for edit in plan["edits"]:
        if edit.get("path"):
            outcome = plugin_write_path(
                edit["path"], edit["value"], edit["window_index"], dry_run=False
            )
        else:
            outcome = plugin_write(
                edit["parameter"], edit["value"], edit["window_index"], dry_run=False
            )
        results.append({**edit, "result": outcome})
        if stop_on_failure and not outcome.get("ok"):
            break
    plan["applied"] = True
    verified = sum(1 for r in results if r["result"].get("verified"))
    return {
        "plan_id": plan_id,
        "attempted": len(results),
        "verified": verified,
        "failed": len(results) - verified,
        "stopped_early": stop_on_failure and len(results) < len(plan["edits"]),
        "results": results,
    }


KEY_COMMANDS = {
    "play_stop": ("code", 49, []),
    "go_to_start": ("code", 36, []),
    "select_previous_track": ("code", 126, []),
    "select_next_track": ("code", 125, []),
    "cycle": ("key", "c", []),
    "solo_selected": ("key", "s", []),
    "mute_selected": ("key", "m", []),
    "toggle_mixer": ("key", "x", []),
    "toggle_inspector": ("key", "i", []),
    "toggle_loops": ("key", "o", []),
    "zoom_to_fit": ("key", "z", []),
}

MODIFIER_NAMES = {
    "cmd": "command down",
    "shift": "shift down",
    "option": "option down",
    "control": "control down",
}


def focus_main(process: str) -> None:
    osa(
        f'tell application "{process}" to activate\n'
        'delay 0.15\n'
        f'tell application "System Events" to tell process "{process}"\n'
        "try\n"
        'perform action "AXRaise" of (first window whose subrole is "AXStandardWindow")\n'
        "end try\n"
        "end tell"
    )


def send_key(process: str, kind: str, key, modifiers: list[str]) -> None:
    using = ""
    if modifiers:
        names = ", ".join(MODIFIER_NAMES[m] for m in modifiers)
        using = f" using {{{names}}}"
    action = f"key code {key}{using}" if kind == "code" else f'keystroke "{key}"{using}'
    osa(
        f'tell application "System Events" to tell process "{process}" to {action}',
        timeout=30,
    )


def read_role_and_value(process: str, reference: str) -> tuple[str, str]:
    raw = osa(
        f'tell application "System Events" to tell process "{process}" to '
        f'return (role of {reference} as string) & "~" & (value of {reference} as string)'
    )
    role, _, value = raw.partition("~")
    return role, value


def read_value(process: str, reference: str) -> str:
    return osa(
        f'tell application "System Events" to tell process "{process}" to '
        f"return value of {reference} as string"
    )


def send_value(process: str, reference: str, value: str, numeric: bool) -> None:
    literal = value if numeric else f'"{value}"'
    osa(
        f'tell application "System Events" to tell process "{process}" to '
        f"set value of {reference} to {literal}",
        timeout=60,
    )


def probe_window_size(process: str, window: int, seconds: int = 8) -> dict:
    script = (
        f'tell application "System Events" to tell process "{process}"\n'
        f"set target to window {int(window)}\n"
        "set total to 0\n"
        "set kids to 0\n"
        "try\n"
        "set kids to count of UI elements of target\n"
        "end try\n"
        "set total to kids\n"
        "repeat with i from 1 to kids\n"
        "try\n"
        "set total to total + (count of UI elements of (UI element i of target))\n"
        "end try\n"
        "end repeat\n"
        "return (total as string)\n"
        "end tell"
    )
    try:
        raw = osa(script, timeout=seconds)
    except ProbeError as exc:
        return {"readable": False, "reason": str(exc)}
    try:
        return {"readable": True, "shallow_count": int(float(raw.replace(",", ".")))}
    except ValueError:
        return {"readable": False, "reason": f"unexpected count {raw!r}"}


@tool
def ax_subtree(
    window_index: int = 1,
    path: str = "",
    max_depth: int = 3,
    limit: int = 200,
) -> dict:
    """Walk one branch of a window's accessibility tree instead of the whole window. The
    main Logic window holds thousands of elements, so drilling into a named group by path
    is the only practical way to reach the mixer strips or any other pane."""
    name = require_logic()
    if path and not all(p.strip().isdigit() for p in path.split(".") if p.strip()):
        return {"error": f"path must be dotted integers, got {path!r}"}
    try:
        elements = walk_window(
            name, int(window_index), int(max_depth), budget=1200, seconds=45, root=path
        )
    except ProbeError as exc:
        return {
            "window_index": window_index,
            "root": path or "window",
            "error": str(exc),
            "advice": "branches such as the Tracks area and the Mixer hold tens of "
            "thousands of elements. Accessibility cannot enumerate them at this scale",
        }
    roles: dict[str, int] = {}
    for entry in elements:
        roles[entry["role"]] = roles.get(entry["role"], 0) + 1
    return {
        "window_index": window_index,
        "root": path or "window",
        "max_depth": max_depth,
        "element_count": len(elements),
        "role_histogram": dict(sorted(roles.items(), key=lambda kv: -kv[1])),
        "elements": elements[:limit],
        "truncated": len(elements) > limit,
    }


def find_control_bar(process: str, window: int) -> dict:
    elements = walk_window(process, window, 2, root="6")
    index: dict[str, dict] = {}
    for entry in elements:
        label = entry["name"] or entry["description"]
        if label and label not in index:
            index[label] = entry
    return index


@tool
def control_bar(window_index: int = 1) -> dict:
    """Read Logic's Control Bar: whether transport is playing or recording, cycle, solo and
    metronome state, live tempo, time and key signature, playhead position and master
    volume. Unlike a key command this reports actual state rather than assuming a toggle."""
    process = require_logic()
    index = find_control_bar(process, int(window_index))
    def value_of(label):
        entry = index.get(label)
        return entry["value"] if entry else None
    def path_of(label):
        entry = index.get(label)
        return entry["path"] if entry else None
    return {
        "playing": value_of("Play") == "1",
        "recording": value_of("Record") == "1",
        "cycle": value_of("Cycle") == "1",
        "solo": value_of("Solo") == "1",
        "metronome": value_of("Metronome Click") == "1",
        "tempo": value_of("Tempo"),
        "time_signature": value_of("Time Signature"),
        "key_signature": value_of("Key Signature"),
        "master_volume": value_of("Master Volume"),
        "mixer_open": value_of("Mixer") == "1",
        "paths": {k: path_of(k) for k in ("Play", "Record", "Cycle", "Solo", "Go to Beginning", "Metronome Click", "Master Volume", "Mixer")},
        "available": sorted(index),
    }


@tool
def transport_press(button: str, window_index: int = 1, verify: bool = True) -> dict:
    """Press a Control Bar button by name and report the state before and after. This is
    the reliable transport path: it targets the actual button rather than sending a
    keystroke that could land in the wrong place, and it can confirm what happened.
    Names come from control_bar, for example Play, Record, Cycle, Solo, Go to Beginning."""
    process = require_logic()
    index = find_control_bar(process, int(window_index))
    entry = index.get(button)
    if entry is None:
        return {"ok": False, "error": f"no Control Bar element named {button!r}", "available": sorted(index)}
    reference = element_reference(entry["path"], int(window_index))
    before = entry["value"]
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {reference}',
            timeout=30,
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"press failed: {exc}", "path": entry["path"]}
    if not verify:
        return {"ok": True, "button": button, "path": entry["path"], "before": before}
    time.sleep(0.4)
    try:
        after = read_value(process, reference)
    except ProbeError as exc:
        return {"ok": True, "button": button, "path": entry["path"], "before": before, "after_error": str(exc)}
    return {
        "ok": True,
        "button": button,
        "path": entry["path"],
        "before": before,
        "after": after,
        "changed": before != after,
    }


@tool
def keys_list() -> dict:
    """List the key commands this server is allowed to send to Logic. The list is an
    allowlist on purpose: keystrokes land wherever focus is, so destructive commands are
    not exposed."""
    return {
        "commands": sorted(KEY_COMMANDS),
        "note": "these are Logic's factory key commands. If you have remapped them, the "
        "effect will differ and should be checked with a harmless one first",
    }


@tool
def keys_send(command: str, repeat: int = 1) -> dict:
    """Send one allowlisted key command to Logic, raising the main window first so the
    keystroke does not land in a plugin editor or a text field. Use keys_list to see what
    is available."""
    if command not in KEY_COMMANDS:
        return {"ok": False, "error": f"unknown command, expected one of {sorted(KEY_COMMANDS)}"}
    process = require_logic()
    kind, key, modifiers = KEY_COMMANDS[command]
    focus_main(process)
    count = max(1, min(int(repeat), 64))
    for _ in range(count):
        send_key(process, kind, key, modifiers)
        time.sleep(0.08)
    return {"ok": True, "command": command, "repeat": count}


@tool
def transport(action: str) -> dict:
    """Drive Logic's transport. Actions: play_stop toggles playback, stop returns to the
    start, go_to_start moves the playhead to bar one, cycle toggles the cycle region.
    Playback state is not readable from here, so a toggle is a toggle."""
    mapping = {
        "play_stop": ["play_stop"],
        "stop": ["play_stop"],
        "go_to_start": ["go_to_start"],
        "restart": ["go_to_start", "play_stop"],
        "cycle": ["cycle"],
    }
    if action not in mapping:
        return {"ok": False, "error": f"unknown action, expected one of {sorted(mapping)}"}
    for step in mapping[action]:
        result = keys_send(step)
        if not result.get("ok"):
            return result
        time.sleep(0.15)
    return {"ok": True, "action": action, "sent": mapping[action]}


@tool
def track_navigate(direction: str = "next", count: int = 1) -> dict:
    """Move the track selection in Logic's Tracks area. Direction is next or previous.
    Selection is what solo_selected and mute_selected act on."""
    if direction not in ("next", "previous"):
        return {"ok": False, "error": "direction must be next or previous"}
    return keys_send(
        "select_next_track" if direction == "next" else "select_previous_track", count
    )


@tool
def meter_watch(
    window_index: int,
    paths: list,
    seconds: float = 8.0,
    interval: float = 0.5,
) -> dict:
    """Poll named control paths in an open plugin window over a period of time and report
    the series plus the minimum, maximum and final value of each. Use it while transport is
    running to capture meter readings, which are only populated when audio flows."""
    process = require_logic()
    if not paths:
        return {"ok": False, "error": "give at least one path from plugin_snapshot"}
    references = {}
    for path in paths:
        text = str(path)
        if not all(part.strip().isdigit() for part in text.split(".") if part.strip()):
            return {"ok": False, "error": f"path must be dotted integers, got {text!r}"}
        references[text] = element_reference(text, int(window_index))
    series: dict[str, list] = {path: [] for path in references}
    deadline = time.time() + max(0.5, min(float(seconds), 120.0))
    tick = max(0.1, float(interval))
    samples = 0
    while time.time() < deadline:
        for path, reference in references.items():
            try:
                series[path].append(read_value(process, reference))
            except ProbeError:
                series[path].append("")
        samples += 1
        time.sleep(tick)
    summary = {}
    for path, values in series.items():
        numbers = [n for n in (as_number(v) for v in values) if n is not None]
        summary[path] = {
            "samples": len(values),
            "final": values[-1] if values else "",
            "min": f"{min(numbers):g}" if numbers else "",
            "max": f"{max(numbers):g}" if numbers else "",
            "series": values[:40],
        }
    return {"window_index": window_index, "samples": samples, "meters": summary}


@tool
def window_find(title_contains: str) -> dict:
    """Find open Logic windows whose title contains the given text, returning their index
    for ax_dump and plugin_snapshot."""
    listing = ax_windows()
    needle = title_contains.lower()
    matches = [w for w in listing["windows"] if needle in w["title"].lower()]
    return {"query": title_contains, "matches": matches, "total_windows": listing["count"]}


@tool
def health() -> dict:
    """Report what this server can currently do and which capability is blocking the
    rest."""
    name = logic_process()
    accessibility = False
    if name:
        try:
            osa(f'tell application "System Events" to tell process "{name}" to return name')
            accessibility = True
        except ProbeError:
            accessibility = False
    return {
        "platform": sys.platform,
        "logic_process": name,
        "accessibility": accessibility,
        "auval": shutil.which("auval") or "not found",
        "settings_roots_present": [str(r) for r in SETTINGS_ROOTS if r.is_dir()],
        "tools_defined": len(TOOLS),
        "plans_held": sorted(PLANS),
        "unvalidated": (
            "the accessibility read and write paths have never been run against a real "
            "Logic plugin window. Run plugins_probe then ax_dump before trusting them"
        ),
    }


def build_server(name: str):
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP(name)
    except ImportError:
        pass
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name)
    except ImportError as exc:
        raise RuntimeError(
            "no supported MCP server class found. Install the SDK with 'pip install mcp'."
        ) from exc


def build():
    server = build_server("logic-plugins")
    for fn in TOOLS:
        server.tool()(fn)
    return server


def check_names() -> list[str]:
    import ast

    source = pathlib.Path(__file__).read_text()
    tree = ast.parse(source)
    defined = set(dir(__builtins__)) | {"__file__", "__name__", "__builtins__"}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
    missing = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        local = {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                local.add(inner.id)
            elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local.add(inner.name)
            elif isinstance(inner, ast.comprehension) and isinstance(inner.target, ast.Name):
                local.add(inner.target.id)
            elif isinstance(inner, (ast.Import, ast.ImportFrom)):
                for alias in inner.names:
                    local.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(inner, ast.ExceptHandler) and inner.name:
                local.add(inner.name)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                if inner.id not in defined and inner.id not in local:
                    missing.append(f"{node.name}: {inner.id} (line {inner.lineno})")
    return sorted(set(missing))


def selfcheck() -> int:
    rows = []
    undefined = check_names()
    rows.append(
        ("ok", "name resolution", "no undefined references")
        if not undefined
        else ("FAIL", "name resolution", "; ".join(undefined[:4]))
    )
    rows.append(("ok" if sys.platform == "darwin" else "FAIL", "platform", sys.platform))
    rows.append(("ok", "python", sys.version.split()[0]))
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: F401

        rows.append(("ok", "mcp sdk", "v2_mcpserver"))
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401

            rows.append(("ok", "mcp sdk", "v1_fastmcp"))
        except ImportError:
            rows.append(("FAIL", "mcp sdk", "NOT installed: <venv>/bin/pip install mcp"))
    rows.append(
        ("ok" if shutil.which("osascript") else "FAIL", "osascript", shutil.which("osascript") or "missing")
    )
    rows.append(("ok" if shutil.which("auval") else "warn", "auval", shutil.which("auval") or "missing"))
    name = logic_process()
    rows.append(("ok" if name else "warn", "logic", name or "not running"))
    for root in SETTINGS_ROOTS:
        rows.append(("ok" if root.is_dir() else "warn", root.name, str(root) if root.is_dir() else "absent"))
    rows.append(("ok", "tools defined", str(len(TOOLS))))
    width = max(len(n) for _, n, _ in rows)
    print()
    for status, label, detail in rows:
        print(f"{status:>4}  {label:<{width}}  {detail}")
    failures = sum(1 for s, _, _ in rows if s == "FAIL")
    print(f"\n{failures} failed, {sum(1 for s, _, _ in rows if s == 'warn')} warnings\n")
    return 1 if failures else 0


def main() -> None:
    if "--check" in sys.argv:
        raise SystemExit(selfcheck())
    build().run()


if __name__ == "__main__":
    main()
