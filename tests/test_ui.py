"""Drive the real Textual screens, including the complete inert wizard."""
import asyncio
from html import unescape
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from textual import events
from textual.widgets import Checkbox, Input, OptionList, Static

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
from kiosk import common, install, preview
from kiosk.ui import Back, BusyScreen, Field, FormScreen, KioskApp, MenuScreen, NoticeScreen, SSHAccessScreen


async def screen(pilot, kind, title=None):
    for _ in range(150):
        current = pilot.app.screen
        if isinstance(current, kind) and (title is None or current.title_text == title):
            await pilot.pause()
            return current
        await pilot.pause(0.01)
    raise AssertionError(f'Expected {kind.__name__} {title}; got {type(pilot.app.screen).__name__}')


async def paste(pilot, value, key=None):
    field = pilot.app.screen.query_one('#' + key, Input) if key else pilot.app.screen.query_one(Input)
    field.focus()
    field.value = ''
    field.post_message(events.Paste(value))
    await pilot.pause()


class TextualTests(unittest.IsolatedAsyncioTestCase):
    def test_summary_keeps_long_values_visible_and_does_not_invent_battery_thresholds(self):
        data = dict(hostname='a' * 41, timezone='America/Argentina/ComodRivadavia', user='u' * 31,
                    layout=dict(label='German (no dead keys)'), net=dict(path='Ethernet'),
                    keys=['key'] * 100, disk=dict(name='/dev/nvme0n1'), sb='enabled',
                    decision='BAT0: behaviour inhibit-charge verified (60s polling); BAT1: behaviour auto verified (60s polling)')
        rows = install.summary_rows(data)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(len(row) <= 72 for row in rows))
        self.assertIn(data['hostname'], '\n'.join(rows))
        self.assertIn(data['user'], '\n'.join(rows))
        self.assertEqual('\n'.join(rows).count('/dev/nvme0n1'), 1)
        self.assertNotIn('45-55%', '\n'.join(rows))

    async def test_arrow_keys_edit_in_place_and_unicode_paste_stays_in_one_row(self):
        result = []
        async def flow(ui):
            result.append(await ui.ask('Hostname', 'Hostname', 'hello'))
        app = KioskApp(flow)
        async with app.run_test(size=(100, 30)) as pilot:
            await screen(pilot, FormScreen)
            await pilot.press('end', 'left', 'backspace', 'X', 'enter')
        self.assertEqual(result, ['helXo'])
        self.assertIsNone(app.failure)

    async def test_paste_and_validation_keep_the_input_until_corrected(self):
        result = []
        async def flow(ui):
            result.append(await ui.ask('Name', 'Name', '', valid=lambda value: value == 'café界', error='Enter the expected name'))
        app = KioskApp(flow)
        async with app.run_test(size=(80, 24)) as pilot:
            await screen(pilot, FormScreen)
            await paste(pilot, 'wrong')
            await pilot.press('enter')
            self.assertEqual(app.screen.query_one(Input).value, 'wrong')
            self.assertIn('expected name', str(app.screen.query_one('#error', Static).render()))
            await paste(pilot, 'café界')
            await pilot.press('enter')
        self.assertEqual(result, ['café界'])
        self.assertEqual(common.safe('界\u0301'), '界\u0301')
        self.assertNotIn('\x1b', common.safe('evil\x1b[2J\n'))

    async def test_menu_arrows_cross_pages_and_resize_preserves_selection(self):
        result = []
        async def flow(ui):
            result.append(await ui.menu('Keyboard', [f'Layout {i}' for i in range(36)], context='Select a layout.'))
        app = KioskApp(flow)
        async with app.run_test(size=(120, 40)) as pilot:
            await screen(pilot, MenuScreen)
            await pilot.press('up')
            self.assertEqual(app.screen.query_one(OptionList).highlighted, 0)
            await pilot.press(*(['down'] * 8))
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertLessEqual(app.screen.query_one('.canvas').size.height, 12)
            self.assertEqual(app.screen.query_one('.canvas').size.width, 72)
            await pilot.press('enter')
        self.assertEqual(result, [8])

    async def test_back_clears_visible_secrets_and_returns_only_public_draft(self):
        result, held = [], []
        async def flow(ui):
            try:
                await ui.form('Account', [Field('username', 'Username', 'server'),
                                          Field('password', 'Password', 'visible-test-secret', secret=True)])
            except Back as back:
                result.append(back.values)
            await ui.menu('Next', ['Continue'], back=False)
        app = KioskApp(flow)
        async with app.run_test(size=(80, 24)) as pilot:
            current = await screen(pilot, FormScreen)
            field = current.query_one('#password', Input)
            held.append(field)
            await pilot.press('escape')
            await screen(pilot, MenuScreen)
            self.assertEqual(field.value, '')
            self.assertNotIn('visible-test-secret', app.export_screenshot())
            await pilot.press('enter')
        self.assertEqual(result, [{'username': 'server'}])

    async def test_long_pasted_key_does_not_expand_the_canvas(self):
        result = []
        async def flow(ui):
            result.append(await ui.ask('SSH keys', 'Public key', '', limit=16384))
        app = KioskApp(flow)
        key = 'ssh-ed25519 ' + 'A' * 5000
        async with app.run_test(size=(80, 24)) as pilot:
            await screen(pilot, FormScreen)
            await paste(pilot, key)
            self.assertEqual(app.screen.query_one(Input).size.height, 1)
            self.assertLessEqual(app.screen.query_one('.canvas').size.height, 12)
            await pilot.press('enter')
        self.assertEqual(result, [key])

    async def test_ctrl_c_exits_while_blocking_work_is_running(self):
        release = threading.Event()
        shutdown = []
        async def flow(ui):
            await ui.busy('Network', 'Connecting', release.wait, 3)
        app = KioskApp(flow, on_abort=lambda: shutdown.append(not release.is_set()))
        try:
            async with app.run_test() as pilot:
                await screen(pilot, BusyScreen)
                await pilot.press('ctrl+c')
                self.assertTrue(app.cancelled)
                self.assertEqual(shutdown, [True])
        finally:
            release.set()

    async def test_full_preview_has_back_edit_and_one_exact_disk_confirmation(self):
        services = preview.DemoServices()
        app = KioskApp(lambda ui: install.wizard(ui, services))
        with patch.object(preview, 'SHARE', ROOT / 'image/usr/share/kiosk'):
            async with app.run_test(size=(100, 30)) as pilot:
                await screen(pilot, MenuScreen, 'Keyboard')
                await pilot.press('enter')
                await screen(pilot, FormScreen, 'Identity')
                await pilot.press('enter', 'enter')
                await screen(pilot, MenuScreen, 'Network')
                await pilot.press('escape')
                await screen(pilot, FormScreen, 'Identity')
                self.assertEqual(app.screen.query_one('#hostname', Input).value, 'calm-fox')
                await pilot.press('enter', 'enter')
                await screen(pilot, MenuScreen, 'Network')
                await pilot.press('enter')
                await screen(pilot, MenuScreen, 'Wi-Fi')
                await pilot.press('enter')
                await screen(pilot, FormScreen, 'Wi-Fi password')
                await paste(pilot, 'preview-wifi-pass')
                await pilot.press('enter')
                await screen(pilot, MenuScreen, 'Disk')
                # The live USB is listed but the eligible SSD is selected by default.
                self.assertEqual(app.screen.query_one(OptionList).highlighted, 1)
                await pilot.press('enter')
                await screen(pilot, FormScreen, 'Account')
                await paste(pilot, 'new-password', 'password')
                await pilot.press('enter')
                await paste(pilot, 'different-password', 'repeat')
                await pilot.press('enter')
                self.assertIn('Passwords differ', str(app.screen.query_one('#error', Static).render()))
                await paste(pilot, 'new-password', 'repeat')
                await pilot.press('enter')
                await screen(pilot, MenuScreen, 'SSH keys')
                await pilot.press('down', 'down', 'enter')
                await screen(pilot, SSHAccessScreen, 'SSH access')
                self.assertTrue(app.screen.query_one(Checkbox).value)
                await pilot.press('enter')
                self.assertIn('Add an SSH key', str(app.screen.query_one('#error', Static).render()))
                self.assertFalse(services.installed)
                await pilot.press('space', 'enter')
                await screen(pilot, NoticeScreen, 'SSH access')
                await pilot.press('enter')
                await screen(pilot, MenuScreen, 'Always-on VPN')
                await pilot.press('down', 'enter')
                await screen(pilot, FormScreen, 'VPN profile')
                await paste(pilot, 'https://vpn.example.test/profile?token=preview')
                await pilot.press('enter')
                await screen(pilot, MenuScreen, 'VPN profile')
                await pilot.press('enter')
                await screen(pilot, FormScreen, 'Automatic updates')
                self.assertEqual(app.screen.query_one(Input).value, '8')
                await pilot.press('enter')
                await screen(pilot, MenuScreen, 'Automatic reboot')
                self.assertEqual(app.screen.query_one(OptionList).highlighted, 0)
                await pilot.press('enter')
                await screen(pilot, FormScreen, 'Review and install')
                review = unescape(app.export_screenshot()).replace('\xa0', ' ')
                for value in ('SSH password allowed', 'WireGuard at boot', 'Every 8 h', 'Reboot: never'):
                    self.assertIn(value, review)
                self.assertLessEqual(app.screen.query_one('.canvas').size.height, 12)
                self.assertNotIn('preview-wifi-pass', app.export_screenshot())
                self.assertNotIn('new-password', app.export_screenshot())
                await pilot.press('f2')
                await screen(pilot, MenuScreen, 'Change settings')
                await pilot.press('enter')
                await screen(pilot, FormScreen, 'Identity')
                await paste(pilot, 'edited-host', 'hostname')
                await pilot.press('enter', 'enter')
                await screen(pilot, FormScreen, 'Review and install')
                self.assertIn('edited-host', str(app.screen.query_one('.canvas').render()) + app.export_screenshot())
                self.assertFalse(services.installed)
                await pilot.press('enter')
                self.assertFalse(services.installed)
                self.assertIn('exact path', str(app.screen.query_one('#error', Static).render()))
                await paste(pilot, '/dev/nvme0n1')
                await pilot.press('enter')
                await screen(pilot, NoticeScreen, 'Preview complete')
                await pilot.press('enter')
        self.assertTrue(services.installed)
        self.assertIsNone(app.failure)


if __name__ == '__main__':
    unittest.main()
