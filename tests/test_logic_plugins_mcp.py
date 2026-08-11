import unittest
from unittest import mock
from pathlib import Path
import tempfile

import logic_plugins_mcp as plugins


class NumberHandlingTests(unittest.TestCase):
    def test_fourcc_preserves_spaces_and_symbols(self):
        self.assertEqual(plugins.fourcc(int.from_bytes(b"aac ", "big")), "aac ")
        self.assertEqual(plugins.fourcc(int.from_bytes(b"pc+3", "big")), "pc+3")

    def test_values_match_accepts_system_decimal_separator(self):
        self.assertTrue(plugins.values_match("21.0", "21,0"))
        self.assertTrue(plugins.values_match("-12", "-12,0 dB"))
        self.assertFalse(plugins.values_match("-12 dB", "-12 LUFS"))

    def test_fader_conversion_stays_inside_measured_span(self):
        self.assertEqual(plugins.fader_db_from_raw("173"), 0.0)
        self.assertEqual(plugins.fader_db_from_raw("113,0"), -6.0)
        self.assertIsNone(plugins.fader_db_from_raw("112.9"))
        self.assertIsNone(plugins.fader_db_from_raw("not a number"))


class ProjectIdentityTests(unittest.TestCase):
    def test_tracks_window_title_selects_active_bundle_not_shorter_template(self):
        selected = plugins.select_open_logic_project(
            "Project - Tracks",
            [
                Path("/Users/test/Music/Templates/01 Hip Hop.logicx"),
                Path("/Volumes/Work/Video/Project.logicx"),
            ],
        )
        self.assertEqual(selected, Path("/Volumes/Work/Video/Project.logicx"))

    def test_duplicate_project_names_fail_closed(self):
        selected = plugins.select_open_logic_project(
            "Song.logicx - Tracks",
            [Path("/Volumes/A/Song.logicx"), Path("/Volumes/B/Song.logicx")],
        )
        self.assertIsNone(selected)

    def test_exact_open_project_is_verified_without_writing(self):
        with mock.patch.object(
            plugins, "find_open_logic_project", return_value=Path("/tmp/Test.logicx")
        ):
            result = plugins.mix_project_identity("/tmp/Test.logicx")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertFalse(result["write_attempted"])

    def test_wrong_open_project_fails_closed(self):
        with mock.patch.object(
            plugins, "find_open_logic_project", return_value=Path("/tmp/Other.logicx")
        ):
            result = plugins.mix_project_identity("/tmp/Test.logicx")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("does not match", result["error"])


class TransportPositionTests(unittest.TestCase):
    def test_position_normalisation_reads_segmented_ax_value(self):
        self.assertEqual(plugins.normalise_logic_position("\t101\t3\t1\t1"), "101.3.1.1")
        self.assertIsNone(plugins.normalise_logic_position("101.3.1"))

    def test_goto_is_dry_run_by_default(self):
        with mock.patch.object(plugins, "require_logic") as require_logic:
            result = plugins.transport_goto_position("101.3.1.1")
        require_logic.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["verified"])

    def test_goto_rejects_noncanonical_position_before_ui(self):
        with mock.patch.object(plugins, "require_logic") as require_logic:
            result = plugins.transport_goto_position("101,3,1,1", dry_run=False)
        require_logic.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertFalse(result["write_attempted"])


