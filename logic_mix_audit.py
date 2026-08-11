from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


SCHEMA = "logic_mix_audit_plan.v1"
VALID_SCOPES = {"track", "group", "aux", "bus", "master", "all"}
VALID_MEASUREMENTS = {"bounce_bs1770", "existing_meter", "both"}
MASTER_NAMES = {"master", "stereo out", "output 1-2", "main out", "main output"}
GENERIC_NAMES = {"", "audio plug-in", "midi plug-in", "channel strip", "track"}


def _items(value, *keys: str) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = _items(candidate, *keys)
            if nested:
                return nested
    return []


def _first(record: dict, *keys: str, default=None):
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return default


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip()).strip("-.")
    return text[:80] or "target"


def classify_channel(record: dict) -> str:
    explicit = str(
        _first(
            record,
            "kind",
            "type",
            "channel_type",
            "strip_type",
            "role",
            default="",
        )
    ).strip().casefold()
    aliases = {
        "audio": "track",
        "instrument": "track",
        "software instrument": "track",
        "drummer": "track",
        "external midi": "track",
        "track": "track",
        "track stack": "group",
        "stack": "group",
        "summing stack": "group",
        "folder stack": "group",
        "group": "group",
        "aux": "aux",
        "auxiliary": "aux",
        "bus": "bus",
        "output": "master",
        "master": "master",
        "stereo output": "master",
    }
    if explicit in aliases:
        return aliases[explicit]
    name = str(_first(record, "name", "title", "label", default="")).strip()
    folded = name.casefold()
    if folded in MASTER_NAMES or folded.endswith(" stereo out"):
        return "master"
    if re.fullmatch(r"bus\s+\d+", folded) or folded.endswith(" bus"):
        return "bus"
    if re.fullmatch(r"aux\s+\d+", folded) or folded.startswith("aux "):
        return "aux"
    if any(record.get(key) for key in ("members", "children", "child_refs")):
        return "group"
    return "track"


def _record_identity(record: dict) -> tuple:
    index = _integer(_first(record, "index", "trackIndex", "track_index", "strip"))
    name = str(_first(record, "name", "title", "label", default="")).strip().casefold()
    if index is not None and name:
        return ("index_name", index, name)
    ref = str(_first(record, "target_ref", "track_ref", "mixer_ref", "id", default=""))
    if ref:
        return ("ref", ref)
    return ("name", name)


def _normalise_record(record: dict, source: str) -> dict:
    name = str(_first(record, "name", "title", "label", default="")).strip()
    index = _integer(_first(record, "index", "trackIndex", "track_index", "strip"))
    ref = str(_first(record, "target_ref", "id", default=""))
    output = _first(record, "output", "output_bus", "destination")
    group = _first(record, "group", "parent", "parent_name", "stack")
    parent_ref = _first(record, "parent_ref", "group_ref", "stack_ref")
    inserts = record.get("inserts") if isinstance(record.get("inserts"), list) else []
    controls = (
        record.get("insert_controls")
        if isinstance(record.get("insert_controls"), list)
        else []
    )
    result = {
        "name": name,
        "index": index,
        "kind": classify_channel(record),
        "track_ref": ref if ref.startswith("trk_") else str(record.get("track_ref") or ""),
        "mixer_ref": ref if ref.startswith("mix_") else str(record.get("mixer_ref") or ""),
        "target_ref": ref,
        "output": output,
        "group": group,
        "parent_ref": parent_ref,
        "inserts": inserts,
        "insert_controls": controls,
        "control_paths": record.get("control_paths")
        if isinstance(record.get("control_paths"), dict)
        else {},
        "strip_path": str(record.get("path") or record.get("strip_path") or ""),
        "mute": record.get("mute"),
        "solo": record.get("solo"),
        "detail": record.get("detail", "unknown"),
        "sources": [source],
        "raw": {source: record},
    }
    if isinstance(record.get("members"), list):
        result["members"] = record["members"]
    if isinstance(record.get("children"), list):
        result["members"] = record["children"]
    return result


