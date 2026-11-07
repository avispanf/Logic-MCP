"""Reject bundled native code incompatible with the advertised architecture or OS."""
from pathlib import Path
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


if __name__ == '__main__':
    bundle = Path(sys.argv[1]).resolve()
    if sys.argv[2:] == ['--signatures']:
        verify_signatures(bundle)
    elif sys.argv[2:]:
        raise SystemExit('Usage: verify_bundle.py APP [--signatures]')
    else:
        verify(bundle)
