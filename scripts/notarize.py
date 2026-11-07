import argparse
import json
from pathlib import Path
import subprocess


def notarize(archive, profile, label):
    result_path = archive.parent / f'notary-{label}-result.json'
    log_path = archive.parent / f'notary-{label}-log.json'
    if result_path.exists() or log_path.exists():
        raise RuntimeError('Notary records already exist; inspect the previous submission instead of submitting it again')
    result = subprocess.run(['xcrun', 'notarytool', 'submit', str(archive), '--keychain-profile', profile,
                             '--wait', '--timeout', '20m', '--output-format', 'json'],
                            capture_output=True, text=True, timeout=1260)
    result_path.write_text(result.stdout or json.dumps({'error': 'notarytool returned no JSON', 'exit_code': result.returncode}))
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f'No valid notary response. Inspect {result_path}; credentials and submission may need checking.') from exc
    if not isinstance(data, dict):
        raise RuntimeError(f'Malformed notary response. Inspect {result_path}.')
    submission = data.get('id')
    if submission is not None and (not isinstance(submission, str) or not submission.strip()):
        raise RuntimeError(f'Malformed notary submission ID. Inspect {result_path}.')
    if submission:
        subprocess.run(['xcrun', 'notarytool', 'log', submission, '--keychain-profile', profile, str(log_path)],
                       capture_output=True, timeout=60, check=data.get('status') == 'Accepted')
    if result.returncode or data.get('status') != 'Accepted' or not submission:
        raise RuntimeError(f'Notarization not accepted: {data.get("status", "unknown")}; submission {submission}. See {result_path}.')
    print(f'Notarization accepted: {submission}')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Submit one signed archive and retain its notary result/log. Does not publish.')
    parser.add_argument('archive', type=Path)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--label', choices=('app', 'dmg'), required=True)
    args = parser.parse_args()
    notarize(args.archive.resolve(), args.profile, args.label)
