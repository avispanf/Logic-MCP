import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

try:
    from .verify_bundle import verify_release, developer_team
except ImportError:
    from verify_bundle import verify_release, developer_team


ASSETS = ('Logic-MCP-macos-arm64.zip', 'Logic-MCP-macos-arm64.dmg')


def verify_checksums(directory):
    expected = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] not in ASSETS or fields[1] in expected:
            raise RuntimeError('Unexpected or duplicate checksum entry')
        expected[fields[1]] = fields[0]
    if set(expected) != set(ASSETS):
        raise RuntimeError('Both ZIP and DMG checksums are required')
    for name, digest in expected.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Checksum mismatch or missing asset: {name}')
    return expected


def verify_distribution(directory):
    hashes = verify_checksums(directory)
    dmg = directory / ASSETS[1]
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(dmg)], check=True, capture_output=True)
    result = subprocess.run(['/usr/bin/codesign', '-dvvv', str(dmg)], check=True, capture_output=True, text=True)
    team = developer_team(result.stderr, require_runtime=False)
    subprocess.run(['xcrun', 'stapler', 'validate', str(dmg)], check=True, capture_output=True)
    subprocess.run(['/usr/sbin/spctl', '--assess', '--type', 'open', '--context', 'context:primary-signature', str(dmg)],
                   check=True, capture_output=True)
    with tempfile.TemporaryDirectory(prefix='Logic MCP Distribution ') as temporary:
        folder = Path(temporary)
        extracted = folder / 'zip'
        subprocess.run(['/usr/bin/ditto', '-x', '-k', str(directory / ASSETS[0]), str(extracted)], check=True)
        zip_app = extracted / 'Logic MCP.app'
        if verify_release(zip_app) != team:
            raise RuntimeError('ZIP and DMG have different Developer ID teams')
        manifest = json.loads((zip_app / 'Contents/Resources/build-manifest.json').read_text())
        mount = folder / 'dmg'
        mount.mkdir()
        subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(dmg)],
                       check=True, capture_output=True)
        try:
            dmg_app = mount / 'Logic MCP.app'
            if verify_release(dmg_app) != team:
                raise RuntimeError('DMG and contained app have different Developer ID teams')
            for relative in ('Contents/Resources/build-manifest.json', 'Contents/_CodeSignature/CodeResources',
                             'Contents/MacOS/LogicMCPMenuBar'):
                if (zip_app / relative).read_bytes() != (dmg_app / relative).read_bytes():
                    raise RuntimeError(f'ZIP and DMG contain different applications: {relative}')
        finally:
            subprocess.run(['hdiutil', 'detach', str(mount)], check=True, capture_output=True)
    print('Final ZIP and DMG verified: checksums, Developer ID, matching apps, stapled tickets and Gatekeeper')
    return {'assets': hashes, 'manifest': manifest, 'team': team}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Verify the final signed, notarized ZIP/DMG without installing or launching Logic.')
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    verify_distribution(args.directory.resolve())
