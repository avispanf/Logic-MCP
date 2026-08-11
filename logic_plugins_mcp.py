from __future__ import annotations

import ctypes
import json
import math
import os
import pathlib
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import logic_mix_audit as mix_audit

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
AUDIO_UNIT_TYPES = {
    "auou",  # output
    "aumu",  # instrument
    "aumf",  # music effect
    "aufc",  # format converter
    "aufx",  # effect
    "aumx",  # mixer
    "aupn",  # panner
    "auol",  # offline effect
    "augn",  # generator
    "aumi",  # MIDI processor
    "ausp",  # speech synthesizer
}
MATURE_DISPATCH_CONTRACT_VERSION = "3.13.0"


class AudioComponentDescription(ctypes.Structure):
    _fields_ = (
        ("component_type", ctypes.c_uint32),
        ("component_subtype", ctypes.c_uint32),
        ("component_manufacturer", ctypes.c_uint32),
        ("component_flags", ctypes.c_uint32),
        ("component_flags_mask", ctypes.c_uint32),
    )


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
AUDIT_PLANS: dict[str, dict] = {}


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


def logic_pro_mcp_version() -> str | None:
    binary = shutil.which("LogicProMCP")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = (result.stdout or result.stderr).strip().splitlines()
    return version[-1].strip() if result.returncode == 0 and version else None


def require_logic() -> str:
    name = logic_process()
    if name is None:
        raise ProbeError("Logic is not running")
    return name


def accessibility_status(process: str | None = None) -> dict:
    """Test the first AX operation the tools actually need, rather than a process-name
    lookup that System Events may allow even when assistive access is denied."""
    name = process or logic_process()
    if name is None:
        return {"granted": False, "reason": "Logic is not running"}
    try:
        raw = osa(
            f'tell application "System Events" to tell process "{name}" to '
            "return count of windows"
        )
        return {"granted": True, "window_count": int(raw or 0)}
    except (ProbeError, ValueError) as exc:
        return {"granted": False, "reason": str(exc)}


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
    report["accessibility"] = accessibility_status(name)
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


def fourcc(value: int) -> str:
    return int(value).to_bytes(4, "big").decode("mac_roman", errors="replace")


def audio_component_units() -> list[dict]:
    """Read the macOS AudioComponent registry without asking auval to scan plugins."""
    if sys.platform != "darwin":
        raise ProbeError("the AudioComponent registry is only available on macOS")

    audio_toolbox = ctypes.CDLL(
        "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    audio_toolbox.AudioComponentFindNext.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(AudioComponentDescription),
    )
    audio_toolbox.AudioComponentFindNext.restype = ctypes.c_void_p
    audio_toolbox.AudioComponentGetDescription.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(AudioComponentDescription),
    )
    audio_toolbox.AudioComponentGetDescription.restype = ctypes.c_int32
    audio_toolbox.AudioComponentCopyName.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    audio_toolbox.AudioComponentCopyName.restype = ctypes.c_int32
    core_foundation.CFStringGetCString.argtypes = (
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    )
    core_foundation.CFStringGetCString.restype = ctypes.c_bool
    core_foundation.CFRelease.argtypes = (ctypes.c_void_p,)
    core_foundation.CFRelease.restype = None

    def component_name(component) -> str:
        ref = ctypes.c_void_p()
        status = audio_toolbox.AudioComponentCopyName(component, ctypes.byref(ref))
        if status != 0 or not ref.value:
            return ""
        try:
            buffer = ctypes.create_string_buffer(4096)
            converted = core_foundation.CFStringGetCString(
                ref, buffer, len(buffer), 0x08000100  # kCFStringEncodingUTF8
            )
            return buffer.value.decode("utf-8", errors="replace") if converted else ""
        finally:
            core_foundation.CFRelease(ref)

    query = AudioComponentDescription(0, 0, 0, 0, 0)
    component = ctypes.c_void_p()
    units = []
    while True:
        component = ctypes.c_void_p(
            audio_toolbox.AudioComponentFindNext(component, ctypes.byref(query))
        )
        if not component.value:
            break
        description = AudioComponentDescription()
        if audio_toolbox.AudioComponentGetDescription(
            component, ctypes.byref(description)
        ) != 0:
            continue
        kind = fourcc(description.component_type)
        if kind not in AUDIO_UNIT_TYPES:
            continue
        full_name = component_name(component).strip()
        vendor, separator, title = full_name.partition(":")
        units.append(
            {
                "type": kind,
                "subtype": fourcc(description.component_subtype),
                "manufacturer": fourcc(description.component_manufacturer),
                "vendor": vendor.strip()
                if separator
                else fourcc(description.component_manufacturer),
                "name": title.strip() if separator else full_name,
            }
        )
    return units


@tool
def au_list(filter_text: str | None = None, limit: int = 200) -> dict:
    """List installed Audio Units with the four-character codes that au_parameters needs.
    This reads the system AudioComponent registry directly; unlike `auval -a`, it does not
    launch or validate hundreds of plugins and therefore does not hang on a large setup."""
    try:
        units = audio_component_units()
    except (OSError, ProbeError) as exc:
        return {"error": f"AudioComponent registry unavailable: {exc}"}
    needle = (filter_text or "").lower()
    if needle:
        units = [unit for unit in units if needle in json.dumps(unit).lower()]
    units.sort(key=lambda unit: (unit["vendor"].lower(), unit["name"].lower()))
    capped = max(1, min(int(limit), 2000))
    return {
        "count": len(units),
        "returned": min(len(units), capped),
        "source": "macOS AudioComponent registry",
        "units": units[:capped],
    }


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
    if normalise_number(requested) == normalise_number(observed):
        return True

    def number_and_unit(value):
        match = re.fullmatch(
            r"\s*([-+]?\d+(?:[.,]\d+)?)\s*([^\d]*)\s*",
            str(value),
        )
        if not match:
            return None
        return float(match.group(1).replace(",", ".")), match.group(2).strip().casefold()

    left, right = number_and_unit(requested), number_and_unit(observed)
    if left is None or right is None:
        return False
    units_compatible = not left[1] or not right[1] or left[1] == right[1]
    return units_compatible and left[0] == right[0]


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


def read_plugin_identity(process: str, window_index: int) -> dict:
    shallow = walk_window(process, int(window_index), 2, budget=400, seconds=20)
    if not shallow:
        raise ProbeError("no elements returned from the plugin window")
    view_selector = next(
        (
            e["name"] or e["value"]
            for e in shallow
            if e["role"] == "AXMenuButton" and e["description"] == "view"
        ),
        "",
    )
    bypass = next(
        (
            e["value"]
            for e in shallow
            if e["description"] == "bypass" and "." not in e["path"]
        ),
        None,
    )
    titles = [
        e["value"].strip()
        for e in shallow
        if e["role"] == "AXStaticText"
        and "." not in e["path"]
        and e["value"]
        and not e["value"].strip().endswith(":")
    ]
    return {
        "window_index": int(window_index),
        "plugin": titles[0] if titles else "",
        "channel": titles[-1] if len(titles) > 1 else "",
        "view_selector": view_selector,
        "controls_view": view_selector == "Controls",
        "bypass_control": bypass,
    }


def identity_matches(identity: dict, expected_plugin: str = "", expected_channel: str = "") -> bool:
    def same(left, right):
        return str(left or "").strip().casefold() == str(right or "").strip().casefold()

    return (not expected_plugin or same(identity.get("plugin"), expected_plugin)) and (
        not expected_channel or same(identity.get("channel"), expected_channel)
    )


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
    """Read one open plugin editor: which plugin it is, which channel it belongs to, which
    view it is showing, whether it is bypassed, and, when the view is Controls, every
    parameter paired with its value and an exact path. The window is first read two levels
    deep, which is where the identity lives; the deep walk that collects parameters only
    runs when Controls view is active, because a plugin drawing its own interface exposes
    thousands of elements and would otherwise time out."""
    name = require_logic()
    try:
        identity = read_plugin_identity(name, int(window_index))
    except ProbeError as exc:
        return {"window_index": window_index, "error": str(exc)}
    result = {
        **identity,
        "view_mode": identity["view_selector"],
        "parameter_count": 0,
        "parameters": [],
    }
    if not identity["controls_view"]:
        result["note"] = (
            "the plugin is drawing its own interface, so parameters are not exposed. "
            "Switch the window's View menu to Controls to read them"
        )
        return result
    try:
        elements = walk_window(
            name, int(window_index), int(max_depth), budget=int(budget), seconds=int(seconds)
        )
    except ProbeError as exc:
        result["note"] = f"identity read, but the parameter walk failed: {exc}"
        return result
    parameters = pair_parameters(elements)
    result["element_count"] = len(elements)
    result["parameter_count"] = len(parameters)
    result["parameters"] = parameters
    return result


@tool
def plugin_read(window_index: int = 1) -> dict:
    """Compatibility wrapper around plugin_snapshot."""
    return plugin_snapshot(window_index)


def find_parameter_table(process: str, window: int) -> str:
    elements = walk_window(process, window, 2, budget=200, seconds=20)
    for entry in elements:
        if entry["role"] == "AXTable":
            return entry["path"]
    return ""


@tool
def plugin_parameters(
    window_index: int = 1,
    contains: str = "",
    offset: int = 0,
    limit: int = 60,
    seconds: int = 120,
    expected_plugin: str = "",
    expected_channel: str = "",
) -> dict:
    """Read the parameter table of a plugin editor that is showing Controls view, walking
    the table row by row instead of recursing through the whole window. A large plugin such
    as a mastering suite exposes over a hundred parameters, and a recursive walk of that
    tree does not finish; this reads each row directly. Use contains to filter by label,
    and offset with limit to page through a long list."""
    process = require_logic()
    if expected_plugin or expected_channel:
        try:
            identity = read_plugin_identity(process, int(window_index))
        except ProbeError as exc:
            return {"window_index": window_index, "error": str(exc)}
        if not identity_matches(identity, expected_plugin, expected_channel):
            return {
                "window_index": window_index,
                "error": "plugin identity changed; refusing to read a stale window",
                "expected": {"plugin": expected_plugin, "channel": expected_channel},
                "observed": identity,
            }
    table = find_parameter_table(process, int(window_index))
    if not table:
        return {
            "window_index": window_index,
            "error": "no parameter table in this window",
            "advice": "switch the window's View menu to Controls first",
        }
    page_offset = max(0, int(offset))
    page_limit = max(1, min(int(limit), 500))
    needle = contains.lower()
    reference = element_reference(table, int(window_index))
    start_row = 1 if needle else page_offset + 1
    end_row = "n" if needle else str(page_offset + page_limit)
    raw = osa(
        f'tell application "System Events" to tell process "{process}"\n'
        f"set t to {reference}\n"
        "set n to 0\n"
        "try\n"
        "set n to count of rows of t\n"
        "end try\n"
        'set out to ""\n'
        f"set startRow to {start_row}\n"
        f"set endRow to {end_row}\n"
        "if endRow > n then set endRow to n\n"
        "repeat with i from startRow to endRow\n"
        'set lbl to ""\n'
        'set disp to ""\n'
        'set rol to ""\n'
        "try\n"
        "set c to UI element 1 of row i of t\n"
        "try\n"
        "set lbl to (value of static text 1 of c) as string\n"
        "end try\n"
        "try\n"
        "set disp to (value of UI element 2 of c) as string\n"
        "end try\n"
        "try\n"
        "set rol to (role of UI element 2 of c) as string\n"
        "end try\n"
        "end try\n"
        'set out to out & (i as string) & "~" & lbl & "~" & disp & "~" & rol & "|:|"\n'
        "end repeat\n"
        'return (n as string) & "#" & out\n'
        "end tell",
        timeout=int(seconds),
    )
    total, _, body = raw.partition("#")
    try:
        row_count = int(float(total.replace(",", ".")))
    except ValueError:
        row_count = 0
    parameters = []
    for record in split_records(body):
        parts = (record.split("~") + ["", "", "", ""])[:4]
        label = clean(parts[1]).rstrip(":").strip()
        display = clean(parts[2])
        if needle and needle not in label.lower():
            continue
        parameters.append(
            {
                "row": int(parts[0]) if parts[0].isdigit() else None,
                "label": label,
                "display": display,
                "role": parts[3],
                "path": f"{table}.{parts[0]}.1.2" if parts[0].isdigit() else "",
            }
        )
    matched_total = len(parameters) if needle else row_count
    if needle:
        parameters = parameters[page_offset : page_offset + page_limit]
    return {
        "window_index": window_index,
        "table_path": table,
        "rows_total": row_count,
        "offset": page_offset,
        "returned": len(parameters),
        "matched_total": matched_total,
        "next_offset": page_offset + page_limit
        if page_offset + page_limit < matched_total
        else None,
        "filter": contains or None,
        "parameters": parameters,
    }


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
def ax_show_menu(path: str, window_index: int = 1, settle: float = 0.8) -> dict:
    """Open an element's context menu and list what it offers. Insert slots, faders and
    sends all carry context menus that expose operations no other channel reaches, so this
    is how a capability is discovered rather than assumed. Nothing is clicked; use ax_press
    on the returned menu item path to act."""
    process = require_logic()
    if not all(part.strip().isdigit() for part in path.split(".") if part.strip()):
        return {"ok": False, "error": f"path must be dotted integers, got {path!r}"}
    reference = element_reference(path, int(window_index))
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXShowMenu" of {reference}',
            timeout=30,
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"could not open a menu here: {exc}", "path": path}
    time.sleep(max(0.2, min(float(settle), 5.0)))
    try:
        elements = walk_window(process, int(window_index), 2, budget=400, seconds=25, root=path)
    except ProbeError as exc:
        return {"ok": False, "error": f"menu opened but could not be read: {exc}"}
    items = [
        {"path": e["path"], "title": e["name"] or e["value"], "enabled": e["value"] != "0"}
        for e in elements
        if e["role"] == "AXMenuItem"
    ]
    return {
        "ok": True,
        "path": path,
        "menu_items": items,
        "count": len(items),
        "note": "press Escape or click elsewhere to dismiss; ax_press on an item path acts on it",
    }