def normalise_inventory(tracks=None, mixer=None, ax_channels=None) -> dict:
    sources = [
        ("tracks", _items(tracks, "data", "tracks")),
        ("mixer", _items(mixer, "data", "strips", "channels")),
        ("ax", _items(ax_channels, "channels", "strips", "data")),
    ]
    merged: list[dict] = []
    by_identity: dict[tuple, int] = {}
    for source, records in sources:
        for raw in records:
            item = _normalise_record(raw, source)
            identity = _record_identity(item)
            position = by_identity.get(identity)
            if position is None and source in {"mixer", "ax"} and item.get("index") is not None:
                by_index = [
                    index
                    for index, current in enumerate(merged)
                    if current.get("index") == item["index"]
                ]
                if len(by_index) == 1 and (
                    item.get("name", "").casefold() in GENERIC_NAMES
                    or merged[by_index[0]].get("name", "").casefold()
                    == item.get("name", "").casefold()
                ):
                    position = by_index[0]
            if position is None:
                position = len(merged)
                by_identity[identity] = position
                merged.append(item)
                continue
            current = merged[position]
            for key, value in item.items():
                if key == "sources":
                    if source not in current["sources"]:
                        current["sources"].append(source)
                elif key == "raw":
                    current["raw"].update(value)
                elif key == "kind" and "tracks" in current["sources"] and source != "tracks":
                    continue
                elif key == "name" and str(value).casefold() in GENERIC_NAMES and current.get("name"):
                    continue
                elif value not in (None, "", [], "unknown"):
                    current[key] = value
    for item in merged:
        token = item.get("target_ref") or f'{item.get("kind")}:{item.get("index")}:{item.get("name")}'
        item["audit_id"] = hashlib.sha256(str(token).encode()).hexdigest()[:12]
        item["target_ref"] = (
            item.get("track_ref") or item.get("mixer_ref") or item.get("target_ref") or ""
        )
    merged.sort(
        key=lambda item: (
            item.get("index") is None,
            item.get("index") if item.get("index") is not None else 10**9,
            item.get("name", "").casefold(),
        )
    )
    return {
        "schema": "logic_mix_inventory.v1",
        "count": len(merged),
        "channels": merged,
        "source_counts": {source: len(records) for source, records in sources},
        "complete": bool(merged)
        and all(item.get("name") and item.get("detail") != "partial" for item in merged),
    }


def _selector_matches(channel: dict, selector: str) -> bool:
    needle = selector.strip().casefold()
    if not needle:
        return True
    values = (
        channel.get("name"),
        channel.get("audit_id"),
        channel.get("target_ref"),
        channel.get("track_ref"),
        channel.get("mixer_ref"),
    )
    return any(str(value or "").casefold() == needle for value in values)


def resolve_targets(channels: list[dict], scope: str, selector: str = "") -> dict:
    scope = scope.strip().casefold()
    if scope not in VALID_SCOPES:
        return {"error": f"scope must be one of {sorted(VALID_SCOPES)}"}
    candidates = [channel for channel in channels if scope == "all" or channel["kind"] == scope]
    if selector:
        exact = [channel for channel in candidates if _selector_matches(channel, selector)]
        if not exact:
            needle = selector.casefold()
            exact = [
                channel
                for channel in candidates
                if needle in str(channel.get("name") or "").casefold()
            ]
        candidates = exact
    if selector and len(candidates) > 1:
        return {
            "error": "selector is ambiguous",
            "matches": [
                {
                    "name": channel.get("name"),
                    "kind": channel.get("kind"),
                    "index": channel.get("index"),
                    "target_ref": channel.get("target_ref"),
                }
                for channel in candidates
            ],
        }
    if not candidates:
        return {"error": f"no {scope} target matched {selector!r}"}
    resolved = []
    for channel in candidates:
        target = dict(channel)
        refs = []
        primary = channel.get("track_ref") or (
            channel.get("target_ref")
            if str(channel.get("target_ref") or "").startswith("trk_")
            else ""
        )
        if primary:
            refs.append(primary)
        if channel["kind"] == "group":
            group_tokens = {
                str(channel.get("name") or "").casefold(),
                str(channel.get("audit_id") or "").casefold(),
                str(channel.get("target_ref") or "").casefold(),
            }
            for member in channels:
                parent_tokens = {
                    str(member.get("group") or "").casefold(),
                    str(member.get("parent_ref") or "").casefold(),
                }
                if group_tokens & parent_tokens:
                    ref = member.get("track_ref") or (
                        member.get("target_ref")
                        if str(member.get("target_ref") or "").startswith("trk_")
                        else ""
                    )
                    if ref and ref not in refs:
                        refs.append(ref)
        target["isolation_refs"] = refs
        resolved.append(target)
    return {"targets": resolved, "count": len(resolved)}


def _step(step_id: str, phase: str, server: str, operation: str, arguments=None, **extra):
    return {
        "step_id": step_id,
        "phase": phase,
        "server": server,
        "operation": operation,
        "arguments": arguments or {},
        "status": "pending",
        **extra,
    }