class BounceSafetyTests(unittest.TestCase):
    def test_bounce_is_dry_run_until_confirmed(self):
        project = Path("/tmp/Test.logicx")
        target = Path("/tmp/logic-audit-test/master.wav")
        with (
            mock.patch.object(plugins, "find_open_logic_project", return_value=project),
            mock.patch.object(plugins, "run_accessible_bounce") as bounce,
        ):
            result = plugins.mix_bounce_target(str(target), str(project))
        bounce.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["confirmation_required"])

    def test_confirmed_bounce_verifies_helper_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Test.logicx"
            target = root / "audit" / "master.wav"

            def render(planned, _timeout):
                planned.parent.mkdir()
                planned.write_bytes(b"RIFF" + b"0" * 128)
                return {
                    "success": True,
                    "bounce_fired": True,
                    "artifact": str(planned),
                    "size_bytes": planned.stat().st_size,
                }

            with (
                mock.patch.object(plugins, "find_open_logic_project", return_value=project),
                mock.patch.object(plugins, "run_accessible_bounce", side_effect=render),
            ):
                result = plugins.mix_bounce_target(
                    str(target), str(project), confirmed=True
                )
            self.assertTrue(result["ok"])
            self.assertTrue(result["verified"])
            self.assertEqual(result["artifact"], str(target))

    def test_existing_stem_collision_is_refused_before_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Test.logicx"
            target = root / "master.wav"
            target.with_suffix(".aif").write_bytes(b"existing")
            with (
                mock.patch.object(plugins, "find_open_logic_project", return_value=project),
                mock.patch.object(plugins, "run_accessible_bounce") as bounce,
            ):
                result = plugins.mix_bounce_target(
                    str(target), str(project), confirmed=True
                )
            bounce.assert_not_called()
            self.assertFalse(result["ok"])
            self.assertIn("refusing to overwrite", result["error"])


class WindowSelectionTests(unittest.TestCase):
    def test_tracks_window_wins_over_front_plugin_dialog(self):
        listing = (
            "1~AXDialog~Ozone|:|"
            "2~AXStandardWindow~Example Project - Tracks|:|"
            "3~AXStandardWindow~Mixer|:|"
        )
        with mock.patch.object(plugins, "osa", return_value=listing):
            self.assertEqual(plugins.main_window_index("Logic Pro"), 2)

    def test_first_standard_window_is_safe_fallback(self):
        listing = "1~AXDialog~Plugin|:|3~AXStandardWindow~Project|:|"
        with mock.patch.object(plugins, "osa", return_value=listing):
            self.assertEqual(plugins.main_window_index("Logic Pro"), 3)

    def test_accessibility_probe_reports_real_ax_denial(self):
        denial = plugins.ProbeError("assistive access denied (-25211)")
        with mock.patch.object(plugins, "osa", side_effect=denial):
            status = plugins.accessibility_status("Logic Pro")
        self.assertFalse(status["granted"])
        self.assertIn("-25211", status["reason"])


class ParameterTableTests(unittest.TestCase):
    def test_filter_is_applied_before_match_pagination(self):
        rows = (
            "4#"
            "1~Threshold Low~-12,0~AXSlider|:|"
            "2~Ratio~4,0~AXSlider|:|"
            "3~Threshold High~-6,0~AXSlider|:|"
            "4~Threshold Ceiling~-1,0~AXSlider|:|"
        )
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "find_parameter_table", return_value="2.4"),
            mock.patch.object(plugins, "osa", return_value=rows),
        ):
            result = plugins.plugin_parameters(
                window_index=1,
                contains="threshold",
                offset=1,
                limit=1,
            )
        self.assertEqual(result["rows_total"], 4)
        self.assertEqual(result["matched_total"], 3)
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(result["parameters"][0]["label"], "Threshold High")


