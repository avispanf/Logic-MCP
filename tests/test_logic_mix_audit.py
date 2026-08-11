import unittest

import logic_mix_audit as audit


class ClassificationTests(unittest.TestCase):
    def test_explicit_types_win_over_name_heuristics(self):
        self.assertEqual(audit.classify_channel({"name": "VOX", "type": "Aux"}), "aux")
        self.assertEqual(
            audit.classify_channel({"name": "Drums", "type": "Summing Stack"}),
            "group",
        )

    def test_common_output_and_bus_names_are_recognised(self):
        self.assertEqual(audit.classify_channel({"name": "Stereo Out"}), "master")
        self.assertEqual(audit.classify_channel({"name": "Bus 12"}), "bus")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tracks = {
            "data": [
                {"index": 0, "name": "Kick", "type": "Audio", "target_ref": "trk_kick"},
                {"index": 1, "name": "Drums", "type": "Summing Stack", "target_ref": "trk_drums"},
                {"index": 2, "name": "Snare", "type": "Audio", "target_ref": "trk_snare", "group": "Drums"},
                {"index": 3, "name": "Stereo Out", "type": "Output"},
            ]
        }
        self.mixer = {
            "strips": [
                {"index": 0, "name": "Kick", "target_ref": "mix_kick"},
                {"index": 1, "name": "Drums", "target_ref": "mix_drums"},
                {"index": 3, "name": "Stereo Out", "target_ref": "mix_master"},
            ]
        }
        self.ax = {
            "channels": [
                {
                    "index": 0,
                    "name": "Kick",
                    "path": "8.1",
                    "inserts": ["Channel EQ", "Compressor"],
                    "detail": "full",
                }
            ]
        }

    def test_three_sources_merge_without_losing_refs_or_insert_paths(self):
        inventory = audit.normalise_inventory(self.tracks, self.mixer, self.ax)
        kick = next(item for item in inventory["channels"] if item["name"] == "Kick")
        self.assertEqual(kick["track_ref"], "trk_kick")
        self.assertEqual(kick["mixer_ref"], "mix_kick")
        self.assertEqual(kick["strip_path"], "8.1")
        self.assertEqual(kick["inserts"], ["Channel EQ", "Compressor"])
        self.assertEqual(set(kick["sources"]), {"tracks", "mixer", "ax"})
        drums = next(item for item in inventory["channels"] if item["name"] == "Drums")
        self.assertEqual(drums["kind"], "group")
        self.assertEqual(drums["target_ref"], "trk_drums")

    def test_group_resolution_collects_explicit_members(self):
        channels = audit.normalise_inventory(self.tracks, self.mixer, self.ax)["channels"]
        result = audit.resolve_targets(channels, "group", "Drums")
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["targets"][0]["isolation_refs"],
            ["trk_drums", "trk_snare"],
        )


class PlanTests(unittest.TestCase):
    def test_plan_has_isolation_bounce_analysis_and_restore(self):
        inventory = audit.normalise_inventory(
            {"data": [{"index": 0, "name": "Kick", "type": "Audio", "target_ref": "trk_kick"}]},
            None,
            {"channels": [{"index": 0, "name": "Kick", "path": "8.1", "inserts": ["Channel EQ"]}]},
        )
        plan = audit.build_audit_plan(
            inventory,
            "track",
            "Kick",
            "/tmp/Test.logicx",
            "/tmp/logic-audits",
        )
        operations = [step["operation"] for step in plan["steps"]]
        self.assertIn("mix_isolation_dispatch", operations)
        self.assertIn("mix_bounce_target", operations)
        self.assertIn("loudness_measure", operations)
        self.assertIn("mixer_reveal_strip", operations)
        read_strip = next(step for step in plan["steps"] if step["operation"] == "mixer_read_strip")
        self.assertTrue(read_strip["expand_plugin_steps"])
        expanded = audit.build_plugin_inspection_steps("target", plan["targets"][0], ["Channel EQ"])
        expanded_operations = [step["operation"] for step in expanded]
        self.assertIn("plugin_set_view", expanded_operations)
        self.assertIn("plugin_parameters", expanded_operations)
        self.assertIn("plugin_close_verified", expanded_operations)
        self.assertEqual(operations[-1], "mix_restore_dispatch")
        self.assertTrue(plan["confirmation_required"])
        self.assertFalse(plan["safety"]["writes_parameters"])

    def test_generic_offscreen_ax_name_merges_by_unique_track_index(self):
        inventory = audit.normalise_inventory(
            {"data": [{"index": 5, "name": "Lead Vox", "type": "Audio", "target_ref": "trk_vox"}]},
            None,
            {"strips": [{"index": 5, "name": "channel strip", "path": "8.5"}]},
        )
        self.assertEqual(inventory["count"], 1)
        row = inventory["channels"][0]
        self.assertEqual(row["name"], "Lead Vox")
        self.assertEqual(row["strip_path"], "8.5")

    def test_index_fallback_is_used_when_target_ref_is_absent(self):
        inventory = audit.normalise_inventory(
            {"data": [{"index": 4, "name": "Bass", "type": "Audio"}]},
            None,
            None,
        )
        plan = audit.build_audit_plan(
            inventory,
            "track",
            "Bass",
            "/tmp/Test.logicx",
            "/tmp/logic-audits",
        )
        isolation = audit.build_isolation_dispatch(
            {"logic://tracks": {"data": [{"index": 4, "name": "Bass", "solo": False}]}},
            plan["targets"][0],
        )
        self.assertEqual(
            isolation["dispatches"][0]["arguments"],
            {"index": 4, "enabled": True},
        )

    def test_mixer_only_aux_uses_verified_ax_solo_not_track_index(self):
        inventory = audit.normalise_inventory(
            None,
            None,
            {
                "channels": [
                    {
                        "index": 7,
                        "name": "Aux 1",
                        "kind": "aux",
                        "path": "8.7",
                        "solo": "off",
                        "mute": "off",
                    }
                ]
            },
        )
        plan = audit.build_audit_plan(
            inventory,
            "aux",
            "Aux 1",
            "/tmp/Test.logicx",
            "/tmp/logic-audits",
        )
        isolation_step = next(
            step for step in plan["steps"] if step["operation"] == "mix_isolation_dispatch"
        )
        isolation = audit.build_isolation_dispatch(
            {},
            plan["targets"][0],
            isolation_step["arguments"]["ax_state"],
        )
        solo = isolation["dispatches"][0]
        self.assertEqual(solo["operation"], "mixer_set_toggle")
        self.assertEqual(solo["arguments"]["strip_path"], "8.7")
        self.assertFalse(
            any(
                step["operation"] == "logic_plugins.get_inventory"
                for step in plan["steps"]
            )
        )

    def test_fix_plan_reopens_plugin_and_resolves_label_at_apply_time(self):
        inventory = audit.normalise_inventory(
            {"data": [{"index": 0, "name": "Master", "type": "Output"}]},
            None,
            {
                "channels": [
                    {
                        "index": 0,
                        "name": "Master",
                        "path": "8.9",
                        "inserts": ["Ozone 9 Elements"],
                    }
                ]
            },
        )
        plan = audit.build_fix_plan(
            inventory,
            [
                {
                    "target": "Master",
                    "plugin": "Ozone 9 Elements",
                    "parameter": "Threshold",
                    "value": "-6",
                    "expected_before": "-12",
                }
            ],
            "/tmp/Test.logicx",
        )
        operations = [step["operation"] for step in plan["steps"]]
        self.assertIn("plugin_open_insert", operations)
        self.assertIn("plugin_write_label_verified", operations)
        self.assertIn("plugin_close_verified", operations)
        self.assertTrue(plan["confirmation_required"])
        self.assertTrue(plan["safety"]["independent_readback_required"])


