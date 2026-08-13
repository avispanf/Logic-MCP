#!/usr/bin/env python3
"""Run a confirmation-gated Logic mix audit through three local MCP servers.

This client intentionally keeps each MCP server in one stdio session for the whole
run.  It can therefore use freshly edited local server code without restarting the
desktop MCP host, while preserving ordered coordinator steps and mandatory restore.
It never creates a parameter-write plan; fixes remain a separate workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import traceback
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent
DEFAULT_PYTHON = Path("/Users/avispanf/dev/venv/bin/python")
DEFAULT_CORE = Path("/Users/avispanf/.local/bin/LogicProMCP-codex")


def json_text(result: Any) -> dict:
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", "") == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"ok": False, "error": "tool returned non-JSON text", "raw": block.text}
    structured = getattr(result, "structuredContent", None)
    return structured if isinstance(structured, dict) else {
        "ok": False,
        "error": "tool returned no JSON payload",
    }


def succeeded(result: dict) -> bool:
    return not result.get("error") and result.get("ok") is not False


def verified(result: dict) -> bool:
    return succeeded(result) and (
        result.get("verified") is True
        or result.get("state") == "A"
        or result.get("success") is True
    )


def track_name_in_snapshot(observed: str, snapshot_names: set[str]) -> bool:
    """Match exact project names plus Logic's one measured output alias."""
    folded = str(observed or "").strip().casefold()
    if folded in snapshot_names:
        return True
    return folded in {"master", "stereo out"} and bool(
        snapshot_names.intersection({"master", "stereo out"})
    )


def filter_tracks_by_index_range(
    tracks: dict,
    start_index: int = 0,
    end_index: int | None = None,
) -> dict:
    """Return a copied track resource bounded by inclusive project indices."""
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if end_index is not None and end_index < start_index:
        raise ValueError("end_index must be greater than or equal to start_index")
    bounded = copy.deepcopy(tracks)
    bounded["data"] = [
        row
        for row in tracks.get("data", [])
        if int(row.get("index", row.get("id", -1))) >= start_index
        and (
            end_index is None
            or int(row.get("index", row.get("id", -1))) <= end_index
        )
    ]
    return bounded


class AuditRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.sessions: dict[str, ClientSession] = {}
        self.initial_resources: dict[str, dict] = {}
        self.track_state: dict[int, dict] = {}
        self.surface_state: dict[str, bool] = {}
        self.surface_identity: dict[str, str] = {}
        self.initial_track_state: dict[int, dict] = {}
        self.initial_surface_state: dict[str, bool] = {}
        self.transport_position: str | None = None
        self.initial_transport_position: str | None = None
        self.transport_playing = False
        self.initial_transport_playing = False
        self.cycle_enabled = False
        self.initial_cycle_enabled = False
        self.log_path: Path | None = None
        self.summary: list[dict] = []
        self.snapshot_tracks: dict | None = None

    def load_tracks_snapshot(self, expected_project_path: str = "") -> dict | None:
        """Load one previously journaled, project-specific AX track resource."""
        source = self.args.tracks_snapshot
        if source is None:
            return None
        try:
            rows = source.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError(f"could not read tracks snapshot {source}: {exc}") from exc
        journal_project = ""
        decoded = []
        for line in rows:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            decoded.append(event)
            if event.get("event") == "plan_created":
                journal_project = str(event.get("project_path") or "")
        if expected_project_path and journal_project != expected_project_path:
            raise RuntimeError(
                "tracks snapshot project mismatch: "
                f"expected {expected_project_path!r}, journal has {journal_project!r}"
            )
        for event in reversed(decoded):
            if event.get("event") != "step_finished":
                continue
            result = event.get("result", {})
            tracks = result.get("logic://tracks") if isinstance(result, dict) else None
            if (
                isinstance(tracks, dict)
                and tracks.get("source") == "ax_live"
                and tracks.get("readable") is True
                and tracks.get("data")
            ):
                return tracks
        raise RuntimeError(f"no readable ax_live logic://tracks snapshot in {source}")

    async def connect(
        self,
        stack: AsyncExitStack,
        name: str,
        command: Path,
        script: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        params = StdioServerParameters(
            command=str(command),
            args=[str(script)] if script else [],
            env=env,
        )
        reader, writer = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(reader, writer))
        await session.initialize()
        self.sessions[name] = session

    async def tool(self, server: str, name: str, arguments: dict | None = None) -> dict:
        result = await self.sessions[server].call_tool(name, arguments or {})
        return json_text(result)

    async def resource(self, uri: str) -> dict:
        result = await self.sessions["core"].read_resource(uri)
        if not result.contents:
            return {"error": f"empty resource {uri}"}
        return json.loads(result.contents[0].text)

    async def wait_for_tracks(self, timeout: int = 150) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        last = {}
        refresh_requested = False
        while asyncio.get_running_loop().time() < deadline:
            last = await self.resource("logic://tracks")
            rows = last.get("data", [])
            names = tuple(str(row.get("name") or "").strip() for row in rows)
            generic_mcu_bank = bool(names) and all(
                re.fullmatch(r"Track\s+\d+", name, flags=re.IGNORECASE)
                for name in names
            )
            usable = (
                last.get("readable") is True
                and last.get("source") == "ax_live"
                and bool(rows)
                and not generic_mcu_bank
            )
            if usable:
                return last
            if generic_mcu_bank and not refresh_requested:
                refresh_requested = True
                await self.tool(
                    "core",
                    "logic_system",
                    {"command": "refresh_cache", "params": {}},
                )
                continue
            # A newly connected MCU initially publishes a generic eight-strip
            # bank.  Give the slower AX project poll room to replace it; frequent
            # resource reads can otherwise keep sampling the transitional bank.
            await asyncio.sleep(12 if generic_mcu_bank else 3)
        raise RuntimeError(
            "logic://tracks did not settle to a project-specific AX inventory: "
            f"{last.get('reason') or last.get('error') or len(last.get('data', []))} rows"
        )

    def emit(self, event: str, **payload: Any) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        line = json.dumps(row, ensure_ascii=False, default=str)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        # The JSONL journal is the evidence record and keeps every returned
        # parameter.  Stdout is only a progress stream; bounding it prevents
        # large plug-in tables from filling an unattended MCP/PTY transport.
        progress = row
        if event == "step_finished" and isinstance(payload.get("summary"), dict):
            progress = {
                "timestamp": row["timestamp"],
                "event": event,
                "summary": payload["summary"],
            }
        elif event == "run_finished" and isinstance(payload.get("result"), dict):
            result = payload["result"]
            progress = {
                "timestamp": row["timestamp"],
                "event": event,
                "result": {
                    "plan_id": result.get("plan_id"),
                    "complete": result.get("complete"),
                    "steps_executed": result.get("steps_executed"),
                    "failed_step_count": len(result.get("failed_steps", [])),
                    "artifact_count": len(result.get("artifacts", [])),
                    "measurement_count": len(result.get("measurements", [])),
                },
                "summary_path": payload.get("summary_path"),
            }
        print(json.dumps(progress, ensure_ascii=False, default=str), flush=True)

    def capture_known_state(self, resources: dict, ax_state: list[dict]) -> None:
        tracks = resources.get("logic://tracks", {}).get("data", [])
        self.track_state = {
            int(row.get("index", row.get("id"))): {
                "name": row.get("name"),
                "solo": bool(row.get("isSoloed", row.get("solo", False))),
                "mute": bool(row.get("isMuted", row.get("mute", False))),
                "selected": bool(row.get("isSelected", row.get("selected", False))),
            }
            for row in tracks
            if row.get("index", row.get("id")) is not None
        }
        self.initial_track_state = copy.deepcopy(self.track_state)
        transport = resources.get("logic://transport/state", {})
        state = transport.get("data", {}).get("state", transport.get("state", transport))
        self.transport_position = state.get("position")
        self.initial_transport_position = self.transport_position
        self.transport_playing = bool(state.get("isPlaying", state.get("playing", False)))
        self.initial_transport_playing = self.transport_playing
        self.cycle_enabled = bool(state.get("isCycleEnabled", state.get("cycle", False)))
        self.initial_cycle_enabled = self.cycle_enabled
        for row in ax_state:
            path = row.get("strip_path")
            if not path:
                continue
            self.surface_identity[path] = str(row.get("name") or "")
            for control in ("solo", "mute"):
                raw = row.get(control)
                value = raw is True or str(raw).casefold() in {"on", "true", "1"}
                self.surface_state[f"{path}:{control}"] = value
        self.initial_surface_state = dict(self.surface_state)

    async def capture_resources(self) -> dict:
        tracks = (
            copy.deepcopy(self.snapshot_tracks)
            if self.snapshot_tracks is not None
            else await self.wait_for_tracks()
        )
        resources = {
            "logic://tracks": tracks,
            "logic://mixer": await self.resource("logic://mixer"),
            "logic://transport/state": await self.resource("logic://transport/state"),
            "logic://project/info": await self.resource("logic://project/info"),
        }
        self.initial_resources = resources
        return resources

    async def refresh_track_observation(self, index: int, field: str) -> bool | None:
        await asyncio.sleep(18)
        await self.tool("core", "logic_system", {"command": "refresh_cache", "params": {}})
        tracks = await self.resource("logic://tracks")
        keys = {
            "solo": ("isSoloed", "solo"),
            "mute": ("isMuted", "mute"),
        }[field]
        for row in tracks.get("data", []):
            if int(row.get("index", row.get("id", -1))) != index:
                continue
            for key in keys:
                if key in row:
                    return bool(row[key])
        return None

    async def execute_track_child(self, arguments: dict) -> dict:
        command = arguments.get("command")
        params = arguments.get("params", {})
        index = int(params.get("index", -1))
        current = self.track_state.get(index)
        if current is None:
            return {"ok": False, "error": f"unknown project track index {index}"}
        if command in {"solo", "mute"}:
            wanted = bool(params.get("enabled"))
            if current[command] == wanted:
                return {
                    "ok": True,
                    "verified": True,
                    "state": "A",
                    "action": "known-state no-op",
                    "index": index,
                    "command": command,
                    "observed": wanted,
                }
            return {
                "ok": False,
                "verified": False,
                "error": "direct core solo/mute is disabled; use verified Inspector toggle",
            }
        elif command == "select":
            if current.get("selected"):
                return {"ok": True, "verified": True, "state": "A", "action": "known-state no-op"}
            result = await self.tool("core", "logic_tracks", arguments)
            identity = await self.tool(
                "plugins",
                "selected_track_identity",
                {"expected_track": str(current.get("name") or "")},
            )
            if verified(identity):
                result = {
                    **result,
                    "ok": True,
                    "success": True,
                    "verified": True,
                    "state": "A",
                    "observed": index,
                    "verification_source": "inspector_name",
                    "mcu_feedback_accepted": False,
                    "core_result": result,
                    "error": None,
                }
            else:
                result = {
                    "ok": False,
                    "success": False,
                    "verified": False,
                    "state": "C",
                    "error": "track selection did not match the Inspector identity",
                    "index": index,
                    "expected_track": current.get("name"),
                    "selection": result,
                    "identity": identity,
                }
        else:
            result = await self.tool("core", "logic_tracks", arguments)
        if verified(result):
            if command in {"solo", "mute"}:
                current[command] = bool(params.get("enabled"))
            elif command == "select":
                for row in self.track_state.values():
                    row["selected"] = False
                current["selected"] = True
        return result

    async def execute_child(self, dispatch: dict) -> dict:
        server = dispatch.get("server")
        operation = dispatch.get("operation")
        arguments = dispatch.get("arguments", {})
        if server == "logic-pro" and operation == "logic_tracks":
            return await self.execute_track_child(arguments)
        if server == "logic-plugins" and operation == "arrange_track_set_toggle":
            index = int(arguments.get("index", -1))
            control = str(arguments.get("control") or "")
            wanted = bool(arguments.get("enabled"))
            current = self.track_state.get(index)
            if current is None or control not in {"solo", "mute"}:
                return {
                    "ok": False,
                    "verified": False,
                    "error": f"unknown Arrange track/control {index}/{control}",
                }
            if current[control] == wanted:
                return {
                    "ok": True,
                    "verified": True,
                    "action": "known-state no-op",
                    "index": index,
                    "control": control,
                    "observed": wanted,
                }
            # The core MCU route intentionally returns honest State B. The
            # following Inspector toggle independently verifies both the exact
            # selected-track name and the control read-back, so a separate
            # Inspector identity round-trip would be redundant here.
            select_result = await self.tool(
                "core", "logic_tracks", {"command": "select", "params": {"index": index}}
            )
            result = await self.tool("plugins", operation, arguments)
            if verified(result):
                for row in self.track_state.values():
                    row["selected"] = False
                current["selected"] = True
                current[control] = wanted
            elif not result.get("selection"):
                result = {**result, "selection": select_result}
            return result
        if server == "logic-plugins" and operation == "mixer_set_toggle":
            key = f"{arguments.get('strip_path')}:{arguments.get('control')}"
            wanted = bool(arguments.get("enabled"))
            if self.surface_state.get(key) == wanted:
                return {"ok": True, "verified": True, "action": "known-state no-op"}
            result = await self.tool("plugins", "mixer_set_toggle", arguments)
            if verified(result):
                self.surface_state[key] = wanted
            return result
        if server == "logic-plugins" and operation == "transport_goto_position":
            observed = await self.refresh_transport_observation()
            if observed.get("position") == arguments.get("position"):
                return {
                    "ok": True,
                    "verified": True,
                    "action": "verified-position no-op",
                    "observed": observed.get("position"),
                }
            result = await self.tool("plugins", operation, arguments)
            if verified(result):
                self.transport_position = arguments.get("position")
            return result
        if server == "client" and operation == "ensure_resource_state":
            field = arguments.get("field")
            wanted = bool(arguments.get("equals"))
            if field == "isPlaying" and self.transport_playing == wanted:
                return {"ok": True, "verified": True, "action": "known-state no-op"}
            if field == "isCycleEnabled" and self.cycle_enabled == wanted:
                return {"ok": True, "verified": True, "action": "known-state no-op"}
            if field == "isCycleEnabled":
                command = arguments.get("command_if_mismatch")
            else:
                command = arguments.get("command_if_true" if wanted else "command_if_false")
            if not command:
                return {
                    "ok": False,
                    "verified": False,
                    "error": f"no restore command for {field}={wanted}",
                }
            result = await self.tool("core", "logic_transport", {"command": command, "params": {}})
            if verified(result):
                if field == "isPlaying":
                    self.transport_playing = wanted
                elif field == "isCycleEnabled":
                    self.cycle_enabled = wanted
            return result
        return {"ok": False, "error": f"unsupported child {server}/{operation}"}

    async def refresh_transport_observation(self) -> dict:
        payload = await self.resource("logic://transport/state")
        state = payload.get("data", {}).get("state", payload.get("state", payload))
        if isinstance(state, dict):
            self.transport_position = state.get("position", self.transport_position)
            self.transport_playing = bool(
                state.get("isPlaying", state.get("playing", self.transport_playing))
            )
            self.cycle_enabled = bool(
                state.get("isCycleEnabled", state.get("cycle", self.cycle_enabled))
            )
            return state
        return {}

    def restore_needed(self) -> bool:
        track_changed = any(
            self.track_state.get(index, {}).get(field) != initial.get(field)
            for index, initial in self.initial_track_state.items()
            for field in ("solo", "mute")
        )
        surface_changed = any(
            self.surface_state.get(key) != initial
            for key, initial in self.initial_surface_state.items()
        )
        return (
            track_changed
            or surface_changed
            or self.transport_position != self.initial_transport_position
            or self.transport_playing != self.initial_transport_playing
            or self.cycle_enabled != self.initial_cycle_enabled
        )

    async def execute_dispatch_step(self, step: dict) -> dict:
        built = await self.invoke(step)
        if not succeeded(built) or not isinstance(built.get("dispatches"), list):
            return {**built, "executed": False, "verified": False}
        child_results = []
        all_verified = True
        for dispatch in built["dispatches"]:
            result = await self.execute_child(dispatch)
            child_results.append({"dispatch": dispatch, "result": result})
            if not verified(result):
                all_verified = False
                break
        return {
            "ok": all_verified,
            "executed": all_verified,
            "verified": all_verified,
            "dispatch_count": len(built["dispatches"]),
            "children": child_results,
            **({} if all_verified else {"error": "one or more child dispatches failed"}),
        }

    async def read_plugin_parameters(self, arguments: dict) -> dict:
        requested_offset = max(0, int(arguments.get("offset", 0)))
        page_size = max(1, min(int(self.args.parameter_page_size), 500))
        max_parameters = max(page_size, int(self.args.max_parameters))
        offset = requested_offset
        parameters: list[dict] = []
        pages = 0
        first: dict | None = None
        while len(parameters) < max_parameters:
            page_arguments = {
                **arguments,
                "offset": offset,
                "limit": min(page_size, max_parameters - len(parameters)),
            }
            page = await self.tool("plugins", "plugin_parameters", page_arguments)
            pages += 1
            if first is None:
                first = page
            if not succeeded(page):
                return {
                    **page,
                    "parameters": parameters,
                    "returned": len(parameters),
                    "pages_completed": pages - 1,
                    "failed_offset": offset,
                }
            parameters.extend(page.get("parameters", []))
            next_offset = page.get("next_offset")
            self.emit(
                "parameter_page_read",
                plugin=arguments.get("expected_plugin"),
                offset=offset,
                returned=page.get("returned"),
                rows_total=page.get("rows_total"),
                next_offset=next_offset,
            )
            if next_offset is None or int(next_offset) <= offset:
                break
            offset = int(next_offset)
        result = {
            **(first or {}),
            "offset": requested_offset,
            "returned": len(parameters),
            "parameters": parameters,
            "pages_completed": pages,
            "next_offset": offset if len(parameters) >= max_parameters else None,
        }
        if len(parameters) >= max_parameters and result.get("rows_total", 0) > len(parameters):
            result["truncated"] = True
            result["note"] = f"stopped at safety cap of {max_parameters} parameters"
        return result

    async def invoke(self, step: dict) -> dict:
        server = step.get("server")
        operation = step.get("operation")
        arguments = step.get("arguments", {})
        if server == "client" and operation == "read_resources":
            resources = await self.capture_resources()
            ax_state = next(
                (
                    candidate.get("arguments", {}).get("ax_state", [])
                    for candidate in self.plan.get("steps", [])
                    if candidate.get("operation") == "mix_isolation_dispatch"
                ),
                [],
            )
            self.capture_known_state(resources, ax_state)
            return resources
        if server == "client" and operation == "record_limitation":
            return {"ok": True, "recorded": True, **arguments}
        if server == "logic-pro":
            return await self.tool("core", operation, arguments)
        if server == "logic-audio":
            return await self.tool("audio", operation, arguments)
        if server == "logic-plugins":
            if operation == "transport_goto_position":
                observed = await self.refresh_transport_observation()
                if observed.get("position") == arguments.get("position"):
                    return {
                        "ok": True,
                        "verified": True,
                        "action": "verified-position no-op",
                        "observed": observed.get("position"),
                    }
            if operation == "mix_bounce_target":
                arguments = {**arguments, "timeout_seconds": self.args.bounce_timeout}
            if operation == "plugin_parameters":
                return await self.read_plugin_parameters(arguments)
            return await self.tool("plugins", operation, arguments)
        return {"ok": False, "error": f"unsupported step {server}/{operation}"}

    async def emergency_restore(self) -> None:
        self.emit("emergency_restore_started")
        for index, initial in self.initial_track_state.items():
            current = self.track_state.get(index, {})
            for field in ("solo", "mute"):
                if current.get(field) == initial.get(field):
                    continue
                result = await self.execute_child(
                    {
                        "server": "logic-plugins",
                        "operation": "arrange_track_set_toggle",
                        "arguments": {
                            "index": index,
                            "expected_track": str(initial.get("name") or ""),
                            "control": field,
                            "enabled": initial[field],
                            "dry_run": False,
                        },
                    }
                )
                self.emit("emergency_track_restore", index=index, field=field, result=result)
        for key, initial in self.initial_surface_state.items():
            if self.surface_state.get(key) == initial:
                continue
            path, control = key.rsplit(":", 1)
            result = await self.execute_child(
                {
                    "server": "logic-plugins",
                    "operation": "mixer_set_toggle",
                    "arguments": {
                        "strip_path": path,
                        "control": control,
                        "enabled": initial,
                        "expected_strip": self.surface_identity.get(path, ""),
                        "dry_run": False,
                    },
                }
            )
            self.emit("emergency_surface_restore", path=path, control=control, result=result)
        if self.initial_transport_position:
            result = await self.execute_child(
                {
                    "server": "logic-plugins",
                    "operation": "transport_goto_position",
                    "arguments": {
                        "position": self.initial_transport_position,
                        "dry_run": False,
                    },
                }
            )
            self.emit("emergency_position_restore", result=result)
        if self.cycle_enabled != self.initial_cycle_enabled:
            result = await self.tool(
                "core",
                "logic_transport",
                {"command": "toggle_cycle", "params": {}},
            )
            if verified(result):
                self.cycle_enabled = self.initial_cycle_enabled
            self.emit("emergency_cycle_restore", result=result)
        if self.transport_playing != self.initial_transport_playing:
            command = "play" if self.initial_transport_playing else "stop"
            result = await self.tool(
                "core",
                "logic_transport",
                {"command": command, "params": {}},
            )
            if verified(result):
                self.transport_playing = self.initial_transport_playing
            self.emit("emergency_playback_restore", result=result)
        self.emit("emergency_restore_finished")

    async def run_plan(self) -> dict:
        started = await self.tool(
            "plugins", "mix_audit_start", {"plan_id": self.plan["plan_id"], "confirm": True}
        )
        step = started.get("next_step")
        guard = 0
        try:
            while step and guard < self.args.max_steps:
                guard += 1
                self.emit(
                    "step_started",
                    ordinal=guard,
                    step_id=step.get("step_id"),
                    phase=step.get("phase"),
                    operation=step.get("operation"),
                    target_id=step.get("target_id"),
                )
                if step.get("operation") in {"mix_isolation_dispatch", "mix_restore_dispatch"}:
                    result = await self.execute_dispatch_step(step)
                else:
                    result = await self.invoke(step)
                summary = {
                    "step_id": step.get("step_id"),
                    "phase": step.get("phase"),
                    "operation": step.get("operation"),
                    "target_id": step.get("target_id"),
                    "ok": result.get("ok"),
                    "verified": result.get("verified"),
                    "error": result.get("error"),
                    "artifact": result.get("artifact"),
                    "integrated_lufs": result.get("integrated_lufs"),
                    "true_peak_dbtp": result.get("true_peak_dbtp"),
                    "parameter_count": result.get("parameter_count", result.get("rows_total")),
                }
                self.summary.append(summary)
                self.emit("step_finished", summary=summary, result=result)
                advanced = await self.tool(
                    "plugins",
                    "mix_audit_advance",
                    {"plan_id": self.plan["plan_id"], "step_id": step["step_id"], "result": result},
                )
                if advanced.get("error"):
                    raise RuntimeError(f"coordinator rejected result: {advanced}")
                if advanced.get("failed"):
                    # The coordinator can impose stronger verification requirements
                    # than the raw tool's `ok` field.  Preserve that failure in the
                    # durable summary even though cleanup steps must continue.
                    summary["ok"] = False
                    summary["verified"] = False
                    summary["error"] = (
                        summary.get("error")
                        or "coordinator marked the step failed; mandatory restore continued"
                    )
                step = advanced.get("next_step")
                if advanced.get("complete"):
                    break
            if step:
                raise RuntimeError(f"max_steps={self.args.max_steps} reached")
        except BaseException:
            await self.emergency_restore()
            raise
        if self.restore_needed():
            self.emit("coordinator_restore_incomplete")
            await self.emergency_restore()
        return {
            "plan_id": self.plan["plan_id"],
            "complete": step is None,
            "steps_executed": len(self.summary),
            "failed_steps": [row for row in self.summary if row.get("error") or row.get("ok") is False],
            "artifacts": [row["artifact"] for row in self.summary if row.get("artifact")],
            "measurements": [
                row
                for row in self.summary
                if row.get("integrated_lufs") is not None or row.get("true_peak_dbtp") is not None
            ],
        }

    async def run(self) -> dict:
        env = dict(os.environ)
        env["LOGIC_PRO_MCP_MIDI_INSTANCE_ID"] = self.args.midi_instance
        env["LOGIC_PRO_MCP_TRACK_SELECT_MCU_FIRST"] = "1"
        async with AsyncExitStack() as stack:
            await self.connect(stack, "plugins", self.args.python, ROOT / "logic_plugins_mcp.py")
            await self.connect(stack, "audio", self.args.python, ROOT / "logic_audio_mcp.py")
            await self.connect(stack, "core", self.args.core, env=env)
            project = await self.tool("plugins", "mix_project_identity", {})
            if not project.get("verified"):
                raise RuntimeError(f"open project could not be verified: {project}")
            project_path = project["observed_project_path"]
            tracks = self.load_tracks_snapshot(project_path)
            if tracks is None:
                tracks = await self.wait_for_tracks()
            else:
                self.snapshot_tracks = copy.deepcopy(tracks)
                self.emit(
                    "tracks_snapshot_loaded",
                    source=str(self.args.tracks_snapshot),
                    track_count=len(tracks.get("data", [])),
                    project_path=project_path,
                )
            if self.args.start_index or self.args.end_index is not None:
                tracks = filter_tracks_by_index_range(
                    tracks,
                    start_index=self.args.start_index,
                    end_index=self.args.end_index,
                )
                if not tracks["data"]:
                    raise RuntimeError(
                        "no project tracks remain in requested index range "
                        f"{self.args.start_index}..{self.args.end_index}"
                    )
            survey = await self.tool(
                "plugins",
                "mixer_survey",
                {
                    "offset": 0,
                    "strip_limit": self.args.strip_limit,
                    "per_strip_seconds": self.args.per_strip_seconds,
                    "total_seconds": self.args.survey_seconds,
                },
            )
            if self.args.start_index or self.args.end_index is not None:
                remaining_names = {
                    str(row.get("name") or "").strip().casefold()
                    for row in tracks.get("data", [])
                }
                survey = copy.deepcopy(survey)
                survey["channels"] = [
                    row
                    for row in survey.get("channels", [])
                    if track_name_in_snapshot(row.get("name") or "", remaining_names)
                ]
            plan = await self.tool(
                "plugins",
                "mix_audit_plan",
                {
                    "tracks": json.dumps(tracks, ensure_ascii=False),
                    "mixer": "{}",
                    "ax_channels": json.dumps(survey, ensure_ascii=False),
                    "scope": self.args.scope,
                    "selector": self.args.selector,
                    "project_path": project_path,
                    "output_root": str(self.args.output_root),
                    "measurement": self.args.measurement,
                    "start_position": self.args.start_position,
                    "target_name": self.args.target_name,
                },
            )
            if plan.get("error"):
                raise RuntimeError(f"audit plan failed: {plan}")
            self.plan = plan
            self.log_path = self.args.output_root / plan["plan_id"] / "runner.jsonl"
            self.emit(
                "plan_created",
                plan_id=plan["plan_id"],
                target_count=plan.get("target_count"),
                step_count=len(plan.get("steps", [])),
                project_path=project_path,
            )
            result = await self.run_plan()
            summary_path = self.log_path.with_name("summary.json")
            summary_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            self.emit("run_finished", result=result, summary_path=str(summary_path))
            return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--scope", default="track", choices=["track", "group", "aux", "bus", "master", "all"])
    result.add_argument(
        "--selector",
        default="",
        help="exact target name/ref; empty means every target allowed by --scope",
    )
    result.add_argument("--measurement", default="bounce_bs1770", choices=["bounce_bs1770", "existing_meter", "both"])
    result.add_argument("--start-position", default="1.1.1.1")
    result.add_argument("--target-name", default="streaming")
    result.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="omit project tracks below this zero-based index (for safe continuation)",
    )
    result.add_argument(
        "--end-index",
        type=int,
        help="omit project tracks above this inclusive zero-based index",
    )
    result.add_argument(
        "--tracks-snapshot",
        type=Path,
        help="reuse the ax_live logic://tracks capture from a prior runner JSONL",
    )
    result.add_argument("--output-root", type=Path, default=Path.home() / "Desktop" / "Logic-MCP-Audits")
    result.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    result.add_argument("--core", type=Path, default=DEFAULT_CORE)
    result.add_argument("--midi-instance", default="standalone-audit")
    result.add_argument("--strip-limit", type=int, default=128)
    result.add_argument("--per-strip-seconds", type=int, default=12)
    result.add_argument("--survey-seconds", type=int, default=180)
    result.add_argument("--bounce-timeout", type=int, default=900)
    result.add_argument(
        "--parameter-page-size",
        type=int,
        default=500,
        help="bulk AX page request; heterogeneous tables automatically fall back to 40 rows",
    )
    result.add_argument("--max-parameters", type=int, default=2000)
    result.add_argument("--max-steps", type=int, default=5000)
    result.add_argument(
        "--confirmed",
        action="store_true",
        help="required: permits solo, transport and non-overwriting bounce steps",
    )
    return result


async def async_main() -> int:
    args = parser().parse_args()
    if not args.confirmed:
        print("Refusing mutating audit without --confirmed", file=sys.stderr)
        return 2
    runner = AuditRunner(args)
    try:
        result = await runner.run()
    except BaseException as exc:
        runner.emit(
            "run_failed",
            error=f"{type(exc).__name__}: {exc}",
            traceback="".join(traceback.format_exception(exc))[-12000:],
        )
        return 1
    return 0 if result.get("complete") and not result.get("failed_steps") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
