import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

SHARE = Path('/usr/share/kiosk')
RUN = Path('/run/kiosk')
LOG = Path('/var/log/kiosk-install.log')


class Failure(Exception):
    """A safe, public error message, without subprocess output or secrets."""


def run(args, *, data=None, timeout=30, check=True):
    try:
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Failure(f'{Path(args[0]).name} unavailable; retry') from None
    if check and result.returncode:
        raise Failure(f'{Path(args[0]).name} failed; retry')
    return result


def output(args, **kwargs):
    return run(args, **kwargs).stdout.decode('utf-8', 'replace').strip()


def private_write(path, data):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data.encode() if isinstance(data, str) else data)


def log(message):
    # Callers supply fixed events, never user input or command output.
    fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        stream.write(time.strftime('%Y-%m-%dT%H:%M:%SZ ', time.gmtime()) + message + '\n')


def poweroff(*_):
    # ExecStopPost remains responsible if this process is killed or hangs.
    for command in (['sync'], ['poweroff', '-i'], ['poweroff', '-f']):
        try:
            run(command, timeout=0.5, check=False)
        except Failure:
            pass
    raise SystemExit(1)


def install_signals():
    os.umask(0o077)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, poweroff)


def safe(text):
    # Textual/Rich measures cell widths, including wide and combining glyphs.
    return ''.join(c if c.isprintable() else '?' for c in str(text))


def valid_hostname(value):
    return re.fullmatch(r'[a-z][a-z0-9-]{0,40}', value) is not None