class ReviewTests(unittest.TestCase):
    def test_loudness_peak_and_chain_order_create_nonwriting_recommendations(self):
        result = audit.review_measurements(
            [
                {
                    "name": "Master",
                    "integrated_lufs": -5.0,
                    "true_peak_dbtp": 0.2,
                    "inserts": ["S1 Imager", "Ozone"],
                }
            ],
            -9.0,
            1.0,
            -1.0,
        )
        reviewed = result["results"][0]
        self.assertEqual(reviewed["verdict"], "review")
        self.assertEqual(len(reviewed["recommendations"]), 3)
        self.assertTrue(all(not item["automatic_write"] for item in reviewed["recommendations"]))

    def test_before_after_reports_deltas(self):
        result = audit.compare_before_after(
            [{"target_id": "master", "integrated_lufs": -5.0, "true_peak_dbtp": 0.2}],
            [{"target_id": "master", "integrated_lufs": -9.0, "true_peak_dbtp": -1.1}],
        )
        row = result["comparisons"][0]
        self.assertEqual(row["integrated_change_lu"], -4.0)
        self.assertEqual(row["true_peak_change_db"], -1.3)


class RestoreTests(unittest.TestCase):
    def test_master_isolation_clears_all_preexisting_solos(self):
        result = audit.build_isolation_dispatch(
            {
                "logic://tracks": {
                    "data": [
                        {"index": 0, "target_ref": "trk_a", "solo": True},
                        {"index": 1, "target_ref": "trk_b", "solo": False},
                    ]
                }
            },
            {"name": "Stereo Out", "kind": "master", "audit_id": "master"},
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["dispatch_count"], 1)
        self.assertEqual(result["dispatches"][0]["arguments"]["enabled"], False)

    def test_restore_dispatch_preserves_solo_mute_selection_transport_and_cycle(self):
        result = audit.build_restore_dispatch(
            {
                "logic://tracks": {
                    "data": [
                        {
                            "index": 2,
                            "target_ref": "trk_vox",
                            "solo": True,
                            "mute": False,
                            "selected": True,
                        }
                    ]
                },
                "logic://transport/state": {
                    "playing": False,
                    "cycle": True,
                    "position": "9.1.1.1",
                },
            }
        )
        operations = [item["operation"] for item in result["dispatches"]]
        self.assertIn("logic_tracks.solo", operations)
        self.assertIn("logic_tracks.mute", operations)
        self.assertIn("logic_tracks.select", operations)
        self.assertIn("logic_transport.goto_position", operations)
        self.assertEqual(operations.count("client.ensure_transport_state"), 2)
        self.assertTrue(result["complete"])

    def test_mixer_only_aux_state_is_restored_through_verified_ax_toggle(self):
        result = audit.build_restore_dispatch(
            {},
            [
                {
                    "name": "Aux 1",
                    "strip_path": "8.7",
                    "track_ref": "",
                    "solo": "off",
                    "mute": "on",
                }
            ],
        )
        dispatches = [
            item for item in result["dispatches"] if item["operation"] == "mixer_set_toggle"
        ]
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(dispatches[0]["arguments"]["expected_strip"], "Aux 1")


if __name__ == "__main__":
    unittest.main()