@tool
def plugin_set_view(
    window_index: int = 1,
    view: str = "Controls",
    expected_plugin: str = "",
    expected_channel: str = "",
) -> dict:
    """Switch a plugin editor between its own interface and Logic's generic Controls list.
    Controls view is what makes any Audio Unit readable and writable, including third-party
    plugins that draw themselves, so this is the gate to every parameter operation. The
    change is verified by reading the view menu back."""
    process = require_logic()
    if expected_plugin or expected_channel:
        try:
            identity = read_plugin_identity(process, int(window_index))
        except ProbeError as exc:
            return {"ok": False, "error": str(exc)}
        if not identity_matches(identity, expected_plugin, expected_channel):
            return {
                "ok": False,
                "write_attempted": False,
                "error": "plugin identity changed; refusing to operate on a stale window",
                "expected": {"plugin": expected_plugin, "channel": expected_channel},
                "observed": identity,
            }
    try:
        shallow = walk_window(process, int(window_index), 1, budget=200, seconds=20)
    except ProbeError as exc:
        return {"ok": False, "error": str(exc)}
    menu = next(
        (e for e in shallow if e["role"] == "AXMenuButton" and e["description"] == "view"),
        None,
    )
    if menu is None:
        return {"ok": False, "error": "this window has no view menu; it may not be a plugin editor"}
    current = menu["name"] or menu["value"]
    if current == view:
        return {
            "ok": True,
            "verified": True,
            "view": current,
            "changed": False,
            "note": "already in that view",
        }
    reference = element_reference(menu["path"], int(window_index))
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {reference}',
            timeout=30,
        )
        time.sleep(0.6)
        items = walk_window(process, int(window_index), 2, budget=200, seconds=20, root=menu["path"])
    except ProbeError as exc:
        return {"ok": False, "error": f"could not open the view menu: {exc}"}
    target = next(
        (e for e in items if e["role"] == "AXMenuItem" and (e["name"] or "").strip() == view),
        None,
    )
    if target is None:
        offered = [e["name"] for e in items if e["role"] == "AXMenuItem" and e["name"]]
        return {"ok": False, "error": f"{view!r} not offered", "available": offered}
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {element_reference(target["path"], int(window_index))}',
            timeout=30,
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"could not select {view!r}: {exc}"}
    time.sleep(1.0)
    try:
        after = walk_window(process, int(window_index), 1, budget=200, seconds=20)
    except ProbeError as exc:
        return {"ok": False, "error": f"selected but could not verify: {exc}"}
    now = next(
        (
            e["name"] or e["value"]
            for e in after
            if e["role"] == "AXMenuButton" and e["description"] == "view"
        ),
        "",
    )
    return {
        "ok": now == view,
        "verified": now == view,
        "view": now,
        "was": current,
        "changed": now != current,
    }


@tool
def close_plugin_windows(keep: int = 0) -> dict:
    """Close plugin editor windows, leaving the project windows alone. Opening editors one
    at a time to identify them leaves a pile of dialogs behind, and every open editor shifts
    the window indices that other tools depend on."""
    process = require_logic()
    closed = 0
    for _ in range(40):
        listing = ax_windows()
        dialogs = [w for w in listing["windows"] if w["subrole"] == "AXDialog"]
        if len(dialogs) <= int(keep):
            break
        target = dialogs[0]
        try:
            osa(
                f'tell application "System Events" to tell process "{process}" to '
                f'perform action "AXPress" of UI element 1 of window {target["index"]}',
                timeout=20,
            )
        except ProbeError:
            break
        closed += 1
        time.sleep(0.3)
    listing = ax_windows()
    return {
        "closed": closed,
        "remaining_dialogs": sum(1 for w in listing["windows"] if w["subrole"] == "AXDialog"),
        "windows": listing["windows"],
    }


@tool
def plugin_close_verified(
    window_index: int = 1,
    expected_plugin: str = "",
    expected_channel: str = "",
    dry_run: bool = True,
) -> dict:
    """Close exactly one editor after verifying its plugin/channel identity. This avoids
    the broad close_plugin_windows cleanup path during an audit and preserves unrelated
    windows that the user had open before the run."""
    process = require_logic()
    if not (expected_plugin or expected_channel):
        return {"ok": False, "error": "expected_plugin or expected_channel is required"}
    try:
        identity = read_plugin_identity(process, int(window_index))
    except ProbeError as exc:
        return {"ok": False, "error": str(exc)}
    if not identity_matches(identity, expected_plugin, expected_channel):
        return {
            "ok": False,
            "write_attempted": False,
            "error": "plugin identity changed; refusing to close a different window",
            "expected": {"plugin": expected_plugin, "channel": expected_channel},
            "observed": identity,
        }
    try:
        top = walk_window(process, int(window_index), 0, budget=80, seconds=10)
    except ProbeError as exc:
        return {"ok": False, "error": f"could not inspect window controls: {exc}"}
    close_button = next(
        (
            entry
            for entry in top
            if entry["role"] == "AXButton"
            and "close" in f'{entry["name"]} {entry["description"]}'.casefold()
        ),
        None,
    )
    if close_button is None:
        return {
            "ok": False,
            "error": "verified plugin window has no readable close button",
            "identity": identity,
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "identity": identity,
            "close_path": close_button["path"],
            "note": "nothing was closed",
        }
    before = ax_windows()
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {element_reference(close_button["path"], int(window_index))}',
            timeout=20,
        )
    except ProbeError as exc:
        return {"ok": False, "identity": identity, "error": str(exc)}
    time.sleep(0.4)
    after = ax_windows()
    closed = after["count"] < before["count"]
    return {
        "ok": closed,
        "verified": closed,
        "identity": identity,
        "windows_before": before["count"],
        "windows_after": after["count"],
        "note": "window count did not decrease" if not closed else "",
    }


@tool
def plugin_write_path(
    path: str,
    value: str,
    window_index: int = 1,
    dry_run: bool = True,
    allow_stepping: bool = True,
    max_steps: int = 64,
    expected_plugin: str = "",
    expected_channel: str = "",
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
    if not dry_run and not (expected_plugin or expected_channel):
        return {
            "ok": False,
            "verified": False,
            "write_attempted": False,
            "error": "a live write requires expected_plugin or expected_channel",
            "reason": "window indices change when editors open or close; bind the write "
            "to identity returned by plugin_snapshot",
        }
    if expected_plugin or expected_channel:
        try:
            identity = read_plugin_identity(process, int(window_index))
        except ProbeError as exc:
            return {
                "ok": False,
                "verified": False,
                "write_attempted": False,
                "error": f"could not verify plugin identity: {exc}",
            }
        if not identity_matches(identity, expected_plugin, expected_channel):
            return {
                "ok": False,
                "verified": False,
                "write_attempted": False,
                "error": "plugin identity changed; refusing the stale path",
                "expected": {
                    "plugin": expected_plugin or None,
                    "channel": expected_channel or None,
                },
                "observed": identity,
            }
    reference = element_reference(path, int(window_index))
    try:
        role, current = read_role_and_value(process, reference)
    except ProbeError as exc:
        return {"ok": False, "error": f"path does not resolve: {exc}"}
    if role == "AXGroup":
        return write_group_display_control(
            process,
            reference,
            path,
            value,
            dry_run=dry_run,
            max_steps=max_steps,
        )
    if role not in WRITABLE_ROLES and role not in NUMERIC_ROLES:
        return {
            "ok": False,
            "error": f"role {role} is not writable",
            "path": path,
            "current_value": current,
        }
    target_number = as_number(value)
    numeric = target_number is not None and role in NUMERIC_ROLES
    toggle = role in ("AXCheckBox", "AXRadioButton")
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "path": path,
            "role": role,
            "current_value": current,
            "requested_value": value,
            "write_mode": "press to toggle"
            if toggle
            else ("absolute number" if numeric else "string"),
            "note": "nothing was changed. Re-run with dry_run false to apply",
        }
    try:
        if toggle:
            for _ in range(3):
                if values_match(value, read_value(process, reference)):
                    break
                osa(
                    f'tell application "System Events" to tell process "{process}" to '
                    f'perform action "AXPress" of {reference}',
                    timeout=30,
                )
                time.sleep(0.35)
            observed = read_value(process, reference)
            return {
                "ok": values_match(value, observed),
                "verified": values_match(value, observed),
                "method": "pressed",
                "path": path,
                "before": current,
                "requested": value,
                "after": observed,
                "note": ""
                if values_match(value, observed)
                else "a checkbox is toggled by pressing it, and it did not reach the "
                "requested state within three presses",
            }
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


def resolve_group_control(process: str, group_reference: str) -> tuple[str, str]:
    """Resolve the single writable child of a Controls-table display group."""
    raw = osa(
        f'tell application "System Events" to tell process "{process}"\n'
        f"set g to {group_reference}\n"
        "set n to count of UI elements of g\n"
        'if n is not 1 then return (n as string) & "~"\n'
        'return (n as string) & "~" & (role of UI element 1 of g as string)\n'
        "end tell",
        timeout=20,
    )
    count, _, role = raw.partition("~")
    if count != "1" or role not in WRITABLE_ROLES + NUMERIC_ROLES:
        raise ProbeError(
            f"parameter display group has {count or 'unknown'} children and role {role!r}; "
            "refusing to guess a writable control"
        )
    return f"UI element 1 of {group_reference}", role


def write_group_display_control(
    process: str,
    group_reference: str,
    path: str,
    value: str,
    *,
    dry_run: bool,
    max_steps: int,
) -> dict:
    """Write a wrapped Controls-table parameter while verifying its display value.

    Third-party Audio Units often expose a parent AXGroup whose value is the musical
    display (-1.00 dB) and a child AXSlider whose raw value is an unrelated encoded
    number (for example 190). Directional AXIncrement/AXDecrement on the child plus
    read-back from the parent is the only safe mapping-free write.
    """
    before = read_value(process, group_reference)
    try:
        control_reference, control_role = resolve_group_control(process, group_reference)
    except ProbeError as exc:
        return {
            "ok": False,
            "verified": False,
            "write_attempted": False,
            "path": path,
            "before": before,
            "requested": value,
            "error": str(exc),
        }
    preview = {
        "path": path,
        "control_path": f"{path}.1",
        "role": "AXGroup",
        "control_role": control_role,
        "before": before,
        "requested": value,
        "verification_source": "parent display value",
    }
    if values_match(value, before):
        return {"ok": True, "verified": True, "changed": False, **preview, "after": before}
    if dry_run:
        return {
            "ok": True,
            "verified": False,
            "dry_run": True,
            "write_attempted": False,
            **preview,
            "write_mode": "display-directed control action",
            "note": "nothing was changed",
        }
    if control_role in ("AXCheckBox", "AXRadioButton"):
        action = "AXPress"
        budget = 3
        control_raw = None
    else:
        current_number, target_number = as_number(before), as_number(value)
        if current_number is None or target_number is None:
            return {
                "ok": False,
                "verified": False,
                "write_attempted": False,
                **preview,
                "error": "display value is not numeric, so a direction cannot be proven",
            }
        action = "AXIncrement" if target_number > current_number else "AXDecrement"
        budget = max(1, min(int(max_steps), 256))
        control_raw = read_value(process, control_reference)
    trail = [before]
    raw_trail = [control_raw] if control_raw is not None else []
    observed = before
    for step in range(1, budget + 1):
        previous = observed
        try:
            osa(
                f'tell application "System Events" to tell process "{process}" to '
                f'perform action "{action}" of {control_reference}',
                timeout=20,
            )
            time.sleep(0.12)
            observed = read_value(process, group_reference)
            observed_raw = (
                read_value(process, control_reference)
                if control_role not in ("AXCheckBox", "AXRadioButton")
                else None
            )
        except ProbeError as exc:
            return {
                "ok": False,
                "verified": False,
                "write_attempted": True,
                **preview,
                "after": observed,
                "steps": step,
                "trail": trail[:16],
                "error": str(exc),
            }
        trail.append(observed)
        if observed_raw is not None:
            raw_trail.append(observed_raw)
        if values_match(value, observed):
            return {
                "ok": True,
                "verified": True,
                "write_attempted": True,
                "changed": True,
                "method": action,
                **preview,
                "after": observed,
                "steps": step,
                "trail": trail[:16],
                "raw_trail": raw_trail[:16],
            }
        if observed == previous:
            reason = "display stopped moving before the requested value"
            break
        if control_role not in ("AXCheckBox", "AXRadioButton"):
            previous_number = as_number(previous)
            observed_number = as_number(observed)
            target_number = as_number(value)
            if None in (previous_number, observed_number, target_number):
                reason = "display stopped being numeric"
                break
            crossed = (target_number - previous_number) * (
                target_number - observed_number
            ) < 0
            if crossed:
                previous_raw = as_number(raw_trail[-2])
                current_raw = as_number(raw_trail[-1])
                if previous_raw is None or current_raw is None or current_raw == previous_raw:
                    reason = "target was crossed but raw slider calibration was unavailable"
                    break
                slope = (observed_number - previous_number) / (current_raw - previous_raw)
                if slope == 0:
                    reason = "target was crossed but display-to-raw slope was zero"
                    break
                target_raw = previous_raw + (target_number - previous_number) / slope
                target_raw_text = f"{target_raw:g}"
                for fine_step in range(step + 1, budget + 1):
                    prior_raw = raw_trail[-1]
                    try:
                        send_value(process, control_reference, target_raw_text, numeric=True)
                        time.sleep(0.12)
                        observed = read_value(process, group_reference)
                        observed_raw = read_value(process, control_reference)
                    except ProbeError as exc:
                        reason = f"calibrated raw stepping failed: {exc}"
                        break
                    trail.append(observed)
                    raw_trail.append(observed_raw)
                    if values_match(value, observed):
                        return {
                            "ok": True,
                            "verified": True,
                            "write_attempted": True,
                            "changed": True,
                            "method": "calibrated raw stepping",
                            **preview,
                            "after": observed,
                            "steps": fine_step,
                            "calibrated_raw_target": target_raw,
                            "trail": trail[:16],
                            "raw_trail": raw_trail[:16],
                        }
                    if observed_raw == prior_raw:
                        reason = "raw control reached the calibrated target but display did not match"
                        break
                else:
                    reason = "step budget exhausted during calibrated raw stepping"
                break
            if abs(observed_number - target_number) >= abs(previous_number - target_number):
                reason = "display moved away from the requested value"
                break
    else:
        reason = "step budget exhausted"
    return {
        "ok": False,
        "verified": False,
        "write_attempted": True,
        "method": action,
        **preview,
        "after": observed,
        "steps": len(trail) - 1,
        "trail": trail[:16],
        "raw_trail": raw_trail[:16],
        "note": reason,
    }


