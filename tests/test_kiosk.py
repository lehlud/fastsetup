import asyncio
import base64
from configparser import ConfigParser
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
from kiosk import battery, common, install, keyboard, network, storage


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RenderTests(unittest.TestCase):
    def assert_sleep_policy(self, root):
        logind = ConfigParser()
        logind.read(root / 'etc/systemd/logind.conf.d/server-power.conf')
        for name in ('HandleLidSwitch', 'HandleLidSwitchExternalPower', 'HandleLidSwitchDocked',
                     'HandleSuspendKey', 'HandleSuspendKeyLongPress', 'HandleHibernateKey',
                     'HandleHibernateKeyLongPress', 'IdleAction'):
            self.assertEqual(logind['Login'][name], 'ignore', name)
        sleep = ConfigParser()
        sleep.read(root / 'etc/systemd/sleep.conf.d/90-no-sleep.conf')
        for name in ('AllowSuspend', 'AllowHibernation', 'AllowHybridSleep', 'AllowSuspendThenHibernate'):
            self.assertFalse(sleep['Sleep'].getboolean(name), name)

    def values(self):
        values = {key: '' for key in install.TOKENS}
        values.update(TZ='Europe/Berlin', HOSTNAME='calm-fox', USER='server', USERHASH='$6$salt$hash',
                      DISKNAME='nvme0n1', LAYOUT='de (nodeadkeys)', VCKEYMAP='de-latin1-nodeadkeys',
                      SSH_PASSWORD='yes')
        return values

    def test_complete_kickstart_and_literal_substitution(self):
        template = (ROOT / 'image/usr/share/kiosk/ks.template').read_text()
        result = install.render(template, self.values())
        self.assertNotIn('@@', result)
        self.assertIn("--xlayouts='de (nodeadkeys)'", result)
        values = self.values()
        values['HOSTNAME'] = r'back\slash&pipe|'
        self.assertIn(r'back\slash&pipe|', install.render(template, values))

    def test_missing_unknown_and_recursive_tokens_fail(self):
        template = (ROOT / 'image/usr/share/kiosk/ks.template').read_text()
        for text in (template + '@@UNKNOWN@@', template.replace('@@TZ@@', 'UTC')):
            with self.assertRaises(common.Failure):
                install.render(text, self.values())
        values = self.values()
        values['USER'] = '@@TZ@@'
        with self.assertRaises(common.Failure):
            install.render(template, values)

    def test_kickstart_private_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'kiosk.ks'
            common.private_write(path, 'hash')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                common.private_write(link, 'bad')
            self.assertEqual(path.read_text(), 'hash')

    def test_target_payload_excludes_kiosk(self):
        prepare = module('prepare', 'build/prepare.py')
        self.assertNotIn('usr/local/bin/kiosk-install', prepare.TARGET)
        self.assertNotIn('etc/systemd/system/kiosk.service', prepare.TARGET)
        self.assertNotIn('etc/systemd/logind.conf.d/kiosk.conf', prepare.TARGET)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flat.ks'
            prepare.prepare(path)
            self.assertNotIn('appends an embedded payload', path.read_text())
            self.assertIn('KIOSK_PAYLOAD', path.read_text())

    @unittest.skipIf(sys.version_info < (3, 12), 'Image-side extractor uses Python 3.12+ tar filters')
    def test_live_payload_extracts_nested_files_with_permissions(self):
        prepare = module('prepare_live_post', 'build/prepare.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'root'
            root.mkdir()
            flat = Path(temporary) / 'live.ks'
            prepare.prepare(flat)
            script = flat.read_text().split('%post --nochroot --interpreter=/usr/bin/python3 --erroronfail\n', 1)[1].split('%end', 1)[0]
            with patch.dict(os.environ, INSTALL_ROOT=str(root)):
                exec(compile(script, 'live-payload-post.py', 'exec'), {})
            path = root / 'usr/local/bin/kiosk-install'
            self.assertEqual(path.read_bytes(), (ROOT / 'image/usr/local/bin/kiosk-install').read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertTrue((root / 'etc/systemd/system/kiosk.service').is_file())
            foot = ConfigParser()
            foot.read(root / 'etc/xdg/foot/foot.ini')
            self.assertEqual(foot['key-bindings']['quit'], 'Control+Alt+Delete')
            self.assertTrue((root / 'usr/share/kiosk/target-payload.b64').is_file())
            self.assert_sleep_policy(root)

    def test_target_post_script_compiles(self):
        template = (ROOT / 'image/usr/share/kiosk/ks.template').read_text()
        result = install.render(template, self.values())
        script = result.split('%post --nochroot --interpreter=/usr/bin/python3 --erroronfail\n')[1].rsplit('%end', 1)[0]
        compile(script, 'target-post.py', 'exec')

    def test_target_post_materializes_only_server_payload_and_private_network(self):
        prepare = module('prepare_post', 'build/prepare.py')
        manifest = {name: dict(data=install.b64((ROOT / 'image' / name).read_bytes()),
                              mode=0o755 if '/bin/' in name else 0o644) for name in prepare.TARGET}
        values = self.values()
        _, profile = network.profile('wlan0', 'test-network', 'test$pass\\word', 'sae', ident='test-uuid')
        values.update(PAYLOAD_B64=install.b64(json.dumps(manifest)), NETWORK_B64=install.b64(profile),
                      SSID_B64=install.b64('test-network'), PSK_B64=install.b64('test$pass\\word'),
                      BATTERY_B64=install.b64('unsupported test hardware'))
        result = install.render((ROOT / 'image/usr/share/kiosk/ks.template').read_text(), values)
        script = result.split('%post --nochroot --interpreter=/usr/bin/python3 --erroronfail\n')[1].rsplit('%end', 1)[0]
        with tempfile.TemporaryDirectory() as tmp, patch('subprocess.run') as run:
            root = Path(tmp)
            (root / 'etc/sudoers.d').mkdir(parents=True)
            (root / 'etc/libvirt').mkdir(parents=True)
            script = script.replace("root = Path('/mnt/sysroot')", 'root = Path(' + repr(tmp) + ')')
            exec(compile(script, 'test-target-post.py', 'exec'), {})
            path = root / 'etc/NetworkManager/system-connections/kiosk.nmconnection'
            self.assertEqual(path.read_text(), profile)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / 'etc/sudoers.d/10-server-wheel').stat().st_mode), 0o440)
            self.assertFalse((root / 'etc/systemd/system/kiosk.service').exists())
            self.assertFalse((root / 'usr/local/bin/kiosk-install').exists())
            self.assertIn('%wheel ALL=(ALL) ALL', (root / 'etc/sudoers.d/10-server-wheel').read_text())
            self.assertTrue(any('restorecon' in call.args[0] for call in run.call_args_list))
            self.assert_sleep_policy(root)
            masks = {call.args[0][-1] for call in run.call_args_list
                     if call.args[0][:4] == ['chroot', str(root), 'systemctl', 'mask']}
            self.assertEqual(masks, {'sleep.target', 'suspend.target', 'hibernate.target',
                                     'hybrid-sleep.target', 'suspend-then-hibernate.target'})

    def test_hostname_rejects_kickstart_injection(self):
        for value in ('hello\nrootpw bad', '-host', 'a;foo', 'a'*42, 'UPPER'):
            self.assertFalse(common.valid_hostname(value))
        self.assertTrue(common.valid_hostname('calm-fox'))


class KeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.share = ROOT / 'image/usr/share/kiosk'
        cls.table = keyboard.load_table(cls.share)

    def test_at_least_35_variants(self):
        self.assertGreaterEqual(len(keyboard.layouts(self.share)), 35)
        self.assertEqual(set(self.table), {x['id'] for x in keyboard.layouts(self.share)})

    def test_azerty_and_qwertz_do_not_match_each_other(self):
        target = 'yzqwam,1'
        for ident, other in (('fr', 'de'), ('de', 'fr')):
            digits = keyboard.load_digits(self.share)
            reverse = {char: code for code, char in self.table[ident].items() if char}
            reverse.update({char: code for code, char in digits.get(ident, {}).items()})
            codes = [reverse[char] for char in target]
            scores = dict((name, score) for score, name in keyboard.rank(self.table, [(target, codes)], digits))
            self.assertEqual(scores[ident], 0)
            self.assertGreater(scores[other], 1)

    def test_dead_key_variants_are_honest_ties(self):
        self.assertEqual(self.table['de'][21], self.table['de-nodeadkeys'][21])
        self.assertEqual(self.table['de'][44], 'y')
        self.assertEqual(self.table['us'][44], 'z')

    def test_levenshtein_edit_budget(self):
        self.assertEqual(keyboard.distance('abcdef', 'abcdef'), 0)
        self.assertEqual(keyboard.distance('abcde', 'abcdef'), 1)
        self.assertEqual(keyboard.distance('xbcdef', 'abcdef'), 1)
        self.assertEqual(keyboard.distance('bad', 'abcdef'), 4)

    def test_generated_table_integrity(self):
        import hashlib
        metadata = json.loads((self.share / 'keymap-metadata.json').read_text())
        self.assertEqual(metadata['_sha256'], hashlib.sha256((self.share / 'keymap-table.tsv').read_bytes()).hexdigest())
        self.assertTrue(metadata['de']['dead_keys'])
        self.assertFalse(metadata['de-nodeadkeys']['dead_keys'])

    def test_raw_events_are_exclusively_grabbed_and_released(self):
        data = b''.join(keyboard.EVENT.pack(0, 0, 1, code, value)
                        for code, value in ((21, 1), (21, 2), (21, 0), (44, 1)))
        with patch('kiosk.keyboard.os.open', return_value=9), patch('kiosk.keyboard.os.close'), \
             patch('kiosk.keyboard.os.read', return_value=data), patch('kiosk.keyboard.fcntl.ioctl') as ioctl, \
             patch('kiosk.keyboard.select.select', side_effect=[([], [], []), ([9], [], [])]):
            self.assertEqual(keyboard.read_round('/dev/input/event-test', 2), [21, 44])
        self.assertEqual(ioctl.call_args_list[0].args, (9, keyboard.EVIOCGRAB, 1))
        self.assertEqual(ioctl.call_args_list[-1].args, (9, keyboard.EVIOCGRAB, 0))

    def test_modified_round_is_discarded_and_timeout_releases_grab(self):
        data = b''.join(keyboard.EVENT.pack(0, 0, 1, code, 1) for code in (42, 21, 44))
        with patch('kiosk.keyboard.os.open', return_value=9), patch('kiosk.keyboard.os.close'), \
             patch('kiosk.keyboard.os.read', return_value=data), patch('kiosk.keyboard.fcntl.ioctl') as ioctl, \
             patch('kiosk.keyboard.select.select', side_effect=[([], [], []), ([9], [], [])]):
            self.assertIsNone(keyboard.read_round('/dev/input/event-test', 2))
        with patch('kiosk.keyboard.os.open', return_value=9), patch('kiosk.keyboard.os.close'), \
             patch('kiosk.keyboard.fcntl.ioctl') as ioctl, \
             patch('kiosk.keyboard.select.select', return_value=([], [], [])):
            with self.assertRaisesRegex(common.Failure, 'timed out'):
                keyboard.read_round('/dev/input/event-test', 2)
        self.assertEqual(ioctl.call_args_list[-1].args, (9, keyboard.EVIOCGRAB, 0))

    def test_ctrl_c_remains_an_abort_during_raw_grab(self):
        data = b''.join(keyboard.EVENT.pack(0, 0, 1, code, 1) for code in (29, 46))
        with patch('kiosk.keyboard.os.open', return_value=9), patch('kiosk.keyboard.os.close'), \
             patch('kiosk.keyboard.os.read', return_value=data), patch('kiosk.keyboard.fcntl.ioctl') as ioctl, \
             patch('kiosk.keyboard.select.select', side_effect=[([], [], []), ([9], [], [])]), \
             patch('kiosk.keyboard.poweroff', side_effect=SystemExit) as abort:
            with self.assertRaises(SystemExit):
                keyboard.read_round('/dev/input/event-test', 2)
        abort.assert_called_once()
        self.assertEqual(ioctl.call_args_list[-1].args, (9, keyboard.EVIOCGRAB, 0))


