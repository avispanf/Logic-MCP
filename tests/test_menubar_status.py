from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


@unittest.skipUnless(sys.platform == "darwin", "Native menu-bar presentation requires macOS")
class MenuBarStatusTests(unittest.TestCase):
    def test_native_status_parser_and_presentation(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="Logic MCP Status ") as folder:
            binary = Path(folder) / "status-tests"
            compiled = subprocess.run([
                "xcrun", "swiftc", "-parse-as-library", "-target", "arm64-apple-macos14.0",
                str(root / "menubar/LogicMCPStatus.swift"),
                str(root / "tests/fixtures/status_presentation.swift"), "-o", str(binary)
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("checks passed", result.stdout)
            for path in ("/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp",
                         '/Users/test/Audio "Work"/Logic MCP.app/Contents/Resources/runtime/logic-mcp',
                         "/Users/test/Музика\\Projects/Logic MCP.app/Contents/Resources/runtime/logic-mcp"):
                with self.subTest(path=path):
                    config = subprocess.run([str(binary), "--configuration", path], capture_output=True, text=True, timeout=10)
                    self.assertEqual(config.returncode, 0, config.stderr)
                    servers = tomllib.loads(config.stdout)["mcp_servers"]
                    self.assertEqual(servers["logic-pro"]["url"], "http://127.0.0.1:8765/mcp")
                    for role in ("plugins", "audio"):
                        self.assertEqual(servers["logic-" + role]["command"], path)
                        self.assertEqual(servers["logic-" + role]["args"], ["--" + role])
