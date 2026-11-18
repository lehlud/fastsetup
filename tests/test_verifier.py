import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('iso_verifier', ROOT / 'build/verify-iso.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class VerifierTests(unittest.TestCase):
    def test_reference_uses_real_rpm_payload_instead_of_ghost_esp_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / 'payload/shimx64.efi'
            payload.parent.mkdir()
            payload.write_bytes(b'RPM-owned signed bytes')
            ghost = root / 'boot/shimx64.efi'
            ghost.parent.mkdir()
            ghost.write_bytes(b'changed ghost bytes')
            artifact = root / 'BOOTX64.EFI'
            artifact.write_bytes(payload.read_bytes())
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            records = f'ALGO 8\n{ghost}\t{"0" * 64}\n{payload}\t{digest}\n'.encode()
            with patch.object(verifier, 'run', return_value=records):
                result = verifier.rpm_payload_match('shim-x64', lambda name: name == 'shimx64.efi', artifact)
                self.assertEqual(result['rpm_file'], str(payload))
                payload.write_bytes(b'tampered reference')
                with self.assertRaisesRegex(ValueError, 'digest'):
                    verifier.rpm_payload_match('shim-x64', lambda name: name == 'shimx64.efi', artifact)

    def test_ghost_only_and_weak_digests_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / 'shimx64.efi'
            artifact.write_bytes(b'bytes')
            for records in (f'ALGO 8\n{artifact}\t{"0" * 64}\n',
                            f'ALGO 1\n{artifact}\t{hashlib.md5(b"bytes").hexdigest()}\n'):
                with self.subTest(records=records), patch.object(verifier, 'run', return_value=records.encode()):
                    with self.assertRaisesRegex(ValueError, 'digest'):
                        verifier.rpm_payload_match('shim-x64', lambda name: True, artifact)

    def test_fedora_ca_with_generic_kernel_leaf_name_is_accepted(self):
        verifier.check_kernel_signature('issuer: /OU=Fedora Secure Boot CA 20200709/CN=fedoraca',
                                        "The signer's common name is kernel-signer\n")
        with self.assertRaisesRegex(ValueError, 'signature'):
            verifier.check_kernel_signature('issuer: untrusted CA', "The signer's common name is kernel-signer\n")
        with self.assertRaisesRegex(ValueError, 'signature'):
            verifier.check_kernel_signature('issuer: Fedora Secure Boot CA', 'No signatures found')


if __name__ == '__main__':
    unittest.main()
