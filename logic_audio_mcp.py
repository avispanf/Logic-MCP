from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import plistlib
import re
import shutil
import subprocess
import time


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
            "no supported MCP server class found. Install the SDK with "
            "'pip install mcp'. Tried mcp.server.fastmcp.FastMCP (SDK v1) "
            "and mcp.server.mcpserver.MCPServer (SDK v2)."
        ) from exc


def sdk_info() -> dict:
    try:
        import mcp
    except ImportError:
        return {"installed": False}
    flavour = "unknown"
    try:
        __import__("mcp.server.fastmcp")
        flavour = "v1_fastmcp"
    except ImportError:
        try:
            __import__("mcp.server.mcpserver")
            flavour = "v2_mcpserver"
        except ImportError:
            pass
    return {
        "installed": True,
        "version": getattr(mcp, "__version__", "unknown"),
        "api": flavour,
    }


META_TRACK_NAME = 0x03
META_INSTRUMENT = 0x04
META_MARKER = 0x06
META_CUE = 0x07
META_END_OF_TRACK = 0x2F
META_TEMPO = 0x51
META_TIME_SIGNATURE = 0x58
META_KEY_SIGNATURE = 0x59

CHANNEL_EVENT_LENGTH = {
    0x80: 2,
    0x90: 2,
    0xA0: 2,
    0xB0: 2,
    0xC0: 1,
    0xD0: 1,
    0xE0: 2,
}


class SMFError(ValueError):
    pass


@dataclass
class Note:
    tick: int
    duration: int
    pitch: int
    velocity: int
    channel: int


@dataclass
class RawTrack:
    index: int
    name: str = ""
    notes: list[Note] = field(default_factory=list)
    markers: list[tuple[int, str]] = field(default_factory=list)
    tempos: list[tuple[int, int]] = field(default_factory=list)
    signatures: list[tuple[int, int, int]] = field(default_factory=list)
    end_tick: int = 0


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise SMFError("unexpected end of data")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise SMFError("unexpected end of data")
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def uint(self, count: int) -> int:
        return int.from_bytes(self.take(count), "big")

    def varlen(self) -> int:
        value = 0
        for _ in range(4):
            byte = self.byte()
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value
        raise SMFError("variable length quantity too long")


def parse_track(data: bytes, index: int) -> RawTrack:
    reader = Reader(data)
    track = RawTrack(index=index)
    tick = 0
    status = 0
    pending: dict[tuple[int, int], list[tuple[int, int]]] = {}
    while reader.pos < len(data):
        tick += reader.varlen()
        byte = reader.byte()
        if byte & 0x80:
            status = byte
        else:
            reader.pos -= 1
            if not status:
                raise SMFError("running status without prior status byte")
        if status == 0xFF:
            meta_type = reader.byte()
            length = reader.varlen()
            payload = reader.take(length)
            if meta_type in (META_TRACK_NAME, META_INSTRUMENT) and not track.name:
                track.name = payload.decode("utf-8", "replace").strip()
            elif meta_type in (META_MARKER, META_CUE):
                track.markers.append((tick, payload.decode("utf-8", "replace").strip()))
            elif meta_type == META_TEMPO and length == 3:
                track.tempos.append((tick, int.from_bytes(payload, "big")))
            elif meta_type == META_TIME_SIGNATURE and length >= 2:
                track.signatures.append((tick, payload[0], 1 << payload[1]))
            elif meta_type == META_END_OF_TRACK:
                track.end_tick = tick
                break
            continue
        if status in (0xF0, 0xF7):
            reader.take(reader.varlen())
            continue
        kind = status & 0xF0
        channel = status & 0x0F
        length = CHANNEL_EVENT_LENGTH.get(kind)
        if length is None:
            raise SMFError(f"unsupported status byte {status:#04x}")
        payload = reader.take(length)
        if kind == 0x90 and payload[1] > 0:
            pending.setdefault((channel, payload[0]), []).append((tick, payload[1]))
        elif kind == 0x80 or (kind == 0x90 and payload[1] == 0):
            stack = pending.get((channel, payload[0]))
            if stack:
                start, velocity = stack.pop(0)
                track.notes.append(
                    Note(start, max(0, tick - start), payload[0], velocity, channel)
                )
    for (channel, pitch), stack in pending.items():
        for start, velocity in stack:
            track.notes.append(Note(start, 0, pitch, velocity, channel))
    track.notes.sort(key=lambda n: (n.tick, n.pitch))
    track.end_tick = max(track.end_tick, max((n.tick + n.duration for n in track.notes), default=0))
    return track


