"""Smoke-test a DEV release without submitting a valid order."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def request_json(base_url: str, path: str, data: bytes | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(base_url.rstrip('/') + path, data=data,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--expected-version', default='2.0.0-lab')
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    checks = []
    for path, expected, data in [
        ('/health/live', 200, None), ('/health/ready', 200, None), ('/version', 200, None),
        ('/orders/cosmotech-cd-readcheck-' + uuid.uuid4().hex, 404, None),
        ('/orders', 400, b'{}'),
    ]:
        code, body = request_json(args.base_url, path, data)
        result = {'check': '/orders/<nonexistent>' if path.startswith('/orders/') else path,
                  'method': 'POST' if data else 'GET', 'status': code, 'passed': code == expected}
        if path == '/version':
            result['body'] = body
            result['passed'] = bool(result['passed']) and body.get('version') == args.expected_version
        checks.append(result)
    evidence = json.dumps(checks, indent=2) + '\n'
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(evidence)
    print(evidence)
    if not all(check['passed'] for check in checks):
        raise SystemExit('Smoke test failed; no valid order was submitted')


if __name__ == '__main__':
    main()
