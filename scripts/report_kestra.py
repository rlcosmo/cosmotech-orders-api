"""Report real GitHub stage outcomes via fixed SSM documents; no Kestra secret on runners."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time

DOCUMENTS = {'orders-api': 'CosmoTech-Record-Orders-API-Pipeline',
             'orders-worker': 'CosmoTech-Record-Orders-Worker-Pipeline',
             'gitops': 'CosmoTech-Record-GitOps-Pipeline'}


def aws(*args: str) -> dict:
    return json.loads(subprocess.check_output(['aws', '--region', 'us-east-1', *args], text=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', choices=DOCUMENTS, required=True)
    parser.add_argument('--stage', required=True)
    parser.add_argument('--state', choices=['running', 'success', 'failure', 'cancelled', 'skipped'], required=True)
    parser.add_argument('--source-sha')
    args = parser.parse_args()
    sha = args.source_sha or os.environ['GITHUB_SHA']
    run = '0' if args.source_sha else os.environ['GITHUB_RUN_ID']
    attempt = os.getenv('GITHUB_RUN_ATTEMPT', '1')
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or not re.fullmatch(r'[0-9]+', run):
        raise ValueError('Invalid immutable source execution')
    command = aws('ssm', 'send-command', '--document-name', DOCUMENTS[args.app], '--document-version', '$LATEST',
        '--instance-ids', 'i-0def4e806c49cf60e', '--parameters', json.dumps({
            'RunId': [run], 'RunAttempt': [attempt], 'CommitSha': [sha],
            'Stage': [args.stage], 'State': [args.state]}))['Command']['CommandId']
    for _ in range(30):
        result = subprocess.run(['aws', '--region', 'us-east-1', 'ssm', 'get-command-invocation',
            '--command-id', command, '--instance-id', 'i-0def4e806c49cf60e'], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            value = json.loads(result.stdout)
            if value['Status'] == 'Success':
                print(value['StandardOutputContent'].strip())
                return
            if value['Status'] in {'Failed', 'Cancelled', 'TimedOut'}:
                raise RuntimeError('Kestra evidence command failed: ' + command + '; ' + value['StandardErrorContent'][-1000:])
        time.sleep(2)
    raise TimeoutError('Kestra evidence command did not finish: ' + command)


if __name__ == '__main__':
    main()
