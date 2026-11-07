import hashlib
from pathlib import Path
import re
from urllib.request import urlopen


def collect_python_notices(resources, version, fetch=urlopen):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise RuntimeError('A final CPython version is required for version-matched notices')
    url = f'https://raw.githubusercontent.com/python/cpython/v{version}/Doc/license.rst'
    with fetch(url, timeout=20) as response:
        content = response.read()
    text = content.decode('utf-8')
    if not all(marker in text for marker in ('History and License', 'PYTHON SOFTWARE FOUNDATION', 'OpenSSL')):
        raise RuntimeError('Unexpected CPython third-party license document')
    relative = Path('licenses/Python/THIRD-PARTY-LICENSES.rst')
    target = resources / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {'path': relative.as_posix(), 'url': url, 'sha256': hashlib.sha256(content).hexdigest()}
