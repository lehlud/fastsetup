"""Files shared by Kickstart and the installed server's configuration command."""
import os
from pathlib import Path
import tempfile

from .common import Failure

REBOOT_LABELS = {'never': 'Never', 'when-needed': 'When needed', 'when-changed': 'After any update'}
DEFAULT_UPDATES = {'hours': 8, 'reboot': 'never'}
TIMER = 'dnf5-automatic.timer'
UPDATE_CONFIG = 'etc/dnf/automatic.conf'
UPDATE_SCHEDULE = f'etc/systemd/system/{TIMER}.d/10-server-schedule.conf'
SSH_CONFIG = 'etc/ssh/sshd_config.d/00-kiosk-auth.conf'
VPN_TARGETS = {
    'wireguard': ('etc/wireguard/kiosk-vpn.conf', 'wg-quick@kiosk-vpn.service', 'on-failure'),
    'openvpn': ('etc/openvpn/client/kiosk-vpn.conf', 'openvpn-client@kiosk-vpn.service', 'always'),
}


def write(path, content, mode=0o644):
    """Replace a policy file atomically, preserving the requested permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content.encode() if isinstance(content, str) else content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_updates(root, hours, reboot):
    if type(hours) is not int or not 0 <= hours <= 168 or reboot not in REBOOT_LABELS:
        raise Failure('Invalid automatic update policy')
    apply = 'yes' if hours else 'no'
    write(root / UPDATE_CONFIG, f'[commands]\ndownload_updates = {apply}\napply_updates = {apply}\n'
          f'reboot = {reboot if hours else "never"}\nrandom_sleep = 0\n')
    schedule = root / UPDATE_SCHEDULE
    if hours:
        write(schedule, '[Timer]\nOnCalendar=\nRandomizedDelaySec=0\nPersistent=false\n'
              f'OnBootSec={hours}h\nOnUnitInactiveSec={hours}h\n')
    else:
        schedule.unlink(missing_ok=True)


def vpn_recovery(unit):
    return f'etc/systemd/system/{unit}.d/10-reconnect.conf'


def write_vpn(root, profile):
    relative, unit, restart = VPN_TARGETS[profile['kind']]
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write(path, profile['config'], 0o600)
    recovery = '[Unit]\nStartLimitIntervalSec=0\n[Service]\n' + f'Restart={restart}\nRestartSec=15s\n'
    if profile['kind'] == 'wireguard':
        recovery += 'Environment=WG_ENDPOINT_RESOLUTION_RETRIES=5\n'
    write(root / vpn_recovery(unit), recovery)
    return unit


def write_ssh(root, password_login):
    if password_login not in ('yes', 'no'):
        raise Failure('Invalid SSH login policy')
    methods = 'publickey' if password_login == 'no' else 'any'
    write(root / SSH_CONFIG, 'PubkeyAuthentication yes\n'
          f'PasswordAuthentication {password_login}\nKbdInteractiveAuthentication no\n'
          f'AuthenticationMethods {methods}\n')
