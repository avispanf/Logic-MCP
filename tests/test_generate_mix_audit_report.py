import json
import tempfile
import unittest
from pathlib import Path

import generate_mix_audit_report as report


class ConsolidatedReportTests(unittest.TestCase):
    def test_combines_continuation_and_plugin_results(self):
        track = {"id": 8, "name": "Synth", "type": "audio", "track_ref": "trk_synth"}
        target_id = report._audit_id(track)
        rows = [
            {"event": "plan_created", "plan_id": "audit-test", "target_count": 1, "project_path": "/tmp/Test.logicx"},
            {"event": "step_finished", "summary": {"step_id": "capture-state"}, "result": {"logic://tracks": {"data": [track]}}},
            {"event": "step_finished", "summary": {"step_id": f"target-001-{target_id}-plugin-00-open", "operation": "plugin_open_insert", "target_id": target_id, "ok": True}, "result": {"plugin": "Pro-Q 3"}},
            {"event": "step_finished", "summary": {"step_id": f"target-001-{target_id}-plugin-00-parameters", "operation": "plugin_parameters", "target_id": target_id, "parameter_count": 320}, "result": {"parameters": [{"label": "Output", "display": "-1.0 dB"}]}},
            {"event": "step_finished", "summary": {"step_id": "analyze", "operation": "loudness_measure", "target_id": target_id}, "result": {"integrated_lufs": -18.2, "true_peak_dbtp": -3.1}},
            {"event": "run_finished"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "runner.jsonl"
            journal.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            result = report.build_report([journal])
        self.assertEqual(result["measured_targets"], 1)
        self.assertEqual(result["plugin_inspected_targets"], 1)
        self.assertEqual(result["targets"][0]["name"], "Synth")
        self.assertEqual(result["targets"][0]["plugins"][0]["parameter_count"], 320)
        self.assertEqual(
            result["targets"][0]["plugins"][0]["parameters"][0]["display"],
            "-1.0 dB",
        )
        self.assertEqual(result["runs"][0]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
