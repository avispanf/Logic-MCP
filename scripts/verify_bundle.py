"""Reject bundled native code incompatible with the advertised architecture or OS."""
from pathlib import Path
import argparse
import hashlib
import json
import plistlib
import re
import subprocess
import sys


MACHO_HEADERS = {bytes.fromhex(value) for value in (
    'feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
    'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca',
)}
AUTOMATION_ENTITLEMENT = 'com.apple.security.automation.apple-events'
EXECUTABLES = ('Contents/MacOS/LogicMCPMenuBar',
               'Contents/Resources/runtime/logic-mcp',
               'Contents/Resources/bin/LogicProMCP')


def native_files(bundle):
    for path in sorted(bundle.rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                if stream.read(4) in MACHO_HEADERS:
                    yield path


def developer_team(description, expected=None, require_runtime=True):
    if not re.search(r'^Authority=Developer ID Application: .+', description, re.MULTILINE):
        raise RuntimeError('A Developer ID Application signature is required')
    match = re.search(r'^TeamIdentifier=([A-Z0-9]{10})$', description, re.MULTILINE)
    if not match or (expected is not None and match.group(1) != expected):
        raise RuntimeError('Missing or inconsistent Developer ID TeamIdentifier')
    if not re.search(r'^Timestamp=(?!none\s*$).+', description, re.MULTILINE):
        raise RuntimeError('A secure signing timestamp is required')
    if require_runtime and not re.search(r'flags=.*\bruntime\b', description):
        raise RuntimeError('Hardened Runtime is required')
    if re.search(r'\badhoc\b', description):
        raise RuntimeError('Ad hoc signatures cannot be released')
    return match.group(1)


def version(value):
    parts = tuple(map(int, value.split('.')))
    return parts + (0,) * (3 - len(parts))


def deployment_target(output):
    for block in output.split('Load command '):
        if 'cmd LC_BUILD_VERSION\n' in block:
            if not re.search(r'\bplatform\s+(1|MACOS)\b', block):
                raise RuntimeError('Native binary does not target macOS')
            match = re.search(r'\bminos\s+([\d.]+)', block)
        elif 'cmd LC_VERSION_MIN_MACOSX\n' in block:
            match = re.search(r'\bversion\s+([\d.]+)', block)
        else:
            continue
        if match:
            return match.group(1)
    raise RuntimeError('Native binary has no macOS deployment target')


def verify(bundle):
    info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
    minimum = info['LSMinimumSystemVersion']
    count = 0
    for path in bundle.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) not in MACHO_HEADERS:
                continue
        architectures = subprocess.check_output(['/usr/bin/lipo', '-archs', str(path)], text=True).split()
        if 'arm64' not in architectures:
            raise RuntimeError(f'{path}: arm64 is missing ({architectures})')
        output = subprocess.check_output(['/usr/bin/otool', '-arch', 'arm64', '-l', str(path)], text=True)
        target = deployment_target(output)
        if version(target) > version(minimum):
            raise RuntimeError(f'{path}: requires macOS {target}, bundle advertises {minimum}')
        count += 1
    if not count:
        raise RuntimeError('No native binaries found in the bundle')
    print(f'Verified {count} native binaries: arm64, macOS {minimum} or earlier')


def verify_signatures(bundle):
    info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
    description = info.get('NSAppleEventsUsageDescription')
    if not isinstance(description, str) or not description.strip():
        raise RuntimeError('Missing Apple Events privacy description')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(bundle)],
                   check=True, capture_output=True)
    for relative in EXECUTABLES:
        path = bundle / relative
        output = subprocess.check_output(['/usr/bin/codesign', '-d', '--entitlements', ':-', str(path)],
                                         stderr=subprocess.PIPE)
        try:
            entitlements = plistlib.loads(output)
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise RuntimeError(f'{path}: missing or invalid signed entitlements') from exc
        if entitlements.get(AUTOMATION_ENTITLEMENT) is not True:
            raise RuntimeError(f'{path}: Apple Events entitlement is missing')
    print('Verified bundle signatures and Apple Events entitlement on all 3 executables')


def verify_developer_id(bundle):
    verify_signatures(bundle)
    team = None
    paths = [bundle, *native_files(bundle)]
    for path in paths:
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)],
                       check=True, capture_output=True)
        result = subprocess.run(['/usr/bin/codesign', '-dvvv', str(path)],
                                check=True, capture_output=True, text=True)
        team = developer_team(result.stderr, team, path == bundle or path.relative_to(bundle).as_posix() in EXECUTABLES)
    for relative in EXECUTABLES:
        result = subprocess.check_output(['/usr/bin/codesign', '-d', '--entitlements', ':-', str(bundle / relative)],
                                         stderr=subprocess.PIPE)
        entitlements = plistlib.loads(result)
        if entitlements != {AUTOMATION_ENTITLEMENT: True}:
            raise RuntimeError(f'{relative}: unexpected release entitlements')
    print(f'Verified Developer ID team {team} on {len(paths)} signed code objects')
    return team


def verify_release(bundle):
    verify_provenance(bundle)
    verify(bundle)
    team = verify_developer_id(bundle)
    subprocess.run(['xcrun', 'stapler', 'validate', str(bundle)], check=True, capture_output=True)
    subprocess.run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=2', str(bundle)],
                   check=True, capture_output=True)
    print('Verified stapled app ticket and Gatekeeper assessment')
    return team


def verify_provenance(bundle):
    resources = bundle / 'Contents/Resources'
    manifest = json.loads((resources / 'build-manifest.json').read_text())
    info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
    commit = manifest.get('source_commit')
    if manifest.get('source_dirty') is not False or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('Release provenance requires a clean source commit')
    if manifest.get('backend_source_dirty') is not False or manifest.get('backend_commit') != '7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4':
        raise RuntimeError('Release provenance requires the clean pinned backend source')
    if manifest.get('signing_mode') != 'developer-id' or manifest.get('release_channel') != 'beta':
        raise RuntimeError('Only Developer ID public beta candidates are currently supported')
    if manifest.get('version') != info['CFBundleShortVersionString'] + '-beta' or manifest.get('build') != info['CFBundleVersion']:
        raise RuntimeError('Manifest version/build does not match the application')
    for field, path in [('backend_sha256', resources / 'bin/LogicProMCP'),
                        ('runtime_sha256', resources / 'runtime/logic-mcp')]:
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get(field):
            raise RuntimeError(f'Release provenance hash mismatch: {field}')
    notice = manifest.get('python_third_party_notice', {})
    expected_path = 'licenses/Python/THIRD-PARTY-LICENSES.rst'
    if notice.get('path') != expected_path or hashlib.sha256((resources / expected_path).read_bytes()).hexdigest() != notice.get('sha256'):
        raise RuntimeError('Missing or mismatched CPython third-party notices')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--signatures', action='store_true')
    group.add_argument('--developer-id', action='store_true')
    group.add_argument('--release', action='store_true')
    args = parser.parse_args()
    bundle = args.app.resolve()
    if args.release:
        verify_release(bundle)
    elif args.developer_id:
        verify_developer_id(bundle)
    elif args.signatures:
        verify_signatures(bundle)
    else:
        verify(bundle)
