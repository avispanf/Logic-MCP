"""Generate release provenance and retain dependency license files in a built app."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from package_native_licenses import collect_native_licenses, locate_backend_source

app, backend = map(Path, sys.argv[1:3])
resources = app / 'Contents/Resources'
packages = []
for dist in importlib.metadata.distributions():
    name = dist.metadata['Name']
    packages.append({'name': name, 'version': dist.version})
    for file in dist.files or []:
        if 'license' in str(file).lower() or Path(file).name.lower().startswith(('copying', 'notice')):
            source = Path(dist.locate_file(file))
            if source.is_file():
                target = resources / 'licenses' / name / Path(file).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
python_license = next((folder / name for folder in (Path(sys.base_prefix), *Path(sys.base_prefix).parents)
                       for name in ('LICENSE', 'LICENSE.txt') if (folder / name).is_file()), None)
target = resources / 'licenses/Python/LICENSE'
target.parent.mkdir(parents=True, exist_ok=True)
if python_license:
    shutil.copyfile(python_license, target)
else:
    # python.org CI installations omit the source license file. Retrieve the exact
    # runtime version's notice from CPython; a failed fetch fails the build.
    from urllib.request import urlopen
    version = '.'.join(map(str, sys.version_info[:3]))
    with urlopen(f'https://raw.githubusercontent.com/python/cpython/v{version}/LICENSE', timeout=20) as response:
        license_text = response.read().decode('utf-8')
    if 'PYTHON SOFTWARE FOUNDATION' not in license_text:
        raise RuntimeError('Unexpected CPython license response')
    target.write_text(license_text)
manifest = {'version': '0.2.0-beta', 'backend_commit': '7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4',
    'backend_sha256': hashlib.sha256(backend.read_bytes()).hexdigest(),
    'python': sys.version, 'architecture': 'arm64', 'minimum_macos': '14.0',
    'packages': sorted(packages, key=lambda row: row['name'].lower())}
native_source = locate_backend_source(Path(sys.argv[3]), os.environ.get('LOGIC_MCP_BACKEND_SOURCE'))
native_commit = subprocess.run(['git', '-C', str(native_source), 'rev-parse', 'HEAD'],
                               capture_output=True, text=True, check=True, timeout=5).stdout.strip()
if native_commit != manifest['backend_commit']:
    raise RuntimeError('Native notice checkout does not match the pinned backend commit')
manifest['native_license_files'] = collect_native_licenses(native_source, resources)
(resources / 'build-manifest.json').write_text(json.dumps(manifest, indent=2))
