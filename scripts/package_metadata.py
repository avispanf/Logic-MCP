"""Generate release provenance and retain dependency license files in a built app."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys

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
manifest = {'version': '0.2.0-beta', 'backend_commit': '2cb0dfa8b77ac57b64271ffe59558d6023d0bef7',
    'backend_sha256': hashlib.sha256(backend.read_bytes()).hexdigest(),
    'python': sys.version, 'architecture': 'arm64', 'minimum_macos': '14.0',
    'packages': sorted(packages, key=lambda row: row['name'].lower())}
(resources / 'build-manifest.json').write_text(json.dumps(manifest, indent=2))
