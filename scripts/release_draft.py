import argparse
import json
from pathlib import Path
import re
import subprocess

try:
    from .verify_distribution import ASSETS, verify_distribution
except ImportError:
    from verify_distribution import ASSETS, verify_distribution


REPOSITORY = 'avispanf/Logic-MCP'


def draft_command(directory, tag, verified):
    manifest = verified['manifest']
    if manifest.get('release_channel') != 'beta' or not re.fullmatch(re.escape('v' + manifest['version'] + '.') + r'[1-9]\d*', tag):
        raise RuntimeError('The release tag must match the verified beta version, for example v0.2.0-beta.1')
    commit = manifest.get('source_commit', '')
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('Missing verified source commit')
    notes = Path(__file__).resolve().parents[1] / f'docs/release-notes-{tag[1:]}.md'
    if not notes.is_file():
        raise RuntimeError('Version-matched release notes are required before creating a draft')
    return ['gh', 'release', 'create', tag, '--repo', REPOSITORY, '--target', commit,
            '--draft', '--prerelease', '--title', 'Logic MCP ' + tag, '--notes-file', str(notes),
            *[str(directory / name) for name in (*ASSETS, 'SHA256SUMS')]]


def create_draft(command, commit):
    remote = subprocess.check_output(['gh', 'api', f'repos/{REPOSITORY}/commits/{commit}', '--jq', '.sha'], text=True).strip()
    if remote != commit:
        raise RuntimeError('The verified source commit must be pushed to the public repository first')
    subprocess.run(command, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Verify a signed public beta and plan a GitHub DRAFT. Never publishes.')
    parser.add_argument('directory', type=Path)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--create-draft', action='store_true')
    args = parser.parse_args()
    directory = args.directory.resolve()
    verified = verify_distribution(directory)
    command = draft_command(directory, args.tag, verified)
    print(json.dumps({'draft': True, 'prerelease': True, 'source_commit': verified['manifest']['source_commit'],
                      'team': verified['team'], 'assets': verified['assets'], 'command': command}, indent=2))
    if args.create_draft:
        create_draft(command, verified['manifest']['source_commit'])
