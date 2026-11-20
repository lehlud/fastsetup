"""SSH selection, lockout prevention and the emitted target authentication policy."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from textual.widgets import Checkbox, Static

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
from kiosk import common, install
from kiosk.ui import Back, KioskApp, SSHAccessScreen
from test_ui import screen


def materialize_policy(root, password_login):
    values = {key: '' for key in install.TOKENS}
    values.update(TZ='UTC', HOSTNAME='test-server', USER='server', USERHASH='$6$test$hash',
                  DISKNAME='sda', LAYOUT='us', VCKEYMAP='us', SSH_PASSWORD=password_login,
                  PAYLOAD_B64=install.b64('{}'))
    if password_login == 'no':
        values['SSHKEYBLOCK'] = 'sshkey --username=server "ssh-ed25519 TEST-PUBLIC-KEY"'
    text = install.render((ROOT / 'image/usr/share/kiosk/ks.template').read_text(), values)
    script = text.split('%post --nochroot --interpreter=/usr/bin/python3 --erroronfail\n')[1].rsplit('%end', 1)[0]
    script = script.replace("root = Path('/mnt/sysroot')", 'root = Path(' + repr(str(root)) + ')')
    (root / 'etc/sudoers.d').mkdir(parents=True)
    (root / 'etc/libvirt').mkdir(parents=True)
    with patch('subprocess.run'), patch('kiosk.target_policy.configure_swap'):
        exec(compile(script, 'ssh-target-post.py', 'exec'), {})
    return root / 'etc/ssh/sshd_config.d/00-kiosk-auth.conf'


class SSHPolicyTests(unittest.TestCase):
    def test_key_labels_show_both_ends_and_distinguish_shared_prefixes(self):
        keys = ['ssh-ed25519 ' + 'A' * 100 + suffix for suffix in ('FIRST-KEY-END', 'SECOND-KEY-END')]
        labels = [install.key_label(key) for key in keys]
        for key, label in zip(keys, labels):
            self.assertEqual(label, key[:37] + '...' + key[-24:])
            self.assertLessEqual(len(label) + 3, 72)
        self.assertNotEqual(*labels)
        self.assertEqual(install.key_label('short key'), 'short key')

    def test_target_policy_controls_both_password_paths_and_keeps_public_keys(self):
        for mode in ('no', 'yes'):
            with self.subTest(password_login=mode), tempfile.TemporaryDirectory() as temporary:
                config = materialize_policy(Path(temporary), mode)
                text = config.read_text()
                self.assertIn(f'PasswordAuthentication {mode}\n', text)
                self.assertIn('KbdInteractiveAuthentication no\n', text)
                self.assertIn('PubkeyAuthentication yes\n', text)
                self.assertIn('AuthenticationMethods ' + ('publickey' if mode == 'no' else 'any') + '\n', text)
                self.assertEqual(config.stat().st_mode & 0o777, 0o644)

    def test_renderer_refuses_keyless_key_only_configuration_and_invalid_mode(self):
        values = {key: '' for key in install.TOKENS}
        template = (ROOT / 'image/usr/share/kiosk/ks.template').read_text()
        values['SSH_PASSWORD'] = 'no'
        with self.assertRaisesRegex(common.Failure, 'Add an SSH key'):
            install.render(template, values)
        values['SSH_PASSWORD'] = 'maybe'
        with self.assertRaisesRegex(common.Failure, 'Invalid SSH login policy'):
            install.render(template, values)


class SSHInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_selection_displays_abbreviations_but_keeps_the_full_key(self):
        keys = ['ssh-ed25519 ' + 'A' * 100 + 'FIRST-END', 'ssh-ed25519 ' + 'A' * 100 + 'SECOND-END']
        ui = Mock()
        ui.menu = AsyncMock(side_effect=[0, 2])
        ui.ask = AsyncMock(return_value='github-user')
        ui.busy = AsyncMock(return_value=keys)
        result = await install.ssh_keys(ui)
        self.assertEqual(result, [keys[1]])
        self.assertEqual(ui.menu.call_args.args[1], ['All keys'] + [install.key_label(key) for key in keys])

    async def test_checkbox_defaults_to_disabled_password_login(self):
        result = []
        async def flow(ui):
            result.append(await ui.ssh_access(1))
        app = KioskApp(flow)
        async with app.run_test(size=(80, 12)) as pilot:
            await screen(pilot, SSHAccessScreen)
            self.assertTrue(app.screen.query_one(Checkbox).value)
            self.assertTrue(str(app.screen.query_one(Checkbox).render()).startswith('[X]'))
            self.assertLessEqual(app.screen.query_one('.canvas').size.height, 12)
            await pilot.press('enter')
        self.assertEqual(result, [True])
        self.assertIsNone(app.failure)

    async def test_keyless_default_is_blocked_until_password_login_is_explicitly_allowed(self):
        result = []
        async def flow(ui):
            result.append(await ui.ssh_access(0))
        app = KioskApp(flow)
        async with app.run_test(size=(80, 12)) as pilot:
            await screen(pilot, SSHAccessScreen)
            await pilot.press('enter')
            self.assertEqual(result, [])
            self.assertIn('Add an SSH key', str(app.screen.query_one('#error', Static).render()))
            await pilot.press('space')
            self.assertTrue(str(app.screen.query_one(Checkbox).render()).startswith('[ ]'))
            await pilot.press('enter')
        self.assertEqual(result, [False])
        self.assertIsNone(app.failure)

    async def test_back_preserves_the_checkbox_choice_and_selected_keys(self):
        ui = Mock()
        ui.menu = AsyncMock(return_value=0)
        ui.ssh_access = AsyncMock(side_effect=[Back({'disable_ssh_password': False}), False])
        choose_keys = AsyncMock(return_value=['full-public-key'])
        result = await install.ssh_options(ui, choose_keys, {})
        self.assertEqual(result, dict(keys=['full-public-key'], disable_ssh_password=False))
        self.assertTrue(ui.ssh_access.call_args_list[0].kwargs['disable_password'])
        self.assertFalse(ui.ssh_access.call_args_list[1].kwargs['disable_password'])
        choose_keys.assert_awaited_once()

    async def test_existing_password_choice_is_not_reset_when_editing_settings(self):
        ui = Mock()
        ui.menu = AsyncMock(return_value=0)
        ui.ssh_access = AsyncMock(return_value=False)
        choose_keys = AsyncMock()
        previous = dict(keys=['full-public-key'], disable_ssh_password=False)
        self.assertEqual(await install.ssh_options(ui, choose_keys, previous), previous)
        ui.ssh_access.assert_awaited_once_with(1, disable_password=False)
        choose_keys.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