class MixerParsingTests(unittest.TestCase):
    def test_ax_order_is_reversed_into_signal_flow(self):
        kids = [
            {"role": "AXSlider", "description": "send knob", "value": "-20"},
            {"role": "AXGroup", "description": "Bus 2", "value": ""},
            {"role": "AXSlider", "description": "send knob", "value": "-10"},
            {"role": "AXGroup", "description": "Bus 1", "value": ""},
            {"role": "AXGroup", "description": "insert bar", "value": ""},
            {"role": "AXGroup", "description": "Loudness Meter", "value": ""},
            {"role": "AXGroup", "description": "Ozone", "value": ""},
        ]
        row = plugins.parse_strip("Stereo Out", "1.2", kids)
        self.assertEqual(row["inserts"], ["Ozone", "Loudness Meter"])
        self.assertEqual(
            [item["name"] for item in row["insert_controls"]],
            ["Ozone", "Loudness Meter"],
        )
        self.assertEqual(
            row["sends"],
            [{"bus": "Bus 1", "level": "-10"}, {"bus": "Bus 2", "level": "-20"}],
        )
        self.assertEqual(row["order"], "signal flow, first processed first")

    def test_undocumented_send_slider_value_is_not_reported_as_a_level(self):
        kids = [
            {"role": "AXSlider", "description": "send knob", "value": "1,50994944E+9"},
            {"role": "AXGroup", "description": "Bus 24", "value": ""},
            {"role": "AXGroup", "description": "insert bar", "value": ""},
        ]
        row = plugins.parse_strip("Lead", "1.2", kids)
        send = row["sends"][0]
        self.assertIsNone(send["level"])
        self.assertEqual(send["level_raw"], "1,50994944E+9")
        self.assertIn("not guessed", send["level_note"])

    def test_survey_retries_from_first_incomplete_strip(self):
        strips = [
            {"index": 0, "path": "9.2.1", "name": "A"},
            {"index": 1, "path": "9.2.2", "name": "B"},
            {"index": 2, "path": "9.2.3", "name": "C"},
        ]
        readable = [
            {"role": "AXStaticText", "description": "name", "value": "A", "name": "", "path": "9.2.1.1"},
            {"role": "AXSlider", "description": "volume fader", "value": "173", "name": "", "path": "9.2.1.2"},
            {"role": "AXStaticText", "description": "volume fader level", "value": "", "name": "0 dB", "path": "9.2.1.3"},
            {"role": "AXButton", "description": "mute", "value": "off", "name": "", "path": "9.2.1.4"},
            {"role": "AXButton", "description": "solo", "value": "off", "name": "", "path": "9.2.1.5"},
            {"role": "AXValueIndicator", "description": "peak level meter", "value": "clipping off", "name": "", "path": "9.2.1.6"},
        ]
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(
                plugins,
                "mixer_strips",
                return_value={
                    "count": 3,
                    "strips": strips,
                    "mixer_path": "9.2",
                    "window_index": 1,
                },
            ),
            mock.patch.object(
                plugins,
                "walk_window",
                side_effect=[readable, plugins.ProbeError("timed out"), readable],
            ) as walk,
        ):
            result = plugins.mixer_survey(strip_limit=3, total_seconds=60)

        self.assertEqual(result["strips_parsed"], 2)
        self.assertEqual(result["retry_offsets"], [1])
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual([row["index"] for row in result["channels"]], [0, 2])
        self.assertTrue(all(call.args[2] == 0 for call in walk.call_args_list))