def build_plugin_inspection_steps(prefix: str, target: dict, inserts: list[str]) -> list[dict]:
    steps = []
    for insert_index, plugin in enumerate(inserts):
        plugin_prefix = f"{prefix}-plugin-{insert_index:02d}"
        steps.extend(
            [
                _step(
                    f"{plugin_prefix}-open",
                    "inspect",
                    "logic-plugins",
                    "plugin_open_insert",
                    {
                        "strip_path": target["strip_path"],
                        "insert_index": insert_index,
                        "expected_strip": target["name"],
                        "expected_plugin": plugin,
                        "dry_run": False,
                    },
                    target_id=target["audit_id"],
                ),
                _step(
                    f"{plugin_prefix}-snapshot",
                    "inspect",
                    "logic-plugins",
                    "plugin_snapshot",
                    {"window_index": 1},
                    target_id=target["audit_id"],
                    plugin=plugin,
                ),
                _step(
                    f"{plugin_prefix}-controls",
                    "inspect",
                    "logic-plugins",
                    "plugin_set_view",
                    {
                        "window_index": 1,
                        "view": "Controls",
                        "expected_plugin": plugin,
                        "expected_channel": target["name"],
                    },
                    target_id=target["audit_id"],
                    mutates_ui=True,
                ),
                _step(
                    f"{plugin_prefix}-parameters",
                    "inspect",
                    "logic-plugins",
                    "plugin_parameters",
                    {
                        "window_index": 1,
                        "limit": 500,
                        "expected_plugin": plugin,
                        "expected_channel": target["name"],
                    },
                    target_id=target["audit_id"],
                ),
                _step(
                    f"{plugin_prefix}-restore-view",
                    "inspect",
                    "logic-plugins",
                    "plugin_set_view",
                    {
                        "window_index": 1,
                        "expected_plugin": plugin,
                        "expected_channel": target["name"],
                    },
                    arguments_from={"view": f"{plugin_prefix}-snapshot.view_selector"},
                    target_id=target["audit_id"],
                    mutates_ui=True,
                    always_run=True,
                ),
                _step(
                    f"{plugin_prefix}-close",
                    "inspect",
                    "logic-plugins",
                    "plugin_close_verified",
                    {
                        "window_index": 1,
                        "expected_plugin": plugin,
                        "expected_channel": target["name"],
                        "dry_run": False,
                    },
                    target_id=target["audit_id"],
                    mutates_ui=True,
                    always_run=True,
                ),
            ]
        )
    return steps


