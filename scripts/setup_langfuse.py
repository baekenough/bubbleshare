#!/usr/bin/env python3
"""비밀값을 출력하거나 덮어쓰지 않고 누락된 로컬 Langfuse 설정만 추가한다."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import secrets
import uuid


def read_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.removeprefix('export ').split('=', 1)
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).resolve().parents[1] / '.env')
    args = parser.parse_args()
    path = args.env_file.resolve()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'r+', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        text = stream.read()
        current = read_values(text)
        defaults = {
            'LANGFUSE_BASE_URL': 'http://localhost:3000',
            'LANGFUSE_WEB_PORT': '3000',
            'LANGFUSE_MINIO_PORT': '9090',
            'LANGFUSE_PUBLIC_KEY': f'pk-lf-{uuid.uuid4()}',
            'LANGFUSE_SECRET_KEY': f'sk-lf-{uuid.uuid4()}',
            'LANGFUSE_INIT_ORG_ID': 'vivi-local',
            'LANGFUSE_INIT_PROJECT_ID': 'vivi-agent',
            'LANGFUSE_INIT_USER_EMAIL': 'vivi@localhost.local',
            'LANGFUSE_INIT_USER_PASSWORD': secrets.token_hex(24),
            'LANGFUSE_NEXTAUTH_SECRET': secrets.token_hex(32),
            'LANGFUSE_SALT': secrets.token_hex(32),
            'LANGFUSE_ENCRYPTION_KEY': secrets.token_hex(32),
            'LANGFUSE_POSTGRES_PASSWORD': secrets.token_hex(24),
            'LANGFUSE_CLICKHOUSE_PASSWORD': secrets.token_hex(24),
            'LANGFUSE_REDIS_PASSWORD': secrets.token_hex(24),
            'LANGFUSE_MINIO_PASSWORD': secrets.token_hex(24),
        }
        additions = {key: value for key, value in defaults.items() if not current.get(key)}
        if additions:
            stream.seek(0, os.SEEK_END)
            if text and not text.endswith('\n'):
                stream.write('\n')
            stream.write('\n# Local Langfuse settings; generated once, keep private.\n')
            for key, value in additions.items():
                stream.write(f'{key}={value}\n')
            stream.flush()
            os.fsync(stream.fileno())
    print(f'Langfuse configuration ready: {path} (mode 0600; {len(additions)} missing settings added).')
    print('Existing values were preserved. Credentials are available only in the env file.')


if __name__ == '__main__':
    main()
