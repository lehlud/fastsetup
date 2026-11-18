from pathlib import Path
import time
import uuid

from .common import Failure, log, output, private_write, run
from .ui import Back


def fields(line):
    result, value, escaped = [], '', False
    for char in line:
        if escaped:
            value += char
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            result.append(value)
            value = ''
        else:
            value += char
    return result + [value]


def ini(value):
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t').replace(' ', '\\s')


def profile(device, ssid='', psk='', security='wpa-psk', hidden=False, ident=None):
    ident = ident or str(uuid.uuid4())
    kind = 'wifi' if ssid else 'ethernet'
    text = (f'[connection]\nid=kiosk\nuuid={ident}\ntype={kind}\ninterface-name={ini(device)}\n'
            'autoconnect=true\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n')
    if ssid:
        # An integer list preserves arbitrary SSID octets and punctuation.
        encoded = ''.join(f'{byte};' for byte in ssid.encode())
        text += f'\n[wifi]\nmode=infrastructure\nssid={encoded}\nhidden={str(hidden).lower()}\n'
        if security != 'open':
            text += f'\n[wifi-security]\nkey-mgmt={security}\nproto=rsn;\npsk={ini(psk)}\npsk-flags=0\n'
            if security == 'sae':
                text += 'pmf=3\n'
    return ident, text


def connected(device=None):
    bind = ['-I', device] if device else []
    ping = run(['ping', '-n', '-c', '1', '-W', '3'] + bind + ['1.1.1.1'], timeout=5, check=False)
    dns = run(['getent', 'ahostsv4', 'mirrors.fedoraproject.org'], timeout=8, check=False)
    return ping.returncode == 0 and dns.returncode == 0


def activate(device, ssid='', psk='', security='wpa-psk', hidden=False):
    ident, text = profile(device, ssid, psk, security, hidden)
    # /run is tmpfs; NetworkManager requires keyfiles under its configured roots.
    directory = Path('/run/NetworkManager/system-connections')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f'kiosk-{ident}.nmconnection'
    private_write(path, text)
    activated = False
    try:
        run(['nmcli', '--wait', '10', 'connection', 'load', str(path)])
        result = run(['nmcli', '--wait', '30', 'connection', 'up', 'uuid', ident], timeout=35, check=False)
        if result.returncode:
            raise Failure('Connection failed; check password or choose another network' if ssid else
                          'Ethernet connection failed; check cable and DHCP')
        if not connected(device):
            raise Failure('Internet or DNS unavailable; retry or switch connection')
        activated = True
    finally:
        path.unlink(missing_ok=True)
        if not activated:
            # Failed profiles must not keep autoconnecting during the next try.
            try:
                run(['nmcli', '--wait', '5', 'connection', 'delete', 'uuid', ident], timeout=6, check=False)
            except Failure:
                pass
        # Do not reload: the active in-memory profile must survive until handoff.
    return dict(device=device, ssid=ssid, psk=psk, security=security, hidden=hidden,
                profile=text, path='Wi-Fi' if ssid else 'Ethernet')


def wifi_hard_blocked(root=Path('/sys/class/rfkill')):
    for radio in root.glob('rfkill*'):
        try:
            if (radio / 'type').read_text().strip() == 'wlan' and (radio / 'hard').read_text().strip() == '1':
                return True
        except OSError:
            continue  # A hot-unplugged radio must not abort discovery.
    return False


