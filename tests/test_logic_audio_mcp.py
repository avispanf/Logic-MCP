import unittest
from pathlib import Path
from unittest import mock

import logic_audio_mcp as audio
import logic_plugins_mcp as plugins


class OpenProjectIdentityTests(unittest.TestCase):
    def test_audio_uses_the_title_verified_project(self):
        project = Path("/Volumes/Work/Song.logicx")
        with mock.patch.object(plugins, "find_open_logic_project", return_value=project):
            self.assertEqual(audio.find_open_project(), project)

    def test_missing_or_ambiguous_identity_does_not_fall_back_to_a_template(self):
        with mock.patch.object(plugins, "find_open_logic_project", return_value=None), \
             mock.patch.object(audio.subprocess, "run") as process:
            self.assertIsNone(audio.find_open_project())
        process.assert_not_called()

    def test_unreadable_ui_identity_fails_closed(self):
        with mock.patch.object(plugins, "find_open_logic_project", side_effect=plugins.ProbeError("UI unavailable")):
            self.assertIsNone(audio.find_open_project())


class NamingConventionTests(unittest.TestCase):
    def test_vocabularies_and_fixed_width_numbers(self):
        matcher = audio.compile_convention(
            "{kind}_{index:3d}_{variant}",
            {"kind": ["SFX", "AMB"]},
        )
        self.assertIsNotNone(matcher.fullmatch("SFX_007_Close"))
        self.assertIsNone(matcher.fullmatch("MUS_007_Close"))
        self.assertIsNone(matcher.fullmatch("SFX_7_Close"))


class LoudnessEvaluationTests(unittest.TestCase):
    def test_reports_loudness_and_true_peak_failures(self):
        measurement = {
            "integrated_lufs": -5.0,
            "true_peak_dbtp": 0.2,
            "range_lu": 4.0,
            "duration_seconds": 30.0,
        }
        target = {
            "integrated_lufs": -9.0,
            "integrated_tolerance": 1.0,
            "true_peak_max": -1.0,
            "range_min": 2.0,
            "range_max": 12.0,
        }
        result = audio.evaluate(measurement, target)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["deviation_lu"], 4.0)
        self.assertEqual(len(result["problems"]), 2)

    def test_short_material_skips_lra_judgement(self):
        measurement = {
            "integrated_lufs": -9.0,
            "true_peak_dbtp": -1.5,
            "range_lu": 0.1,
            "duration_seconds": 2.0,
        }
        target = {
            "integrated_lufs": -9.0,
            "integrated_tolerance": 1.0,
            "true_peak_max": -1.0,
            "range_min": 2.0,
            "range_max": 12.0,
        }
        result = audio.evaluate(measurement, target)
        self.assertEqual(result["verdict"], "pass")
        self.assertIn("range_check", result)


if __name__ == "__main__":
    unittest.main()