def build_audit_plan(
    inventory: dict,
    scope: str,
    selector: str,
    project_path: str,
    output_root: str,
    measurement: str = "bounce_bs1770",
    start_position: str = "1.1.1.1",
    target_name: str = "streaming",
) -> dict:
    if measurement not in VALID_MEASUREMENTS:
        return {"error": f"measurement must be one of {sorted(VALID_MEASUREMENTS)}"}
    if not Path(project_path).is_absolute():
        return {"error": "project_path must be absolute"}
    if not Path(output_root).is_absolute():
        return {"error": "output_root must be absolute"}
    selected = resolve_targets(inventory.get("channels", []), scope, selector)
    if "error" in selected:
        return selected
    seed = json.dumps(
        {
            "scope": scope,
            "selector": selector,
            "project": project_path,
            "targets": [target["audit_id"] for target in selected["targets"]],
        },
        sort_keys=True,
    )
    plan_id = "audit-" + hashlib.sha256(seed.encode()).hexdigest()[:12]
    steps = [
        _step("preflight-project", "preflight", "logic-pro", "logic_project.audit"),
        _step(
            "capture-state",
            "capture",
            "client",
            "read_resources",
            {"uris": ["logic://transport/state", "logic://tracks", "logic://mixer", "logic://project/info"]},
            save_as="initial_state",
        ),
    ]
    ax_restore_state = [
        {
            "name": channel.get("name"),
            "kind": channel.get("kind"),
            "strip_path": channel.get("strip_path"),
            "track_ref": channel.get("track_ref"),
            "solo": channel.get("solo"),
            "mute": channel.get("mute"),
        }
        for channel in inventory.get("channels", [])
        if channel.get("strip_path")
    ]
    for position, target in enumerate(selected["targets"], start=1):
        prefix = f"target-{position:03d}-{target['audit_id']}"
        refs = target.get("isolation_refs", [])
        if refs or (
            "tracks" in target.get("sources", []) and target.get("index") is not None
        ):
            steps.append(
                _step(
                    f"{prefix}-inventory",
                    "inspect",
                    "logic-pro",
                    "logic_plugins.get_inventory",
                    {
                        **({"target_ref": refs[0]} if refs else {}),
                        **(
                            {"track": target["index"]}
                            if not refs and target.get("index") is not None
                            else {}
                        ),
                    },
                    target_id=target["audit_id"],
                )
            )
        else:
            steps.append(
                _step(
                    f"{prefix}-inventory-ax-only",
                    "inspect",
                    "client",
                    "record_limitation",
                    {
                        "reason": "mature plugin inventory has no track reference for this mixer-only strip",
                        "fallback": "AX insert inventory",
                    },
                    target_id=target["audit_id"],
                )
            )
        if target.get("strip_path"):
            steps.extend(
                [
                    _step(
                        f"{prefix}-reveal",
                        "inspect",
                        "logic-plugins",
                        "mixer_reveal_strip",
                        {
                            "strip_path": target["strip_path"],
                            "expected_strip": target["name"],
                            "dry_run": False,
                        },
                        target_id=target["audit_id"],
                        mutates_ui=True,
                    ),
                    _step(
                        f"{prefix}-read-strip",
                        "inspect",
                        "logic-plugins",
                        "mixer_read_strip",
                        {
                            "strip_path": target["strip_path"],
                            "expected_strip": target["name"],
                        },
                        target_id=target["audit_id"],
                        expand_plugin_steps=True,
                        plugin_prefix=prefix,
                    ),
                ]
            )
        steps.append(
            _step(
                f"{prefix}-isolate",
                "isolate",
                "logic-plugins",
                "mix_isolation_dispatch",
                {"target": target, "ax_state": ax_restore_state},
                arguments_from={"initial_state": "capture-state.$"},
                target_id=target["audit_id"],
                mutates_logic=True,
                requires_dispatch_execution=True,
                compensation={"restore_from": "initial_state"},
            )
        )
        steps.append(
            _step(
                f"{prefix}-locate",
                "measure",
                "logic-pro",
                "logic_transport.goto_position",
                {"position": start_position},
                target_id=target["audit_id"],
                mutates_logic=True,
                compensation={"restore_from": "initial_state"},
            )
        )
        if measurement in {"bounce_bs1770", "both"}:
            target_path = str(
                Path(output_root)
                / plan_id
                / f"{position:03d}-{_slug(target['kind'])}-{_slug(target['name'])}.wav"
            )
            steps.append(
                _step(
                    f"{prefix}-bounce",
                    "measure",
                    "logic-plugins",
                    "mix_bounce_target",
                    {
                        "target_path": target_path,
                        "expected_project_path": project_path,
                        "confirmed": True,
                    },
                    target_id=target["audit_id"],
                    mutates_ui=True,
                    produces="artifact",
                )
            )
            steps.append(
                _step(
                    f"{prefix}-analyze",
                    "measure",
                    "logic-audio",
                    "loudness_measure",
                    {"path_from": f"{prefix}-bounce.artifact"},
                    target_id=target["audit_id"],
                    target_profile=target_name,
                )
            )
        if measurement in {"existing_meter", "both"}:
            meter_candidates = [
                (index, plugin)
                for index, plugin in enumerate(target.get("inserts", []))
                if any(
                    term in str(plugin).casefold()
                    for term in ("meter", "loudness", "analyzer", "insight", "ozone")
                )
            ] if target.get("strip_path") else []
            if not meter_candidates:
                steps.append(
                    _step(
                        f"{prefix}-meter-unavailable",
                        "measure",
                        "client",
                        "record_limitation",
                        {
                            "reason": "no existing analyzer insert was identified",
                            "fallback": "bounce_bs1770",
                        },
                        target_id=target["audit_id"],
                    )
                )
            for meter_index, (insert_index, plugin) in enumerate(meter_candidates):
                meter_prefix = f"{prefix}-meter-{meter_index:02d}"
                steps.extend(
                    [
                        _step(
                            f"{meter_prefix}-open",
                            "measure",
                            "logic-plugins",
                            "plugin_open_insert",
                            {
                                "strip_path": target["strip_path"],
                                "insert_index": insert_index,
                                "expected_strip": target["name"],
                                "expected_plugin": plugin,
                                "dry_run": False,
                            },
                            target_id=target["audit_id"],
                        ),
                        _step(
                            f"{meter_prefix}-read",
                            "measure",
                            "logic-plugins",
                            "plugin_meter_read",
                            {
                                "window_index": 1,
                                "expected_plugin": plugin,
                                "expected_channel": target["name"],
                            },
                            target_id=target["audit_id"],
                        ),
                        _step(
                            f"{meter_prefix}-close",
                            "measure",
                            "logic-plugins",
                            "plugin_close_verified",
                            {
                                "window_index": 1,
                                "expected_plugin": plugin,
                                "expected_channel": target["name"],
                                "dry_run": False,
                            },
                            target_id=target["audit_id"],
                            always_run=True,
                        ),
                    ]
                )
        steps.append(
            _step(
                f"{prefix}-restore",
                "restore",
                "logic-plugins",
                "mix_restore_dispatch",
                {"ax_state": ax_restore_state},
                arguments_from={"initial_state": "capture-state.$"},
                target_id=target["audit_id"],
                always_run=True,
                requires_dispatch_execution=True,
            )
        )
    steps.append(
        _step(
            "final-restore",
            "restore",
            "logic-plugins",
            "mix_restore_dispatch",
            {"ax_state": ax_restore_state},
            arguments_from={"initial_state": "capture-state.$"},
            always_run=True,
            requires_dispatch_execution=True,
        )
    )
    return {
        "schema": SCHEMA,
        "plan_id": plan_id,
        "dry_run": True,
        "confirmation_required": True,
        "project_expected_path": project_path,
        "output_root": output_root,
        "scope": scope,
        "selector": selector or None,
        "measurement": measurement,
        "targets": selected["targets"],
        "target_count": selected["count"],
        "steps": steps,
        "safety": {
            "writes_parameters": False,
            "mutates_solo_and_transport_only_after_confirmation": True,
            "restoration_steps": sum(1 for step in steps if step["phase"] == "restore"),
            "bounce_never_overwrites": True,
            "fixes_require_separate_plugin_plan": True,
        },
    }


