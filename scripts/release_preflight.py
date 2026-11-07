import argparse
import os
from pathlib import Path
import re
import subprocess


def validate_configuration(identity, profile, release):
    if release not in ('0', '1'):
        raise RuntimeError('LOGIC_MCP_RELEASE must be 0 or 1')
    if (release == '1' or profile) and (not identity or identity == '-'):
        raise RuntimeError('Release/notarization requires a Developer ID Application identity')
    if release == '1' and not profile:
        raise RuntimeError('Release requires LOGIC_MCP_NOTARY_PROFILE stored in your Keychain')


def resolve_identity(identity, output):
    matches = re.findall(r'\b([A-Fa-f0-9]{40})\s+"(Developer ID Application: [^"\n]+)"', output)
    selected = [(digest, name) for digest, name in matches if identity == name or identity.lower() == digest.lower()]
    if len(selected) != 1:
        raise RuntimeError('Select exactly one valid Developer ID Application identity from security find-identity -v -p codesigning')
    return selected[0][0]


def preflight(repo, identity, profile, release):
    validate_configuration(identity, profile, release)
    if identity != '-':
        output = subprocess.check_output(['security', 'find-identity', '-v', '-p', 'codesigning'], text=True)
        resolve_identity(identity, output)
    if release == '1' or profile:
        status = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=normal'], text=True)
        if status.strip():
            raise RuntimeError('Release builds require a clean, committed source checkout')
        subprocess.run(['xcrun', 'notarytool', 'history', '--keychain-profile', profile, '--output-format', 'json'],
                       check=True, capture_output=True, timeout=60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check local release prerequisites without submitting or publishing anything.')
    parser.add_argument('--release', action='store_true')
    args = parser.parse_args()
    try:
        preflight(Path(__file__).resolve().parents[1], os.environ.get('LOGIC_MCP_SIGN_IDENTITY', '-'),
                  os.environ.get('LOGIC_MCP_NOTARY_PROFILE', ''), '1' if args.release else os.environ.get('LOGIC_MCP_RELEASE', '0'))
    except (RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))
    print('Release prerequisites verified' if args.release else 'Build configuration verified')
