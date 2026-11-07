from pathlib import Path
import shutil


NOTICE_NAMES = {"license", "license.txt", "license.md", "license-mit", "license-apache",
                "notice", "notice.txt", "copying", "copying.txt"}


def locate_backend_source(binary: Path, configured: str | None = None) -> Path:
    candidates = [Path(configured).expanduser().resolve()] if configured else binary.resolve().parents
    for candidate in candidates:
        if (candidate / "Package.swift").is_file() and (candidate / "LICENSE").is_file():
            return candidate
    raise RuntimeError("Set LOGIC_MCP_BACKEND_SOURCE to the pinned backend checkout so native notices can be packaged")


def collect_native_licenses(source: Path, resources: Path) -> list[str]:
    checkouts = source / ".build/checkouts"
    dependencies = sorted(path for path in checkouts.rglob("*")
                          if path.is_file() and path.name.lower() in NOTICE_NAMES and ".git" not in path.parts)
    if not dependencies:
        raise RuntimeError("Native dependency notices are missing; build the pinned backend checkout first")
    files = [(source / "LICENSE", Path("LogicProMCP/LICENSE"))]
    files.extend((path, path.relative_to(checkouts)) for path in dependencies)
    if any(origin.is_symlink() or not origin.resolve().is_relative_to(source.resolve()) for origin, _ in files):
        raise RuntimeError("Native notice source contains a symlink or escapes the backend checkout")
    collected = []
    for origin, relative in files:
        target = resources / "licenses/Native" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
        collected.append(str(target.relative_to(resources)))
    return sorted(collected)
