import asyncio
import base64
import json
import os
from pathlib import Path
import re
import secrets
import struct
import subprocess

from . import battery, keyboard, network, storage, vpn
from .common import Failure, LOG, SHARE, install_signals, log, output, poweroff, private_write, run, valid_hostname
from .install_progress import InstallProgress, read_progress
from .target_policy import DEFAULT_UPDATES, REBOOT_LABELS
from .ui import Back, BusyScreen, EDIT, Field, KioskApp

TOKENS = {'TZ', 'HOSTNAME', 'USER', 'USERHASH', 'DISKNAME', 'LAYOUT', 'VCKEYMAP',
          'SSID_B64', 'PSK_B64', 'SSHKEYBLOCK', 'SSH_PASSWORD', 'NETWORK_B64', 'PAYLOAD_B64', 'BATTERY_B64',
          'SERVER_POLICY_B64'}

STEPS = ['Identity', 'Network', 'Disk', 'Account', 'SSH access', 'VPN', 'Updates']
INSTALL_IDLE_MINUTES = 15


def b64(value):
    return base64.b64encode(value.encode() if isinstance(value, str) else value).decode()


def render(template, values):
    required = set(re.findall(r'@@([A-Z0-9_]+)@@', template))
    if required != TOKENS or set(values) != TOKENS:
        raise Failure('Kickstart contract mismatch; rebuild the image')
    if values['SSH_PASSWORD'] not in ('yes', 'no'):
        raise Failure('Invalid SSH login policy; select SSH settings again')
    if values['SSH_PASSWORD'] == 'no' and not values['SSHKEYBLOCK'].strip():
        raise Failure('Add an SSH key or allow SSH password login')
    # Callable, single-pass replacement: backslashes and &, | are literal, and
    # substituted input cannot recursively introduce another placeholder.
    rendered = re.sub(r'@@([A-Z0-9_]+)@@', lambda match: str(values[match[1]]), template)
    if '@@' in rendered:
        raise Failure('Kickstart token remains; rebuild the image')
    return rendered


def validate_key(value):
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] not in {
        'ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384',
        'ecdsa-sha2-nistp521', 'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com'}:
        raise Failure('Invalid SSH key; paste a public key')
    try:
        data = base64.b64decode(parts[1], validate=True)
        size = struct.unpack('>I', data[:4])[0]
        if data[4:4 + size].decode() != parts[0] or len(data) < size + 8:
            raise ValueError
    except (ValueError, UnicodeError, struct.error):
        raise Failure('Invalid SSH key; paste a public key') from None
    canonical = ' '.join(parts[:2])
    if run(['ssh-keygen', '-l', '-f', '/dev/stdin'], data=(canonical + '\n').encode(), check=False).returncode:
        raise Failure('Invalid SSH key; paste a public key')
    return canonical


def github_keys(user):
    response = output(['curl', '--fail', '--silent', '--show-error', '--proto', '=https',
                       '--connect-timeout', '5', '--max-time', '15', '--max-filesize', '262144',
                       f'https://github.com/{user}.keys'], timeout=20)
    keys = list(dict.fromkeys(validate_key(row) for row in response.splitlines() if row.strip()))
    if not keys:
        raise Failure('No GitHub keys found; retry or paste a key')
    return keys


def key_label(key):
    """Keep both ends of a canonical public key visible within one menu row."""
    return key if len(key) <= 64 else key[:37] + '...' + key[-24:]


