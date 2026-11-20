import base64
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
from kiosk.common import Failure
from kiosk.log_view import display_log, log_secrets


class LogViewTests(unittest.TestCase):
    def test_view_retains_storage_errors_and_redacts_credentials_and_controls(self):
        wifi_password = 'sample wi-fi pass'
        vpn_password = 'inline vpn credential'
        data = {'hashed': '$6$test-account-hash', 'net': {'psk': wifi_password},
                'vpn': {'config': f'<auth-user-pass>\nsample-user\n{vpn_password}\n</auth-user-pass>\n'}}
        values = [data['hashed'], wifi_password, 'sample\\swi-fi\\spass', vpn_password,
                  base64.b64encode(wifi_password.encode()).decode(), 'Y' * 128]
        with TemporaryDirectory() as folder:
            log = Path(folder) / 'storage.log'
            log.write_text('\x1b[1mNo usable disks selected.\n' + '\n'.join(values) + '\n')
            result = display_log(log, log_secrets(data))
        self.assertIn('No usable disks selected.', result)
        self.assertNotIn('\x1b', result)
        self.assertEqual(result.count('[redacted]'), len(values))
        for value in values:
            self.assertNotIn(value, result)

    def test_view_rejects_symlinks_and_non_regular_logs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.write_text('data')
            link = root / 'storage.log'
            link.symlink_to(source)
            for path in (root, link):
                with self.assertRaisesRegex(Failure, 'unavailable'):
                    display_log(path, ())
