"""Manage the installed Fedora server from the local console or SSH."""
import argparse
from configparser import ConfigParser
from contextlib import contextmanager, nullcontext
import fcntl
from getpass import getpass
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile

from . import battery, target_policy as policy, vpn
from .common import Failure, output, run, valid_hostname

ROOT = Path('/')
BATTERY = ROOT / 'etc/battery-limits.conf'
KEY_LIMIT = 256 * 1024


def systemctl(*args):
    run(['systemctl', *args], timeout=60)


def restore(*paths):
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        run(['restorecon', *existing])


@contextmanager
def rollback_files(paths, on_restore):
    saved = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
             for path in paths}
    try:
        yield
    except (Failure, OSError, KeyboardInterrupt):
        for path, previous in saved.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                policy.write(path, *previous)
        restore(*paths)
        on_restore()
        raise


def account():
    name = (ROOT / 'etc/kiosk-server-user').read_text().strip()
    return pwd.getpwnam(name)


KEY_SCRIPT = '''
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
if sys.argv[2] == 'read':
    if path.exists():
        if not stat.S_ISREG(path.stat().st_mode):
            raise SystemExit(1)
        with path.open('rb') as stream:
            sys.stdout.buffer.write(stream.read(262145))
else:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if sys.argv[2] == 'remove':
        path.unlink(missing_ok=True)
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            try:
                stream.write(sys.stdin.buffer.read())
                stream.flush()
                os.replace(stream.name, path)
            finally:
                Path(stream.name).unlink(missing_ok=True)
'''


