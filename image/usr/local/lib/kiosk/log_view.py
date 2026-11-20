"""Prepare a private, terminal-safe view of the installer logs."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory

from .common import Failure, LOG, safe
from .network import ini

INSTALL_LOGS = (Path('/tmp/storage.log'), Path('/tmp/anaconda.log'), Path('/tmp/program.log'),
                Path('/tmp/packaging.log'), Path('/tmp/ks.error'), LOG)
MAX_LOG_BYTES = 4 * 1024 * 1024
PRIVATE_LINE = re.compile(r'\$[56y]\$|--(?:password|passphrase|wpakey)(?:=|\s)|'
                          r'\b(?:psk|privatekey|presharedkey|password|passphrase)["\']?\s*[:=]|'
                          r'[A-Za-z0-9+/]{80,}={0,2}', re.I)
PRIVATE_VPN_BLOCK = re.compile(r'<(auth-user-pass|key|pkcs12|tls-auth|tls-crypt|tls-crypt-v2)>\s*(.*?)\s*</\1>',
                               re.I | re.S)


def log_secrets(data):
    values = {data.get('hashed', ''), data.get('net', {}).get('psk', '')}
    config = (data.get('vpn') or {}).get('config', '')
    for match in PRIVATE_VPN_BLOCK.finditer(config):
        values.update(line.strip() for line in match[2].splitlines())
    values.update(re.findall(r'^(?:PrivateKey|PresharedKey)\s*=\s*(\S+)', config, re.I | re.M))
    tokens = set()
    for value in values - {''}:
        tokens.update((value, ini(value), repr(value)[1:-1], json.dumps(value)[1:-1],
                       base64.b64encode(value.encode()).decode()))
    return tuple(tokens)


def display_log(path, secrets):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise Failure('Log is unavailable')
            offset = max(0, info.st_size - MAX_LOG_BYTES)
            stream.seek(offset)
            content = stream.read(MAX_LOG_BYTES)
    except OSError:
        raise Failure('Log is unavailable') from None
    lines = content.decode('utf-8', 'replace').splitlines()
    if offset:
        lines[:1] = ['[Showing the end of this log]']
    return '\n'.join('[redacted]' if PRIVATE_LINE.search(row) or any(value in row for value in secrets)
                     else safe(row.expandtabs(8)) for row in lines) + '\n'


def prepare_log(path, secrets, directory):
    plain = directory / path.name
    plain.write_text(display_log(path, secrets), encoding='utf-8')
    highlighted = plain.with_suffix('.color')
    try:
        with highlighted.open('wb') as stream:
            result = subprocess.run(['/usr/bin/bat', '--no-config', '--paging=never', '--language=log',
                                     '--style=numbers', '--color=always', '--', str(plain)],
                                    stdout=stream, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode == 0:
            return highlighted
    except (OSError, subprocess.TimeoutExpired):
        pass
    return plain


@asynccontextmanager
async def log_pager(path, secrets):
    with TemporaryDirectory(prefix='kiosk-log-') as folder:
        plain = await asyncio.to_thread(prepare_log, path, secrets, Path(folder))
        env = {key: value for key, value in os.environ.items() if not key.startswith('LESS')}
        env.update(LESSSECURE='1', LESSHISTFILE='-')
        yield ['/usr/bin/less', '-R', '-K', '--', str(plain)], env