class PluginWriteSafetyTests(unittest.TestCase):
    def test_live_write_requires_window_identity(self):
        with mock.patch.object(plugins, "require_logic", return_value="Logic Pro"):
            result = plugins.plugin_write_path("1.2", "-6", dry_run=False)
        self.assertFalse(result["ok"])
        self.assertFalse(result["write_attempted"])
        self.assertIn("requires expected_plugin", result["error"])

    def test_stale_window_is_refused_before_control_resolution(self):
        identity = {"plugin": "Loudness Meter", "channel": "Stereo Out"}
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "read_plugin_identity", return_value=identity),
            mock.patch.object(plugins, "read_role_and_value") as read_control,
        ):
            result = plugins.plugin_write_path(
                "1.2",
                "-6",
                dry_run=False,
                expected_plugin="Ozone 9 Elements",
                expected_channel="Stereo Out",
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["write_attempted"])
        read_control.assert_not_called()

    def test_label_write_resolves_fresh_path_and_preserves_identity(self):
        table = {
            "parameters": [
                {
                    "label": "Threshold",
                    "display": "-12,0",
                    "path": "2.4.1.7.1.2",
                }
            ]
        }
        outcome = {"ok": True, "dry_run": True, "path": "2.4.1.7.1.2"}
        with (
            mock.patch.object(plugins, "plugin_parameters", return_value=table),
            mock.patch.object(plugins, "plugin_write_path", return_value=outcome) as write,
        ):
            result = plugins.plugin_write_label_verified(
                "Threshold",
                "-6",
                expected_plugin="Ozone 9 Elements",
                expected_channel="Stereo Out",
                expected_before="-12.0",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["resolved_path"], "2.4.1.7.1.2")
        self.assertEqual(write.call_args.kwargs["expected_plugin"], "Ozone 9 Elements")

    def test_wrapped_slider_steps_by_display_not_encoded_raw_value(self):
        displays = iter(["-0.10", "199", "-1.10", "189", "-1.00", "190"])
        with (
            mock.patch.object(
                plugins,
                "resolve_group_control",
                return_value=("UI element 1 of group", "AXSlider"),
            ),
            mock.patch.object(plugins, "read_value", side_effect=lambda *_: next(displays)),
            mock.patch.object(plugins, "osa") as osa,
            mock.patch.object(plugins, "send_value") as send_value,
            mock.patch.object(plugins.time, "sleep"),
        ):
            result = plugins.write_group_display_control(
                "Logic Pro",
                "group",
                "10.1.109.1.2",
                "-1.00",
                dry_run=False,
                max_steps=64,
            )
        self.assertTrue(result["verified"])
        self.assertEqual(result["after"], "-1.00")
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["method"], "calibrated raw stepping")
        self.assertIn("AXDecrement", osa.call_args.args[0])
        send_value.assert_called_once_with(
            "Logic Pro", "UI element 1 of group", "190", numeric=True
        )


class PluginOpenTests(unittest.TestCase):
    def test_insert_open_reuses_unique_verified_editor_without_toggling_it_closed(self):
        strip = {
            "name": "Stereo Out",
            "inserts": ["Ozone 9 El"],
            "insert_controls": [
                {"name": "Ozone 9 El", "path": "9.2.8.15", "role": "AXGroup"}
            ],
        }
        identity = {
            "plugin": "Ozone 9 Elements",
            "channel": "Stereo Out",
            "view_selector": "Controls",
        }
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "main_window_index", return_value=2),
            mock.patch.object(plugins, "parse_strip", return_value=strip),
            mock.patch.object(plugins, "walk_window", return_value=[]),
            mock.patch.object(
                plugins,
                "ax_windows",
                return_value={
                    "count": 2,
                    "windows": [
                        {"index": 1, "subrole": "AXDialog"},
                        {"index": 2, "subrole": "AXStandardWindow"},
                    ],
                },
            ),
            mock.patch.object(plugins, "read_plugin_identity", return_value=identity),
            mock.patch.object(plugins, "osa") as osa,
        ):
            result = plugins.plugin_open_insert(
                "9.2.8",
                0,
                "Stereo Out",
                expected_plugin="Ozone 9 El",
                dry_run=False,
            )
        self.assertTrue(result["verified"])
        self.assertTrue(result["already_open"])
        self.assertEqual(result["press_method"], "existing verified editor")
        osa.assert_not_called()

    def test_insert_open_presses_exact_child_button(self):
        strip = {
            "name": "Stereo Out",
            "inserts": ["Ozone 9 El"],
            "insert_controls": [
                {"name": "Ozone 9 El", "path": "9.2.8.15", "role": "AXGroup"}
            ],
        }
        identity = {
            "plugin": "Ozone 9 Elements",
            "channel": "Stereo Out",
            "view_selector": "Editor",
        }
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "main_window_index", return_value=1),
            mock.patch.object(plugins, "parse_strip", return_value=strip),
            mock.patch.object(
                plugins,
                "walk_window",
                side_effect=[[], [{"path": "9.2.8.15.2", "role": "AXButton", "name": "open"}]],
            ),
            mock.patch.object(
                plugins,
                "ax_windows",
                side_effect=[{"count": 1}, {"count": 2}],
            ),
            mock.patch.object(plugins, "read_plugin_identity", return_value=identity),
            mock.patch.object(plugins, "osa") as osa,
            mock.patch.object(plugins.time, "sleep"),
        ):
            result = plugins.plugin_open_insert(
                "9.2.8",
                0,
                "Stereo Out",
                expected_plugin="Ozone 9 El",
                dry_run=False,
            )
        self.assertTrue(result["verified"])
        self.assertEqual(result["open_path"], "9.2.8.15.2")
        self.assertEqual(result["press_method"], "exact child Open button")
        self.assertIn("UI element 2 of UI element 15", osa.call_args.args[0])

    def test_insert_open_accepts_description_only_open_button(self):
        strip = {
            "name": "Stereo Out",
            "inserts": ["Ozone 9 El"],
            "insert_controls": [
                {"name": "Ozone 9 El", "path": "9.2.8.15", "role": "AXGroup"}
            ],
        }
        identity = {
            "plugin": "Ozone 9 Elements",
            "channel": "Stereo Out",
            "view_selector": "Editor",
        }
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "main_window_index", return_value=1),
            mock.patch.object(plugins, "parse_strip", return_value=strip),
            mock.patch.object(
                plugins,
                "walk_window",
                side_effect=[
                    [],
                    [
                        {
                            "path": "9.2.8.15.2",
                            "role": "AXButton",
                            "name": "",
                            "description": "open",
                        }
                    ],
                ],
            ),
            mock.patch.object(
                plugins,
                "ax_windows",
                side_effect=[{"count": 1}, {"count": 2}],
            ),
            mock.patch.object(plugins, "read_plugin_identity", return_value=identity),
            mock.patch.object(plugins, "osa") as osa,
            mock.patch.object(plugins.time, "sleep"),
        ):
            result = plugins.plugin_open_insert(
                "9.2.8",
                0,
                "Stereo Out",
                expected_plugin="Ozone 9 El",
                dry_run=False,
            )
        self.assertTrue(result["verified"])
        self.assertEqual(result["open_path"], "9.2.8.15.2")
        self.assertEqual(result["press_method"], "exact child Open button")
        self.assertIn("UI element 2 of UI element 15", osa.call_args.args[0])