class DiskTests(unittest.TestCase):
    def nodes(self):
        return {'/dev/sda': dict(name='/dev/sda', type='disk', size=16_000_000_000, mountpoints=[None]),
                '/dev/sda1': dict(name='/dev/sda1', type='part', size=15_000_000_000,
                                  pkname='/dev/sda', mountpoints=['/run/initramfs/live']),
                '/dev/nvme0n1': dict(name='/dev/nvme0n1', type='disk', size=128_000_000_000, mountpoints=[None])}

    def test_usb_ancestor_is_locked(self):
        nodes = self.nodes()
        protected = storage.physical('/dev/sda1', nodes)
        self.assertEqual(protected, {'/dev/sda'})
        with self.assertRaisesRegex(common.Failure, 'protected'):
            storage.validate_disk('/dev/sda', nodes, protected)

    def test_disk_size_partition_and_mount_checks(self):
        nodes = self.nodes()
        self.assertEqual(storage.validate_disk('/dev/nvme0n1', nodes, {'/dev/sda'})['name'], '/dev/nvme0n1')
        for path in ('/dev/sda1', '/dev/missing'):
            with self.assertRaises(common.Failure):
                storage.validate_disk(path, nodes, {'/dev/sda'})
        nodes['/dev/nvme0n1']['size'] = 7_999_999_999
        with self.assertRaises(common.Failure):
            storage.validate_disk('/dev/nvme0n1', nodes, set())
        with self.assertRaisesRegex(common.Failure, 'mounted'):
            storage.validate_disk('/dev/sda', nodes, set())

    @patch('kiosk.storage.output')
    def test_unresolved_live_disk_fails_closed(self, output):
        output.side_effect = ['overlay', 'overlay', '{"filesystems": []}']
        with self.assertRaisesRegex(common.Failure, 'unresolved'):
            storage.live_disks(self.nodes())