def build_fix_plan(inventory: dict, fixes: list[dict], project_path: str) -> dict:
    if not Path(project_path).is_absolute():
        return {"error": "project_path must be absolute"}
    if not isinstance(fixes, list) or not fixes:
        return {"error": "fixes must be a non-empty list"}
    resolved_fixes = []
    for position, fix in enumerate(fixes):
        if not isinstance(fix, dict):
            return {"error": f"fix {position} is not an object"}
        missing = [key for key in ("target", "plugin", "parameter", "value") if key not in fix]
        if missing:
            return {"error": f"fix {position} is missing {missing}"}
        scope = str(fix.get("scope") or "all")
        selected = resolve_targets(inventory.get("channels", []), scope, str(fix["target"]))
        if "error" in selected:
            return {"error": f"fix {position}: {selected['error']}", **{k: v for k, v in selected.items() if k != "error"}}
        if selected["count"] != 1:
            return {"error": f"fix {position}: target must resolve exactly once"}
        target = selected["targets"][0]
        if not target.get("strip_path"):
            return {"error": f"fix {position}: target has no current AX strip_path"}
        plugin = str(fix["plugin"])
        matches = [
            index
            for index, name in enumerate(target.get("inserts", []))
            if str(name).strip().casefold() == plugin.strip().casefold()
        ]
        requested_index = _integer(fix.get("insert_index"))
        if requested_index is not None:
            if requested_index not in matches:
                return {
                    "error": f"fix {position}: insert_index does not hold expected plugin",
                    "matches": matches,
                    "inserts": target.get("inserts", []),
                }
            insert_index = requested_index
        elif len(matches) == 1:
            insert_index = matches[0]
        elif not matches:
            return {"error": f"fix {position}: plugin {plugin!r} is not in the readable chain"}
        else:
            return {"error": f"fix {position}: plugin is duplicated; insert_index is required", "matches": matches}
        resolved_fixes.append(
            {
                **fix,
                "target_record": target,
                "plugin": plugin,
                "insert_index": insert_index,
                "parameter": str(fix["parameter"]),
                "value": str(fix["value"]),
                "expected_before": str(fix.get("expected_before") or ""),
            }
        )
    seed = json.dumps(
        [
            {
                "target": fix["target_record"]["audit_id"],
                "plugin": fix["plugin"],
                "insert": fix["insert_index"],
                "parameter": fix["parameter"],
                "value": fix["value"],
            }
            for fix in resolved_fixes
        ],
        sort_keys=True,
    )
    plan_id = "fix-" + hashlib.sha256((project_path + seed).encode()).hexdigest()[:12]
    steps = [
        _step("preflight-project", "preflight", "logic-pro", "logic_project.audit"),
        _step(
            "capture-state",
            "capture",
            "client",
            "read_resources",
            {"uris": ["logic://transport/state", "logic://tracks", "logic://mixer", "logic://project/info"]},
            save_as="initial_state",
        ),
    ]
    ax_restore_state = [
        {
            "name": channel.get("name"),
            "kind": channel.get("kind"),
            "strip_path": channel.get("strip_path"),
            "track_ref": channel.get("track_ref"),
            "solo": channel.get("solo"),
            "mute": channel.get("mute"),
        }
        for channel in inventory.get("channels", [])
        if channel.get("strip_path")
    ]
    public_fixes = []
    for position, fix in enumerate(resolved_fixes, start=1):
        target = fix["target_record"]
        prefix = f"fix-{position:03d}-{target['audit_id']}"
        public_fixes.append(
            {
                "target": target["name"],
                "target_id": target["audit_id"],
                "plugin": fix["plugin"],
                "insert_index": fix["insert_index"],
                "parameter": fix["parameter"],
                "value": fix["value"],
                "expected_before": fix["expected_before"] or None,
            }
        )
        steps.extend(
            [
                _step(
                    f"{prefix}-open",
                    "fix",
                    "logic-plugins",
                    "plugin_open_insert",
                    {
                        "strip_path": target["strip_path"],
                        "insert_index": fix["insert_index"],
                        "expected_strip": target["name"],
                        "expected_plugin": fix["plugin"],
                        "dry_run": False,
                    },
                    target_id=target["audit_id"],
                ),
                _step(
                    f"{prefix}-snapshot",
                    "fix",
                    "logic-plugins",
                    "plugin_snapshot",
                    {"window_index": 1},
                    target_id=target["audit_id"],
                ),
                _step(
                    f"{prefix}-controls",
                    "fix",
                    "logic-plugins",
                    "plugin_set_view",
                    {
                        "window_index": 1,
                        "view": "Controls",
                        "expected_plugin": fix["plugin"],
                        "expected_channel": target["name"],
                    },
                    target_id=target["audit_id"],
                    mutates_ui=True,
                ),
                _step(
                    f"{prefix}-write",
                    "fix",
                    "logic-plugins",
                    "plugin_write_label_verified",
                    {
                        "window_index": 1,
                        "parameter": fix["parameter"],
                        "value": fix["value"],
                        "expected_before": fix["expected_before"],
                        "expected_plugin": fix["plugin"],
                        "expected_channel": target["name"],
                        "dry_run": False,
                    },
                    target_id=target["audit_id"],
                    writes_parameter=True,
                ),
                _step(
                    f"{prefix}-restore-view",
                    "restore",
                    "logic-plugins",
                    "plugin_set_view",
                    {
                        "window_index": 1,
                        "expected_plugin": fix["plugin"],
                        "expected_channel": target["name"],
                    },
                    arguments_from={"view": f"{prefix}-snapshot.view_selector"},
                    target_id=target["audit_id"],
                    always_run=True,
                ),
                _step(
                    f"{prefix}-close",
                    "restore",
                    "logic-plugins",
                    "plugin_close_verified",
                    {
                        "window_index": 1,
                        "expected_plugin": fix["plugin"],
                        "expected_channel": target["name"],
                        "dry_run": False,
                    },
                    target_id=target["audit_id"],
                    always_run=True,
                ),
            ]
        )
    steps.append(
        _step(
            "final-restore",
            "restore",
            "logic-plugins",
            "mix_restore_dispatch",
            {"ax_state": ax_restore_state},
            arguments_from={"initial_state": "capture-state.$"},
            always_run=True,
            requires_dispatch_execution=True,
        )
    )
    return {
        "schema": "logic_mix_fix_plan.v1",
        "plan_id": plan_id,
        "dry_run": True,
        "confirmation_required": True,
        "project_expected_path": project_path,
        "fixes": public_fixes,
        "fix_count": len(public_fixes),
        "steps": steps,
        "safety": {
            "exact_target_required": True,
            "exact_plugin_required": True,
            "exact_parameter_label_required": True,
            "expected_before_supported": True,
            "independent_readback_required": True,
            "remeasure_after_apply": True,
        },
    }


