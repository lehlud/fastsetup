import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
from kiosk import install, vpn
from kiosk.common import Failure
from kiosk.ui import KioskApp, MenuScreen
import test_kiosk
from test_ui import screen

WIREGUARD = f"""
[Interface]
Address = 192.0.2.2/24
PrivateKey = {base64.b64encode(b'x' * 32).decode()}
DNS = 192.0.2.53
[Peer]
PublicKey = {base64.b64encode(b'y' * 32).decode()}
AllowedIPs = 192.0.2.0/24
Endpoint = [2001:db8::2]:51820
""".encode()

OPENVPN = b"""
client
dev tun
remote vpn.example.test 1194
remote-cert-tls server
<ca>
-----BEGIN CERTIFICATE-----
test
-----END CERTIFICATE-----
</ca>
<auth-user-pass>
test-user
test-password
</auth-user-pass>
"""


class VPNTests(unittest.TestCase):
    def test_detects_both_profiles_and_preserves_connection_settings(self):
        wg = vpn.identify(WIREGUARD)
        self.assertEqual(wg['kind'], 'wireguard')
        self.assertIn('Endpoint = [2001:db8::2]:51820', wg['config'])
        self.assertIn('PersistentKeepalive = 25', wg['config'])
        ovpn = vpn.identify(OPENVPN)
        self.assertEqual(ovpn['kind'], 'openvpn')
        self.assertIn('remote "vpn.example.test" "1194"', ovpn['config'])
        self.assertIn('<auth-user-pass>\ntest-user\ntest-password\n</auth-user-pass>', ovpn['config'])

    def test_invalid_or_executable_profiles_are_rejected(self):
        unsafe = [b'<html>Not a config</html>', b'bytes\x00',
                  WIREGUARD.replace(b'[Peer]', b'PostUp = run-command\n[Peer]'),
                  WIREGUARD.replace(b'192.0.2.53', b'192.0.2.53; run-command'),
                  OPENVPN + b'plugin library.so\n', OPENVPN + b'auth-user-pass passwords.txt\n',
                  OPENVPN.replace(b'</ca>', b'</ca> extra\nup command\n</ca>'),
                  OPENVPN.replace(b'remote-cert-tls server', b'')]
        for profile in unsafe:
            with self.subTest(profile=profile[:40]), self.assertRaises(Failure):
                vpn.identify(profile)

    @patch('kiosk.vpn.run')
    def test_download_puts_token_url_on_stdin_and_limits_protocol(self, run):
        url = 'https://vpn.example.test/private?token=test'
        run.return_value = Mock(returncode=0, stdout=WIREGUARD)
        self.assertEqual(vpn.download(url), WIREGUARD)
        args, kwargs = run.call_args
        self.assertNotIn(url, args[0])
        self.assertIn(url.encode(), kwargs['data'])
        self.assertEqual(args[0][args[0].index('--proto-redir') + 1], '=https')
        with self.assertRaises(Failure):
            vpn.download('http://vpn.example.test')


class VPNInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_on_default_skips_vpn_without_fetch_or_editor(self):
        result = []
        fetch, edit = Mock(), AsyncMock()

        async def flow(ui):
            result.append(await vpn.configure(ui, fetch=fetch, edit=edit))

        async with KioskApp(flow).run_test(size=(80, 12)) as pilot:
            await screen(pilot, MenuScreen, 'Always-on VPN')
            await pilot.press('enter')
        self.assertEqual(result, [None])
        fetch.assert_not_called()
        edit.assert_not_awaited()


def render_policy(profile=None, updates=None):
    values = test_kiosk.RenderTests().values()
    values['PAYLOAD_B64'] = install.b64('{}')
    values['SERVER_POLICY_B64'] = install.b64(json.dumps(
        dict(vpn=profile, updates=updates or install.DEFAULT_UPDATES)))
    return install.render((ROOT / 'image/usr/share/kiosk/ks.template').read_text(), values)


class TargetPolicyTests(unittest.TestCase):
    def test_post_configures_target_services_and_private_profiles(self):
        cases = [
            (None, install.DEFAULT_UPDATES, None, None),
            (vpn.identify(WIREGUARD), {'hours': 4, 'reboot': 'when-needed'},
             'etc/wireguard/kiosk-vpn.conf', 'wg-quick@kiosk-vpn.service'),
            (vpn.identify(OPENVPN), {'hours': 0, 'reboot': 'never'},
             'etc/openvpn/client/kiosk-vpn.conf', 'openvpn-client@kiosk-vpn.service'),
        ]
        for profile, updates, relative, unit in cases:
            with self.subTest(unit=unit), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'etc/sudoers.d').mkdir(parents=True)
                (root / 'etc/libvirt').mkdir(parents=True)
                rendered = render_policy(profile, updates)
                script = rendered.split('%post --nochroot --interpreter=/usr/bin/python3 --erroronfail\n')[1].rsplit('%end', 1)[0]
                script = script.replace("root = Path('/mnt/sysroot')", f'root = Path({str(root)!r})')
                with patch('subprocess.run') as commands:
                    exec(compile(script, 'server-policy-post.py', 'exec'), {})
                args = [call.args[0] for call in commands.call_args_list]
                config = (root / 'etc/dnf/automatic.conf').read_text()
                timer = root / 'etc/systemd/system/dnf5-automatic.timer.d/10-server-schedule.conf'
                hours, reboot = updates['hours'], updates['reboot']
                self.assertIn(f'reboot = {reboot}\n', config)
                verb = 'enable' if hours else 'disable'
                self.assertIn(['chroot', str(root), 'systemctl', verb, 'dnf5-automatic.timer'], args)
                if hours:
                    self.assertIn('apply_updates = yes\n', config)
                    self.assertIn('OnCalendar=\n', timer.read_text())
                    self.assertIn(f'OnUnitInactiveSec={hours}h', timer.read_text())
                else:
                    self.assertIn('apply_updates = no\n', config)
                    self.assertFalse(timer.exists())
                if relative:
                    path = root / relative
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.read_text(), profile['config'])
                    self.assertTrue(any('enable' in call and unit in call for call in args))
                else:
                    self.assertFalse((root / 'etc/wireguard/kiosk-vpn.conf').exists())
                    self.assertFalse((root / 'etc/openvpn/client/kiosk-vpn.conf').exists())
                    self.assertFalse(any('wg-quick@kiosk-vpn.service' in call
                                         or 'openvpn-client@kiosk-vpn.service' in call for call in args))