def authorized_keys(user, action, data=None):
    path = Path(user.pw_dir) / '.ssh/authorized_keys'
    result = subprocess.run([sys.executable, '-I', '-c', KEY_SCRIPT, str(path), action],
                            user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                            input=data if data is not None else b'', stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode or len(result.stdout) > KEY_LIMIT:
        raise Failure('Cannot access the server account authorized_keys file')
    return result.stdout


def file_data(path, limit):
    with (nullcontext(sys.stdin.buffer) if path == '-' else open(path, 'rb')) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise Failure('File is too large')
    return content


def edit(content, suffix):
    if not sys.stdin.isatty():
        raise Failure('Open an interactive terminal to use the editor')
    with tempfile.TemporaryDirectory(prefix='server-config-', dir='/run') as scratch:
        path = Path(scratch) / suffix
        policy.write(path, content, 0o600)
        result = subprocess.run(['/usr/bin/nvim', '-u', 'NONE', '-i', 'NONE', '-n',
                                 '--cmd', 'set nomodeline noexrc', str(path)],
                                cwd=scratch, check=False)
        if result.returncode:
            raise Failure('Editor exited without saving changes')
        return file_data(path, KEY_LIMIT)


def key_count(content):
    count = 0
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith(b'#'):
            continue
        if run(['ssh-keygen', '-l', '-f', '/dev/stdin'], data=line + b'\n', check=False).returncode:
            raise Failure('Invalid SSH public key or authorized_keys entry')
        count += 1
    return count


def ssh(args):
    user = account()
    config = ROOT / policy.SSH_CONFIG
    original = authorized_keys(user, 'read')
    match = re.search(r'^PasswordAuthentication (yes|no)$', config.read_text(), re.M) if config.exists() else None
    password_login = args.password_login or (match[1] if match else 'no')
    keys = file_data(args.keys, KEY_LIMIT) if args.keys else edit(original, 'authorized_keys') if args.edit_keys else original
    count = key_count(keys)
    if not (args.keys or args.edit_keys or args.password_login):
        print(f'{user.pw_name}: {count} SSH keys, password login {password_login}')
        return
    if not count and password_login == 'no':
        raise Failure('Keep at least one public key or enable SSH password login')
    changed_keys = keys != original
    path = Path(user.pw_dir) / '.ssh/authorized_keys'

    def rollback():
        if changed_keys:
            authorized_keys(user, 'write', original)
            restore(path)
        systemctl('reload', 'sshd.service')

    with rollback_files([config], rollback):
        if changed_keys:
            authorized_keys(user, 'write', keys)
        policy.write_ssh(ROOT, password_login)
        restore(config, path.parent, path)
        run(['/usr/sbin/sshd', '-t'])
        systemctl('reload', 'sshd.service')
    print(f'SSH saved for {user.pw_name}: {count} keys, password login {password_login}')


def charging(args):
    config = battery.config(BATTERY)
    changes = dict(START_PERCENT=args.start, STOP_PERCENT=args.stop, FULL_PERCENT=args.full,
                   GUARD_ENABLED=args.guard, GUARD_SHUTDOWN_AT=args.shutdown_at, BATTERY=args.pack)
    if all(value is None for value in changes.values()) and args.mode is None:
        print(output(['/usr/local/bin/battery-limits', 'status']))
        print(f"Configured corridor: {config['START_PERCENT']}–{config['STOP_PERCENT']}%")
        print(f"UPS guard: {config['GUARD_ENABLED']} at {config['GUARD_SHUTDOWN_AT']}%")
        return
    if any(value is not None for value in changes.values()):
        if args.pack and not re.fullmatch(r'auto|[A-Za-z0-9_-]+', args.pack):
            raise Failure('Use a battery name or auto')
        config.update({key: value for key, value in changes.items() if value is not None})
        content = ''.join(f'{key}={value}\n' for key, value in config.items())
        with tempfile.TemporaryDirectory(prefix='server-charge-', dir='/run') as temporary:
            candidate = Path(temporary) / 'battery-limits.conf'
            policy.write(candidate, content)
            battery.config(candidate)
        with rollback_files([BATTERY], lambda: None):
            policy.write(BATTERY, content)
            restore(BATTERY)
    command = ['/usr/local/bin/battery-limits', args.mode or 'apply']
    result = subprocess.run(command, check=False, timeout=60)
    if result.returncode:
        raise Failure('Battery settings saved. Hardware could not apply every limit.')


def updates(args):
    config = ROOT / policy.UPDATE_CONFIG
    schedule = ROOT / policy.UPDATE_SCHEDULE
    parser = ConfigParser()
    parser.read(config)
    reboot = parser.get('commands', 'reboot', fallback=policy.DEFAULT_UPDATES['reboot'])
    match = re.search(r'^OnUnitInactiveSec=([0-9]+)h$', schedule.read_text(), re.M) if schedule.exists() else None
    enabled = run(['systemctl', 'is-enabled', '--quiet', policy.TIMER], check=False).returncode == 0
    hours = int(match[1]) if match and enabled else (8 if enabled else 0)
    if args.hours is None and args.reboot is None:
        print(f'Updates every {hours} h, reboot {reboot}' if hours else 'Automatic updates disabled')
        return
    hours = args.hours if args.hours is not None else hours
    reboot = args.reboot or reboot
    if not hours and reboot != 'never' and args.reboot:
        raise Failure('Enable updates with --hours before choosing automatic reboots')

    def activate(on):
        systemctl('daemon-reload')
        if on:
            systemctl('enable', policy.TIMER)
            systemctl('restart', policy.TIMER)
        else:
            systemctl('disable', '--now', policy.TIMER)

    with rollback_files([config, schedule], lambda: activate(enabled)):
        policy.write_updates(ROOT, hours, reboot)
        restore(config, schedule)
        activate(hours > 0)
    print(f'Updates every {hours} h, reboot {reboot}' if hours else 'Automatic updates disabled')


def tunnel(args):
    current = {kind: ROOT / path for kind, (path, _, _) in policy.VPN_TARGETS.items()
               if (ROOT / path).exists()}
    if not (args.off or args.url or args.file or args.edit):
        names = ', '.join(vpn.VPN_NAMES[kind] for kind in current)
        print('VPN: ' + (names or 'off'))
        for kind in current:
            unit = policy.VPN_TARGETS[kind][1]
            print(f"{unit}: {output(['systemctl', 'is-active', unit], check=False)}")
        return
    profile = None
    if not args.off:
        if args.url:
            url = getpass('Profile HTTPS URL: ')
            content = vpn.download(url)
        elif args.file:
            content = file_data(args.file, vpn.MAX_BYTES)
        else:
            original = current[next(iter(current))].read_bytes() if current else b''
            # Remove installer-provided OpenVPN runtime defaults before editing.
            if original.startswith(b'script-security 1\nping 15\nping-restart 60\n'):
                original = original.split(b'\n', 3)[3]
            content = edit(original, 'vpn.conf')
        profile = vpn.identify(content)
    affected = [ROOT / path for path, unit, _ in policy.VPN_TARGETS.values()]
    affected += [ROOT / policy.vpn_recovery(unit) for _, unit, _ in policy.VPN_TARGETS.values()]
    active = [policy.VPN_TARGETS[kind][1] for kind in current]

    def rollback():
        systemctl('daemon-reload')
        if active:
            systemctl('enable', *active)
            for unit in active:
                systemctl('start', '--no-block', unit)

    with rollback_files(affected, rollback):
        for unit in active:
            systemctl('disable', '--now', unit)
        for path in affected:
            path.unlink(missing_ok=True)
        if profile:
            unit = policy.write_vpn(ROOT, profile)
            config = ROOT / policy.VPN_TARGETS[profile['kind']][0]
            restore(config, ROOT / policy.vpn_recovery(unit))
            systemctl('daemon-reload')
            if profile['kind'] == 'wireguard':
                systemctl('enable', '--now', 'systemd-resolved.service')
            try:
                systemctl('enable', unit)
                systemctl('start', '--no-block', unit)
            except Failure:
                systemctl('disable', '--now', unit)
                raise
        else:
            systemctl('daemon-reload')
    print(f"{vpn.VPN_NAMES[profile['kind']]} enabled. Check status with server-config vpn." if profile else 'VPN disabled')


def identity(args):
    if args.hostname and not valid_hostname(args.hostname):
        raise Failure('Use a lowercase hostname of up to 41 characters')
    if args.timezone and args.timezone not in output(['timedatectl', 'list-timezones']).splitlines():
        raise Failure('Unknown timezone')
    if args.keymap and args.keymap not in output(['localectl', 'list-keymaps']).splitlines():
        raise Failure('Unknown console keymap')
    if args.hostname:
        run(['hostnamectl', 'set-hostname', args.hostname])
    if args.timezone:
        run(['timedatectl', 'set-timezone', args.timezone])
    if args.keymap:
        run(['localectl', '--no-convert', 'set-keymap', args.keymap])
    print(output(['hostnamectl', '--static']))
    print(output(['timedatectl', 'show', '--property=Timezone', '--value']))
    print(output(['localectl', 'status']))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='server-config', description='Configure the installed Fedora server.')
    commands = parser.add_subparsers(dest='command')
    charge = commands.add_parser('battery', help='Inspect or change charge limits and UPS reserve')
    for name in ('start', 'stop', 'full', 'shutdown-at'):
        charge.add_argument('--' + name, type=int, metavar='PERCENT')
    charge.add_argument('--guard', choices=('yes', 'no'))
    charge.add_argument('--pack', help='Battery name or auto')
    charge.add_argument('--mode', choices=('normal', 'full', 'off'))
    charge.set_defaults(handler=charging)
    access = commands.add_parser('ssh', help='Manage the server account SSH keys and password login')
    source = access.add_mutually_exclusive_group()
    source.add_argument('--keys', metavar='FILE', help='Replace authorized keys from a file or - for stdin')
    source.add_argument('--edit-keys', action='store_true', help='Edit authorized keys in Neovim')
    access.add_argument('--password-login', choices=('yes', 'no'))
    access.set_defaults(handler=ssh)
    maintenance = commands.add_parser('updates', help='Change automatic updates and reboot policy')
    maintenance.add_argument('--hours', type=int, metavar='0-168', help='Interval in hours, 0 disables')
    maintenance.add_argument('--reboot', choices=policy.REBOOT_LABELS)
    maintenance.set_defaults(handler=updates)
    vpn_cmd = commands.add_parser('vpn', help='Inspect, replace or disable the VPN')
    source = vpn_cmd.add_mutually_exclusive_group()
    source.add_argument('--off', action='store_true')
    source.add_argument('--url', action='store_true', help='Prompt for an HTTPS profile URL')
    source.add_argument('--file', metavar='FILE', help='Import a profile from a file or - for stdin')
    source.add_argument('--edit', action='store_true', help='Edit the profile in Neovim')
    vpn_cmd.set_defaults(handler=tunnel)
    basics = commands.add_parser('identity', help='Change hostname, timezone or console keymap')
    for name in ('hostname', 'timezone', 'keymap'):
        basics.add_argument('--' + name)
    basics.set_defaults(handler=identity)
    commands.add_parser('network', help='Edit NetworkManager connections in nmtui').set_defaults(
        handler=lambda args: subprocess.run(['/usr/bin/nmtui'], check=True))
    commands.add_parser('password', help='Change the local account password').set_defaults(
        handler=lambda args: subprocess.run(['/usr/bin/passwd', account().pw_name], check=True))
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if os.geteuid():
            raise Failure('Run this command with sudo')
        with open('/run/server-config.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Failure('Another server-config session is active') from None
            args.handler(args)
    except (Failure, OSError, subprocess.SubprocessError, KeyboardInterrupt, EOFError, KeyError, ValueError) as error:
        print(str(error) if isinstance(error, Failure) else 'Configuration change failed.', file=sys.stderr)
        return 1
    return 0
