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


if __name__ == '__main__':
    verify(Path(sys.argv[1]).resolve())