def parse_file(path: Path) -> tuple[int, int, list[RawTrack]]:
    data = path.read_bytes()
    reader = Reader(data)
    if reader.take(4) != b"MThd":
        raise SMFError("not a standard MIDI file")
    header_length = reader.uint(4)
    header = reader.take(header_length)
    smf_format = int.from_bytes(header[0:2], "big")
    division = int.from_bytes(header[4:6], "big")
    if division & 0x8000:
        raise SMFError("SMPTE time division is not supported")
    tracks = []
    index = 0
    while reader.pos < len(data):
        tag = reader.take(4)
        length = reader.uint(4)
        chunk = reader.take(length)
        if tag != b"MTrk":
            continue
        tracks.append(parse_track(chunk, index))
        index += 1
    return smf_format, division, tracks


class Timeline:
    def __init__(self, ppq: int, tempos: list[tuple[int, int]], signatures: list[tuple[int, int, int]]):
        self.ppq = ppq
        self.tempos = sorted(tempos) or [(0, 500000)]
        if self.tempos[0][0] != 0:
            self.tempos.insert(0, (0, 500000))
        self.signatures = sorted(signatures) or [(0, 4, 4)]
        if self.signatures[0][0] != 0:
            self.signatures.insert(0, (0, 4, 4))
        self._bars = self._build_bars()

    def _build_bars(self) -> list[tuple[int, int, int, int]]:
        table = []
        for i, (tick, numerator, denominator) in enumerate(self.signatures):
            bar_ticks = int(numerator * self.ppq * 4 / denominator)
            if i == 0:
                start_bar = 1
            else:
                prev_tick, prev_bar, prev_len, _ = table[-1]
                start_bar = prev_bar + (tick - prev_tick) // max(prev_len, 1)
            table.append((tick, start_bar, bar_ticks, numerator))
        return table

    def seconds(self, tick: int) -> float:
        total = 0.0
        for i, (start, micros) in enumerate(self.tempos):
            if start >= tick:
                break
            end = self.tempos[i + 1][0] if i + 1 < len(self.tempos) else tick
            end = min(end, tick)
            total += (end - start) * micros / (self.ppq * 1_000_000)
        return round(total, 4)

    def bar_beat(self, tick: int) -> str:
        entry = self._bars[0]
        for candidate in self._bars:
            if candidate[0] <= tick:
                entry = candidate
            else:
                break
        base_tick, base_bar, bar_ticks, numerator = entry
        offset = tick - base_tick
        bar = base_bar + offset // max(bar_ticks, 1)
        within = offset % max(bar_ticks, 1)
        beat_ticks = max(bar_ticks // max(numerator, 1), 1)
        beat = within // beat_ticks + 1
        sub = within % beat_ticks
        return f"{bar}.{beat}.{sub}"

    def bar_number(self, tick: int) -> int:
        return int(self.bar_beat(tick).split(".")[0])

    def bpm_at(self, tick: int) -> float:
        micros = self.tempos[0][1]
        for start, value in self.tempos:
            if start <= tick:
                micros = value
            else:
                break
        return round(60_000_000 / micros, 3)


def cluster_regions(notes: list[Note], gap_ticks: int) -> list[tuple[int, int, int]]:
    if not notes:
        return []
    regions = []
    start = notes[0].tick
    end = notes[0].tick + notes[0].duration
    count = 0
    for note in notes:
        if note.tick - end > gap_ticks:
            regions.append((start, end, count))
            start = note.tick
            end = note.tick + note.duration
            count = 0
        end = max(end, note.tick + note.duration)
        count += 1
    regions.append((start, end, count))
    return regions


def snapshot(path: Path, region_gap_bars: float = 1.0) -> dict:
    smf_format, ppq, tracks = parse_file(path)
    tempos = [t for track in tracks for t in track.tempos]
    signatures = [s for track in tracks for s in track.signatures]
    timeline = Timeline(ppq, tempos, signatures)
    end_tick = max((track.end_tick for track in tracks), default=0)
    gap_ticks = int(region_gap_bars * ppq * 4)

    markers = []
    for track in tracks:
        for tick, name in track.markers:
            markers.append(
                {
                    "position": timeline.bar_beat(tick),
                    "bar": timeline.bar_number(tick),
                    "seconds": timeline.seconds(tick),
                    "name": name,
                }
            )
    markers.sort(key=lambda m: m["seconds"])

    track_report = []
    for track in tracks:
        if not track.notes and not track.name:
            continue
        entry: dict = {
            "index": track.index,
            "name": track.name or f"Track {track.index}",
            "note_count": len(track.notes),
        }
        if track.notes:
            pitches = [n.pitch for n in track.notes]
            velocities = [n.velocity for n in track.notes]
            regions = cluster_regions(track.notes, gap_ticks)
            entry.update(
                {
                    "first": timeline.bar_beat(track.notes[0].tick),
                    "last": timeline.bar_beat(track.end_tick),
                    "pitch_range": [min(pitches), max(pitches)],
                    "velocity_range": [min(velocities), max(velocities)],
                    "channels": sorted({n.channel + 1 for n in track.notes}),
                    "region_count": len(regions),
                    "regions": [
                        {
                            "start": timeline.bar_beat(s),
                            "end": timeline.bar_beat(e),
                            "quarters": round((e - s) / ppq, 3),
                            "seconds": round(timeline.seconds(e) - timeline.seconds(s), 3),
                            "notes": c,
                        }
                        for s, e, c in regions[:64]
                    ],
                }
            )
        track_report.append(entry)

    return {
        "file": str(path),
        "format": smf_format,
        "ticks_per_quarter": ppq,
        "length_bars": timeline.bar_number(end_tick),
        "length_seconds": timeline.seconds(end_tick),
        "tempo_map": [
            {
                "position": timeline.bar_beat(tick),
                "seconds": timeline.seconds(tick),
                "bpm": round(60_000_000 / micros, 3),
            }
            for tick, micros in timeline.tempos
        ],
        "time_signatures": [
            {"position": timeline.bar_beat(tick), "signature": f"{n}/{d}"}
            for tick, n, d in timeline.signatures
        ],
        "markers": markers,
        "track_count": len(track_report),
        "tracks": track_report,
    }


AUDIO_SUFFIXES = {".wav", ".aif", ".aiff", ".caf", ".flac", ".m4a", ".mp3"}

RANGE_MINIMUM_SECONDS = 30.0

TARGETS = {
    "game_pc_mix": {
        "integrated_lufs": -16.0,
        "integrated_tolerance": 2.0,
        "true_peak_max": -1.0,
        "range_min": 13.0,
        "range_max": 15.0,
    },
    "game_pc_asset": {
        "integrated_lufs": -16.0,
        "integrated_tolerance": 2.0,
        "true_peak_max": -1.0,
        "range_min": None,
        "range_max": None,
    },
    "streaming": {
        "integrated_lufs": -14.0,
        "integrated_tolerance": 1.0,
        "true_peak_max": -1.0,
        "range_min": None,
        "range_max": None,
    },
    "broadcast_ebu": {
        "integrated_lufs": -23.0,
        "integrated_tolerance": 0.5,
        "true_peak_max": -1.0,
        "range_min": None,
        "range_max": None,
    },
}

SUMMARY_PATTERNS = {
    "integrated_lufs": r"I:\s*(-?[\d.]+)\s*LUFS",
    "threshold_lufs": r"Threshold:\s*(-?[\d.]+)\s*LUFS",
    "range_lu": r"LRA:\s*(-?[\d.]+)\s*LU",
    "range_low_lufs": r"LRA low:\s*(-?[\d.]+)\s*LUFS",
    "range_high_lufs": r"LRA high:\s*(-?[\d.]+)\s*LUFS",
    "true_peak_dbtp": r"Peak:\s*(-?[\d.]+)\s*dBFS",
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def duration_seconds(path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError:
        return None


def measure(path: Path) -> dict:
    if not path.exists():
        return {"file": str(path), "error": "file not found"}
    if not ffmpeg_available():
        return {"file": str(path), "error": "ffmpeg not on PATH"}
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostats",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true:framelog=quiet",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    text = proc.stderr
    result: dict = {"file": str(path), "name": path.name}
    tail = text[text.rfind("Summary:") :] if "Summary:" in text else text
    for key, pattern in SUMMARY_PATTERNS.items():
        match = re.search(pattern, tail)
        if match:
            result[key] = float(match.group(1))
    seconds = duration_seconds(path)
    if seconds is not None:
        result["duration_seconds"] = seconds
    if "integrated_lufs" not in result:
        result["error"] = "ebur128 produced no summary"
    return result


def evaluate(measurement: dict, target: dict) -> dict:
    if "error" in measurement:
        return {**measurement, "verdict": "error"}
    problems = []
    integrated = measurement.get("integrated_lufs")
    peak = measurement.get("true_peak_dbtp")
    lra = measurement.get("range_lu")
    goal = target["integrated_lufs"]
    tolerance = target["integrated_tolerance"]
    if integrated is not None:
        deviation = round(integrated - goal, 2)
        measurement["deviation_lu"] = deviation
        if abs(deviation) > tolerance:
            problems.append(f"integrated {integrated} LUFS is {deviation:+} LU off {goal}")
    if peak is not None and peak > target["true_peak_max"]:
        problems.append(f"true peak {peak} dBTP exceeds {target['true_peak_max']}")
    low, high = target.get("range_min"), target.get("range_max")
    duration = measurement.get("duration_seconds")
    range_applicable = (low is not None or high is not None) and (
        duration is None or duration >= RANGE_MINIMUM_SECONDS
    )
    if lra is not None and range_applicable:
        if low is not None and lra < low:
            problems.append(f"loudness range {lra} LU below {low}")
        if high is not None and lra > high:
            problems.append(f"loudness range {lra} LU above {high}")
    elif (low is not None or high is not None) and not range_applicable:
        measurement["range_check"] = (
            f"skipped, material shorter than {RANGE_MINIMUM_SECONDS:.0f}s "
            "so loudness range is not meaningful"
        )
    return {**measurement, "verdict": "pass" if not problems else "fail", "problems": problems}


def audit(folder: Path, target_name: str = "game_pc_asset", limit: int = 200) -> dict:
    if target_name not in TARGETS:
        return {"error": f"unknown target, expected one of {sorted(TARGETS)}"}
    if not folder.is_dir():
        return {"error": f"not a directory: {folder}"}
    target = TARGETS[target_name]
    files = sorted(
        (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    if not files:
        return {"folder": str(folder), "target": target_name, "files": [], "note": "no audio found"}
    results = [evaluate(measure(path), target) for path in files]
    passed = sum(1 for r in results if r["verdict"] == "pass")
    failed = [r for r in results if r["verdict"] == "fail"]
    values = [r["integrated_lufs"] for r in results if "integrated_lufs" in r]
    return {
        "folder": str(folder),
        "target": target_name,
        "target_spec": target,
        "checked": len(results),
        "passed": passed,
        "failed": len(failed),
        "errors": sum(1 for r in results if r["verdict"] == "error"),
        "spread_lu": round(max(values) - min(values), 2) if len(values) > 1 else 0.0,
        "results": results,
    }


TOKEN_PATTERN = re.compile(r"\{(\w+)(?::(\d+)d)?\}")


def compile_convention(pattern: str, vocabularies: dict | None = None) -> re.Pattern:
    vocabularies = vocabularies or {}
    parts = []
    cursor = 0
    for match in TOKEN_PATTERN.finditer(pattern):
        parts.append(re.escape(pattern[cursor : match.start()]))
        name, digits = match.group(1), match.group(2)
        if digits:
            body = rf"\d{{{digits}}}"
        elif name in vocabularies:
            body = "|".join(re.escape(v) for v in vocabularies[name])
        else:
            body = r"[A-Za-z0-9]+"
        parts.append(f"(?P<{name}>{body})")
        cursor = match.end()
    parts.append(re.escape(pattern[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


def check_folder(
    folder: Path,
    pattern: str,
    vocabularies: dict | None = None,
    limit: int = 500,
) -> dict:
    if not folder.is_dir():
        return {"error": f"not a directory: {folder}"}
    try:
        matcher = compile_convention(pattern, vocabularies)
    except re.error as exc:
        return {"error": f"invalid convention: {exc}"}
    files = sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    )[:limit]
    conforming = []
    violations = []
    fields: dict[str, set] = {}
    for path in files:
        match = matcher.match(path.stem)
        if match:
            captured = match.groupdict()
            conforming.append({"name": path.name, "fields": captured})
            for key, value in captured.items():
                fields.setdefault(key, set()).add(value)
        else:
            violations.append({"name": path.name, "path": str(path)})
    duplicates: dict[str, list[str]] = {}
    seen: dict[str, list[str]] = {}
    for path in files:
        seen.setdefault(path.name.lower(), []).append(str(path))
    for name, paths in seen.items():
        if len(paths) > 1:
            duplicates[name] = paths
    return {
        "folder": str(folder),
        "convention": pattern,
        "checked": len(files),
        "conforming": len(conforming),
        "violations": violations,
        "duplicate_names": duplicates,
        "observed_values": {k: sorted(v) for k, v in sorted(fields.items())},
    }


def diff_manifest(folder: Path, manifest_path: Path) -> dict:
    if not folder.is_dir():
        return {"error": f"not a directory: {folder}"}
    if not manifest_path.exists():
        return {"error": f"manifest not found: {manifest_path}"}
    raw = manifest_path.read_text(encoding="utf-8")
    if manifest_path.suffix.lower() == ".json":
        parsed = json.loads(raw)
        expected = parsed if isinstance(parsed, list) else parsed.get("files", [])
    else:
        expected = [line.strip() for line in raw.splitlines() if line.strip()]
    expected_set = {Path(name).name for name in expected}
    present = {
        p.name for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    }
    return {
        "folder": str(folder),
        "manifest": str(manifest_path),
        "expected": len(expected_set),
        "present": len(present),
        "missing": sorted(expected_set - present),
        "unexpected": sorted(present - expected_set),
        "matched": len(expected_set & present),
    }


BUNDLE_FOLDERS = [
    "Audio Files",
    "Bounces",
    "Stems",
    "Freeze Files",
    "Movie Files",
    "Sampler Instruments",
    "Ultrabeat Samples",
    "Impulse Responses",
    "Project File Backups",
    "Undo Data",
]

INTERESTING_KEYS = [
    "BeatsPerMinute",
    "SampleRate",
    "NumberOfTracks",
    "SongSignatureNumerator",
    "SongSignatureDenominator",
    "SongKey",
    "SignatureKey",
    "SongGenderKey",
    "FrameRateIndex",
    "HasARAPlugins",
    "HasGrid",
    "isTimeCodeBased",
    "SurroundFormatIndex",
    "SurroundModeIndex",
    "Version",
]

KEY_NAMES = [
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
]


def derive_key(metadata: dict) -> str:
    root = metadata.get("SongKey")
    quality = metadata.get("SongGenderKey")
    if isinstance(root, int) and 0 <= root < len(KEY_NAMES):
        root = KEY_NAMES[root]
    elif not isinstance(root, str):
        return ""
    if isinstance(quality, int):
        quality = "minor" if quality == 1 else "major"
    elif not isinstance(quality, str):
        quality = ""
    return f"{root} {quality}".strip()


def logic_pid() -> int | None:
    proc = subprocess.run(["pgrep", "-x", "Logic Pro"], capture_output=True, text=True)
    if proc.returncode != 0:
        proc = subprocess.run(
            ["pgrep", "-x", "Logic Pro Creator Studio"], capture_output=True, text=True
        )
    if proc.returncode != 0:
        return None
    values = proc.stdout.split()
    return int(values[0]) if values else None


def find_open_project() -> Path | None:
    process_id = logic_pid()
    if process_id is None:
        return None
    proc = subprocess.run(
        ["lsof", "-p", str(process_id), "-Fn"], capture_output=True, text=True
    )
    candidates = []
    for line in proc.stdout.splitlines():
        if not line.startswith("n"):
            continue
        match = re.search(r"^(.*?\.logicx)(/|$)", line[1:])
        if match:
            candidates.append(Path(match.group(1)))
    if not candidates:
        return None
    return sorted(set(candidates), key=lambda p: len(str(p)))[0]


def alternative_dir(root: Path) -> Path | None:
    alternatives = root / "Alternatives"
    if not alternatives.is_dir():
        return None
    folders = sorted(p for p in alternatives.iterdir() if p.is_dir())
    return folders[0] if folders else None


def read_metadata(root: Path) -> dict:
    result: dict = {}
    alternative = alternative_dir(root)
    if alternative:
        meta = alternative / "MetaData.plist"
        if meta.exists():
            with meta.open("rb") as handle:
                data = plistlib.load(handle)
            metadata = {k: data[k] for k in INTERESTING_KEYS if k in data}
            numerator = metadata.get("SongSignatureNumerator")
            denominator = metadata.get("SongSignatureDenominator")
            if numerator and denominator:
                metadata["time_signature"] = f"{int(numerator)}/{int(denominator)}"
            metadata["key"] = derive_key(metadata)
            result["metadata"] = metadata
            result["metadata_keys"] = sorted(data.keys())
            result["alternative"] = alternative.name
        project_data = alternative / "ProjectData"
        if project_data.exists():
            result["project_data_bytes"] = project_data.stat().st_size
    info = root / "Resources" / "ProjectInformation.plist"
    if info.exists():
        with info.open("rb") as handle:
            data = plistlib.load(handle)
        result["project_information"] = {
            k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))
        }
    return result


def bundle_report(root: Path) -> dict:
    if not root.exists():
        return {"error": f"bundle not found: {root}"}
    report: dict = {"path": str(root), "name": root.stem, "folders": {}}
    total = 0
    for folder in BUNDLE_FOLDERS:
        target = root / folder
        if not target.is_dir():
            continue
        files = [p for p in target.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        total += size
        report["folders"][folder] = {
            "files": len(files),
            "megabytes": round(size / 1048576, 2),
        }
    report["total_megabytes"] = round(total / 1048576, 2)
    report.update(read_metadata(root))
    return report


def list_audio(root: Path, folder: str, limit: int = 60) -> list[dict]:
    target = root / folder
    if not target.is_dir():
        return []
    files = [
        p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "path": str(p),
            "megabytes": round(p.stat().st_size / 1048576, 2),
            "modified": int(p.stat().st_mtime),
        }
        for p in files[:limit]
    ]


SLOT_CHANNEL = 15
SLOT_COUNT = 16

SUGGESTED_SLOTS = {
    0: "File > Export > All Tracks as MIDI File",
    1: "File > Save",
    2: "File > Bounce > Project or Section",
    3: "File > Export > All Tracks as Audio Files",
    4: "Edit > Undo",
    5: "Edit > Redo",
    6: "Region > Bounce in Place",
    7: "Functions > Normalize Region Parameters",
    8: "View > Zoom to Fit All Contents",
    9: "Navigate > Go to Beginning",
    10: "Mix > Toggle Metronome",
    11: "Free",
    12: "Free",
    13: "Free",
    14: "Free",
    15: "Free",
}


class KeyCommandTrigger:
    def __init__(self, port_name: str = "LogicKeys"):
        self.port_name = port_name
        self.midi_out = None
        self.opened = False

    def backend_status(self) -> dict:
        try:
            import rtmidi
        except ImportError as exc:
            return {"available": False, "reason": f"python-rtmidi not importable: {exc}"}
        try:
            probe = rtmidi.MidiOut()
            del probe
            return {"available": True}
        except Exception as exc:
            return {"available": False, "reason": f"no MIDI backend: {exc}"}

    def open(self) -> dict:
        if not self.opened:
            import rtmidi

            self.midi_out = rtmidi.MidiOut()
            self.midi_out.open_virtual_port(self.port_name)
            self.opened = True
        return {
            "port": self.port_name,
            "channel": SLOT_CHANNEL + 1,
            "slots": SLOT_COUNT,
            "learn_hint": (
                "In Logic open Key Commands, select the command, click Learn New Assignment, "
                f"then fire the slot from here. Slots are notes 0-{SLOT_COUNT - 1} on channel "
                f"{SLOT_CHANNEL + 1}."
            ),
        }

    def close(self) -> None:
        if self.opened and self.midi_out is not None:
            self.midi_out.close_port()
            self.midi_out = None
            self.opened = False

    def fire(self, slot: int, hold: float = 0.06) -> dict:
        if not 0 <= slot < SLOT_COUNT:
            return {"ok": False, "error": f"slot must be 0-{SLOT_COUNT - 1}"}
        if not self.opened:
            try:
                self.open()
            except Exception as exc:
                return {"ok": False, "error": f"could not open MIDI port: {exc}"}
        status = 0x90 | SLOT_CHANNEL
        self.midi_out.send_message([status, slot, 0x7F])
        time.sleep(hold)
        self.midi_out.send_message([status, slot, 0x00])
        return {
            "ok": True,
            "slot": slot,
            "note": slot,
            "channel": SLOT_CHANNEL + 1,
            "suggested_binding": SUGGESTED_SLOTS.get(slot, "Free"),
        }


TOOLS: list = []
trigger = KeyCommandTrigger(os.environ.get("LOGIC_KEYS_PORT", "LogicKeys"))


def tool(fn):
    TOOLS.append(fn)
    return fn


def _resolve(path: str | None, folder: str) -> Path | None:
    if path:
        return Path(path).expanduser()
    bundle = find_open_project()
    return bundle / folder if bundle else None


@tool
def project_report() -> dict:
    """Inspect the open Logic project bundle: folder sizes, asset counts, and the readable
    plist metadata (tempo, sample rate, time signature, track count, last saved version).
    This is the part of the project that is plain plist rather than opaque binary."""
    bundle = find_open_project()
    if bundle is None:
        return {"error": "no open .logicx bundle could be resolved from a running Logic process"}
    return bundle_report(bundle)


@tool
def project_assets(folder: str = "Bounces", limit: int = 60) -> dict:
    """List audio assets inside a folder of the open project bundle, newest first.
    Typical folders: Bounces, Audio Files, Stems, Freeze Files."""
    bundle = find_open_project()
    if bundle is None:
        return {"error": "no open .logicx bundle could be resolved"}
    return {"folder": folder, "files": list_audio(bundle, folder, limit)}


@tool
def midi_snapshot(path: str, region_gap_bars: float = 1.0) -> dict:
    """Parse a MIDI file exported from Logic into a full arrangement snapshot: tempo map,
    time signature changes, markers, and per-track note counts, pitch and velocity ranges,
    and inferred region boundaries. This is the exact read of the arrangement that the
    Accessibility API cannot give, because Logic writes it rather than us guessing it.
    Export with File > Export > All Tracks as MIDI File first."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"error": f"file not found: {target}"}
    try:
        return snapshot(target, region_gap_bars)
    except SMFError as exc:
        return {"error": str(exc), "file": str(target)}


@tool
def midi_snapshot_latest(folder: str | None = None) -> dict:
    """Parse the most recently modified MIDI file in a folder. Defaults to the user's
    Desktop, which is where Logic's MIDI export lands by default."""
    root = Path(folder).expanduser() if folder else Path.home() / "Desktop"
    if not root.is_dir():
        return {"error": f"not a directory: {root}"}
    candidates = sorted(
        (p for p in root.glob("*.mid")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {"error": f"no .mid files in {root}"}
    return midi_snapshot(str(candidates[0]))


@tool
def loudness_measure(path: str) -> dict:
    """Measure integrated loudness, loudness range and true peak of one audio file
    using the ffmpeg ebur128 filter."""
    return measure(Path(path).expanduser())


@tool
def loudness_audit(folder: str | None = None, target: str = "game_pc_asset", limit: int = 200) -> dict:
    """Audit a whole folder of audio against a loudness target and report which files
    pass and why the others fail. Targets: game_pc_asset (-16 LUFS +/-2 LU, -1 dBTP, no
    range check), game_pc_mix (same plus 13-15 LU range), streaming (-14 LUFS),
    broadcast_ebu (-23 LUFS). Defaults to the Bounces folder of the open project."""
    root = _resolve(folder, "Bounces")
    if root is None:
        return {"error": "no folder given and no open project to infer one from"}
    return audit(root, target, limit)


@tool
def loudness_targets() -> dict:
    """List the available loudness target presets and their tolerances."""
    return TARGETS


@tool
def naming_check(
    convention: str,
    folder: str | None = None,
    vocabularies: dict | None = None,
) -> dict:
    """Validate audio filenames in a folder against a naming convention and report
    violations, duplicates, and the set of values actually observed per field.
    Convention uses brace tokens: SFX_{category}_{name}_{index:03d} where a token with
    :Nd matches exactly N digits, a token listed in vocabularies matches only those
    values, and any other token matches alphanumerics."""
    root = _resolve(folder, "Bounces")
    if root is None:
        return {"error": "no folder given and no open project to infer one from"}
    return check_folder(root, convention, vocabularies)


@tool
def naming_diff_manifest(manifest: str, folder: str | None = None) -> dict:
    """Compare the audio files present in a folder against an expected list, and report
    what is missing and what is unexpected. The manifest may be a JSON array, a JSON
    object with a files key, or a plain text file with one name per line."""
    root = _resolve(folder, "Bounces")
    if root is None:
        return {"error": "no folder given and no open project to infer one from"}
    return diff_manifest(root, Path(manifest).expanduser())


@tool
def keys_setup() -> dict:
    """Open the virtual MIDI port used to trigger Logic key commands and return the
    slot map. Each slot is a MIDI note that can be bound to any Logic command through
    Key Commands > Learn New Assignment, which survives interface language changes and
    Logic updates in a way that clicking menu items does not."""
    info = trigger.open()
    return {**info, "suggested_bindings": SUGGESTED_SLOTS}


@tool
def keys_fire(slot: int) -> dict:
    """Fire a key command slot. Whatever Logic command was learned to this slot runs,
    without stealing keyboard focus. Call keys_setup first to see the slot map."""
    return trigger.fire(slot)


@tool
def health() -> dict:
    """Report what this server can currently do: whether Logic is running, whether a
    project was resolved, whether ffmpeg is present, and whether the key command port
    is open."""
    bundle = find_open_project()
    return {
        "logic_pid": logic_pid(),
        "project_path": str(bundle) if bundle else None,
        "ffmpeg": ffmpeg_available(),
        "ffprobe": __import__("shutil").which("ffprobe") is not None,
        "midi_backend": trigger.backend_status(),
        "keys_port_open": trigger.opened,
        "mcp_sdk": sdk_info(),
        "note": "control operations belong to logic-pro-mcp; this server reads and audits",
    }


def build() -> object:
    server = build_server("logic-audio")
    for fn in TOOLS:
        server.tool()(fn)
    return server


def selfcheck() -> int:
    import platform
    import sys

    rows = []

    def add(status, name, detail=""):
        rows.append((status, name, detail))

    add("ok" if platform.system() == "Darwin" else "FAIL", "platform",
        f"{platform.system()} {platform.mac_ver()[0]} {platform.machine()}".strip())
    add("ok", "python", f"{sys.version.split()[0]}  ({sys.executable})")

    info = sdk_info()
    if info.get("installed"):
        add("ok", "mcp sdk", f"api={info.get('api')} version={info.get('version')}")
    else:
        add("FAIL", "mcp sdk", "NOT installed. Run: <venv>/bin/pip install mcp")

    backend = trigger.backend_status()
    add("ok" if backend.get("available") else "warn", "midi backend",
        backend.get("reason", "available") if not backend.get("available") else "available")

    for binary in ("ffmpeg", "ffprobe", "osascript"):
        found = shutil.which(binary)
        add("ok" if found else "warn", binary, found or "not on PATH")

    found = shutil.which("LogicProMCP")
    add("ok" if found else "warn", "LogicProMCP", found or "control server not installed")

    pid = logic_pid()
    add("ok" if pid else "warn", "Logic running", f"pid {pid}" if pid else "not running")

    bundle = find_open_project()
    add("ok" if bundle else "warn", "project resolved", str(bundle) if bundle else "no open .logicx")

    add("ok", "tools defined", str(len(TOOLS)))
    if info.get("installed"):
        try:
            build()
            add("ok", "server builds", "all tools registered")
        except Exception as exc:
            add("FAIL", "server builds", f"{type(exc).__name__}: {exc}")

    width = max(len(n) for _, n, _ in rows)
    print()
    for status, name, detail in rows:
        print(f"{status:>4}  {name:<{width}}  {detail}")
    failures = sum(1 for s, _, _ in rows if s == "FAIL")
    warns = sum(1 for s, _, _ in rows if s == "warn")
    print(f"\n{failures} failed, {warns} warnings\n")
    return 1 if failures else 0


def main() -> None:
    import sys

    if "--check" in sys.argv:
        raise SystemExit(selfcheck())
    build().run()


if __name__ == "__main__":
    main()
