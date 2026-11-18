"""Target-side configuration commands operate on disposable files only."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
from kiosk import server_config as config, target_policy as policy
from kiosk.common import Failure
from test_server_policy import render_policy


class ServerConfigTests(unittest.TestCase):
    def test_install_assigns_fish_to_the_server_account(self):
        user = next(line for line in render_policy().splitlines() if line.startswith('user '))
        self.assertIn('--shell=/usr/bin/fish', user)
        self.assertIn('sudo server-config', (ROOT / 'image/etc/fish/conf.d/80-server-config.fish').read_text())

    def test_can_change_update_interval_or_disable_it_shared_with_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            online = Mock(returncode=0)
            with patch.object(config, 'ROOT', root), patch.object(config, 'systemctl') as systemctl, \
                 patch.object(config, 'run', return_value=online), patch('builtins.print'):
                config.updates(argparse.Namespace(hours=4, reboot='when-needed'))
                self.assertIn('reboot = when-needed', (root / policy.UPDATE_CONFIG).read_text())
                self.assertIn('OnUnitInactiveSec=4h', (root / policy.UPDATE_SCHEDULE).read_text())
                systemctl.assert_any_call('restart', policy.TIMER)
                config.updates(argparse.Namespace(hours=0, reboot=None))
                self.assertFalse((root / policy.UPDATE_SCHEDULE).exists())
                systemctl.assert_called_with('disable', '--now', policy.TIMER)

    def test_ssh_refuses_to_remove_last_key_in_key_only_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            account = Mock(pw_name='server', pw_dir=temporary)
            empty = root / 'empty.keys'
            empty.write_bytes(b'')
            policy.write_ssh(root, 'no')
            with patch.object(config, 'ROOT', root), patch.object(config, 'account', return_value=account), \
                 patch.object(config, 'authorized_keys', return_value=b'original\n') as keys, \
                 patch.object(config, 'systemctl') as systemctl:
                with self.assertRaisesRegex(Failure, 'Keep at least one'):
                    config.ssh(argparse.Namespace(keys=str(empty), edit_keys=False, password_login=None))
                keys.assert_called_once_with(account, 'read')
                systemctl.assert_not_called()

    def test_invalid_sshd_config_rolls_back_both_policy_and_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            account = Mock(pw_name='server', pw_dir=temporary)
            replacement = root / 'replacement.keys'
            replacement.write_bytes(b'new\n')
            policy.write_ssh(root, 'no')
            old_policy = (root / policy.SSH_CONFIG).read_text()
            def run(command, **kwargs):
                if command == ['/usr/sbin/sshd', '-t']:
                    raise Failure('Invalid configured SSH policy')
                return Mock(returncode=0)
            with patch.object(config, 'ROOT', root), patch.object(config, 'account', return_value=account), \
                 patch.object(config, 'authorized_keys', return_value=b'old\n') as keys, \
                 patch.object(config, 'run', side_effect=run), patch.object(config, 'systemctl'):
                with self.assertRaisesRegex(Failure, 'Invalid configured SSH policy'):
                    config.ssh(argparse.Namespace(keys=str(replacement), edit_keys=False, password_login='yes'))
                keys.assert_any_call(account, 'write', b'new\n')
                keys.assert_called_with(account, 'write', b'old\n')
            self.assertEqual((root / policy.SSH_CONFIG).read_text(), old_policy)

    def test_turning_off_vpn_disables_unit_and_removes_private_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = policy.write_vpn(root, {'kind': 'wireguard', 'config': 'private test data'})
            with patch.object(config, 'ROOT', root), patch.object(config, 'systemctl') as systemctl, \
                 patch('builtins.print'):
                config.tunnel(argparse.Namespace(off=True, url=False, file=None, edit=False))
            systemctl.assert_any_call('disable', '--now', unit)
            self.assertFalse((root / policy.VPN_TARGETS['wireguard'][0]).exists())
