"""Import declarative, unattended VPN client profiles."""
import base64
import binascii
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from urllib.parse import urlsplit

from .common import Failure, private_write, run

MAX_BYTES = 256 * 1024
VPN_NAMES = {'wireguard': 'WireGuard', 'openvpn': 'OpenVPN'}

WG_INTERFACE = {'address', 'privatekey', 'listenport', 'dns', 'mtu', 'table', 'fwmark'}
WG_PEER = {'publickey', 'presharedkey', 'allowedips', 'endpoint', 'persistentkeepalive'}
OPENVPN_INLINE = frozenset("""
    ca cert key extra-certs pkcs12 crl-verify tls-auth tls-crypt tls-crypt-v2
    auth-user-pass peer-fingerprint
""".split())
# These directives describe a TLS client connection. Scripts, plugins,
# external files, management sockets and nested configuration are excluded.
OPENVPN_OPTIONS = frozenset("""
    client tls-client dev proto remote remote-random remote-random-hostname
    resolv-retry connect-retry connect-retry-max connect-timeout nobind bind
    remote-cert-tls verify-x509-name peer-fingerprint remote-cert-eku remote-cert-ku
    tls-version-min tls-version-max tls-cipher tls-ciphersuites tls-groups
    key-direction auth auth-nocache auth-retry cipher data-ciphers data-ciphers-fallback
    persist-key persist-tun ping ping-restart ping-timer-rem keepalive
    redirect-gateway redirect-private route route-ipv6 route-metric route-nopull
    route-delay pull pull-filter dhcp-option tun-mtu mssfix sndbuf rcvbuf
    explicit-exit-notify reneg-sec verb mute fast-io float allow-compression
    compat-mode disable-dco
""".split())


def valid_url(value):
    if any(c.isspace() or not c.isprintable() or c in '"\\' for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == 'https' and bool(parts.hostname) and '@' not in parts.netloc
                and not parts.fragment and (parts.port is None or parts.port > 0))
    except ValueError:
        return False


def download(url):
    if not valid_url(url):
        raise Failure('Enter an HTTPS URL for the VPN profile')
    # A provider URL can itself be a credential. Pass it to curl over stdin.
    result = run(['curl', '-q', '--config', '-', '--fail', '--silent', '--globoff',
                  '--location', '--max-redirs', '4', '--proto', '=https',
                  '--proto-redir', '=https', '--connect-timeout', '5', '--max-time', '20',
                  '--max-filesize', str(MAX_BYTES)],
                 data=('url = ' + json.dumps(url, ensure_ascii=False) + '\n').encode(),
                 timeout=22, check=False)
    if result.returncode:
        raise Failure('VPN download failed; check the HTTPS URL')
    return result.stdout


def valid_key(value):
    try:
        key = base64.b64decode(value, validate=True)
        return len(key) == 32 and any(key) and base64.b64encode(key).decode() == value
    except binascii.Error:
        return False


def hostname(value):
    return bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', value))


def endpoint(value):
    host, colon, port = value.rpartition(':')
    if not colon or not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535:
        return False
    if host.startswith('[') and host.endswith(']'):
        try:
            return ipaddress.ip_address(host[1:-1]).version == 6 and '%' not in host
        except ValueError:
            return False
    return hostname(host)


def valid_wg_value(key, value):
    if key in ('privatekey', 'publickey', 'presharedkey'):
        return valid_key(value)
    if key == 'endpoint':
        return endpoint(value)
    if key == 'table':
        return value in ('auto', 'off') or bool(re.fullmatch(r'[0-9]{1,10}', value))
    if key == 'fwmark':
        return bool(re.fullmatch(r'(?:[0-9]{1,10}|0x[0-9a-fA-F]{1,8})', value))
    if key in ('listenport', 'mtu', 'persistentkeepalive'):
        return (key == 'persistentkeepalive' and value == 'off') or (
            bool(re.fullmatch(r'[0-9]{1,5}', value)) and int(value) <= 65535
            and (key != 'mtu' or int(value) >= 68))
    items = [item.strip() for item in value.split(',')]
    try:
        for item in items:
            if '%' in item:
                return False
            if key == 'address':
                ipaddress.ip_interface(item)
            elif key == 'allowedips':
                ipaddress.ip_network(item, strict=False)
            elif not hostname(item):
                ipaddress.ip_address(item)
        return True
    except ValueError:
        return False


