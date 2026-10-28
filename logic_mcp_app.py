"""Bundled executable: supervisor, MCP servers and setup without external Python."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


def app_bundle():
    executable = Path(sys.executable).resolve()
    return next((p for p in executable.parents if p.suffix == '.app'), None)


def setup():
    bundle = app_bundle()
    if bundle is None:
        raise RuntimeError('Setup must be run from the installed application bundle')
    home = Path.home()
    executable = str(Path(sys.executable).resolve())
    logs = home / 'Library/Logs/LogicMCP'
    logs.mkdir(parents=True, exist_ok=True)
    folder = home / 'Library/LaunchAgents'
    folder.mkdir(parents=True, exist_ok=True)
    menu_label = 'com.avispanf.logic-mcp-menubar'
    menu_target = f'gui/{os.getuid()}/{menu_label}'
    menu_path = folder / (menu_label + '.plist')
    menu_desired = {'Label': menu_label,
        'ProgramArguments': [str(bundle / 'Contents/MacOS/LogicMCPMenuBar')],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
        'StandardOutPath': str(logs / 'menu.log'), 'StandardErrorPath': str(logs / 'menu.error.log')}
    menu_existing = plistlib.loads(menu_path.read_bytes()) if menu_path.exists() else None
    if menu_existing != menu_desired:
        if menu_existing and menu_existing.get('Label') != menu_label:
            raise RuntimeError('Unexpected menu LaunchAgent; refusing to replace it')
        subprocess.run(['launchctl', 'bootout', menu_target], capture_output=True)
        menu_path.write_bytes(plistlib.dumps(menu_desired))
    subprocess.run(['launchctl', 'enable', menu_target], check=True)
    if subprocess.run(['launchctl', 'print', menu_target], capture_output=True).returncode != 0:
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(menu_path)], check=True)
    label = 'com.avispanf.logic-pro-mcp-bridge'
    target = f'gui/{os.getuid()}/{label}'
    path = folder / (label + '.plist')
    desired = {'Label': label, 'ProgramArguments': [executable, '--service'],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
        'StandardOutPath': str(logs / 'service.log'), 'StandardErrorPath': str(logs / 'service.error.log')}
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if existing.get('Label') != label:
            raise RuntimeError('Unexpected LaunchAgent; refusing to replace it')
        if existing == desired and subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0:
            return {'mode': 'setup', 'verified': True, 'message': 'Service installed'}
    import logic_manual_mode as mode
    paused = mode.operate('pause')
    if not paused['verified']:
        raise RuntimeError('Cannot safely migrate service: ' + json.dumps(paused))
    import signal
    current = mode.processes()
    for record in mode.read_state()['processes']:
        if record.get('resume', True) and mode.same_process(record, current.get(record['pid'])):
            try:
                mode.signal_verified(record, signal.SIGKILL)
            except ProcessLookupError:
                pass
    subprocess.run(['launchctl', 'bootout', target], capture_output=True, timeout=15)
    path.write_bytes(plistlib.dumps(desired))
    subprocess.run(['launchctl', 'enable', target], check=True)
    for _ in range(20):
        result = subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], capture_output=True)
        if result.returncode == 0:
            return {'mode': 'setup', 'verified': True, 'message': 'Service installed'}
        time.sleep(.2)
    raise RuntimeError(result.stderr.decode())


def uninstall():
    import logic_manual_mode as mode
    result = mode.operate('pause')
    if not result['verified']:
        raise RuntimeError('Cannot verify worker shutdown: ' + json.dumps(result))
    # Cancel suspended jobs before removing the service. Never resume old work.
    current = mode.processes()
    for row in mode.read_state()['processes']:
        if row.get('resume', True) and mode.same_process(row, current.get(row['pid'])):
            import signal
            mode.signal_verified(row, signal.SIGKILL)
    label = 'com.avispanf.logic-pro-mcp-bridge'
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    if path.exists() and plistlib.loads(path.read_bytes()).get('Label') == label:
        path.unlink()
    menu_label = 'com.avispanf.logic-mcp-menubar'
    subprocess.run(['launchctl', 'disable', f'gui/{os.getuid()}/{menu_label}'], capture_output=True)
    menu_path = Path.home() / 'Library/LaunchAgents' / (menu_label + '.plist')
    if menu_path.exists() and plistlib.loads(menu_path.read_bytes()).get('Label') == menu_label:
        menu_path.unlink()
    # Keep manual mode as a deliberate barrier for clients still configured to connect.
    return {'mode': 'uninstalled', 'verified': True, 'message': 'Service removed; manual barrier retained'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--service', action='store_true')
    group.add_argument('--plugins', action='store_true')
    group.add_argument('--audio', action='store_true')
    group.add_argument('--audit', action='store_true')
    group.add_argument('--control', choices=('status', 'pause', 'resume', 'toggle', 'repair', 'setup', 'uninstall'))
    args, rest = parser.parse_known_args()
    if args.control:
        try:
            if args.control == 'setup':
                result = setup()
            elif args.control == 'uninstall':
                result = uninstall()
            else:
                import logic_manual_mode
                result = logic_manual_mode.operate(args.control)
        except Exception as exc:
            result = {'mode': 'unknown', 'verified': False, 'errors': [str(exc)]}
        print(json.dumps(result))
        return 0 if result['verified'] else 1
    sys.argv = [sys.argv[0]] + rest
    if not args.service and any(arg in ('--help', '-h') for arg in rest):
        print('MCP STDIO server: connect a client; --check runs diagnostics. Manual mode rejects tool calls.')
        return 0
    if args.service:
        import logic_manual_mode as manual
        with manual.register_supervisor():
            import logic_pro_http_bridge
            logic_pro_http_bridge.main()
    elif args.plugins:
        import logic_plugins_mcp
        logic_plugins_mcp.main()
    elif args.audio:
        import logic_audio_mcp
        logic_audio_mcp.main()
    elif args.audit:
        import asyncio
        import run_mix_audit
        return asyncio.run(run_mix_audit.async_main())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