@tool
def plugin_write(
    parameter: str,
    value: str,
    window_index: int = 1,
    dry_run: bool = True,
    expected_plugin: str = "",
    expected_channel: str = "",
) -> dict:
    """Write one plugin parameter located by its accessibility name. Refuses when the name
    is ambiguous and reports the competing paths so plugin_write_path can be used instead.
    Runs as a dry run unless dry_run is set to false, and verifies every write by read-back."""
    process = require_logic()
    try:
        target = find_control(parameter, int(window_index), process)
    except ProbeError as exc:
        return {"ok": False, "error": str(exc)}
    return plugin_write_path(
        target["path"],
        value,
        window_index,
        dry_run,
        expected_plugin=expected_plugin,
        expected_channel=expected_channel,
    )


@tool
def plugin_write_label_verified(
    parameter: str,
    value: str,
    window_index: int = 1,
    expected_plugin: str = "",
    expected_channel: str = "",
    expected_before: str = "",
    dry_run: bool = True,
) -> dict:
    """Resolve an exact Controls-table label immediately before a verified write. This is
    the audit fix path: it does not reuse an earlier AX path, refuses ambiguous labels,
    checks optional expected_before, binds window identity, and delegates to read-back
    verified plugin_write_path."""
    if not (expected_plugin or expected_channel):
        return {"ok": False, "error": "expected_plugin or expected_channel is required"}
    table = plugin_parameters(
        window_index=window_index,
        contains=parameter,
        offset=0,
        limit=500,
        expected_plugin=expected_plugin,
        expected_channel=expected_channel,
    )
    if "error" in table:
        return {"ok": False, "error": table["error"], "write_attempted": False}
    matches = [
        entry
        for entry in table.get("parameters", [])
        if entry.get("label", "").strip().casefold() == parameter.strip().casefold()
    ]
    if not matches:
        return {
            "ok": False,
            "write_attempted": False,
            "error": f"no exact parameter label {parameter!r}",
            "near_matches": [entry.get("label") for entry in table.get("parameters", [])[:20]],
        }
    if len(matches) != 1:
        return {
            "ok": False,
            "write_attempted": False,
            "error": f"{len(matches)} exact parameters named {parameter!r}; refusing to guess",
            "paths": [entry.get("path") for entry in matches],
        }
    resolved = matches[0]
    before = resolved.get("display", "")
    if expected_before and not values_match(expected_before, before):
        return {
            "ok": False,
            "write_attempted": False,
            "error": "parameter value changed since the fix was planned",
            "expected_before": expected_before,
            "observed_before": before,
            "path": resolved.get("path"),
        }
    outcome = plugin_write_path(
        resolved["path"],
        value,
        window_index=window_index,
        dry_run=dry_run,
        expected_plugin=expected_plugin,
        expected_channel=expected_channel,
    )
    return {
        **outcome,
        "parameter": parameter,
        "resolved_path": resolved.get("path"),
        "resolved_before": before,
    }


