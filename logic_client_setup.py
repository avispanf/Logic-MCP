import json
import os
from pathlib import Path
import tempfile
import tomllib
import uuid

import tomlkit
import logic_manual_mode as mode


NAMES = ('logic-pro', 'logic-plugins', 'logic-audio')


def matches(existing, expected):
    if any(field in existing and field not in expected for field in ('command', 'args', 'url')):
        return False
    for field, value in expected.items():
        actual = existing.get(field)
        if field == 'env':
            if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in value.items()):
                return False
        elif actual != value:
            return False
    return True


def entries(runtime, client):
    if client not in ('codex', 'claude'):
        raise RuntimeError('Unsupported MCP client')
    result = {name: {'command': str(runtime), 'args': ['--' + role]}
              for name, role in zip(NAMES, ('core', 'plugins', 'audio'))}
    for value in result.values():
        value['env'] = {'LOGIC_MCP_STATE_DIR': str(mode.STATE_DIR)}
    if client == 'codex':
        result['logic-pro'] = {'url': 'http://127.0.0.1:8765/mcp'}
        for value in result.values():
            value['tool_timeout_sec'] = 180
    return result


def merge_config(before, runtime, client):
    text = before.decode('utf-8')
    document = tomlkit.parse(text) if client == 'codex' else json.loads(text or '{}')
    if not isinstance(document, dict):
        raise RuntimeError('Client configuration must be an object/table')
    key = 'mcp_servers' if client == 'codex' else 'mcpServers'
    if key not in document:
        document[key] = {}
    servers = document[key]
    if not isinstance(servers, dict):
        raise RuntimeError('MCP server configuration must be an object/table')
    for name, entry in servers.items():
        if name not in NAMES and isinstance(entry, dict) and Path(str(entry.get('command', ''))).name in ('LogicProMCP', 'LogicProMCP-codex'):
            raise RuntimeError(f'Legacy standalone Logic backend {name!r} could create duplicate MIDI ports. Remove or disable it explicitly before automatic setup.')
    for name, values in entries(runtime, client).items():
        existing = servers.get(name, {})
        if not isinstance(existing, dict):
            raise RuntimeError(f'Invalid existing MCP server entry: {name}')
        if matches(existing, values):
            continue
        updated = existing.copy()
        for field in ('command', 'args', 'url'):
            updated.pop(field, None)
        environment = updated.get('env', {})
        if not isinstance(environment, dict):
            raise RuntimeError(f'Invalid environment for MCP server: {name}')
        updated.update(values)
        if 'env' in values:
            updated['env'] = {**environment, **values['env']}
        servers[name] = updated
    encoded = (tomlkit.dumps(document) if client == 'codex' else json.dumps(document, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    parsed = tomllib.loads(encoded.decode()) if client == 'codex' else json.loads(encoded)
    if any(not matches(parsed[key][name], values) for name, values in entries(runtime, client).items()):
        raise RuntimeError('Client configuration verification failed')
    return encoded


def read_config(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f'Refusing a non-regular client configuration: {path}')
    if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError(f'Client configuration is unexpectedly large: {path}')
    return path.read_bytes() if path.exists() else None


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.logic-mcp-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_clients(runtime, home=None, codex_directory=None):
    home = Path.home() if home is None else Path(home)
    codex = Path(codex_directory) if codex_directory else home / '.codex'
    targets = [('codex', codex / 'config.toml'), ('claude', home / 'Library/Application Support/Claude/claude_desktop_config.json')]
    plans = []
    for client, path in targets:
        before = read_config(path)
        after = merge_config(before or b'', runtime, client)
        plans.append((path, before, after))
    written = []
    backups = []
    try:
        for path, before, after in plans:
            if before == after:
                continue
            if read_config(path) != before:
                raise RuntimeError(f'Client configuration changed during setup: {path}')
            if before is not None:
                backup = path.with_name(path.name + '.logic-mcp-backup-' + uuid.uuid4().hex)
                atomic_write(backup, before)
                backups.append(str(backup))
            if read_config(path) != before:
                raise RuntimeError(f'Client configuration changed during backup: {path}')
            atomic_write(path, after)
            written.append((path, before, after))
            if read_config(path) != after:
                raise RuntimeError(f'Client configuration readback failed: {path}')
    except Exception:
        for path, before, after in reversed(written):
            if read_config(path) == after:
                if before is None:
                    path.unlink()
                else:
                    atomic_write(path, before)
        raise
    return {'clients': ['codex', 'claude'], 'paths': [str(path) for _, path in targets], 'backups': backups,
            'restart_required': ['Codex', 'Claude Desktop']}
