import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_mix_audit as runner_module


def arguments() -> argparse.Namespace:
    return argparse.Namespace(parameter_page_size=40, max_parameters=2000)


class AuditRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_tracks_rejects_transient_generic_mcu_bank(self):
        runner = runner_module.AuditRunner(arguments())
        generic = {
            "readable": True,
            "source": "ax_live",
            "data": [{"id": index, "name": f"Track {index + 1}"} for index in range(8)],
        }
        project = {
            "readable": True,
            "source": "ax_live",
            "data": [
                {"id": 0, "name": "Cloudless #3 -"},
                {"id": 1, "name": "LEAD VOICE"},
            ],
        }
        runner.resource = mock.AsyncMock(side_effect=[generic, project])
        runner.tool = mock.AsyncMock(return_value={"ok": True})

        with mock.patch.object(runner_module.asyncio, "sleep", new=mock.AsyncMock()):
            result = await runner.wait_for_tracks(timeout=1)

        self.assertEqual(result["data"][0]["name"], "Cloudless #3 -")
        self.assertEqual(runner.resource.await_count, 2)
        runner.tool.assert_awaited_once_with(
            "core", "logic_system", {"command": "refresh_cache", "params": {}}
        )

    def test_emit_keeps_full_journal_but_compacts_progress(self):
        runner = runner_module.AuditRunner(arguments())
        with tempfile.TemporaryDirectory() as folder:
            runner.log_path = Path(folder) / "runner.jsonl"
            with mock.patch("builtins.print") as printed:
                runner.emit(
                    "step_finished",
                    summary={"step_id": "parameters", "parameter_count": 120},
                    result={"parameters": [{"row": index} for index in range(120)]},
                )

            journal = json.loads(runner.log_path.read_text(encoding="utf-8"))
            progress = json.loads(printed.call_args.args[0])
            self.assertEqual(len(journal["result"]["parameters"]), 120)
            self.assertNotIn("result", progress)
            self.assertEqual(progress["summary"]["parameter_count"], 120)

    async def test_cycle_restore_uses_toggle_once(self):
        runner = runner_module.AuditRunner(arguments())
        runner.cycle_enabled = False
        runner.tool = mock.AsyncMock(
            return_value={"ok": True, "verified": True, "state": "A"}
        )

        result = await runner.execute_child(
            {
                "server": "client",
                "operation": "ensure_resource_state",
                "arguments": {
                    "field": "isCycleEnabled",
                    "equals": True,
                    "command_if_mismatch": "toggle_cycle",
                },
            }
        )

        self.assertTrue(result["verified"])
        self.assertTrue(runner.cycle_enabled)
        runner.tool.assert_awaited_once_with(
            "core",
            "logic_transport",
            {"command": "toggle_cycle", "params": {}},
        )

    async def test_arrange_toggle_updates_known_state_only_after_verified_readback(self):
        runner = runner_module.AuditRunner(arguments())
        runner.track_state = {
            0: {
                "name": "Cloudless #3 -",
                "solo": False,
                "mute": True,
                "selected": False,
            }
        }
        runner.tool = mock.AsyncMock(
            return_value={"ok": True, "verified": True, "after": True}
        )
        dispatch = {
            "server": "logic-plugins",
            "operation": "arrange_track_set_toggle",
            "arguments": {
                "index": 0,
                "expected_track": "Cloudless #3 -",
                "control": "solo",
                "enabled": True,
                "dry_run": False,
            },
        }

        result = await runner.execute_child(dispatch)

        self.assertTrue(result["verified"])
        self.assertTrue(runner.track_state[0]["solo"])
        self.assertEqual(
            runner.tool.await_args_list,
            [
                mock.call(
                    "core", "logic_tracks", {"command": "select", "params": {"index": 0}}
                ),
                mock.call("plugins", "arrange_track_set_toggle", dispatch["arguments"]),
            ],
        )

    async def test_known_playback_state_is_no_op(self):
        runner = runner_module.AuditRunner(arguments())
        runner.transport_playing = False
        runner.tool = mock.AsyncMock()

        result = await runner.execute_child(
            {
                "server": "client",
                "operation": "ensure_resource_state",
                "arguments": {
                    "field": "isPlaying",
                    "equals": False,
                    "command_if_true": "play",
                    "command_if_false": "stop",
                },
            }
        )

        self.assertTrue(result["verified"])
        runner.tool.assert_not_awaited()

    def test_restore_needed_covers_track_surface_and_transport(self):
        runner = runner_module.AuditRunner(arguments())
        runner.initial_track_state = {1: {"solo": False, "mute": False}}
        runner.track_state = {1: {"solo": False, "mute": False}}
        runner.initial_surface_state = {"8.1:solo": False}
        runner.surface_state = {"8.1:solo": False}
        runner.initial_transport_position = runner.transport_position = "1.1.1.1"
        runner.initial_transport_playing = runner.transport_playing = False
        runner.initial_cycle_enabled = runner.cycle_enabled = True

        self.assertFalse(runner.restore_needed())
        runner.surface_state["8.1:solo"] = True
        self.assertTrue(runner.restore_needed())

    def test_track_snapshot_preserves_integer_indices(self):
        runner = runner_module.AuditRunner(arguments())
        runner.track_state = {1: {"solo": True, "mute": False}}
        runner.initial_track_state = copy.deepcopy(runner.track_state)
        self.assertEqual(list(runner.initial_track_state), [1])
        self.assertFalse(runner.restore_needed())

    async def test_position_dispatch_skips_ui_after_live_resource_readback(self):
        runner = runner_module.AuditRunner(arguments())
        runner.resource = mock.AsyncMock(
            return_value={
                "data": {
                    "state": {
                        "position": "1.1.1.1",
                        "isPlaying": False,
                        "isCycleEnabled": True,
                    }
                }
            }
        )
        runner.tool = mock.AsyncMock()

        result = await runner.execute_child(
            {
                "server": "logic-plugins",
                "operation": "transport_goto_position",
                "arguments": {"position": "1.1.1.1", "dry_run": False},
            }
        )

        self.assertTrue(result["verified"])
        self.assertEqual(result["action"], "verified-position no-op")
        runner.tool.assert_not_awaited()

    async def test_parameter_reader_pages_until_complete(self):
        runner = runner_module.AuditRunner(arguments())
        runner.tool = mock.AsyncMock(
            side_effect=[
                {
                    "window_index": 1,
                    "rows_total": 3,
                    "returned": 2,
                    "next_offset": 2,
                    "parameters": [{"row": 1}, {"row": 2}],
                },
                {
                    "window_index": 1,
                    "rows_total": 3,
                    "returned": 1,
                    "next_offset": None,
                    "parameters": [{"row": 3}],
                },
            ]
        )
        runner.args.parameter_page_size = 2

        result = await runner.read_plugin_parameters(
            {"window_index": 1, "expected_plugin": "Pro-MB"}
        )

        self.assertEqual(result["returned"], 3)
        self.assertEqual(result["pages_completed"], 2)
        self.assertEqual([row["row"] for row in result["parameters"]], [1, 2, 3])
        self.assertEqual(runner.tool.await_args_list[1].args[2]["offset"], 2)


if __name__ == "__main__":
    unittest.main()