def review_measurements(
    measurements: list[dict],
    integrated_lufs: float,
    tolerance_lu: float,
    true_peak_max: float,
) -> dict:
    results = []
    for item in measurements:
        measured = item.get("integrated_lufs")
        peak = item.get("true_peak_dbtp")
        problems = []
        recommendations = []
        if measured is None:
            problems.append("integrated loudness was not measured")
        else:
            deviation = round(float(measured) - float(integrated_lufs), 2)
            if abs(deviation) > float(tolerance_lu):
                problems.append(f"integrated loudness is {deviation:+g} LU from target")
                recommendations.append(
                    {
                        "kind": "gain_review",
                        "suggested_delta_db": round(-deviation, 2),
                        "automatic_write": False,
                        "reason": "compression and limiting make loudness response nonlinear; resolve an exact plugin parameter before applying",
                    }
                )
        if peak is not None and float(peak) > float(true_peak_max):
            problems.append(f"true peak {peak} dBTP exceeds {true_peak_max} dBTP")
            recommendations.append(
                {
                    "kind": "limiter_ceiling_review",
                    "suggested_ceiling_dbtp": float(true_peak_max),
                    "automatic_write": False,
                }
            )
        inserts = item.get("inserts") or []
        for position, name in enumerate(inserts):
            if "s1 imager" in str(name).casefold() and position != len(inserts) - 1:
                problems.append("S1 Imager is not last in the chain")
                recommendations.append(
                    {
                        "kind": "chain_order",
                        "automatic_write": False,
                        "reason": "insert reordering is unavailable and unsafe through current channels",
                    }
                )
        results.append(
            {
                **item,
                "verdict": "pass" if not problems else "review",
                "problems": problems,
                "recommendations": recommendations,
            }
        )
    return {
        "schema": "logic_mix_audit_review.v1",
        "target": {
            "integrated_lufs": integrated_lufs,
            "tolerance_lu": tolerance_lu,
            "true_peak_max": true_peak_max,
        },
        "reviewed": len(results),
        "passed": sum(1 for item in results if item["verdict"] == "pass"),
        "needs_review": sum(1 for item in results if item["verdict"] != "pass"),
        "results": results,
    }