def wireguard(lines):
    sections = []
    for row in lines:
        row = row.partition('#')[0].strip()
        if not row or row.startswith(';'):
            continue
        if row in ('[Interface]', '[Peer]'):
            sections.append((row, {}))
            continue
        key, equals, value = row.partition('=')
        key, value = key.strip(), value.strip()
        allowed = WG_INTERFACE if sections and sections[-1][0] == '[Interface]' else WG_PEER
        if not sections or not equals or key.lower() not in allowed:
            raise Failure('Use a WireGuard profile without commands or extra options')
        options = sections[-1][1]
        if key.lower() in options or not valid_wg_value(key.lower(), value):
            raise Failure('Invalid or repeated WireGuard setting; check the profile')
        options[key.lower()] = (key, value)
    if (len(sections) < 2 or sections[0][0] != '[Interface]'
            or not {'privatekey', 'address'} <= sections[0][1].keys()
            or any(name != '[Peer]' or not {'publickey', 'allowedips'} <= body.keys()
                   for name, body in sections[1:])
            or not any('endpoint' in body for _, body in sections[1:])):
        raise Failure('WireGuard needs an address, private key and peer endpoint')
    for _, body in sections[1:]:
        if 'endpoint' in body:
            body.setdefault('persistentkeepalive', ('PersistentKeepalive', '25'))
    return '\n\n'.join('\n'.join([name] + [f'{key} = {value}' for key, value in body.values()])
                       for name, body in sections) + '\n'


def openvpn(lines):
    profile, inline, directives = [], set(), {}
    tag = None
    for row in lines:
        stripped = row.strip()
        if tag:
            if stripped == '</' + tag + '>':
                tag = None
            elif stripped.startswith('</'):
                raise Failure('Use a standalone OpenVPN closing tag')
            elif ((tag == 'key' and stripped.startswith('-----BEGIN ENCRYPTED PRIVATE KEY-----'))
                  or stripped.replace(' ', '').startswith('Proc-Type:4,ENCRYPTED')):
                raise Failure('OpenVPN private keys must work without a passphrase')
            profile.append(row)
            continue
        if not stripped or stripped.startswith(('#', ';')):
            continue
        if stripped.startswith('<'):
            name = stripped[1:-1]
            if stripped != f'<{name}>' or name not in OPENVPN_INLINE or name in inline:
                raise Failure('Use an OpenVPN client profile with inline credentials')
            tag = name
            inline.add(name)
            profile.append(stripped)
            continue
        try:
            parts = shlex.split(row, comments=True)
        except ValueError:
            raise Failure('Invalid OpenVPN syntax; check quoting') from None
        option, *args = parts
        if option not in OPENVPN_OPTIONS:
            raise Failure('Use OpenVPN without scripts, external files or unsupported options')
        directives.setdefault(option, []).append(args)
        # Write tokens back in OpenVPN syntax, without shell evaluation.
        profile.append(' '.join([option] + [json.dumps(arg, ensure_ascii=False) for arg in args]))
    if tag:
        raise Failure('Close the inline OpenVPN credential block')
    if (not {'client', 'tls-client'} & directives.keys()
            or not directives.get('remote') or any(not args for args in directives['remote'])
            or directives.get('dev') != [['tun']]):
        raise Failure('OpenVPN needs a TLS client, dev tun and a remote server')
    pinned = 'peer-fingerprint' in inline or any(directives.get('peer-fingerprint', ()))
    if 'ca' not in inline and 'pkcs12' not in inline and not pinned:
        raise Failure('Include the OpenVPN CA certificate or peer fingerprint')
    if not (pinned or any(directives.get('verify-x509-name', ()))
            or directives.get('remote-cert-tls') == [['server']]):
        raise Failure('Include OpenVPN server certificate verification')
    # Ensure detection of a dead link even when the server does not push keepalive.
    return 'script-security 1\nping 15\nping-restart 60\n' + '\n'.join(profile) + '\n'