async def ssh_keys(ui, fetch=github_keys, validate=validate_key):
    github_user, pasted_key = '', ''
    while True:
        choice = await ui.menu('SSH keys', ['GitHub', 'Paste public key', 'Skip keys'])
        try:
            if choice == 2:
                return []
            if choice == 1:
                pasted_key = await ui.ask('SSH keys', 'Public key', pasted_key, limit=16384)
                return [await ui.busy('SSH keys', 'Checking key', validate, pasted_key)]
            github_user = await ui.ask('GitHub', 'Username', github_user,
                                valid=lambda x: re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', x),
                                error='Invalid GitHub username; enter it again')
            keys = await ui.busy('SSH keys', 'Fetching public keys', fetch, github_user)
            if len(keys) == 1:
                return keys
            selected = await ui.menu('SSH keys', ['All keys'] + [key_label(key) for key in keys])
            return keys if selected == 0 else [keys[selected - 1]]
        except Back:
            continue
        except Failure as error:
            await ui.notice('SSH keys', str(error))


async def ssh_options(ui, choose_keys, previous):
    keys = previous.get('keys')
    disabled = previous.get('disable_ssh_password', True)
    while True:
        keep = keys is not None and await ui.menu('SSH keys', [f'Keep {len(keys)} keys', 'Change keys']) == 0
        if not keep:
            keys = await choose_keys(ui)
        try:
            disabled = await ui.ssh_access(len(keys), disable_password=disabled)
        except Back as back:
            disabled = back.values.get('disable_ssh_password', disabled)
            continue
        if not keys:
            await ui.notice('SSH access', 'Password-only SSH login will be enabled', warning=True)
        return dict(keys=keys, disable_ssh_password=disabled)


def identity_defaults():
    words = json.loads((SHARE / 'hostnames.json').read_text())
    proposal = secrets.choice(words['adjectives']) + '-' + secrets.choice(words['nouns'])
    zones = set(output(['timedatectl', 'list-timezones']).splitlines())
    return proposal, zones


async def identity(ui, previous=None, draft=None, defaults=identity_defaults):
    previous = previous or {}
    values = dict(previous, **(draft or {}))
    proposal, zones = await ui.busy('Identity', 'Loading defaults', defaults)
    result = await ui.form('Identity', [
        Field('hostname', 'Hostname', values.get('hostname', proposal), valid=valid_hostname,
              error='Use a letter first, then lowercase letters, digits or hyphens'),
        Field('timezone', 'Timezone', values.get('timezone', 'Europe/Berlin'), valid=lambda x: x in zones,
              error='Unknown timezone; use a name such as Europe/Berlin', suggestions=tuple(sorted(zones))),
    ], context='Set the server name and timezone.')
    return result


def hash_password(password):
    hashed = output(['openssl', 'passwd', '-6', '-stdin'], data=password.encode() + b'\n')
    if not re.fullmatch(r'\$6\$[./A-Za-z0-9$]+', hashed):
        raise Failure('Password hashing failed; restart the stick')
    return hashed


async def account(ui, previous=None, draft=None, hasher=hash_password):
    previous = previous or {}
    user = (draft or {}).get('username', previous.get('user', 'server'))
    proposal = secrets.token_urlsafe(15)
    values = await ui.form('Account', [
        Field('username', 'Username', user,
              valid=lambda x: bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', x)) and x not in ('root', 'nobody'),
              error='Use a lowercase login name other than root or nobody'),
        Field('password', 'Password', proposal, secret=True,
              valid=lambda x: len(x) >= 8 and '\x00' not in x and '\n' not in x,
              error='Password must contain at least eight characters'),
        Field('repeat', 'Repeat', proposal, secret=True),
    ], context='Keep the generated password or replace both password fields.',
       validate=lambda data: None if secrets.compare_digest(data['password'].encode(), data['repeat'].encode())
                             else 'Passwords differ; correct the password fields')
    hashed = await ui.busy('Account', 'Hashing password', hasher, values['password'])
    values.pop('password')
    values.pop('repeat')
    del proposal
    return values['username'], hashed


def secure_boot():
    result = output(['mokutil', '--sb-state'], check=False)
    if not Path('/sys/firmware/efi').exists():
        return 'legacy BIOS'
    return 'enabled' if 'SecureBoot enabled' in result else 'disabled' if 'SecureBoot disabled' in result else 'unknown'


