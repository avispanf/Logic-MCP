#!/usr/bin/env python3
"""Combine one or more durable mix-audit journals into JSON and Markdown.

The runner deliberately journals every result before advancing.  This utility
turns completed runs and safe continuation runs into one operator-readable
report without opening Logic or invoking an MCP server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


PLUGIN_STEP = re.compile(r"target-\d+-[0-9a-f]+-plugin-(\d+)-(open|parameters)$")


def _events(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
    return rows


def _audit_id(track: dict) -> str:
    token = track.get("track_ref") or f'{track.get("type", "track")}:{track.get("id")}:{track.get("name", "")}'
    return hashlib.sha256(str(token).encode()).hexdigest()[:12]


def _tracks(events: list[dict]) -> list[dict]:
    for event in events:
        if event.get("event") != "step_finished":
            continue
        if event.get("summary", {}).get("step_id") != "capture-state":
            continue
        tracks = event.get("result", {}).get("logic://tracks", {}).get("data")
        if isinstance(tracks, list):
            return tracks
    return []


def build_report(journals: list[Path]) -> dict:
    targets: dict[str, dict] = {}
    runs = []
    project_path = None
    track_index: dict[str, dict] = {}

    loaded = [(path, _events(path)) for path in journals]
    for _, events in loaded:
        for track in _tracks(events):
            track_id = _audit_id(track)
            track_index[track_id] = track
            targets.setdefault(
                track_id,
                {
                    "target_id": track_id,
                    "project_index": track.get("id"),
                    "name": track.get("name"),
                    "kind": "aux" if track.get("type") == "aux" else "track",
                    "inserts": [],
                    "plugins": [],
                },
            )

    for path, events in loaded:
        plan = next((row for row in events if row.get("event") == "plan_created"), {})
        project_path = project_path or plan.get("project_path")
        failures = []
        run_finished = next((row for row in reversed(events) if row.get("event") == "run_finished"), None)
        run_failed = next((row for row in reversed(events) if row.get("event") == "run_failed"), None)
        open_plugins: dict[tuple[str, str], str] = {}

        for event in events:
            if event.get("event") != "step_finished":
                continue
            summary = event.get("summary", {})
            result = event.get("result", {})
            target_id = summary.get("target_id")
            if not target_id:
                continue
            target = targets.setdefault(
                target_id,
                {
                    "target_id": target_id,
                    "project_index": result.get("project_index"),
                    "name": None,
                    "kind": None,
                    "inserts": [],
                    "plugins": [],
                },
            )
            operation = summary.get("operation")
            if operation == "record_limitation":
                target["project_index"] = result.get("project_index", target.get("project_index"))
                target["inspection_limitation"] = result.get("reason")
            elif operation == "mixer_read_strip":
                target["name"] = result.get("name") or target.get("name")
                target["inserts"] = result.get("inserts") or target.get("inserts", [])
            elif operation == "mix_bounce_target" and result.get("artifact"):
                target["artifact"] = result["artifact"]
                artifact_name = Path(result["artifact"]).stem
                if not target.get("name"):
                    target["name"] = re.sub(r"^\d+-(?:track|aux|bus|group|master)-", "", artifact_name).replace("-", " ")
                match = re.match(r"^\d+-(track|aux|bus|group|master)-", artifact_name)
                if match:
                    target["kind"] = match.group(1)
            elif operation == "loudness_measure":
                target["integrated_lufs"] = result.get("integrated_lufs")
                target["true_peak_dbtp"] = result.get("true_peak_dbtp")

            step_id = str(summary.get("step_id") or "")
            plugin_match = PLUGIN_STEP.match(step_id)
            if plugin_match:
                slot, action = plugin_match.groups()
                key = (target_id, slot)
                if action == "open":
                    open_plugins[key] = result.get("plugin") or f"insert {int(slot) + 1}"
                else:
                    target["plugins"].append(
                        {
                            "slot": int(slot),
                            "name": open_plugins.get(key, f"insert {int(slot) + 1}"),
                            "parameter_count": summary.get("parameter_count"),
                            "parameters": result.get("parameters") or [],
                        }
                    )

            if summary.get("ok") is False or summary.get("error"):
                failures.append(
                    {
                        "step_id": summary.get("step_id"),
                        "target_id": target_id,
                        "error": summary.get("error") or result.get("error"),
                    }
                )

        runs.append(
            {
                "plan_id": plan.get("plan_id"),
                "journal": str(path),
                "target_count": plan.get("target_count"),
                "status": "complete" if run_finished else "failed" if run_failed else "in_progress",
                "failed_steps": failures,
            }
        )

    ordered = sorted(
        (row for row in targets.values() if row.get("integrated_lufs") is not None),
        key=lambda row: (row.get("project_index") is None, row.get("project_index", 10**9)),
    )
    for row in ordered:
        row["plugins"] = sorted(row.get("plugins", []), key=lambda plugin: plugin["slot"])
        row["parameter_count"] = sum(
            plugin.get("parameter_count") or 0 for plugin in row.get("plugins", [])
        )

    peaks = [row for row in ordered if row.get("true_peak_dbtp") is not None]
    return {
        "schema": "logic_mix_audit_consolidated.v1",
        "project_path": project_path,
        "runs": runs,
        "measured_targets": len(ordered),
        "plugin_inspected_targets": sum(bool(row.get("plugins")) for row in ordered),
        "failed_steps": sum(len(run["failed_steps"]) for run in runs),
        "highest_true_peak": max(peaks, key=lambda row: row["true_peak_dbtp"], default=None),
        "targets": ordered,
    }


def markdown(report: dict) -> str:
    lines = [
        "# Logic MCP mix audit",
        "",
        f"Project: `{report.get('project_path') or 'unknown'}`",
        "",
        f"Measured targets: {report['measured_targets']}; plugin-inspected targets: {report['plugin_inspected_targets']}; failed steps: {report['failed_steps']}.",
        "",
        "## Runs",
        "",
        "| Plan | Status | Planned targets | Failed steps |",
        "|---|---:|---:|---:|",
    ]
    for run in report["runs"]:
        lines.append(
            f"| {run.get('plan_id') or 'unknown'} | {run['status']} | {run.get('target_count') or 0} | {len(run['failed_steps'])} |"
        )
    lines.extend(
        [
            "",
            "## Measurements",
            "",
            "| # | Target | Kind | LUFS-I | dBTP | Plugins / params | Artifact |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
    )
    for row in report["targets"]:
        artifact = row.get("artifact")
        artifact_link = f"[WAV]({Path(artifact).as_uri()})" if artifact else ""
        lines.append(
            "| {index} | {name} | {kind} | {lufs} | {peak} | {plugins} / {params} | {artifact} |".format(
                index=row.get("project_index", ""),
                name=str(row.get("name") or row["target_id"]).replace("|", "\\|"),
                kind=row.get("kind") or "",
                lufs=row.get("integrated_lufs", ""),
                peak=row.get("true_peak_dbtp", ""),
                plugins=len(row.get("plugins", [])),
                params=row.get("parameter_count", 0),
                artifact=artifact_link,
            )
        )
    lines.extend(
        [
            "",
            "## Safety notes",
            "",
            "Loudness values are measurements, not automatic gain targets. Plugin writes require an exact target, exact parameter label, expected-before guard, and independent readback. Insert reordering remains unavailable and is not attempted.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journals", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, required=True, dest="json_path")
    parser.add_argument("--markdown", type=Path, required=True, dest="markdown_path")
    args = parser.parse_args()
    report = build_report(args.journals)
    args.json_path.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_path.parent.mkdir(parents=True, exist_ok=True)
    args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(args.json_path), "markdown": str(args.markdown_path), "measured_targets": report["measured_targets"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