def identify(data):
    if not data or len(data) > MAX_BYTES:
        raise Failure('Use a VPN profile between 1 byte and 256 KiB')
    try:
        content = data.decode('utf-8-sig')
    except UnicodeError:
        raise Failure('Use a plain-text UTF-8 VPN profile') from None
    if any(not c.isprintable() and c not in '\r\n\t' for c in content):
        raise Failure('Use a plain-text VPN profile without control characters')
    lines = content.splitlines()
    active = [row.strip() for row in lines if row.strip() and not row.lstrip().startswith(('#', ';'))]
    if active and active[0].partition('#')[0].strip() == '[Interface]':
        return dict(kind='wireguard', config=wireguard(lines))
    if any(re.match(r'(?:client|tls-client|remote)(?:\s|$)', row) for row in active):
        return dict(kind='openvpn', config=openvpn(lines))
    raise Failure('Unknown VPN format; use a WireGuard or OpenVPN client profile')


EDITORS = [
    ('Neovim', ['nvim', '-u', 'NONE', '-i', 'NONE', '-n',
                '--cmd', 'noremap <C-c> <Cmd>cquit 130<CR>',
                '--cmd', 'inoremap <C-c> <Cmd>cquit 130<CR>',
                '--cmd', 'cnoremap <C-c> <C-u>cquit 130<CR>']),
    ('Helix', ['hx']),
]


async def edit_profile(ui, editor):
    name, command = EDITORS[editor]
    live = ui.app.on_abort is not None
    with tempfile.TemporaryDirectory(prefix='kiosk-vpn-', dir='/run' if live else None) as directory:
        path = Path(directory) / 'vpn.conf'
        helix = Path(directory) / 'helix.toml'
        hint = 'Esc then :wq' if name == 'Neovim' else 'Esc then :write-quit'
        private_write(path, f'# Enter the VPN configuration below. Save and quit with {hint}.\n')
        if name == 'Helix':
            private_write(helix, '\n'.join(
                f'[keys.{mode}]\nC-c = ":cquit! 130"\n'
                for mode in ('normal', 'insert', 'select')))
            command = command + ['-c', str(helix)]
        identity = {}
        if live and os.geteuid() == 0:
            import pwd
            account = pwd.getpwnam('nobody')
            os.chown(directory, account.pw_uid, account.pw_gid)
            os.chown(path, account.pw_uid, account.pw_gid)
            if helix.exists():
                os.chown(helix, account.pw_uid, account.pw_gid)
            identity.update(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        env = {key: os.environ[key] for key in ('PATH', 'TERM', 'COLORTERM', 'LANG') if key in os.environ}
        env.update(HOME=directory, XDG_CONFIG_HOME=directory, XDG_DATA_HOME=directory,
                   XDG_STATE_HOME=directory, XDG_CACHE_HOME=directory)
        try:
            result = ui.run_editor(command + [str(path)], cwd=directory, env=env, **identity)
            if result.returncode:
                raise Failure(f'{name} exited without a profile; retry')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                return stream.read(MAX_BYTES + 1)
        except OSError:
            raise Failure(f'{name} or the saved profile is unavailable; retry') from None


async def configure(ui, previous=None, *, fetch=download, edit=edit_profile):
    from .ui import Back

    while True:
        options = ['No VPN', 'Fetch from HTTPS URL', 'Enter in Neovim', 'Enter in Helix']
        if previous:
            options.insert(0, f'Keep {VPN_NAMES[previous["kind"]]}')
        choice = await ui.menu('Always-on VPN', options, context='Select a VPN profile for server startup.')
        if previous and choice == 0:
            return previous
        choice -= bool(previous)
        if choice == 0:
            return None
        try:
            if choice == 1:
                url = await ui.ask('VPN profile', 'HTTPS URL', secret=True, limit=4096,
                                   valid=valid_url, error='Enter an HTTPS URL')
                raw = await ui.busy('VPN profile', 'Downloading profile', fetch, url)
            else:
                raw = await edit(ui, choice - 2)
            profile = identify(raw)
            chosen = await ui.menu('VPN profile', [f'Use {VPN_NAMES[profile["kind"]]}', 'Choose another profile'])
            if chosen == 0:
                return profile
        except Back:
            continue
        except Failure as error:
            await ui.notice('VPN profile', str(error))