def apply_battery():
    if not battery.batteries():
        log('battery: absent; skipped')
        return 'No battery; skipped', []
    detected = battery.laptop()
    log('battery: laptop majority ' + ('yes' if detected else 'no; battery still protected'))
    warnings = []
    if any(row.startswith('Warning:') for row in battery.status(battery.config())):
        log('battery: adapter online with discharge; inspect supply or firmware')
        warnings.append('Adapter undersized or firmware discharge; check supply')
    messages = battery.apply(battery.config(), 'normal')
    if any('unavailable' in message for message in messages):
        warnings.append('Limits unavailable; check firmware charge settings')
    for message in messages:
        log('battery: ' + message)
    return '; '.join(messages), warnings


async def battery_decision(ui):
    decision, warnings = await ui.busy('Battery', 'Applying battery policy', apply_battery)
    for warning in warnings:
        await ui.notice('Battery', warning, warning=True)
    return decision


async def update_policy(ui, previous=None):
    selected = (previous or DEFAULT_UPDATES).copy()
    while True:
        hours = await ui.ask('Automatic updates', 'Hours', str(selected['hours']),
                             context='Set the update interval in hours (0 disables automatic updates).',
                             valid=lambda value: bool(re.fullmatch(r'[0-9]{1,3}', value)) and int(value) <= 168,
                             error='Enter a whole number from 0 to 168', limit=3)
        selected['hours'] = int(hours)
        if not selected['hours']:
            return {'hours': 0, 'reboot': 'never'}
        modes = list(REBOOT_LABELS)
        try:
            index = await ui.menu('Automatic reboot', list(REBOOT_LABELS.values()),
                                  context='Choose when updates may reboot the server.',
                                  default=modes.index(selected['reboot']))
            return dict(hours=selected['hours'], reboot=modes[index])
        except Back:
            continue


def summary_rows(data):
    decision = data['decision']
    if len(decision) > 61:
        decision = 'Limits unavailable; see battery-limits status' if 'unavailable' in decision else 'Verified settings: see battery-limits status'
    disk = data['disk']
    size = f"  {disk['size'] / 1e9:.1f} GB" if 'size' in disk else ''
    model = ('  ' + disk['model']) if disk.get('model') else ''
    access = 'keys only' if data.get('disable_ssh_password', True) else 'password allowed'
    tunnel = vpn.VPN_NAMES[data['vpn']['kind']] + ' at boot' if data.get('vpn') else 'Off'
    updates = data.get('updates', DEFAULT_UPDATES)
    schedule = (f"Every {updates['hours']} h   Reboot: {REBOOT_LABELS[updates['reboot']].lower()}"
                if updates['hours'] else 'Manual   Reboot: never')
    region = f"{data['timezone']}   {data['layout']['label']}"
    return [f"! Erase   {disk['name']}{size}{model}",
            f"  Host    {data['hostname']}",
            f"  Login   {data['user']}   SSH {access} ({len(data['keys'])} keys)",
            f"  Region  {region}",
            f"  Link    {data['net']['path']}   Secure Boot: {data['sb']}",
            f"  VPN     {tunnel}",
            f"  Update  {schedule}",
            f"  Battery {decision}"]


async def summary(ui, data):
    decision = await ui.form('Review and install', [
        Field('value', 'Type device', valid=lambda value: value == data['disk']['name'],
              error='Type the exact path on the Erase line'),
    ], rows=summary_rows(data), editable=True, compact=True)
    return EDIT if decision == EDIT else decision['value']


