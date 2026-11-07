from pathlib import Path
import tempfile
import unittest

from scripts.package_native_licenses import collect_native_licenses, locate_backend_source


class NativeNoticesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="Logic MCP Notices ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "core source"
        self.source.mkdir()
        (self.source / "Package.swift").write_text("package")
        (self.source / "LICENSE").write_text("core notice")

    def test_source_is_found_from_swift_build_product(self):
        binary = self.source / ".build/out/Products/Release/LogicProMCP"
        self.assertEqual(locate_backend_source(binary), self.source.resolve())

    def test_missing_or_invalid_configured_source_is_not_guessed(self):
        with self.assertRaisesRegex(RuntimeError, "LOGIC_MCP_BACKEND_SOURCE"):
            locate_backend_source(self.root / "bin/LogicProMCP")
        with self.assertRaises(RuntimeError):
            locate_backend_source(self.source / "bin/LogicProMCP", str(self.root / "missing"))

    def test_nested_vendor_notices_keep_their_relative_paths(self):
        dependency = self.source / ".build/checkouts/swift-nio"
        vendor = dependency / "Sources/Vendor"
        vendor.mkdir(parents=True)
        (dependency / "LICENSE.txt").write_text("package notice")
        (vendor / "LICENSE").write_text("vendor notice")
        resources = self.root / "app resources"
        files = collect_native_licenses(self.source, resources)
        self.assertEqual(len(files), 3)
        self.assertEqual((resources / "licenses/Native/LogicProMCP/LICENSE").read_text(), "core notice")
        self.assertEqual((resources / "licenses/Native/swift-nio/Sources/Vendor/LICENSE").read_text(), "vendor notice")

    def test_missing_dependency_notices_fail_the_package(self):
        with self.assertRaisesRegex(RuntimeError, "notices are missing"):
            collect_native_licenses(self.source, self.root / "resources")

    def test_a_notice_symlink_cannot_package_an_external_file(self):
        dependency = self.source / ".build/checkouts/dependency"
        dependency.mkdir(parents=True)
        external = self.root / "private notice"
        external.write_text("must not be packaged")
        (dependency / "LICENSE").symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            collect_native_licenses(self.source, self.root / "resources")