class MeterReadTests(unittest.TestCase):
    def test_plugin_title_alone_is_not_a_measurement(self):
        identity = {"plugin": "Loudness Meter", "channel": "Stereo Out"}
        elements = [
            {
                "path": "7",
                "role": "AXStaticText",
                "name": "Loudness Meter",
                "description": "text",
                "value": "Loudness Meter",
            }
        ]
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "read_plugin_identity", return_value=identity),
            mock.patch.object(plugins, "walk_window", return_value=elements),
        ):
            result = plugins.plugin_meter_read(1, "Loudness Meter", "Stereo Out")
        self.assertFalse(result["measurement_available"])
        self.assertEqual(result["readout_count"], 0)

    def test_numeric_lufs_readout_is_reported(self):
        identity = {"plugin": "Meter", "channel": "Stereo Out"}
        elements = [
            {
                "path": "9",
                "role": "AXStaticText",
                "name": "Integrated",
                "description": "LUFS",
                "value": "-9,2 LUFS",
            }
        ]
        with (
            mock.patch.object(plugins, "require_logic", return_value="Logic Pro"),
            mock.patch.object(plugins, "read_plugin_identity", return_value=identity),
            mock.patch.object(plugins, "walk_window", return_value=elements),
        ):
            result = plugins.plugin_meter_read(1, "Meter", "Stereo Out")
        self.assertTrue(result["measurement_available"])
        self.assertEqual(result["readout_count"], 1)