def compare_before_after(before: list[dict], after: list[dict]) -> dict:
    def key(item):
        return str(item.get("target_id") or item.get("name") or "").casefold()

    earlier = {key(item): item for item in before}
    comparisons = []
    for current in after:
        previous = earlier.get(key(current))
        if previous is None:
            comparisons.append({"target": key(current), "error": "no before measurement"})
            continue
        comparisons.append(
            {
                "target": current.get("name") or current.get("target_id"),
                "integrated_lufs_before": previous.get("integrated_lufs"),
                "integrated_lufs_after": current.get("integrated_lufs"),
                "integrated_change_lu": _delta(previous.get("integrated_lufs"), current.get("integrated_lufs")),
                "true_peak_before": previous.get("true_peak_dbtp"),
                "true_peak_after": current.get("true_peak_dbtp"),
                "true_peak_change_db": _delta(previous.get("true_peak_dbtp"), current.get("true_peak_dbtp")),
            }
        )
    return {"schema": "logic_mix_before_after.v1", "comparisons": comparisons}


def build_isolation_dispatch(
    initial_state: dict,
    target: dict,
    ax_state: list[dict] | None = None,
) -> dict:
    """Build exclusive-solo intents from the captured state without confusing mixer and
    track indices. Master clears every known solo; groups enable their member refs."""
    tracks_payload = initial_state.get("logic://tracks") or initial_state.get("tracks") or {}
    track_rows = _items(tracks_payload, "data", "tracks")
    desired_refs = set(target.get("isolation_refs") or [])
    target_index = target.get("index")
    target_is_master = target.get("kind") == "master"
    dispatches = []
    target_addressed = target_is_master
    for row in track_rows:
        ref = str(_first(row, "target_ref", "track_ref", "id", default=""))
        index = _integer(_first(row, "index", "trackIndex", "track_index"))
        selector = {"target_ref": ref} if ref.startswith("trk_") else ({"index": index} if index is not None else {})
        if not selector:
            continue
        enabled = False
        if not target_is_master:
            enabled = ref in desired_refs if desired_refs else (
                "tracks" in target.get("sources", []) and index == target_index
            )
        if enabled:
            target_addressed = True
        current = _boolean(_first(row, "solo", "soloed"))
        if current is None or current != enabled:
            dispatches.append(
                {
                    "server": "logic-pro",
                    "operation": "logic_tracks.solo",
                    "arguments": {**selector, "enabled": enabled},
                    "verify": {"resource": "logic://tracks", "field": "solo", "equals": enabled},
                }
            )
    for row in ax_state or []:
        if row.get("track_ref") or not row.get("strip_path"):
            continue
        enabled = False
        if not target_is_master:
            enabled = (
                row.get("strip_path") == target.get("strip_path")
                and str(row.get("name") or "").casefold()
                == str(target.get("name") or "").casefold()
            )
        if enabled:
            target_addressed = True
        current = _boolean(row.get("solo"))
        if current is None or current != enabled:
            dispatches.append(
                {
                    "server": "logic-plugins",
                    "operation": "mixer_set_toggle",
                    "arguments": {
                        "strip_path": row["strip_path"],
                        "control": "solo",
                        "enabled": enabled,
                        "expected_strip": row.get("name", ""),
                        "dry_run": False,
                    },
                }
            )
    return {
        "schema": "logic_mix_isolation_dispatch.v1",
        "ok": target_addressed,
        "complete": target_addressed and bool(track_rows or ax_state),
        "target": {
            "name": target.get("name"),
            "kind": target.get("kind"),
            "audit_id": target.get("audit_id"),
        },
        "dispatch_count": len(dispatches),
        "dispatches": dispatches,
        "requires_execution_and_readback": True,
        "error": None if target_addressed else "target could not be addressed for exclusive solo",
    }