def prepare_kickstart(data):
    disk, layout, net = data['disk'], data['layout'], data['net']
    fresh = storage.inventory()
    current = storage.validate_disk(disk['name'], fresh, storage.live_disks(fresh))
    if storage.fingerprint(disk) != storage.fingerprint(current):
        raise Failure('Disk changed; select and confirm it again')
    xlayout = layout['layout'] + (f" ({layout['variant']})" if layout['variant'] else '')
    user = data['user']
    values = dict(TZ=data['timezone'], HOSTNAME=data['hostname'], USER=user, USERHASH=data['hashed'],
                  DISKNAME=Path(disk['name']).name, LAYOUT=xlayout, VCKEYMAP=layout['vconsole'],
                  SSID_B64=b64(net['ssid']), PSK_B64=b64(net['psk']),
                  SSHKEYBLOCK='\n'.join(f'sshkey --username={user} "{key}"' for key in data['keys']),
                  SSH_PASSWORD='no' if data.get('disable_ssh_password', True) else 'yes',
                  NETWORK_B64=b64(net['profile']), PAYLOAD_B64=(SHARE / 'target-payload.b64').read_text().strip(),
                  BATTERY_B64=b64(data['decision']),
                  SERVER_POLICY_B64=b64(json.dumps(dict(vpn=data.get('vpn'),
                                                        updates=data.get('updates', DEFAULT_UPDATES)))))
    if output(['findmnt', '-n', '-o', 'FSTYPE', '--target', '/tmp']) != 'tmpfs':
        raise Failure('Temporary storage is not RAM-backed; use recovery')
    ks = Path('/tmp/kiosk.ks')
    private_write(ks, render((SHARE / 'ks.template').read_text(), values))
    run(['ksvalidator', '-v', 'F44', str(ks)], timeout=15)
    log('kickstart: validated; handoff')
    return ks


async def run_anaconda(progress):
    # Match Fedora's liveinst launcher. Shadow updates account files in the
    # target before Anaconda labels them. The target policy is set by Kickstart.
    enforcing = output(['/usr/sbin/getenforce'], timeout=5) == 'Enforcing'
    if enforcing:
        run(['/usr/sbin/setenforce', '0'], timeout=5)
    try:
        return await run_anaconda_process(progress)
    finally:
        if enforcing:
            run(['/usr/sbin/setenforce', '1'], timeout=5)


async def run_anaconda_process(progress):
    command = ['/usr/local/bin/kiosk-anaconda', '--cmdline', '--kickstart=/tmp/kiosk.ks', '--nosave=all']
    env = dict(os.environ, LANG='en_US.UTF-8', LC_ALL='en_US.UTF-8', PYTHONUNBUFFERED='1',
               TZ='UTC', ANACONDA_PRODUCTNAME='Fedora',
               ANACONDA_PRODUCTVERSION='44', ANACONDA_ISFINAL='true')
    try:
        proc = await asyncio.create_subprocess_exec(*command, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError:
        raise Failure('Anaconda could not start') from None
    loop = asyncio.get_running_loop()
    idle_seconds = INSTALL_IDLE_MINUTES * 60
    reader = None
    watch_activity = True
    try:
        async with asyncio.timeout(idle_seconds) as deadline:
            def advance_deadline():
                if watch_activity and not deadline.expired():
                    deadline.reschedule(loop.time() + idle_seconds)

            reader = asyncio.create_task(read_progress(proc.stdout, progress, advance_deadline))
            status = await proc.wait()
            await reader
            return status
    except TimeoutError:
        raise Failure(f'Anaconda reported no progress for {INSTALL_IDLE_MINUTES} minutes') from None
    finally:
        watch_activity = False
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        finally:
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)


