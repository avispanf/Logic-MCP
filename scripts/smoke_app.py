"""Verify a packaged app without connecting to Logic or publishing MIDI ports."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    bundle = Path(sys.argv[1]).resolve()
    runtime = bundle / 'Contents/Resources/runtime/logic-mcp'
    resources = bundle / 'Contents/Resources'
    manifest = json.loads((resources / 'build-manifest.json').read_text())
    notice = manifest['python_third_party_notice']
    notice_path = Path(notice['path'])
    assert not notice_path.is_absolute() and '..' not in notice_path.parts
    assert hashlib.sha256((resources / notice_path).read_bytes()).hexdigest() == notice['sha256']
    native_notices = manifest.get('native_license_files', [])
    assert len(native_notices) > 1, 'Native dependency notices must be bundled'
    for name in native_notices:
        relative = Path(name)
        assert not relative.is_absolute() and '..' not in relative.parts
        assert (resources / relative).is_file(), name
    for args in (['--help'], ['--plugins', '--help'], ['--service', '--help']):
        subprocess.run([str(runtime), *args], check=True, stdout=subprocess.DEVNULL, timeout=10)
    with tempfile.TemporaryDirectory(prefix='Logic MCP Smoke ') as directory:
        env = {'LOGIC_MCP_STATE_DIR': directory}
        # Verify actual frozen tool registration while manual mode is active.
        Path(directory, 'manual-mode.json').write_text(json.dumps({'version': 2, 'processes': []}))
        for role, count in (('--plugins', 65), ('--audio', 12)):
            async with stdio_client(StdioServerParameters(command=str(runtime), args=[role], env=env)) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == count, (role, len(tools.tools))
                    result = await session.call_tool('health', {})
                    assert result.is_error, 'Manual mode must refuse the tool'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        log = Path(directory, 'log').open('w+')
        service = subprocess.Popen([str(runtime), '--service', '--port', str(port)],
            env={**os.environ, **env}, stdout=log, stderr=log)
        try:
            for _ in range(100):
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                        status = json.load(response)
                    assert status['state'] == 'manual' and status['ready'] is False
                    break
                except OSError:
                    await asyncio.sleep(.1)
            else:
                log.seek(0)
                raise RuntimeError(log.read())
        finally:
            service.terminate()
            try:
                service.wait(timeout=8)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=3)
            log.close()
    print('Packaged app: native notices, help, 77 tools, manual request rejection and HTTP health verified')


if __name__ == '__main__':
    asyncio.run(main())