@tool
def plugin_plan(edits: list, note: str = "") -> dict:
    """Register a batch without writing. The current plugin and channel identity is bound
    into every edit, so plugin_apply refuses if z-order changes before confirmation."""
    if not isinstance(edits, list) or not edits:
        return {"error": "edits must be a non-empty list"}
    normalised = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {"error": f"edit {index} is not an object"}
        if "value" not in edit or ("parameter" not in edit and "path" not in edit):
            return {"error": f"edit {index} needs value plus either parameter or path"}
        window_index = int(edit.get("window_index", 1))
        expected_plugin = str(edit.get("expected_plugin", ""))
        expected_channel = str(edit.get("expected_channel", ""))
        if not (expected_plugin or expected_channel):
            try:
                identity = read_plugin_identity(require_logic(), window_index)
            except ProbeError as exc:
                return {
                    "error": f"edit {index} could not bind plugin identity: {exc}",
                    "write_attempted": False,
                }
            expected_plugin = identity["plugin"]
            expected_channel = identity["channel"]
        normalised.append(
            {
                "parameter": str(edit.get("parameter", "")),
                "path": str(edit["path"]) if edit.get("path") else "",
                "value": str(edit["value"]),
                "window_index": window_index,
                "expected_plugin": expected_plugin,
                "expected_channel": expected_channel,
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
                edit["path"],
                edit["value"],
                edit["window_index"],
                dry_run=False,
                expected_plugin=edit["expected_plugin"],
                expected_channel=edit["expected_channel"],
            )
        else:
            outcome = plugin_write(
                edit["parameter"],
                edit["value"],
                edit["window_index"],
                dry_run=False,
                expected_plugin=edit["expected_plugin"],
                expected_channel=edit["expected_channel"],
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
        'tell application "System Events"\n'
        f'tell process "{process}"\n'
        "set frontmost to true\n"
        f"{action}\n"
        "end tell\n"
        "end tell",
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
    literal = value if numeric else apple_script_string(value)
    osa(
        f'tell application "System Events" to tell process "{process}" to '
        f"set value of {reference} to {literal}",
        timeout=60,
    )


def apple_script_string(value: str) -> str:
    """Quote user-controlled text for an AppleScript string literal."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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


def find_control_bar(process: str, window: int = 0) -> dict:
    window = window or main_window_index(process)
    elements = walk_window(process, window, 2, root="6")
    index: dict[str, dict] = {}
    for entry in elements:
        label = entry["name"] or entry["description"]
        if label and label not in index:
            index[label] = entry
    return index


@tool
def control_bar(window_index: int = 0) -> dict:
    """Read Logic's Control Bar: whether transport is playing or recording, cycle, solo and
    metronome state, live tempo, time and key signature, playhead position and master
    volume. Unlike a key command this reports actual state rather than assuming a toggle."""
    process = require_logic()
    window_index = int(window_index) or main_window_index(process)
    index = find_control_bar(process, window_index)
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
def transport_press(button: str, window_index: int = 0, verify: bool = True) -> dict:
    """Press a Control Bar button by name and report the state before and after. This is
    the reliable transport path: it targets the actual button rather than sending a
    keystroke that could land in the wrong place, and it can confirm what happened.
    Names come from control_bar, for example Play, Record, Cycle, Solo, Go to Beginning."""
    process = require_logic()
    window_index = int(window_index) or main_window_index(process)
    index = find_control_bar(process, window_index)
    entry = index.get(button)
    if entry is None:
        return {"ok": False, "error": f"no Control Bar element named {button!r}", "available": sorted(index)}
    reference = element_reference(entry["path"], window_index)
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
    """Drive Logic's transport by pressing the real Control Bar buttons and reporting the
    state each one reported. Actions: play, stop, go_to_start, restart, cycle. Logic swaps
    the Go to Beginning button for a Stop button while playing, so play and stop are
    separate buttons rather than one toggle, and the button that is not currently present
    will report that it could not be found."""
    mapping = {
        "play": [("press", "Play")],
        "stop": [("press", "Stop")],
        "play_stop": [("press", "Play")],
        "go_to_start": [("press", "Go to Beginning")],
        "restart": [("press", "Go to Beginning"), ("press", "Play")],
        "cycle": [("press", "Cycle")],
    }
    if action not in mapping:
        return {"ok": False, "error": f"unknown action, expected one of {sorted(mapping)}"}
    sent = []
    for _, button in mapping[action]:
        result = transport_press(button)
        sent.append({"button": button, **{k: result[k] for k in ("ok", "before", "after", "changed") if k in result}})
        if not result.get("ok"):
            return {"ok": False, "action": action, "sent": sent, "error": result.get("error")}
        time.sleep(0.2)
    return {"ok": True, "action": action, "sent": sent}


def normalise_logic_position(value: str) -> str | None:
    parts = re.findall(r"\d+", str(value or ""))
    if len(parts) < 4:
        return None
    values = [int(part) for part in parts[:4]]
    if any(value < 1 for value in values):
        return None
    return ".".join(str(value) for value in values)


def close_goto_position_dialog(process: str) -> None:
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            'click first button of front window whose name is "Cancel"',
            timeout=10,
        )
    except ProbeError:
        pass


@tool
def transport_goto_position(position: str, dry_run: bool = True) -> dict:
    """Set an exact bar.beat.division.tick position through Logic's own Go To Position
    dialog and independently reopen the dialog to read Current back. This avoids an
    abandoned mature-server operation continuing after its timeout. Dry-run is default."""
    requested = normalise_logic_position(position)
    if requested is None or requested != str(position).strip():
        return {
            "ok": False,
            "verified": False,
            "write_attempted": False,
            "error": "position must be four positive dot-separated integers",
            "requested": position,
        }
    preview = {"requested": requested, "method": "logic_go_to_position_dialog"}
    if dry_run:
        return {"ok": True, "verified": False, "dry_run": True, **preview}
    process = require_logic()
    opened = menu_click(["Navigate", "Go To", "Position…"])
    if not opened.get("ok") or not any(
        window.get("title") == "Go To Position" for window in opened.get("opened", [])
    ):
        return {
            "ok": False,
            "verified": False,
            "write_attempted": False,
            **preview,
            "error": "Go To Position dialog did not open",
            "open_result": opened,
        }
    try:
        # The editable New position field is focused on open. Selecting the complete
        # segmented value and typing the dotted form fills all four segments atomically;
        # AXSlider writes only move one encoded step and cannot safely set this control.
        send_key(process, "key", "a", ["cmd"])
        send_key(process, "key", requested, [])
        send_key(process, "code", 36, [])
        time.sleep(0.7)
        reopened = menu_click(["Navigate", "Go To", "Position…"])
        if not reopened.get("ok"):
            raise ProbeError("verification dialog did not reopen")
        current_raw = osa(
            f'tell application "System Events" to tell process "{process}" to '
            "return value of UI element 4 of front window as string",
            timeout=15,
        )
        observed = normalise_logic_position(current_raw)
    except ProbeError as exc:
        close_goto_position_dialog(process)
        return {
            "ok": False,
            "verified": False,
            "write_attempted": True,
            **preview,
            "error": str(exc),
        }
    close_goto_position_dialog(process)
    verified = observed == requested
    return {
        "ok": verified,
        "verified": verified,
        "write_attempted": True,
        **preview,
        "observed": observed,
        "observed_raw": current_raw,
        **({"error": "position readback did not match"} if not verified else {}),
    }


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
    expect_plugin: str = "",
) -> dict:
    """Poll named control paths in an open plugin window over a period of time and report
    the series plus the minimum, maximum and final value of each. Every path is resolved
    once before polling starts and an unresolvable path is reported as an error rather than
    as an empty reading, because a silent meter and a wrong window look identical
    otherwise. Pass expect_plugin to assert which plugin the window should hold; the call
    fails closed if the window holds something else, which happens whenever Logic reorders
    or closes plugin editors."""
    process = require_logic()
    if not paths:
        return {"ok": False, "error": "give at least one path from plugin_snapshot"}
    try:
        title = osa(
            f'tell application "System Events" to tell process "{process}" to '
            f"return name of window {int(window_index)} as string"
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"window {window_index} does not resolve: {exc}"}
    if expect_plugin:
        snapshot = plugin_snapshot(int(window_index), max_depth=3, seconds=20)
        found = snapshot.get("plugin", "")
        if expect_plugin.lower() not in found.lower():
            return {
                "ok": False,
                "error": f"window {window_index} holds {found!r}, not {expect_plugin!r}",
                "window_title": title,
                "advice": "plugin editors move and close, especially with an MCU surface "
                "attached. Re-read ax_windows and plugin_snapshot before polling",
            }
    references, unresolved = {}, {}
    for path in paths:
        text = str(path)
        if not all(part.strip().isdigit() for part in text.split(".") if part.strip()):
            unresolved[text] = "path must be dotted integers"
            continue
        reference = element_reference(text, int(window_index))
        try:
            read_value(process, reference)
            references[text] = reference
        except ProbeError as exc:
            unresolved[text] = str(exc)
    if not references:
        return {
            "ok": False,
            "error": "no path resolved in this window",
            "window_index": window_index,
            "window_title": title,
            "unresolved": unresolved,
        }
    series: dict[str, list] = {path: [] for path in references}
    errors: dict[str, int] = {path: 0 for path in references}
    deadline = time.time() + max(0.5, min(float(seconds), 120.0))
    tick = max(0.1, float(interval))
    samples = 0
    while time.time() < deadline:
        for path, reference in references.items():
            try:
                series[path].append(read_value(process, reference))
            except ProbeError:
                series[path].append("")
                errors[path] += 1
        samples += 1
        time.sleep(tick)
    summary = {}
    for path, values in series.items():
        numbers = [n for n in (as_number(v) for v in values) if n is not None]
        summary[path] = {
            "samples": len(values),
            "read_errors": errors[path],
            "final": values[-1] if values else "",
            "min": f"{min(numbers):g}" if numbers else None,
            "max": f"{max(numbers):g}" if numbers else None,
            "moved": bool(numbers) and len(set(numbers)) > 1,
            "series": values[:40],
        }
    return {
        "ok": True,
        "window_index": window_index,
        "window_title": title,
        "samples": samples,
        "unresolved": unresolved,
        "meters": summary,
    }


@tool
def window_find(title_contains: str) -> dict:
    """Find open Logic windows whose title contains the given text, returning their index
    for ax_dump and plugin_snapshot."""
    listing = ax_windows()
    needle = title_contains.lower()
    matches = [w for w in listing["windows"] if needle in w["title"].lower()]
    return {"query": title_contains, "matches": matches, "total_windows": listing["count"]}


@tool
def ax_press(path: str, window_index: int = 1, settle: float = 0.6) -> dict:
    """Press any accessibility element addressed by its dotted path and report the window
    list before and after, so the effect of the press is visible. Pressing an insert slot
    in the mixer is how a plugin editor is opened without touching the mouse."""
    process = require_logic()
    if not all(part.strip().isdigit() for part in path.split(".") if part.strip()):
        return {"ok": False, "error": f"path must be dotted integers, got {path!r}"}
    reference = element_reference(path, int(window_index))
    try:
        role = osa(
            f'tell application "System Events" to tell process "{process}" to '
            f"return role of {reference} as string"
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"path does not resolve: {exc}"}
    before = ax_windows()
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {reference}',
            timeout=30,
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"press failed: {exc}", "role": role}
    time.sleep(max(0.1, min(float(settle), 5.0)))
    after = ax_windows()
    delta = after["count"] - before["count"]
    def tally(windows):
        counts: dict[str, int] = {}
        for w in windows:
            counts[w["title"]] = counts.get(w["title"], 0) + 1
        return counts
    before_tally, after_tally = tally(before["windows"]), tally(after["windows"])
    gained = {
        title: after_tally[title] - before_tally.get(title, 0)
        for title in after_tally
        if after_tally[title] > before_tally.get(title, 0)
    }
    lost = {
        title: before_tally[title] - after_tally.get(title, 0)
        for title in before_tally
        if before_tally[title] > after_tally.get(title, 0)
    }
    return {
        "ok": True,
        "path": path,
        "role": role,
        "windows_before": before["count"],
        "windows_after": after["count"],
        "window_delta": delta,
        "opened": gained,
        "closed": lost,
        "windows": after["windows"],
        "note": "windows are counted by title because several editors on one channel share "
        "the channel name; a delta with no title change means another editor of the same "
        "channel opened or closed",
    }


FADER_UNITY_RAW = 173.0
FADER_RAW_PER_DB = 10.0
FADER_LINEAR_FLOOR = 113.0


def fader_db_from_raw(raw: str):
    """Convert a raw fader position to decibels, but only across the span where the
    relationship was actually measured. Logic's fader is linear at ten raw units per
    decibel from unity down to about minus six, then steepens sharply: a strip reading
    forty-seven showed minus twenty-one where the linear rule predicts minus twelve point
    six, and zero is silence rather than the minus seventeen the rule would give. Outside
    the measured span this returns nothing, because a plausible wrong number is worse than
    an admitted gap."""
    value = as_number(raw)
    if value is None:
        return None
    if value < FADER_LINEAR_FLOOR:
        return None
    return round((value - FADER_UNITY_RAW) / FADER_RAW_PER_DB, 1)


def parse_db(text: str):
    match = re.search(r"(-?[\d.,]+)\s*dB", text or "")
    if not match:
        return None
    return as_number(match.group(1))


def usable_send_level(raw):
    """Return Logic's displayed send value only when it is numerically plausible.

    Some Logic 12.3 send sliders expose an implementation value such as 535750240
    or 1.50994944E+9 instead of the displayed level. Those numbers are neither dB
    nor a documented normalized position, so presenting them as a measurement is
    misleading. Preserve the raw value separately and leave the level unknown.
    """
    value = as_number(raw)
    if value is None or not math.isfinite(value) or value < -200 or value > 100:
        return None
    return raw


NON_SEND_DESCRIPTIONS = (
    "automation",
    "volume",
    "input",
    "insert bar",
    "audio plug-in",
    "midi plug-in",
    "send button",
    "name",
    "mute",
    "solo",
    "record",
    "monitoring",
    "volume fader",
    "volume fader level",
    "peak level meter",
    "pan",
    "group",
)


def is_automation(text: str) -> bool:
    return bool(text) and text.split(",")[0].strip() in (
        "Read",
        "Write",
        "Touch",
        "Latch",
        "Trim",
        "Off",
    )


DESTRUCTIVE_MENU_WORDS = (
    "delete",
    "remove",
    "clear",
    "erase",
    "quit",
    "revert",
    "close project",
    "empty",
    "reset",
    "restore",
    "replace",
    "overwrite",
)


def menu_reference(path: list) -> str:
    if not path:
        raise ProbeError("menu path is empty")
    reference = f'menu bar item "{path[0]}" of menu bar 1'
    for title in path[1:]:
        reference = f'menu item "{title}" of menu 1 of {reference}'
    return reference


@tool
def menu_list(path: list = None) -> dict:
    """List Logic's menus. With no path it returns the menu bar titles; with a path such as
    ["Logic Pro", "Control Surfaces"] it returns the items inside that menu, marking which
    ones open a submenu and which are currently enabled. Menu titles carry typographic
    ellipses and other characters that are easy to mistype, so read them here rather than
    assuming."""
    process = require_logic()
    path = list(path or [])
    if not path:
        raw = osa(
            f'tell application "System Events" to tell process "{process}"\n'
            'set out to ""\n'
            "repeat with mbi in menu bar items of menu bar 1\n"
            "try\n"
            'set out to out & (name of mbi as string) & "|:|"\n'
            "end try\n"
            "end repeat\n"
            "return out\n"
            "end tell"
        )
        return {"level": "menu bar", "items": split_records(raw)}
    reference = menu_reference(path)
    try:
        raw = osa(
            f'tell application "System Events" to tell process "{process}"\n'
            'set out to ""\n'
            f"repeat with mi in menu items of menu 1 of {reference}\n"
            "try\n"
            "set nm to (name of mi as string)\n"
            "on error\n"
            'set nm to "-"\n'
            "end try\n"
            "set sub to 0\n"
            "try\n"
            "if (count of menus of mi) > 0 then set sub to 1\n"
            "end try\n"
            "set en to 0\n"
            "try\n"
            "if enabled of mi then set en to 1\n"
            "end try\n"
            'set mk to ""\n'
            "try\n"
            "set mk to (value of attribute \"AXMenuItemMarkChar\" of mi) as string\n"
            "end try\n"
            'set out to out & nm & "~" & (sub as string) & "~" & (en as string) & "~" & mk & "|:|"\n'
            "end repeat\n"
            "return out\n"
            "end tell",
            timeout=30,
        )
    except ProbeError as exc:
        return {"error": f"menu path {path} does not resolve: {exc}"}
    items = []
    for record in split_records(raw):
        parts = (record.split("~") + ["", "", "", ""])[:4]
        mark = clean(parts[3])
        items.append(
            {
                "title": parts[0],
                "submenu": parts[1] == "1",
                "enabled": parts[2] == "1",
                "checked": bool(mark),
            }
        )
    return {
        "level": " > ".join(path),
        "count": len(items),
        "items": [i for i in items if i["title"] != "missing value"],
        "separators": sum(1 for i in items if i["title"] == "missing value"),
    }


@tool
def menu_click(path: list, allow_destructive: bool = False) -> dict:
    """Click a Logic menu item addressed by its full path, for example
    ["Logic Pro", "Control Surfaces", "Setup..."]. Items whose titles suggest a destructive
    action are refused unless allow_destructive is set, because a mistyped menu path can
    land on a neighbouring item. Read exact titles with menu_list first."""
    process = require_logic()
    path = list(path or [])
    if not path:
        return {"ok": False, "error": "menu path is empty"}
    target = path[-1].lower()
    if not allow_destructive and any(word in target for word in DESTRUCTIVE_MENU_WORDS):
        return {
            "ok": False,
            "refused": True,
            "path": path,
            "error": f"{path[-1]!r} looks destructive; pass allow_destructive to proceed",
        }
    reference = menu_reference(path)
    before = ax_windows()
    try:
        osa(
            f'tell application "{process}" to activate\n'
            "delay 0.2\n"
            f'tell application "System Events" to tell process "{process}" to '
            f"click {reference}",
            timeout=30,
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"click failed: {exc}", "path": path}
    time.sleep(0.8)
    after = ax_windows()
    opened = [w for w in after["windows"] if w["title"] not in {b["title"] for b in before["windows"]}]
    return {
        "ok": True,
        "path": path,
        "windows_before": before["count"],
        "windows_after": after["count"],
        "opened": opened,
    }


SURFACE_BYPASS_PATH = ["Logic Pro", "Control Surfaces", "Bypass All Control Surfaces"]


def read_menu_item(path: list) -> dict:
    parent = menu_list(path[:-1])
    if "error" in parent:
        return parent
    for item in parent.get("items", []):
        if item["title"] == path[-1]:
            return item
    return {"error": f"{path[-1]!r} not found under {' > '.join(path[:-1])}"}


@tool
def surfaces_bypass(state: str = "read") -> dict:
    """Read or set Logic's Bypass All Control Surfaces switch, which silences every control
    surface including the MCU without disturbing its port assignment. State is read, on to
    bypass surfaces, off to re-enable them, or toggle. Because the port assignment is left
    intact, re-enabling does not need Logic to be restarted the way re-assigning ports does.
    Bypassing is worth doing during heavy manual editing, when the constant feedback traffic
    to a surface is unwanted."""
    if state not in ("read", "on", "off", "toggle"):
        return {"ok": False, "error": "state must be read, on, off or toggle"}
    require_logic()
    item = read_menu_item(SURFACE_BYPASS_PATH)
    if "error" in item:
        return {"ok": False, **item}
    bypassed = item["checked"]
    if state == "read":
        return {"ok": True, "bypassed": bypassed, "surfaces_active": not bypassed}
    wanted = (not bypassed) if state == "toggle" else (state == "on")
    if wanted == bypassed:
        return {
            "ok": True,
            "bypassed": bypassed,
            "surfaces_active": not bypassed,
            "changed": False,
            "note": "already in the requested state",
        }
    clicked = menu_click(SURFACE_BYPASS_PATH)
    if not clicked.get("ok"):
        return {"ok": False, "error": clicked.get("error"), "bypassed": bypassed}
    time.sleep(0.4)
    after = read_menu_item(SURFACE_BYPASS_PATH)
    if "error" in after:
        return {"ok": False, "error": "clicked but could not read the state back"}
    return {
        "ok": after["checked"] == wanted,
        "verified": after["checked"] == wanted,
        "bypassed": after["checked"],
        "surfaces_active": not after["checked"],
        "changed": after["checked"] != bypassed,
        "requested": state,
    }


def main_window_index(process: str) -> int:
    raw = osa(
        f'tell application "System Events" to tell process "{process}"\n'
        'set out to ""\n'
        "repeat with i from 1 to (count of windows)\n"
        "try\n"
        'set out to out & (i as string) & "~" & (subrole of window i as string) & "~" & (name of window i as string) & "|:|"\n'
        "end try\n"
        "end repeat\n"
        "return out\n"
        "end tell"
    )
    standard = []
    for record in split_records(raw):
        parts = (record.split("~") + ["", "", ""])[:3]
        if parts[1] == "AXStandardWindow":
            standard.append((int(parts[0]), parts[2]))
    if not standard:
        raise ProbeError("no standard window found; Logic may have only dialogs open")
    for index, title in standard:
        if title.endswith(" - Tracks"):
            return index
    return standard[0][0]


@tool
def main_window() -> dict:
    """Report which window index currently holds Logic's main Tracks window. Window indices
    are ordered by which window is in front, so opening any plugin editor pushes the main
    window off index 1. Every tool that means the main window resolves it this way instead
    of assuming, because assuming produced invalid-index errors that looked like the mixer
    had disappeared."""
    process = require_logic()
    index = main_window_index(process)
    listing = ax_windows()
    return {
        "main_window_index": index,
        "windows": listing["windows"],
        "note": "a plugin editor in front takes index 1, which is why a path that worked a "
        "moment ago can stop resolving",
    }


def locate_mixer(process: str, hint: str = "") -> dict:
    try:
        main = main_window_index(process)
    except ProbeError as exc:
        return {"found": False, "reason": str(exc)}
    if hint:
        try:
            role = osa(
                f'tell application "System Events" to tell process "{process}" to '
                f"return role of {element_reference(hint, main)} as string"
            )
            if role == "AXLayoutArea":
                return {"found": True, "path": hint, "window_index": main, "source": "hint"}
        except ProbeError:
            pass
    try:
        elements = walk_window(process, main, 2, budget=600, seconds=30)
    except ProbeError as exc:
        return {"found": False, "reason": f"could not read the main window: {exc}"}
    areas = [
        e
        for e in elements
        if e["role"] == "AXLayoutArea" and "mixer" in (e["description"] or "").lower()
    ]
    if not areas:
        panes = [e for e in elements if "mixer" in (e["description"] or "").lower()]
        return {
            "found": False,
            "reason": "no mixer layout area in the main window",
            "mixer_like_elements": [{"path": e["path"], "role": e["role"], "description": e["description"]} for e in panes[:6]],
            "advice": "open the Mixer pane in Logic, for example with keys_send toggle_mixer, "
            "then try again",
        }
    best = None
    for area in areas:
        try:
            count = osa(
                f'tell application "System Events" to tell process "{process}" to '
                f"return count of UI elements of {element_reference(area['path'], main)} as string",
                timeout=15,
            )
            strips = int(float(count.replace(",", ".")))
        except (ProbeError, ValueError):
            strips = 0
        if best is None or strips > best[1]:
            best = (area["path"], strips)
    if best is None or best[1] == 0:
        return {"found": False, "reason": "mixer area found but it holds no channel strips"}
    return {
        "found": True,
        "path": best[0],
        "window_index": main,
        "strip_count": best[1],
        "source": "discovered",
    }


@tool
def mixer_locate(hint: str = "") -> dict:
    """Find where the Mixer pane currently sits in the main window's accessibility tree.
    Element indices shift whenever Logic's layout changes or the application restarts, so
    a path that worked earlier can silently point at nothing. Every mixer tool calls this
    unless given an explicit path."""
    return locate_mixer(require_logic(), hint)


def parse_strip(label: str, path: str, kids: list) -> dict:
    row: dict = {
        "path": path,
        "name": label,
        "fader_db": None,
        "fader_raw": None,
        "pan": None,
        "clipping": None,
        "mute": None,
        "solo": None,
        "automation": None,
        "output": None,
        "fader_db_source": None,
        "order": None,
        "control_paths": {},
    }
    send_levels, output = [], None
    bars = [i for i, k in enumerate(kids) if k["description"] == "insert bar"]
    first_bar = min(bars) if bars else len(kids)
    sends, inserts = [], []
    for position, kid in enumerate(kids):
        what = (kid["description"] or "").strip()
        role = kid["role"]
        if what == "name" and kid["value"]:
            row["name"] = kid["value"]
        elif what == "volume fader":
            row["fader_raw"] = kid["value"]
        elif what == "volume fader level":
            row["fader_db"] = parse_db(kid["name"])
        elif what == "pan":
            row["pan"] = as_number(kid["value"])
        elif what == "peak level meter":
            row["clipping"] = "clipping off" not in (kid["value"] or "")
        elif what in ("mute", "solo", "record", "monitoring"):
            row[what] = kid["value"]
            row["control_paths"][what] = kid.get("path", "")
        elif is_automation(what):
            row["automation"] = what.split(",")[0].strip()
        elif role == "AXSlider" and what == "send knob":
            send_levels.append(kid["value"])
        elif what == "insert bar":
            continue
        elif role == "AXGroup" and what and not is_automation(what):
            (sends if position < first_bar else inserts).append(
                {"name": what, "path": kid.get("path", ""), "role": role}
            )
        elif role == "AXButton" and what and what not in NON_SEND_DESCRIPTIONS:
            output = output or what
    parsed_sends = []
    for i, bus in enumerate(item["name"] for item in sends):
        raw_level = send_levels[i] if i < len(send_levels) else None
        level = usable_send_level(raw_level)
        send = {"bus": bus, "level": level}
        if raw_level not in (None, "") and level is None:
            send["level_raw"] = raw_level
            send["level_note"] = (
                "Logic exposed an undocumented internal slider value; the displayed "
                "send level is unavailable and was not guessed"
            )
        parsed_sends.append(send)
    row["sends"] = list(reversed(parsed_sends))
    row["send_controls"] = list(reversed(sends))
    row["insert_controls"] = list(reversed(inserts))
    row["inserts"] = [item["name"] for item in row["insert_controls"]]
    row["order"] = "signal flow, first processed first"
    row["output"] = output
    if row.get("fader_db") is None and row.get("fader_raw"):
        derived = fader_db_from_raw(row["fader_raw"])
        if derived is not None:
            row["fader_db"] = derived
            row["fader_db_source"] = "derived from fader position"
        else:
            row["fader_db_source"] = (
                "not derivable: the fader sits below the span where the raw-to-decibel "
                "relationship was measured, and guessing there produced errors of eight "
                "decibels or more"
            )
    elif row.get("fader_db") is not None:
        row["fader_db_source"] = "read from the strip label"
    generic = {"audio plug-in", "midi plug-in", "send button", ""}
    detail_missing = []
    if row.get("fader_db") is None:
        detail_missing.append("fader level in dB")
    if row["inserts"] and all(i in generic for i in row["inserts"]):
        detail_missing.append("plugin names")
    if row["sends"] and all(s["bus"] in generic for s in row["sends"]):
        detail_missing.append("send destinations")
    if detail_missing:
        row["detail"] = "partial"
        row["missing"] = detail_missing
        row["reason"] = (
            "Logic only labels channel strips that are scrolled into view; this one was "
            "off screen, so names and levels came back as generic placeholders"
        )
    else:
        row["detail"] = "full"
    return row


@tool
def mixer_strips(mixer_path: str = "") -> dict:
    """List the channel strips in the open Mixer with their names and paths, without
    reading their contents. This is the cheap index that mixer_survey walks through, and
    it is how the size of the job is known before starting it. The mixer is located
    automatically unless a path is given, because element indices move between sessions."""
    process = require_logic()
    located = locate_mixer(process, mixer_path)
    if not located.get("found"):
        return located
    mixer_path = located["path"]
    main = located["window_index"]
    # Channel strips are direct children of the mixer area. Descending into
    # every strip here turns the cheap index into a full insert/send survey.
    elements = walk_window(process, main, 0, budget=400, seconds=25, root=mixer_path)
    strips = [
        {"index": i, "path": e["path"], "name": e["description"] or e["name"]}
        for i, e in enumerate(elements)
        if e["role"] == "AXLayoutItem"
    ]
    return {
        "mixer_path": mixer_path,
        "window_index": main,
        "located_by": located.get("source"),
        "count": len(strips),
        "strips": strips,
    }


@tool
def mixer_reveal_strip(
    strip_path: str,
    expected_strip: str,
    dry_run: bool = True,
) -> dict:
    """Scroll one mixer strip into the visible viewport using AXScrollToVisible, then
    verify the real name. This turns off-screen generic labels into readable plugin names
    without assuming that a mixer index is a track index."""
    if not strip_path or not all(
        part.strip().isdigit() for part in strip_path.split(".") if part.strip()
    ):
        return {"ok": False, "error": f"strip_path must be dotted integers, got {strip_path!r}"}
    process = require_logic()
    try:
        main = main_window_index(process)
        role = osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'return role of {element_reference(strip_path, main)} as string',
            timeout=15,
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"strip path does not resolve: {exc}"}
    if role != "AXLayoutItem":
        return {"ok": False, "error": f"strip path resolved to {role}, expected AXLayoutItem"}
    preview = {
        "strip_path": strip_path,
        "expected_strip": expected_strip,
        "window_index": main,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, **preview, "note": "viewport was not changed"}
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXScrollToVisible" of {element_reference(strip_path, main)}',
            timeout=20,
        )
        time.sleep(0.45)
        kids = walk_window(process, main, 0, budget=350, seconds=20, root=strip_path)
    except ProbeError as exc:
        return {"ok": False, **preview, "error": f"scroll/read failed: {exc}"}
    strip = parse_strip("", strip_path, kids)
    observed = strip.get("name") or ""
    verified = bool(observed) and observed.casefold() == expected_strip.casefold()
    return {
        "ok": verified,
        "verified": verified,
        **preview,
        "observed_strip": observed,
        "detail": strip.get("detail"),
        "inserts": strip.get("inserts", []),
        "note": "revealed strip identity did not match" if not verified else "",
    }


@tool
def mixer_read_strip(strip_path: str, expected_strip: str) -> dict:
    """Read one already revealed strip with verified name, insert paths and solo/mute
    controls. Call mixer_reveal_strip first for an off-screen channel."""
    process = require_logic()
    try:
        main = main_window_index(process)
        kids = walk_window(process, main, 0, budget=350, seconds=20, root=strip_path)
    except ProbeError as exc:
        return {"ok": False, "error": str(exc)}
    strip = parse_strip("", strip_path, kids)
    observed = strip.get("name") or ""
    if not observed or observed.casefold() != expected_strip.casefold():
        return {
            "ok": False,
            "error": "strip is not visible with the expected identity",
            "expected_strip": expected_strip,
            "observed_strip": observed,
            "advice": "call mixer_reveal_strip immediately before this read",
        }
    return {"ok": True, **strip}


@tool
def plugin_open_insert(
    strip_path: str,
    insert_index: int,
    expected_strip: str,
    expected_plugin: str = "",
    dry_run: bool = True,
) -> dict:
    """Open one insert by its signal-flow index after re-reading the strip identity.
    The operation refuses stale paths or unexpected plugin names. Opening an editor does
    not alter audio, but defaults to a dry run because it changes window z-order."""
    if not strip_path or not all(
        part.strip().isdigit() for part in strip_path.split(".") if part.strip()
    ):
        return {"ok": False, "error": f"strip_path must be dotted integers, got {strip_path!r}"}
    slot = int(insert_index)
    if slot < 0:
        return {"ok": False, "error": "insert_index must be zero or greater"}
    process = require_logic()
    try:
        main = main_window_index(process)
        kids = walk_window(
            process, main, 0, budget=350, seconds=20, root=strip_path
        )
    except ProbeError as exc:
        return {"ok": False, "error": f"could not re-read the strip: {exc}"}
    strip = parse_strip("", strip_path, kids)
    observed_strip = strip.get("name") or ""
    if expected_strip and observed_strip.casefold() != expected_strip.casefold():
        return {
            "ok": False,
            "write_attempted": False,
            "error": "strip identity changed; refusing the stale path",
            "expected_strip": expected_strip,
            "observed_strip": observed_strip,
        }
    controls = strip.get("insert_controls", [])
    if slot >= len(controls):
        return {
            "ok": False,
            "error": f"insert_index {slot} is outside the {len(controls)} readable inserts",
            "inserts": strip.get("inserts", []),
        }
    target = controls[slot]
    observed_plugin = target.get("name", "")
    if expected_plugin and observed_plugin.casefold() != expected_plugin.casefold():
        return {
            "ok": False,
            "write_attempted": False,
            "error": "insert identity changed; refusing the stale path",
            "expected_plugin": expected_plugin,
            "observed_plugin": observed_plugin,
            "inserts": strip.get("inserts", []),
        }
    preview = {
        "strip": observed_strip,
        "strip_path": strip_path,
        "insert_index": slot,
        "plugin": observed_plugin,
        "insert_path": target.get("path"),
    }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            **preview,
            "note": "nothing was opened; re-call with dry_run false",
        }
    before = ax_windows()
    try:
        descendants = walk_window(
            process, main, 1, budget=20, seconds=10, root=target["path"]
        )
        open_buttons = [
            entry
            for entry in descendants
            if entry["role"] == "AXButton" and entry["name"].casefold() == "open"
        ]
        if len(open_buttons) != 1:
            return {
                "ok": False,
                "write_attempted": False,
                **preview,
                "error": f"insert exposes {len(open_buttons)} exact Open buttons; refusing to guess",
            }
        open_path = open_buttons[0]["path"]
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {element_reference(open_path, main)}',
            timeout=30,
        )
    except ProbeError as exc:
        return {"ok": False, **preview, "error": f"insert press failed: {exc}"}
    time.sleep(0.8)
    after = ax_windows()
    try:
        identity = read_plugin_identity(process, 1)
    except ProbeError as exc:
        return {
            "ok": False,
            **preview,
            "opened": after["count"] >= before["count"],
            "error": f"editor opened but identity could not be read: {exc}",
        }
    verified = identity_matches(
        identity,
        expected_plugin or observed_plugin,
        expected_strip,
    )
    return {
        "ok": verified,
        "verified": verified,
        **preview,
        "open_path": open_path,
        "window_index": 1,
        "identity": identity,
        "windows_before": before["count"],
        "windows_after": after["count"],
        "note": "editor identity did not match the requested insert"
        if not verified
        else "",
    }


@tool
def mixer_set_toggle(
    strip_path: str,
    control: str,
    enabled: bool,
    expected_strip: str,
    dry_run: bool = True,
) -> dict:
    """Set mute or solo on a mixer-only Aux/Bus strip after re-reading its identity.
    This is used when a mixer index is not a valid track index. AXPress is independently
    read back and stale strip paths are refused."""
    if control not in ("mute", "solo"):
        return {"ok": False, "error": "control must be mute or solo"}
    if not strip_path or not all(
        part.strip().isdigit() for part in strip_path.split(".") if part.strip()
    ):
        return {"ok": False, "error": f"strip_path must be dotted integers, got {strip_path!r}"}
    process = require_logic()
    try:
        main = main_window_index(process)
        kids = walk_window(process, main, 0, budget=350, seconds=20, root=strip_path)
    except ProbeError as exc:
        return {"ok": False, "error": f"could not re-read strip: {exc}"}
    strip = parse_strip("", strip_path, kids)
    observed_strip = strip.get("name") or ""
    if expected_strip and observed_strip.casefold() != expected_strip.casefold():
        return {
            "ok": False,
            "write_attempted": False,
            "error": "strip identity changed; refusing the stale path",
            "expected_strip": expected_strip,
            "observed_strip": observed_strip,
        }
    path = strip.get("control_paths", {}).get(control)
    if not path:
        return {"ok": False, "error": f"{control} control is not readable on this strip"}

    def toggle_value(value):
        folded = str(value or "").strip().casefold()
        if folded in ("1", "true", "on", "yes", "enabled"):
            return True
        if folded in ("0", "false", "off", "no", "disabled"):
            return False
        return None

    before_raw = strip.get(control)
    before = toggle_value(before_raw)
    if before is None:
        return {"ok": False, "error": f"unrecognised {control} state {before_raw!r}"}
    preview = {
        "strip": observed_strip,
        "strip_path": strip_path,
        "control": control,
        "control_path": path,
        "before": before,
        "requested": bool(enabled),
    }
    if before == bool(enabled):
        return {"ok": True, "verified": True, "changed": False, **preview}
    if dry_run:
        return {"ok": True, "dry_run": True, **preview, "note": "nothing was changed"}
    reference = element_reference(path, main)
    try:
        osa(
            f'tell application "System Events" to tell process "{process}" to '
            f'perform action "AXPress" of {reference}',
            timeout=20,
        )
        time.sleep(0.35)
        after_raw = read_value(process, reference)
    except ProbeError as exc:
        return {"ok": False, "verified": False, **preview, "error": str(exc)}
    after = toggle_value(after_raw)
    verified = after == bool(enabled)
    return {
        "ok": verified,
        "verified": verified,
        "changed": after != before,
        "after": after,
        **preview,
        "note": "state read-back did not match" if not verified else "",
    }


@tool
def mixer_survey(
    offset: int = 0,
    strip_limit: int = 12,
    mixer_path: str = "",
    total_seconds: int = 150,
    per_strip_seconds: int = 12,
) -> dict:
    """Survey channel strips in the open Mixer: name, fader level in decibels, pan, mute
    and solo, whether the peak meter shows clipping, the output bus, sends with their
    buses, and the insert chain. Each strip is walked on its own, so offset genuinely skips
    work and a large mixer can be read in slices without re-reading what came before. The
    Mixer pane must be visible. Strips that overrun their allowance are reported as
    incomplete rather than returned half-parsed."""
    process = require_logic()
    index = mixer_strips(mixer_path)
    if not index.get("count"):
        return index
    strips = index["strips"]
    window = strips[int(offset) : int(offset) + int(strip_limit)]
    deadline = time.time() + max(15, min(int(total_seconds), 600))
    report, incomplete = [], []
    for strip in window:
        remaining = deadline - time.time()
        if remaining <= 4:
            incomplete.append({**strip, "reason": "survey deadline reached"})
            continue
        allowance = int(min(max(4, remaining - 2), int(per_strip_seconds)))
        try:
            kids = walk_window(
                process,
                index["window_index"],
                0,
                budget=300,
                seconds=allowance,
                root=strip["path"],
            )
        except ProbeError as exc:
            incomplete.append({**strip, "reason": str(exc)})
            continue
        if len(kids) < 6:
            incomplete.append({**strip, "children_read": len(kids), "reason": "strip read was cut short"})
            continue
        parsed = parse_strip(strip["name"], strip["path"], kids)
        # Preserve the visible-surface namespace. `mix_inventory` uses this
        # index only to bridge the named AX strip to the mature server's
        # nameless mixer row; it is never treated as a project track index.
        parsed["index"] = strip["index"]
        report.append(parsed)

    clipping = [r["name"] for r in report if r.get("clipping")]
    muted = [r["name"] for r in report if r.get("mute") == "on"]
    partial = [r["name"] for r in report if r.get("detail") == "partial"]
    faders = [r["fader_db"] for r in report if isinstance(r.get("fader_db"), float)]
    outputs: dict[str, int] = {}
    for r in report:
        if r.get("output"):
            outputs[r["output"]] = outputs.get(r["output"], 0) + 1
    retry_offsets = sorted({int(item["index"]) for item in incomplete})
    return {
        "mixer_path": index["mixer_path"],
        "window_index": index["window_index"],
        "strips_total": index["count"],
        "offset": offset,
        "strips_parsed": len(report),
        "strips_incomplete": incomplete,
        # Never page past a strip that was not read. A caller can retry from
        # this offset without silently losing an Aux/Bus/master from the audit.
        "next_offset": retry_offsets[0] if retry_offsets else int(offset) + len(window),
        "retry_offsets": retry_offsets,
        "strips_full_detail": len(report) - len(partial),
        "strips_partial_detail": partial,
        "clipping_strips": clipping,
        "muted_strips": muted,
        "fader_range_db": [min(faders), max(faders)] if faders else None,
        "outputs": dict(sorted(outputs.items(), key=lambda kv: -kv[1])),
        "channels": report,
        "note": "strips marked partial were off screen. Scroll the Mixer so they are "
        "visible, or survey in smaller slices while scrolling, to get plugin names and "
        "fader levels for them"
        if partial
        else "",
    }


@tool
def plugin_meter_read(
    window_index: int = 1,
    expected_plugin: str = "",
    expected_channel: str = "",
    contains: str = "",
    max_depth: int = 3,
    budget: int = 700,
    seconds: int = 20,
) -> dict:
    """Read AX-exposed live meter labels and values from an already open analyzer.
    Generic Controls parameters are settings, not measurements, so this intentionally
    reads the editor view and reports only observable values. It refuses a shifted window
    identity and does not invent LUFS/peak values when a plugin does not expose them."""
    process = require_logic()
    try:
        identity = read_plugin_identity(process, int(window_index))
    except ProbeError as exc:
        return {"ok": False, "error": f"could not identify meter window: {exc}"}
    if not identity_matches(identity, expected_plugin, expected_channel):
        return {
            "ok": False,
            "error": "meter window identity changed",
            "expected": {"plugin": expected_plugin, "channel": expected_channel},
            "observed": identity,
        }
    try:
        elements = walk_window(
            process,
            int(window_index),
            max(1, min(int(max_depth), 4)),
            budget=max(50, min(int(budget), 1200)),
            seconds=max(5, min(int(seconds), 45)),
        )
    except ProbeError as exc:
        return {"ok": False, "identity": identity, "error": str(exc)}
    terms = (
        "lufs",
        "loudness",
        "integrated",
        "momentary",
        "short-term",
        "short term",
        "true peak",
        "peak",
        "range",
        "rms",
        "dbtp",
        "dbfs",
        " lu",
        " db",
    )
    needle = contains.casefold().strip()
    readouts = []
    seen = set()
    for entry in elements:
        if entry["role"] not in (
            "AXStaticText",
            "AXValueIndicator",
            "AXTextField",
            "AXSlider",
        ):
            continue
        text_value = " | ".join(
            value for value in (entry["name"], entry["description"], entry["value"]) if value
        )
        folded = text_value.casefold()
        has_number = bool(re.search(r"[-+]?\d+(?:[.,]\d+)?", text_value))
        if needle:
            relevant = needle in folded and has_number
        else:
            relevant = has_number and any(term in folded for term in terms)
        if not relevant or (entry["path"], text_value) in seen:
            continue
        seen.add((entry["path"], text_value))
        readouts.append({**entry, "text": text_value})
    return {
        "ok": True,
        "identity": identity,
        "readout_count": len(readouts),
        "readouts": readouts,
        "measurement_available": bool(readouts),
        "note": "no live measurement was exposed through AX; use a bounce plus BS.1770"
        if not readouts
        else "values are raw plugin readouts and should be interpreted by label and unit",
    }


def select_open_logic_project(project_title: str, candidates: list[Path]) -> Path | None:
    """Select one project bundle by the exact Tracks-window title. Logic keeps many
    template and Live Loops bundles open, so shortest-path or first-path selection is
    unsafe. Ambiguous and missing matches intentionally resolve to None."""
    title = str(project_title or "").strip()
    if title.endswith(" - Tracks"):
        title = title[: -len(" - Tracks")].strip()
    folded = title.casefold()
    matches = {
        candidate
        for candidate in candidates
        if candidate.name.casefold() == folded or candidate.stem.casefold() == folded
    }
    return next(iter(matches)) if len(matches) == 1 else None


def find_open_logic_project() -> Path | None:
    process = logic_process()
    if process is None:
        return None
    tracks_titles = [
        window.get("title", "")
        for window in ax_windows().get("windows", [])
        if window.get("subrole") == "AXStandardWindow"
        and str(window.get("title", "")).endswith(" - Tracks")
    ]
    if len(tracks_titles) != 1:
        return None
    pid_result = subprocess.run(["pgrep", "-x", process], capture_output=True, text=True)
    if pid_result.returncode != 0 or not pid_result.stdout.split():
        return None
    listing = subprocess.run(
        ["lsof", "-p", pid_result.stdout.split()[0], "-Fn"],
        capture_output=True,
        text=True,
    )
    candidates = []
    for line in listing.stdout.splitlines():
        if not line.startswith("n"):
            continue
        match = re.search(r"^(.*?\.logicx)(/|$)", line[1:])
        if match:
            candidates.append(Path(match.group(1)))
    return select_open_logic_project(tracks_titles[0], candidates)


@tool
def mix_project_identity(expected_project_path: str = "") -> dict:
    """Read the open project bundle from Logic's file descriptors and optionally verify
    it against an exact absolute path. Audit and fix plans use this local preflight before
    any UI, solo, transport, bounce or parameter mutation."""
    expected = Path(expected_project_path).expanduser() if expected_project_path else None
    if expected is not None and not expected.is_absolute():
        return {
            "ok": False,
            "verified": False,
            "write_attempted": False,
            "error": "expected_project_path must be absolute",
        }
    observed = find_open_logic_project()
    matches = bool(
        observed
        and (
            expected is None
            or observed.resolve(strict=False) == expected.resolve(strict=False)
        )
    )
    result = {
        "ok": matches,
        "verified": matches,
        "write_attempted": False,
        "observed_project_path": str(observed) if observed else None,
        "expected_project_path": str(expected) if expected else None,
    }
    if observed is None:
        result["error"] = "no open Logic project bundle could be resolved"
    elif expected is not None and not matches:
        result["error"] = "open project identity does not match the plan"
    return result


def find_front_element(
    process: str,
    role: str,
    *,
    name: str = "",
    value: str = "",
    exclude_names: tuple[str, ...] = (),
    timeout: float = 20.0,
) -> dict:
    """Resolve one element in the front Logic window, retrying while sheets animate."""
    deadline = time.monotonic() + timeout
    last_matches: list[dict] = []
    while time.monotonic() < deadline:
        try:
            elements = walk_window(process, 1, 5, budget=500, seconds=12)
        except ProbeError:
            time.sleep(0.4)
            continue
        matches = [entry for entry in elements if entry["role"] == role]
        if name:
            matches = [entry for entry in matches if entry["name"] == name]
        if value:
            matches = [entry for entry in matches if entry["value"] == value]
        if exclude_names:
            excluded = {item.casefold() for item in exclude_names}
            matches = [
                entry
                for entry in matches
                if not any(
                    excluded_name in f'{entry["name"]} {entry["description"]}'.casefold()
                    for excluded_name in excluded
                )
            ]
        last_matches = matches
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.4)
    detail = [entry["path"] for entry in last_matches[:8]]
    raise ProbeError(
        f"expected one front-window {role} name={name!r} value={value!r}; "
        f"found {len(last_matches)} at {detail}"
    )


def press_front_button(process: str, name: str, timeout: float = 20.0) -> dict:
    target = find_front_element(process, "AXButton", name=name, timeout=timeout)
    reference = element_reference(target["path"], 1)
    osa(
        f'tell application "System Events" to tell process "{process}" to '
        f'perform action "AXPress" of {reference}',
        timeout=20,
    )
    return target


def front_window_titles() -> list[str]:
    try:
        return [str(item.get("title", "")) for item in ax_windows().get("windows", [])]
    except ProbeError:
        return []


def cancel_bounce_ui(process: str, bounce_fired: bool) -> None:
    """Best-effort cleanup. Cmd-period cancels rendering; Cancel closes preflight sheets."""
    if bounce_fired:
        try:
            send_key(process, "key", ".", ["cmd"])
        except ProbeError:
            pass
        return
    try:
        # Escape closes a nested Go to Folder sheet first, or the save/settings
        # sheet itself when no nested sheet is present.
        send_key(process, "code", 53, [])
        time.sleep(0.3)
    except ProbeError:
        pass
    for _ in range(2):
        try:
            press_front_button(process, "Cancel", timeout=2)
        except ProbeError:
            break
        time.sleep(0.3)


def find_staged_artifact(staging: Path, staged_name: str, min_mtime: float) -> Path | None:
    candidates = []
    for candidate in staging.glob(f"{staged_name}.*"):
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.parent.resolve(strict=False) != staging.resolve(strict=False)
            or stat.st_mtime < min_mtime
        ):
            continue
        candidates.append((stat.st_mtime, candidate))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else None


def prepare_safe_output_dir(path: Path) -> str | None:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_symlink():
        # `/tmp` is the OS-owned compatibility alias for `/private/tmp` on macOS.
        # Permit only this exact, stable system alias; arbitrary symlink ancestors
        # remain refused so a planned output cannot be redirected elsewhere.
        if existing == Path("/tmp") and existing.resolve() == Path("/private/tmp"):
            existing = existing.resolve()
        else:
            return "artifact_output_dir_unsafe"
    if not existing.is_dir():
        return "artifact_output_dir_unsafe"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"artifact_output_dir_create_failed: {exc}"
    if path.is_symlink() or not path.is_dir():
        return "artifact_output_dir_unsafe"
    return None


def move_staged_artifact_no_overwrite(staged: Path, final: Path) -> str | None:
    directory_error = prepare_safe_output_dir(final.parent)
    if directory_error:
        return directory_error
    try:
        source = staged.open("rb")
    except OSError as exc:
        return f"artifact_stage_unreadable: {exc}"
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(final.parent, directory_flags)
        destination_fd = os.open(final.name, create_flags, 0o644, dir_fd=directory_fd)
    except FileExistsError:
        source.close()
        if "directory_fd" in locals():
            os.close(directory_fd)
        return "artifact_already_exists"
    except OSError as exc:
        source.close()
        if "directory_fd" in locals():
            os.close(directory_fd)
        return f"artifact_move_failed: {exc}"
    try:
        with source, os.fdopen(destination_fd, "wb") as destination:
            shutil.copyfileobj(source, destination)
        staged.unlink()
    except OSError as exc:
        try:
            os.unlink(final.name, dir_fd=directory_fd)
        except OSError:
            pass
        return f"artifact_move_failed: {exc}"
    finally:
        os.close(directory_fd)
    return None


def run_accessible_bounce(target: Path, timeout_seconds: int, staging: Path | None = None) -> dict:
    """Drive Logic 12.3's AX-exposed Bounce sheets without screen coordinates."""
    process = require_logic()
    staging = (staging or (Path.home() / "Downloads")).expanduser()
    if staging.is_symlink() or not staging.is_dir():
        return {"success": False, "bounce_fired": False, "error": "staging_directory_unsafe"}
    staged_name = f"{target.stem}--logic-mcp-{uuid.uuid4().hex[:10]}"
    result: dict = {
        "success": False,
        "bounce_fired": False,
        "staging": str(staging),
        "staged_name": staged_name,
        "driver": "logic_12_3_accessibility",
    }
    try:
        opened = menu_click(["File", "Bounce", "Project or Section…"])
        if not opened.get("ok"):
            raise ProbeError(f"bounce settings did not open: {opened.get('error', opened)}")
        press_front_button(process, "OK", timeout=20)

        # Logic 12.3 exposes the standard save panel's filename AXTextField. The Search
        # field is excluded explicitly, then the exact value is read back before Bounce.
        filename = find_front_element(process, "AXTextField", name="Save As:", timeout=25)
        filename_ref = element_reference(filename["path"], 1)
        send_value(process, filename_ref, staged_name, numeric=False)
        if read_value(process, filename_ref) != staged_name:
            raise ProbeError("save panel filename readback did not match")

        # Go to Folder is reliable even when the save panel lives on an off-screen
        # display. Its text field accepts an absolute AX value, avoiding input-source
        # and keyboard-layout corruption of Cyrillic or punctuation.
        # Physical key code 5 is G on the hardware layout. `keystroke "g"` is
        # layout-dependent and failed to open this sheet on the Spanish system.
        send_key(process, "code", 5, ["cmd", "shift"])
        folder = find_front_element(
            process,
            "AXTextField",
            exclude_names=("Search", "Save As"),
            timeout=12,
        )
        folder_ref = element_reference(folder["path"], 1)
        send_value(process, folder_ref, str(staging.resolve()), numeric=False)
        if read_value(process, folder_ref) != str(staging.resolve()):
            raise ProbeError("Go to Folder path readback did not match")
        send_key(process, "code", 36, [])
        time.sleep(0.8)
        if read_value(process, filename_ref) != staged_name:
            raise ProbeError("save panel filename changed after folder navigation")
        where = find_front_element(process, "AXPopUpButton", name="Where:", timeout=12)
        if Path(where.get("value", "")).name.casefold() != staging.name.casefold():
            raise ProbeError(
                f"save panel location readback was {where.get('value')!r}, expected {staging.name!r}"
            )

        started_at = time.time()
        press_front_button(process, "Bounce", timeout=15)
        result["bounce_fired"] = True
        deadline = time.monotonic() + max(60, min(int(timeout_seconds), 3600))
        stable_path: Path | None = None
        stable_size = -1
        stable_reads = 0
        while time.monotonic() < deadline:
            candidate = find_staged_artifact(staging, staged_name, started_at - 2)
            if candidate is not None:
                try:
                    size = candidate.stat().st_size
                except OSError:
                    size = -1
                if candidate == stable_path and size > 0 and size == stable_size:
                    stable_reads += 1
                else:
                    stable_path, stable_size, stable_reads = candidate, size, 0
                bouncing = any("bouncing" in title.casefold() for title in front_window_titles())
                if stable_reads >= 2 and not bouncing:
                    final = target.with_suffix(candidate.suffix)
                    move_error = move_staged_artifact_no_overwrite(candidate, final)
                    if move_error:
                        return {**result, "error": move_error, "artifact": str(final)}
                    return {
                        **result,
                        "success": True,
                        "artifact": str(final),
                        "size_bytes": final.stat().st_size,
                    }
            time.sleep(1)
        raise ProbeError("bounce timed out before a stable artifact was verified")
    except ProbeError as exc:
        cancel_bounce_ui(process, bool(result["bounce_fired"]))
        return {**result, "error": str(exc)}


@tool
def mix_bounce_target(
    target_path: str,
    expected_project_path: str,
    confirmed: bool = False,
    timeout_seconds: int = 1800,
) -> dict:
    """Bounce the currently isolated target to a planned non-existing path. This is the
    measurement bridge used after a mature-server solo/isolation step. It refuses unless
    the exact open project matches and confirmed is true. Logic 12.3's AX-exposed save
    controls are used without screen coordinates; the artifact is copied with exclusive
    creation and is never overwritten."""
    target = Path(target_path).expanduser()
    expected = Path(expected_project_path).expanduser()
    if not target.is_absolute() or not expected.is_absolute():
        return {"ok": False, "error": "target_path and expected_project_path must be absolute"}
    current = find_open_logic_project()
    preview = {
        "target_path": str(target),
        "expected_project_path": str(expected),
        "observed_project_path": str(current) if current else None,
    }
    if current is None or current.resolve() != expected.resolve():
        return {
            "ok": False,
            "write_attempted": False,
            **preview,
            "error": "open project identity does not match the bounce plan",
        }
    if target.exists() or any(target.parent.glob(f"{target.stem}.*")):
        return {
            "ok": False,
            "write_attempted": False,
            **preview,
            "error": "planned artifact already exists; refusing to overwrite",
        }
    if not confirmed:
        return {
            "ok": True,
            "dry_run": True,
            "confirmation_required": True,
            **preview,
            "driver": "logic_12_3_accessibility",
            "note": "no UI action or file write occurred",
        }
    result = run_accessible_bounce(target, timeout_seconds)
    artifact = Path(str(result.get("artifact") or ""))
    verified = bool(
        result.get("success")
        and artifact.is_file()
        and artifact.stat().st_size > 0
        and artifact.parent.resolve(strict=False) == target.parent.resolve(strict=False)
        and artifact.stem == target.stem
    )
    return {
        "ok": verified,
        "verified": verified,
        **result,
        **({"error": "bounce result did not verify the planned artifact"} if not verified and not result.get("error") else {}),
    }


@tool
def mix_inventory(tracks=None, mixer=None, ax_channels=None) -> dict:
    """Merge mature-server track/mixer resources and mixer_survey output into one typed
    inventory. Track indices and visible-surface positions are never assumed equivalent;
    uncorroborated rows remain separate and are reported as binding warnings."""
    return mix_audit.normalise_inventory(tracks, mixer, ax_channels)


@tool
def mix_audit_plan(
    tracks,
    mixer,
    ax_channels,
    scope: str,
    project_path: str,
    output_root: str,
    selector: str = "",
    measurement: str = "bounce_bs1770",
    start_position: str = "1.1.1.1",
    target_name: str = "streaming",
) -> dict:
    """Create a serializable dry-run plan for track, group, aux, bus, master or all.
    Pass the live logic://tracks and logic://mixer payloads plus mixer_survey output. The
    plan contains exact cross-server dispatches and mandatory restore steps, but performs
    no solo, transport, bounce or plugin write."""
    inventory = mix_audit.normalise_inventory(tracks, mixer, ax_channels)
    plan = mix_audit.build_audit_plan(
        inventory,
        scope,
        selector,
        project_path,
        output_root,
        measurement,
        start_position,
        target_name,
    )
    if "error" not in plan:
        AUDIT_PLANS[plan["plan_id"]] = {
            "plan": plan,
            "confirmed": False,
            "cancelled": False,
            "current": 0,
            "results": {},
        }
    return plan


@tool
def mix_fix_plan(
    tracks,
    mixer,
    ax_channels,
    fixes: list,
    project_path: str,
) -> dict:
    """Create a confirmation-gated fix plan from explicit target/plugin/parameter/value
    requests. Paths are resolved again at apply time and every write is identity-bound and
    read back. Run a new mix audit afterwards and compare with mix_before_after."""
    inventory = mix_audit.normalise_inventory(tracks, mixer, ax_channels)
    plan = mix_audit.build_fix_plan(inventory, fixes, project_path)
    if "error" not in plan:
        AUDIT_PLANS[plan["plan_id"]] = {
            "plan": plan,
            "confirmed": False,
            "cancelled": False,
            "current": 0,
            "results": {},
        }
    return plan


def audit_next_step(run: dict) -> dict | None:
    steps = run["plan"]["steps"]
    while run["current"] < len(steps) and steps[run["current"]]["status"] in (
        "completed",
        "skipped",
    ):
        run["current"] += 1
    return steps[run["current"]] if run["current"] < len(steps) else None


def materialise_audit_step(run: dict, step: dict) -> dict:
    result = json.loads(json.dumps(step))
    source = result.get("arguments", {}).pop("path_from", None)
    if source:
        step_id, _, field = source.partition(".")
        observed = run["results"].get(step_id, {})
        path = observed.get(field)
        if path:
            result["arguments"]["path"] = path
        else:
            result["blocked"] = f"{source} is not available"
    for argument, source in result.pop("arguments_from", {}).items():
        step_id, _, field = source.partition(".")
        observed = run["results"].get(step_id, {})
        value = observed if field == "$" else observed.get(field)
        if value is not None and value != "":
            result["arguments"][argument] = value
        else:
            result["blocked"] = f"{source} is not available"
    return result


@tool
def mix_audit_start(plan_id: str, confirm: bool = False) -> dict:
    """Start a stored audit plan. Confirmation unlocks solo/transport/bounce dispatches,
    not parameter writes. The client executes the returned next_step and reports it with
    mix_audit_advance; this server preserves ordering and restoration obligations."""
    run = AUDIT_PLANS.get(plan_id)
    if run is None:
        return {"error": f"unknown plan_id {plan_id!r}", "known": sorted(AUDIT_PLANS)}
    if not confirm:
        return {
            "plan_id": plan_id,
            "confirmation_required": True,
            "targets": run["plan"].get("target_count"),
            "fixes": run["plan"].get("fix_count"),
            "mutating_steps": sum(
                1
                for step in run["plan"]["steps"]
                if step.get("mutates_logic")
                or step.get("mutates_ui")
                or step.get("writes_parameter")
            ),
            "reason": "review the dry-run plan, save the project, then re-call with confirm true",
        }
    run["confirmed"] = True
    step = audit_next_step(run)
    return {
        "plan_id": plan_id,
        "started": True,
        "next_step": materialise_audit_step(run, step) if step else None,
    }


@tool
def mix_audit_advance(plan_id: str, step_id: str, result: dict) -> dict:
    """Record one externally dispatched step and return the next ordered step. A failed
    step skips forward to the next always-run restoration step instead of cascading."""
    run = AUDIT_PLANS.get(plan_id)
    if run is None:
        return {"error": f"unknown plan_id {plan_id!r}"}
    if not run["confirmed"]:
        return {"error": "plan has not been confirmed"}
    step = audit_next_step(run)
    if step is None:
        return {"plan_id": plan_id, "complete": True}
    if step["step_id"] != step_id:
        return {
            "error": "out-of-order audit result",
            "expected_step_id": step["step_id"],
            "received_step_id": step_id,
        }
    dispatch_incomplete = step.get("requires_dispatch_execution") and not (
        result.get("executed") is True and result.get("verified") is True
    )
    verification_incomplete = (
        step.get("requires_verified_result") and result.get("verified") is not True
    )
    failed = (
        bool(result.get("error"))
        or result.get("ok") is False
        or dispatch_incomplete
        or verification_incomplete
    )
    if dispatch_incomplete:
        result = {
            **result,
            "error": "child dispatches were not reported as executed and verified",
        }
    elif verification_incomplete:
        result = {
            **result,
            "error": "step did not return verified true",
        }
    step["status"] = "failed" if failed else "completed"
    run["results"][step_id] = result
    run["current"] += 1
    if not failed and step.get("expand_plugin_steps"):
        target = next(
            (
                item
                for item in run["plan"].get("targets", [])
                if item.get("audit_id") == step.get("target_id")
            ),
            None,
        )
        inserts = result.get("inserts") if isinstance(result.get("inserts"), list) else []
        if target is not None and inserts:
            target["inserts"] = inserts
            target["strip_path"] = result.get("path") or target.get("strip_path")
            expanded = mix_audit.build_plugin_inspection_steps(
                step.get("plugin_prefix") or step["step_id"], target, inserts
            )
            run["plan"]["steps"][run["current"] : run["current"]] = expanded
            meter_placeholder = next(
                (
                    pending
                    for pending in run["plan"]["steps"]
                    if pending.get("expand_meter_steps")
                    and pending.get("target_id") == step.get("target_id")
                ),
                None,
            )
            if meter_placeholder is not None:
                meter_index = run["plan"]["steps"].index(meter_placeholder)
                meter_steps = mix_audit.build_meter_measurement_steps(
                    meter_placeholder.get("meter_prefix") or step["step_id"],
                    target,
                    inserts,
                )
                run["plan"]["steps"][meter_index : meter_index + 1] = meter_steps
    if failed and step["phase"] != "restore" and not step.get("continue_on_failure"):
        remaining = run["plan"]["steps"][run["current"] :]
        if step["phase"] in ("preflight", "capture"):
            for pending in remaining:
                pending["status"] = "skipped"
            run["current"] = len(run["plan"]["steps"])
            destination = None
        else:
            destination = next(
                (
                    pending
                    for pending in remaining
                    if pending.get("always_run")
                    and pending.get("target_id") == step.get("target_id")
                ),
                None,
            )
            if destination is None:
                destination = next(
                    (pending for pending in reversed(remaining) if pending.get("always_run")),
                    None,
                )
        if destination is not None:
            for pending in remaining:
                if pending is destination:
                    run["current"] = run["plan"]["steps"].index(pending)
                    break
                pending["status"] = "skipped"
    next_step = audit_next_step(run)
    return {
        "plan_id": plan_id,
        "accepted": step_id,
        "failed": failed,
        "complete": next_step is None,
        "next_step": materialise_audit_step(run, next_step) if next_step else None,
    }


@tool
def mix_audit_cancel(plan_id: str) -> dict:
    """Cancel remaining work. Preserve cleanup only for an editor that was actually
    opened, and preserve final state restoration only after isolation/transport/bounce
    began. Cancelling a dry preflight returns no mutating restore work."""
    run = AUDIT_PLANS.get(plan_id)
    if run is None:
        return {"error": f"unknown plan_id {plan_id!r}"}
    run["cancelled"] = True
    steps = run["plan"]["steps"]
    keep_ids = set()
    for opened in steps[: run["current"]]:
        if opened.get("operation") != "plugin_open_insert":
            continue
        opened_result = run["results"].get(opened["step_id"], {})
        may_be_open = opened["status"] == "completed" or (
            opened["status"] == "failed" and opened_result.get("opened") is True
        )
        if not may_be_open or not opened["step_id"].endswith("-open"):
            continue
        prefix = opened["step_id"][: -len("-open")]
        close_id = f"{prefix}-close"
        close_step = next((item for item in steps if item["step_id"] == close_id), None)
        if close_step is None or close_step["status"] == "completed":
            continue
        restore_view_id = f"{prefix}-restore-view"
        if any(item["step_id"] == restore_view_id for item in steps):
            keep_ids.add(restore_view_id)
        keep_ids.add(close_id)
    state_was_mutated = any(
        step["status"] in ("completed", "failed")
        and (step.get("mutates_logic") or step.get("operation") == "mix_bounce_target")
        for step in steps[: run["current"]]
    )
    if state_was_mutated:
        final_restore = next(
            (step for step in reversed(steps) if step["step_id"] == "final-restore"),
            None,
        )
        if final_restore is not None:
            keep_ids.add(final_restore["step_id"])
    for step in steps[run["current"] :]:
        if step["status"] == "pending" and step["step_id"] not in keep_ids:
            step["status"] = "skipped"
    restore = next(
        (
            step
            for step in steps[run["current"] :]
            if step["status"] == "pending" and step["step_id"] in keep_ids
        ),
        None,
    )
    run["current"] = steps.index(restore) if restore is not None else len(steps)
    return {
        "plan_id": plan_id,
        "cancelled": True,
        "state_restore_required": state_was_mutated,
        "cleanup_steps": sorted(keep_ids),
        "complete": restore is None,
        "next_step": materialise_audit_step(run, restore) if restore else None,
    }


@tool
def mix_audit_status(plan_id: str) -> dict:
    run = AUDIT_PLANS.get(plan_id)
    if run is None:
        return {"error": f"unknown plan_id {plan_id!r}"}
    counts = {}
    for step in run["plan"]["steps"]:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    step = audit_next_step(run)
    return {
        "plan_id": plan_id,
        "confirmed": run["confirmed"],
        "cancelled": run["cancelled"],
        "step_status": counts,
        "next_step": materialise_audit_step(run, step) if step else None,
        "complete": step is None,
    }


@tool
def mix_audit_results(
    plan_id: str,
    target_id: str = "",
    phase: str = "",
    offset: int = 0,
    limit: int = 50,
    include_payload: bool = True,
) -> dict:
    """Read completed/failed audit results in plan order. Filter by target_id or phase
    and page by step so large all-mixer runs stay bounded. include_payload=false returns
    compact evidence summaries; true returns the exact tool result, including plugin
    parameter pages, meter readouts, loudness analysis and verified fix write-back."""
    run = AUDIT_PLANS.get(plan_id)
    if run is None:
        return {"error": f"unknown plan_id {plan_id!r}"}
    wanted_target = str(target_id or "").casefold()
    wanted_phase = str(phase or "").casefold()
    records = []
    for step in run["plan"]["steps"]:
        if step["status"] not in ("completed", "failed"):
            continue
        if wanted_target and str(step.get("target_id") or "").casefold() != wanted_target:
            continue
        if wanted_phase and str(step.get("phase") or "").casefold() != wanted_phase:
            continue
        payload = run["results"].get(step["step_id"], {})
        summary = {
            key: payload.get(key)
            for key in (
                "ok",
                "verified",
                "error",
                "artifact",
                "parameter_count",
                "readout_count",
                "measurement_available",
                "integrated_lufs",
                "true_peak_dbtp",
                "write_attempted",
                "before",
                "after",
            )
            if key in payload
        }
        record = {
            "step_id": step["step_id"],
            "target_id": step.get("target_id"),
            "phase": step.get("phase"),
            "server": step.get("server"),
            "operation": step.get("operation"),
            "status": step["status"],
            "summary": summary,
        }
        if include_payload:
            record["result"] = payload
        records.append(record)
    start = max(0, int(offset))
    page_limit = max(1, min(int(limit), 200))
    page = records[start : start + page_limit]
    return {
        "plan_id": plan_id,
        "complete": audit_next_step(run) is None,
        "matched": len(records),
        "offset": start,
        "returned": len(page),
        "next_offset": start + len(page) if start + len(page) < len(records) else None,
        "results": page,
    }


@tool
def mix_audit_review(
    measurements: list,
    integrated_lufs: float = -14.0,
    tolerance_lu: float = 1.0,
    true_peak_max: float = -1.0,
) -> dict:
    """Evaluate per-target measurements and generate non-writing fix recommendations."""
    return mix_audit.review_measurements(
        measurements, integrated_lufs, tolerance_lu, true_peak_max
    )


@tool
def mix_before_after(before: list, after: list) -> dict:
    """Compare repeated BS.1770 measurements after a confirmed fix plan."""
    return mix_audit.compare_before_after(before, after)


@tool
def mix_restore_dispatch(initial_state: dict, ax_state: list = None) -> dict:
    """Convert captured track/transport resources into exact verified restore intents."""
    return mix_audit.build_restore_dispatch(initial_state, ax_state)


@tool
def mix_isolation_dispatch(initial_state: dict, target: dict, ax_state: list = None) -> dict:
    """Build exclusive-solo child dispatches for one audit target or the full master."""
    return mix_audit.build_isolation_dispatch(initial_state, target, ax_state)


@tool
def health() -> dict:
    """Report what this server can currently do and which capability is blocking the
    rest."""
    name = logic_process()
    ax = accessibility_status(name)
    mature_version = logic_pro_mcp_version()
    blocking = None
    if name is None:
        blocking = "Logic is not running"
    elif not ax["granted"]:
        blocking = f"Accessibility is unavailable: {ax.get('reason', 'unknown reason')}"
    return {
        "platform": sys.platform,
        "logic_process": name,
        "accessibility": ax["granted"],
        "accessibility_detail": ax,
        "auval": shutil.which("auval") or "not found",
        "logic_pro_mcp_version": mature_version,
        "dispatch_contract_version": MATURE_DISPATCH_CONTRACT_VERSION,
        "dispatch_contract_matches": mature_version == MATURE_DISPATCH_CONTRACT_VERSION,
        "settings_roots_present": [str(r) for r in SETTINGS_ROOTS if r.is_dir()],
        "tools_defined": len(TOOLS),
        "plans_held": sorted(PLANS),
        "blocking": blocking,
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
    mature_version = logic_pro_mcp_version()
    rows.append(
        (
            "ok" if mature_version == MATURE_DISPATCH_CONTRACT_VERSION else "warn",
            "LogicProMCP contract",
            f"installed={mature_version or 'missing'} expected={MATURE_DISPATCH_CONTRACT_VERSION}",
        )
    )
    name = logic_process()
    rows.append(("ok" if name else "warn", "logic", name or "not running"))
    if name:
        ax = accessibility_status(name)
        rows.append(
            (
                "ok" if ax["granted"] else "warn",
                "accessibility",
                f"{ax.get('window_count', 0)} windows"
                if ax["granted"]
                else ax.get("reason", "denied"),
            )
        )
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
