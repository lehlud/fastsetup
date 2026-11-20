"""Files shared by Kickstart and the installed server's configuration command."""
import os
from pathlib import Path
import subprocess
import tempfile

from .common import Failure

REBOOT_LABELS = {'never': 'Never', 'when-needed': 'When needed', 'when-changed': 'After any update'}
DEFAULT_UPDATES = {'hours': 8, 'reboot': 'never'}
TIMER = 'dnf5-automatic.timer'
UPDATE_CONFIG = 'etc/dnf/automatic.conf'
UPDATE_SCHEDULE = f'etc/systemd/system/{TIMER}.d/10-server-schedule.conf'
SSH_CONFIG = 'etc/ssh/sshd_config.d/00-kiosk-auth.conf'
ZRAM_CONFIG = 'etc/systemd/zram-generator.conf.d/60-server.conf'
MIB = 1024 ** 2
DISK_SWAP_PATH = '/var/swap/swapfile'
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


def configure_swap(root):
    write(root / ZRAM_CONFIG, '[zram0]\nzram-size = ram / 2\nswap-priority = 100\n')
    fstab = root / 'etc/fstab'
    entries = fstab.read_text()
    if any(line.split()[:1] == [DISK_SWAP_PATH] for line in entries.splitlines()):
        return
    space = os.statvfs(root)
    size_mib = min(16 * 1024, space.f_bavail * space.f_frsize // (4 * MIB))
    if size_mib < 1024:
        print('Disk swap skipped because less than 4 GiB is free')
        return
    swap = root / DISK_SWAP_PATH.lstrip('/')
    if not swap.parent.exists():
        subprocess.run(['btrfs', 'subvolume', 'create', str(swap.parent)], check=True)
    swap.parent.chmod(0o700)
    if not swap.exists():
        temporary = swap.with_name('.swapfile')
        temporary.unlink(missing_ok=True)
        try:
            subprocess.run(['btrfs', 'filesystem', 'mkswapfile', '--size', f'{size_mib}M', str(temporary)],
                           check=True)
            temporary.chmod(0o600)
            temporary.replace(swap)
        finally:
            temporary.unlink(missing_ok=True)
    subprocess.run(['chroot', str(root), 'semanage', 'fcontext', '-a', '-f', 'f', '-t', 'swapfile_t',
                    DISK_SWAP_PATH], check=True)
    subprocess.run(['chroot', str(root), 'restorecon', '-RF', str(Path(DISK_SWAP_PATH).parent)], check=True)
    write(fstab, entries.rstrip('\n') + f'\n{DISK_SWAP_PATH} none swap defaults,pri=10,nofail 0 0\n')


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