class NetworkTests(unittest.TestCase):
    @patch('kiosk.network.ready_wifi')
    @patch('kiosk.network.output', return_value='broken\nHome:20:WPA2\nHome:90:WPA3\nHidden::WPA2\nCafe\\:West:75:--')
    def test_scan_keeps_strongest_ssid_and_ignores_incomplete_rows(self, output, ready):
        self.assertEqual(network.scan('wlan0'), [['Home', '90', 'WPA3'], ['Cafe:West', '75', '--']])

    def test_failed_connection_removes_keyfile_and_autoconnect_profile(self):
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch('kiosk.network.Path', return_value=directory), patch('kiosk.network.run') as run:
                run.side_effect = [CompletedProcess([], 0), CompletedProcess([], 10), CompletedProcess([], 0)]
                with self.assertRaisesRegex(common.Failure, 'check password'):
                    network.activate('wlan0', 'Home', 'never-in-argv')
                self.assertFalse(list(directory.iterdir()))
                self.assertIn('delete', run.call_args.args[0])
                self.assertNotIn('never-in-argv', str(run.call_args_list))

    def test_live_and_target_explicitly_include_wireless_firmware_and_modules(self):
        # These packages cannot rely on weak dependencies: both images exclude them.
        firmware = {'atheros-firmware', 'atmel-firmware', 'brcmfmac-firmware',
                    'iwlegacy-firmware', 'iwlwifi-dvm-firmware', 'iwlwifi-mld-firmware',
                    'iwlwifi-mvm-firmware', 'libertas-firmware', 'mt7xxx-firmware',
                    'nxpwireless-firmware', 'realtek-firmware', 'tiwilink-firmware',
                    'zd1211-firmware'}
        for path in ('build/kiosk-live.ks', 'image/usr/share/kiosk/ks.template'):
            with self.subTest(path=path):
                text = (ROOT / path).read_text()
                packages = set(text.split('%packages --exclude-weakdeps\n', 1)[1].split('%end', 1)[0].splitlines())
                self.assertLessEqual(firmware | {'linux-firmware', 'kernel', 'kernel-modules-extra',
                                                'NetworkManager-wifi', 'NetworkManager-tui'}, packages)

    @patch('kiosk.network.time.sleep')
    @patch('kiosk.network.wifi_hard_blocked', return_value=False)
    @patch('kiosk.network.output')
    @patch('kiosk.network.run')
    def test_discovery_enables_radio_and_waits_for_delayed_wifi(self, run, output, blocked, sleep):
        def report(args):
            self.assertTrue(any(call.args[0][-3:] == ['radio', 'wifi', 'on'] for call in run.call_args_list))
            return reports.pop(0)

        reports = ['enp1s0:ethernet\nlo:loopback',
                   'enp1s0:ethernet\nwlp2s0:wifi\np2p-dev-wlp2s0:wifi-p2p\n']
        output.side_effect = report
        self.assertEqual(network.devices(), (['wlp2s0'], ['enp1s0']))
        sleep.assert_called_once_with(1)

    @patch('kiosk.network.time.sleep')
    @patch('kiosk.network.wifi_hard_blocked', return_value=False)
    @patch('kiosk.network.output', return_value='lo:loopback\n')
    @patch('kiosk.network.run')
    def test_discovery_stops_waiting_when_no_adapter_exists(self, run, output, blocked, sleep):
        self.assertEqual(network.devices(), ([], []))
        self.assertEqual(sleep.call_count, 5)

    def test_rfkill_distinguishes_bluetooth_software_and_wlan_hardware_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, kind, hard in [('rfkill0', 'bluetooth', '1'), ('rfkill1', 'wlan', '0')]:
                radio = root / name
                radio.mkdir()
                (radio / 'type').write_text(kind)
                (radio / 'hard').write_text(hard)
                (radio / 'soft').write_text('1')
            self.assertFalse(network.wifi_hard_blocked(root))
            (root / 'rfkill1/hard').write_text('1')
            self.assertTrue(network.wifi_hard_blocked(root))

    @patch('kiosk.network.log')
    @patch('kiosk.network.wifi_hard_blocked', return_value=True)
    @patch('kiosk.network.output', return_value='20 (unavailable)')
    def test_hardware_block_has_an_actionable_error(self, output, blocked, log):
        with self.assertRaisesRegex(common.Failure, 'enable the radio switch'):
            network.ready_wifi('wlan0')

    @patch('kiosk.network.time.sleep')
    @patch('kiosk.network.wifi_hard_blocked', return_value=False)
    @patch('kiosk.network.output', side_effect=['20 (unavailable)', '30 (disconnected)'])
    def test_wifi_waits_for_radio_to_become_usable(self, output, blocked, sleep):
        network.ready_wifi('wlan0')
        sleep.assert_called_once_with(1)

    @patch('kiosk.network.log')
    def test_missing_wifi_can_retry_and_connect_without_falling_into_ethernet(self, log):
        for wired in ([], ['enp1s0']):
            with self.subTest(wired=wired):
                backend = Mock()
                backend.devices.side_effect = [([], wired), (['wlan0'], wired)]
                backend.hard_blocked.return_value = False
                backend.scan.return_value = [['test-network', '80', 'WPA2']]
                backend.activate.return_value = {'path': 'Wi-Fi'}
                ui = Mock()
                ui.menu = AsyncMock(side_effect=[len(wired), 0, 0])
                ui.ask = AsyncMock(return_value='test-passphrase')
                ui.busy = AsyncMock(side_effect=lambda title, message, function, *args: function(*args))
                self.assertEqual(asyncio.run(network.configure(ui, backend)), {'path': 'Wi-Fi'})
                first = ui.menu.call_args_list[0]
                self.assertEqual(first.args[1], (['Ethernet'] if wired else []) + ['Retry detection'])
                self.assertIn('Wi-Fi not detected', first.kwargs['context'])
                ui.notice.assert_not_called()
                self.assertEqual(backend.activate.call_args.args[:3], ('wlan0', 'test-network', 'test-passphrase'))

    def test_nmcli_escaped_fields(self):
        self.assertEqual(network.fields(r'ssid\:with\\slash:80:WPA3'), ['ssid:with\\slash', '80', 'WPA3'])

    def test_profile_encodes_newlines_and_dollar_without_shell(self):
        _, text = network.profile('wlan0', 'a;[$]\\ssid', 'pass\\word$\nline', 'sae', True, ident='fixed')
        self.assertIn('key-mgmt=sae', text)
        self.assertIn(r'psk=pass\\word$\nline', text)
        self.assertIn('hidden=true', text)
        self.assertNotIn('ssid=a;', text)
        self.assertEqual(text.count('[wifi-security]'), 1)

    def test_ethernet_profile_has_no_wifi_secret(self):
        _, text = network.profile('enp1s0', ident='fixed')
        self.assertIn('type=ethernet', text)
        self.assertNotIn('psk', text)

    def test_sae_requires_protected_frames(self):
        _, text = network.profile('wlan0', 'network', 'test-passphrase', 'sae')
        self.assertIn('pmf=3', text)
        self.assertIn('proto=rsn;', text)

    @patch('kiosk.network.run')
    def test_network_gate_requires_both_ip_and_dns(self, run):
        from subprocess import CompletedProcess
        for ping, dns in ((0, 1), (1, 0), (1, 1), (0, 0)):
            run.side_effect = [CompletedProcess([], ping), CompletedProcess([], dns)]
            self.assertEqual(network.connected(), ping == 0 and dns == 0)