def devices():
    # Enable software-disabled Wi-Fi before discovery, not after choosing it.
    run(['udevadm', 'settle', '--timeout=5'], timeout=6, check=False)
    run(['nmcli', '--wait', '5', 'radio', 'wifi', 'on'], timeout=6, check=False)
    for attempt in range(6):
        rows = [fields(x) for x in output(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device']).splitlines()]
        wifi = [x[0] for x in rows if len(x) >= 2 and x[1] == 'wifi']
        wired = [x[0] for x in rows if len(x) >= 2 and x[1] == 'ethernet']
        if wifi or wifi_hard_blocked() or attempt == 5:
            return wifi, wired
        # udev settling alone does not wait for NetworkManager's device objects.
        time.sleep(1)


def ready_wifi(device):
    for attempt in range(6):
        state = output(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', device]).split(' ', 1)[0]
        if state == '10':
            raise Failure('Wi-Fi unmanaged; retry detection or use Ethernet')
        if state != '20':
            return
        if wifi_hard_blocked():
            log('network: Wi-Fi hardware blocked')
            raise Failure('Wi-Fi hardware blocked; enable the radio switch and retry')
        if attempt < 5:
            time.sleep(1)
    log('network: Wi-Fi device unavailable')
    raise Failure('Wi-Fi unavailable; retry or check driver and firmware')


def scan(device):
    ready_wifi(device)
    raw = output(['nmcli', '-t', '--escape', 'yes', '-f', 'SSID,SIGNAL,SECURITY',
                  'device', 'wifi', 'list', 'ifname', device, '--rescan', 'yes'])
    networks = {}
    for text in raw.splitlines():
        row = fields(text)
        if len(row) != 3 or not row[0] or not row[1].isdigit():
            continue
        ssid, signal, security = row
        if ssid not in networks or int(signal) > int(networks[ssid][1]):
            networks[ssid] = row
    return sorted(networks.values(), key=lambda row: -int(row[1]))


class NetworkBackend:
    devices = staticmethod(devices)
    scan = staticmethod(scan)
    activate = staticmethod(activate)
    hard_blocked = staticmethod(wifi_hard_blocked)

    @staticmethod
    def carrier(device):
        return Path('/sys/class/net', device, 'carrier').read_text().strip() == '1'


async def wifi_connection(ui, backend, device):
    while True:
        networks = await ui.busy('Wi-Fi', 'Scanning networks', backend.scan, device)
        idx = await ui.menu('Wi-Fi', [f'{ssid}  {signal}%  {sec if sec and sec != "--" else "open"}'
                                     for ssid, signal, sec in networks] + ['Hidden network', 'Scan again'])
        if idx == len(networks) + 1:
            continue
        try:
            hidden = idx == len(networks)
            if hidden:
                ssid = await ui.ask('Hidden network', 'SSID', '', valid=lambda x: 1 <= len(x.encode()) <= 32,
                                    error='SSID must contain 1 to 32 bytes')
                security = ['wpa-psk', 'sae', 'open'][await ui.menu('Wi-Fi security', ['WPA2', 'WPA3 SAE', 'Open'])]
            else:
                ssid, _, advertised = networks[idx]
                if '802.1X' in advertised or 'EAP' in advertised:
                    raise Failure('Enterprise Wi-Fi unsupported; choose another connection')
                security = 'sae' if 'WPA3' in advertised else 'wpa-psk' if advertised and advertised != '--' else 'open'
            psk = '' if security == 'open' else await ui.ask(
                'Wi-Fi password', 'Passphrase', '', secret=True, context=ssid,
                valid=lambda x: (1 <= len(x) <= 63 if security == 'sae' else
                                 8 <= len(x) <= 63 or len(x) == 64 and all(c in '0123456789abcdefABCDEF' for c in x)),
                error='Use 8 to 63 characters or a 64-digit hex key' if security != 'sae' else 'Use 1 to 63 characters')
            return await ui.busy('Network', 'Connecting and checking Internet', backend.activate,
                                 device, ssid, psk, security, hidden)
        except Back:
            continue
        except Failure as error:
            await ui.notice('Wi-Fi', str(error))


async def configure(ui, backend=None):
    backend = backend or NetworkBackend()
    while True:
        try:
            wifi, wired = await ui.busy('Network', 'Detecting adapters', backend.devices)
            context = None
            if backend.hard_blocked():
                context = 'Wi-Fi hardware blocked; enable its radio switch and retry'
            elif not wifi:
                log('network: no Wi-Fi device detected')
                context = 'Wi-Fi not detected; retry or check driver and firmware'
            paths = (['Wi-Fi'] if wifi else []) + (['Ethernet'] if wired else []) + ['Retry detection']
            mode = paths[await ui.menu('Network', paths, context=context)]
            if mode == 'Retry detection':
                continue
            if mode == 'Ethernet':
                try:
                    device = wired[await ui.menu('Ethernet', wired)] if len(wired) > 1 else wired[0]
                    if not backend.carrier(device):
                        raise Failure('No Ethernet link; connect a cable or choose Wi-Fi')
                    return await ui.busy('Network', 'Connecting and checking Internet', backend.activate, device)
                except Back:
                    continue
            try:
                device = wifi[await ui.menu('Wi-Fi device', wifi)] if len(wifi) > 1 else wifi[0]
                return await wifi_connection(ui, backend, device)
            except Back:
                continue
        except (Failure, OSError) as error:
            await ui.notice('Network', str(error) if isinstance(error, Failure) else 'Network device unavailable; retry')