def build_restore_dispatch(initial_state: dict, ax_state: list[dict] | None = None) -> dict:
    """Turn captured mature-server resources into explicit idempotent restore intents."""
    tracks_payload = initial_state.get("logic://tracks") or initial_state.get("tracks") or {}
    transport = (
        initial_state.get("logic://transport/state")
        or initial_state.get("transport")
        or {}
    )
    track_rows = _items(tracks_payload, "data", "tracks")
    dispatches = []
    selected = None
    for row in track_rows:
        ref = str(_first(row, "target_ref", "track_ref", "id", default=""))
        index = _integer(_first(row, "index", "trackIndex", "track_index"))
        selector = {"target_ref": ref} if ref.startswith("trk_") else ({"index": index} if index is not None else {})
        if not selector:
            continue
        for command, keys in (("solo", ("solo", "soloed")), ("mute", ("mute", "muted"))):
            raw = _first(row, *keys)
            enabled = _boolean(raw)
            if enabled is None:
                continue
            dispatches.append(
                {
                    "server": "logic-pro",
                    "operation": f"logic_tracks.{command}",
                    "arguments": {**selector, "enabled": enabled},
                    "verify": {"resource": "logic://tracks", "field": command, "equals": enabled},
                }
            )
        if _boolean(_first(row, "selected", "is_selected")):
            selected = selector
    if selected:
        dispatches.append(
            {
                "server": "logic-pro",
                "operation": "logic_tracks.select",
                "arguments": selected,
            }
        )
    position = _first(transport, "position", "playhead_position", "playhead")
    if position:
        dispatches.append(
            {
                "server": "logic-pro",
                "operation": "logic_transport.goto_position",
                "arguments": {"position": str(position)},
            }
        )
    cycle = _boolean(_first(transport, "cycle", "cycle_enabled"))
    if cycle is not None:
        dispatches.append(
            {
                "server": "logic-pro",
                "operation": "client.ensure_transport_state",
                "arguments": {"field": "cycle", "equals": cycle, "toggle_command": "toggle_cycle"},
            }
        )
    playing = _boolean(_first(transport, "playing", "is_playing"))
    if playing is not None:
        dispatches.append(
            {
                "server": "logic-pro",
                "operation": "client.ensure_transport_state",
                "arguments": {
                    "field": "playing",
                    "equals": playing,
                    "true_command": "play",
                    "false_command": "stop",
                },
            }
        )
    for row in ax_state or []:
        if row.get("track_ref") or not row.get("strip_path"):
            continue
        for control in ("solo", "mute"):
            enabled = _boolean(row.get(control))
            if enabled is None:
                continue
            dispatches.append(
                {
                    "server": "logic-plugins",
                    "operation": "mixer_set_toggle",
                    "arguments": {
                        "strip_path": row["strip_path"],
                        "control": control,
                        "enabled": enabled,
                        "expected_strip": row.get("name", ""),
                        "dry_run": False,
                    },
                }
            )
    return {
        "schema": "logic_mix_restore_dispatch.v1",
        "dispatch_count": len(dispatches),
        "dispatches": dispatches,
        "complete": bool(track_rows) and bool(transport),
        "note": "each intent must be read back; missing fields are never guessed",
    }


def _boolean(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    folded = str(value).strip().casefold()
    if folded in {"1", "true", "on", "yes", "enabled", "selected"}:
        return True
    if folded in {"0", "false", "off", "no", "disabled", "not selected"}:
        return False
    return None


def _delta(before, after):
    if before is None or after is None:
        return None
    return round(float(after) - float(before), 2)