class LiveServices:
    keyboard = staticmethod(keyboard.confirm)
    identity = staticmethod(identity)
    network = staticmethod(network.configure)
    disk = staticmethod(storage.choose_disk)
    account = staticmethod(account)
    keys = staticmethod(ssh_keys)
    vpn = staticmethod(vpn.configure)
    updates = staticmethod(update_policy)
    battery = staticmethod(battery_decision)

    @staticmethod
    def boot_state():
        state = secure_boot()
        log('Secure Boot: ' + state)
        return state

    @staticmethod
    def connected(net):
        return network.connected(net['device'])

    @staticmethod
    async def commit(ui, data):
        try:
            await ui.busy('Install Fedora', 'Validating installation', prepare_kickstart, data)
        except Failure as error:
            await ui.notice('Install Fedora', str(error))
            return False
        screen = BusyScreen('Install Fedora', 'Starting Anaconda')
        progress = InstallProgress(lambda message: setattr(screen, 'message', message))
        status, reason = None, None
        await ui.app.push_screen(screen)
        try:
            status = await run_anaconda(progress)
        except Failure as error:
            reason = str(error)
        finally:
            if not ui.app.cancelled:
                await ui.app.pop_screen()
        if status == 0 and progress.completed:
            log('anaconda: completed')
            return True
        reason = reason or progress.failure(status)
        log(f'anaconda: {reason} (status {status}, stage {progress.stage})')
        rows = [f'x {reason}', f'  Last step  {progress.stage}']
        if progress.missing:
            rows.append('  Check      ' + ', '.join(progress.missing))
        elif status is not None:
            rows.append(f'  Exit status  {status}')
        rows.extend(['/tmp/anaconda.log', '/tmp/ks.error', '/tmp/packaging.log', '/tmp/storage.log', str(LOG)])
        ui.screen('Installation failed', rows)
        await ui.read('Power off', 'Enter', back=False)
        return True


async def wizard(ui, services=None):
    services = services or LiveServices()
    data = {'layout': await services.keyboard(ui)}
    if data['layout'] is None:
        return keyboard.RELOAD_LAYOUT
    drafts = {}
    stage, editing = 0, False
    review = len(STEPS)
    while True:
        ui.step = f'{stage + 1}/{review + 1}'
        try:
            if stage == 0:
                data.update(await services.identity(ui, previous=data, draft=drafts.pop(stage, None)))
            elif stage == 1:
                keep = 'net' in data and await ui.menu('Network', [f"Keep {data['net']['path']}", 'Change connection']) == 0
                if not keep:
                    data['net'] = await services.network(ui)
            elif stage == 2:
                data['disk'] = await services.disk(ui, previous=data.get('disk'))
            elif stage == 3:
                keep = 'hashed' in data and await ui.menu('Account', [f"Keep {data['user']}", 'Change account']) == 0
                if not keep:
                    data['user'], data['hashed'] = await services.account(ui, previous=data, draft=drafts.pop(stage, None))
            elif stage == 4:
                data.update(await ssh_options(ui, services.keys, data))
            elif stage == 5:
                data['vpn'] = await services.vpn(ui, previous=data.get('vpn'))
            elif stage == 6:
                data['updates'] = await services.updates(ui, previous=data.get('updates'))
            else:
                if 'decision' not in data:
                    data['decision'] = await services.battery(ui)
                    data['sb'] = await ui.busy('Install Fedora', 'Checking Secure Boot', services.boot_state)
                choice = await summary(ui, data)
                if choice == EDIT:
                    try:
                        stage = await ui.menu('Change settings', STEPS)
                    except Back:
                        continue
                    editing = True
                    continue
                if not await ui.busy('Network', 'Checking Internet', services.connected, data['net']):
                    await ui.notice('Network', 'Connection lost; reconnect before installing')
                    data.pop('net', None)
                    stage, editing = 1, True
                    continue
                if await services.commit(ui, data):
                    return
                stage, editing = 2, True
                continue
            stage = review if editing else stage + 1
            editing = False
        except Back as back:
            drafts[stage] = back.values
            if editing:
                stage, editing = review, False
            elif stage:
                stage -= 1
            else:
                data['layout'] = await services.keyboard(ui)
                if data['layout'] is None:
                    return keyboard.RELOAD_LAYOUT


def main():
    install_signals()
    try:
        app = KioskApp(wizard, on_abort=poweroff)
        app.run(mouse=False)
        if app.failure:
            log('installer: aborted; inspect system state in recovery')
            return 1
        return app.return_code
    finally:
        Path('/tmp/kiosk.ks').unlink(missing_ok=True)