class BatteryTests(unittest.TestCase):
    def pack(self, tmp):
        root = Path(tmp)
        pack = root / 'BAT0'
        pack.mkdir()
        for name, value in dict(type='Battery', present='1', capacity='55', status='Not charging',
                                charge_control_start_threshold='0', charge_control_end_threshold='100').items():
            (pack / name).write_text(value)
        ac = root / 'AC'
        ac.mkdir()
        (ac / 'type').write_text('Mains')
        (ac / 'online').write_text('1')
        return root, pack

    def cfg(self):
        return battery.config(Path('/nonexistent-kiosk-config'))

    def test_normal_full_off_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, pack = self.pack(tmp)
            cfg = self.cfg()
            for mode, lower, upper in (('normal', '45', '55'), ('full', '0', '100'), ('off', '0', '100')):
                self.assertIn('verified', battery.apply(cfg, mode, root)[0])
                self.assertEqual((pack / 'charge_control_start_threshold').read_text().strip(), lower)
                self.assertEqual((pack / 'charge_control_end_threshold').read_text().strip(), upper)

    def test_ignored_write_is_failure(self):
        class Ignored:
            def write_text(self, value):
                pass
            def read_text(self):
                return '100'
        with self.assertRaises(common.Failure):
            battery.write_verified(Ignored(), 55)

    def test_guard_only_shuts_down_on_known_ac_loss_below_reserve(self):
        with tempfile.TemporaryDirectory() as tmp, patch('kiosk.battery.run') as run:
            root, pack = self.pack(tmp)
            (pack / 'capacity').write_text('19')
            cfg = self.cfg()
            state = root / 'state'
            battery.guard(cfg, root, state)
            run.assert_not_called()
            (root / 'AC/online').write_text('0')
            battery.guard(cfg, root, state)
            run.assert_called_once_with(['systemctl', 'poweroff'], timeout=10)
            run.reset_mock()
            (root / 'AC/online').unlink()
            battery.guard(cfg, root, state)
            run.assert_not_called()

    def test_adequacy_warning_and_no_battery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, pack = self.pack(tmp)
            (pack / 'status').write_text('Discharging')
            self.assertIn('Warning:', '\n'.join(battery.status(self.cfg(), root)))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(battery.apply(self.cfg(), 'normal', Path(tmp)), ['No battery; policy skipped'])

    def test_config_is_data_not_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config'
            path.write_text('BATTERY=../../etc\n')
            with self.assertRaises(common.Failure):
                battery.config(path)
            path.write_text('START_PERCENT=$(touch bad)\n')
            with self.assertRaises(common.Failure):
                battery.config(path)


if __name__ == '__main__':
    unittest.main()