class AuditStateMachineTests(unittest.TestCase):
    def test_cancel_before_any_mutation_requires_no_restore(self):
        plan = plugins.mix_audit_plan(
            tracks={"data": [{"id": 0, "name": "Kick", "type": "audio"}]},
            mixer={},
            ax_channels={},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        cancelled = plugins.mix_audit_cancel(plan["plan_id"])
        self.assertTrue(cancelled["complete"])
        self.assertFalse(cancelled["state_restore_required"])
        self.assertIsNone(cancelled["next_step"])

    def test_cancel_with_open_editor_keeps_verified_close_cleanup(self):
        plan = plugins.mix_audit_plan(
            tracks={"data": [{"id": 0, "name": "Kick", "type": "audio"}]},
            mixer={},
            ax_channels={"strips": [{"index": 0, "name": "Kick", "path": "8.1"}]},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        run = plugins.AUDIT_PLANS[plan["plan_id"]]
        read_index = next(
            index for index, step in enumerate(run["plan"]["steps"])
            if step["operation"] == "mixer_read_strip"
        )
        for step in run["plan"]["steps"][:read_index]:
            step["status"] = "completed"
        run["confirmed"] = True
        run["current"] = read_index
        read_step = run["plan"]["steps"][read_index]
        plugins.mix_audit_advance(
            plan["plan_id"],
            read_step["step_id"],
            {"ok": True, "path": "8.1", "inserts": ["Channel EQ"]},
        )
        open_step = plugins.audit_next_step(run)
        open_step["status"] = "completed"
        run["results"][open_step["step_id"]] = {"ok": True, "verified": True}
        run["current"] += 1
        cancelled = plugins.mix_audit_cancel(plan["plan_id"])
        self.assertFalse(cancelled["complete"])
        self.assertTrue(cancelled["next_step"]["step_id"].endswith("-restore-view"))
        self.assertTrue(any(item.endswith("-close") for item in cancelled["cleanup_steps"]))

    def test_preflight_failure_completes_without_stale_restore(self):
        plan = plugins.mix_audit_plan(
            tracks={
                "data": [
                    {
                        "index": 0,
                        "name": "Kick",
                        "type": "Audio",
                        "target_ref": "trk_kick",
                    }
                ]
            },
            mixer={},
            ax_channels={},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        started = plugins.mix_audit_start(plan["plan_id"], confirm=True)
        self.assertEqual(started["next_step"]["step_id"], "preflight-project")
        advanced = plugins.mix_audit_advance(
            plan["plan_id"],
            "preflight-project",
            {"ok": False, "error": "project audit failed"},
        )
        self.assertTrue(advanced["complete"])
        self.assertIsNone(advanced["next_step"])
        run = plugins.AUDIT_PLANS[plan["plan_id"]]
        self.assertTrue(
            all(
                step["status"] == "skipped"
                for step in run["plan"]["steps"][1:]
            )
        )

    def test_verified_step_cannot_pass_with_ok_true_only(self):
        plan = plugins.mix_audit_plan(
            tracks={
                "data": [
                    {
                        "index": 0,
                        "name": "Kick",
                        "type": "Audio",
                        "target_ref": "trk_kick",
                    }
                ]
            },
            mixer={},
            ax_channels={},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        plugins.mix_audit_start(plan["plan_id"], confirm=True)
        advanced = plugins.mix_audit_advance(
            plan["plan_id"],
            "preflight-project",
            {"ok": True, "verified": False},
        )
        self.assertTrue(advanced["failed"])
        self.assertTrue(advanced["complete"])
        self.assertIsNone(advanced["next_step"])

    def test_audit_results_exposes_paged_evidence(self):
        plan = plugins.mix_audit_plan(
            tracks={"data": [{"id": 0, "name": "Kick", "type": "audio"}]},
            mixer={},
            ax_channels={},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        plugins.mix_audit_start(plan["plan_id"], confirm=True)
        plugins.mix_audit_advance(
            plan["plan_id"],
            "preflight-project",
            {
                "ok": True,
                "verified": True,
                "observed_project_path": "/tmp/Test.logicx",
            },
        )
        result = plugins.mix_audit_results(
            plan["plan_id"], phase="preflight", limit=1, include_payload=True
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["results"][0]["operation"], "mix_project_identity")
        self.assertTrue(result["results"][0]["result"]["verified"])

    def test_fresh_strip_read_expands_each_insert_into_verified_steps(self):
        plan = plugins.mix_audit_plan(
            tracks={"data": [{"index": 0, "name": "Kick", "type": "Audio", "target_ref": "trk_kick"}]},
            mixer={},
            ax_channels={"strips": [{"index": 0, "name": "Kick", "path": "8.1"}]},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        run = plugins.AUDIT_PLANS[plan["plan_id"]]
        run["confirmed"] = True
        read_index = next(
            index
            for index, step in enumerate(run["plan"]["steps"])
            if step["operation"] == "mixer_read_strip"
        )
        for step in run["plan"]["steps"][:read_index]:
            step["status"] = "completed"
        run["current"] = read_index
        read_step = run["plan"]["steps"][read_index]
        advanced = plugins.mix_audit_advance(
            plan["plan_id"],
            read_step["step_id"],
            {"ok": True, "name": "Kick", "path": "8.1", "inserts": ["Channel EQ", "Compressor"]},
        )
        self.assertEqual(advanced["next_step"]["operation"], "plugin_open_insert")
        expanded = run["plan"]["steps"][read_index + 1 : read_index + 13]
        self.assertEqual(
            sum(1 for step in expanded if step["operation"] == "plugin_open_insert"),
            2,
        )

    def test_mature_inventory_timeout_falls_back_to_ax_instead_of_aborting_target(self):
        plan = plugins.mix_audit_plan(
            tracks={"data": [{"id": 0, "name": "Kick", "type": "audio"}]},
            mixer={},
            ax_channels={"strips": [{"index": 0, "name": "Kick", "path": "8.1"}]},
            scope="track",
            selector="Kick",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
        )
        run = plugins.AUDIT_PLANS[plan["plan_id"]]
        run["confirmed"] = True
        inventory_index = next(
            index
            for index, step in enumerate(run["plan"]["steps"])
            if step["operation"] == "logic_plugins"
            and step["arguments"].get("command") == "get_inventory"
        )
        for step in run["plan"]["steps"][:inventory_index]:
            step["status"] = "completed"
        run["current"] = inventory_index
        inventory_step = run["plan"]["steps"][inventory_index]
        advanced = plugins.mix_audit_advance(
            plan["plan_id"],
            inventory_step["step_id"],
            {"ok": False, "error": "operation_timeout"},
        )
        self.assertTrue(advanced["failed"])
        self.assertEqual(advanced["next_step"]["operation"], "mixer_reveal_strip")

    def test_fresh_strip_read_expands_meter_candidates_at_measurement_position(self):
        plan = plugins.mix_audit_plan(
            tracks={"data": [{"index": 0, "name": "Master", "type": "Output"}]},
            mixer={},
            ax_channels={"strips": [{"index": 0, "name": "Master", "path": "8.1"}]},
            scope="master",
            selector="Master",
            project_path="/tmp/Test.logicx",
            output_root="/tmp/logic-audits",
            measurement="existing_meter",
        )
        run = plugins.AUDIT_PLANS[plan["plan_id"]]
        run["confirmed"] = True
        read_index = next(
            index
            for index, step in enumerate(run["plan"]["steps"])
            if step["operation"] == "mixer_read_strip"
        )
        for step in run["plan"]["steps"][:read_index]:
            step["status"] = "completed"
        run["current"] = read_index
        read_step = run["plan"]["steps"][read_index]
        plugins.mix_audit_advance(
            plan["plan_id"],
            read_step["step_id"],
            {
                "ok": True,
                "name": "Master",
                "path": "8.1",
                "inserts": ["Ozone 9 Elements", "Loudness Meter"],
            },
        )
        steps = run["plan"]["steps"]
        self.assertFalse(any(step["operation"] == "mix_expand_meter_steps" for step in steps))
        meter_reads = [step for step in steps if step["operation"] == "plugin_meter_read"]
        self.assertEqual(len(meter_reads), 2)
        isolate_index = next(
            index for index, step in enumerate(steps) if step["operation"] == "mix_isolation_dispatch"
        )
        self.assertTrue(all(steps.index(step) > isolate_index for step in meter_reads))


if __name__ == "__main__":
    unittest.main()
