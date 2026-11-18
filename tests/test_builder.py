import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_builder', ROOT / 'build/build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PortableBuilderTests(unittest.TestCase):
    def test_creator_stages_updated_boot_files_and_omits_hfs(self):
        events = []

        class LegacyCreator:
            def configure(self):
                self._create_bootconfig()
                events.append('post regenerated initramfs')

            def _create_bootconfig(self):
                events.append('stage boot files')
                self._generate_efiboot('/iso')

            def _generate_efiboot(self, directory):
                events.append('legacy HFS')

        class BaseCreator:
            def _generate_efiboot(self, directory):
                events.append('FAT EFI')

        package = types.ModuleType('imgcreate')
        package.LiveImageCreator = LegacyCreator
        live = types.ModuleType('imgcreate.live')
        live.LiveImageCreatorBase = BaseCreator
        spec = importlib.util.spec_from_file_location('create_live_adapter', ROOT / 'build/create-live.py')
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        with patch.dict('sys.modules', {'imgcreate': package, 'imgcreate.live': live}), \
             patch('shutil.which', return_value='/usr/bin/livecd-creator'), \
             patch('sys.argv', ['create-live.py']), \
             patch('runpy.run_path', side_effect=lambda *a, **k: package.LiveImageCreator().configure()):
            adapter.main()
        self.assertEqual(events, ['post regenerated initramfs', 'stage boot files', 'FAT EFI'])

    def test_smoke_selects_raw_firmware_instead_of_treating_qcow2_as_raw(self):
        spec = importlib.util.spec_from_file_location('smoke_firmware', ROOT / 'build/smoke.py')
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, kind in enumerate(('qcow2', 'raw')):
                record = dict(features=['secure-boot', 'enrolled-keys'], targets=[dict(architecture='x86_64')],
                              mapping=dict(device='flash', executable=dict(format=kind, filename=f'/code.{kind}'),
                                           **{'nvram-template': dict(format=kind, filename=f'/vars.{kind}')}))
                (root / f'{index}.json').write_text(json.dumps(record))
            self.assertEqual(smoke.firmware(root), ('/code.raw', '/vars.raw'))

    def fixture(self, root):
        for name in builder.SOURCE_DIRS:
            (root / name).mkdir()
            (root / name / 'source.txt').write_text('source')
        for name in ('README.md', '.gitignore', '.dockerignore'):
            (root / name).write_text('metadata')

    def test_source_snapshot_excludes_host_state_and_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            destination = Path(temporary) / 'snapshot'
            root.mkdir()
            destination.mkdir()
            self.fixture(root)
            for name in ('.aws', '.git', '.codex', 'out', 'tests'):
                (root / name).mkdir()
                (root / name / 'private').write_text('excluded-test-data')
            (root / 'build/__pycache__').mkdir()
            (root / 'build/__pycache__/cached.pyc').write_bytes(b'cache')
            builder.copy_source(root, destination)
            self.assertTrue((destination / 'image/source.txt').is_file())
            self.assertFalse((destination / 'build/__pycache__').exists())
            for name in ('.aws', '.git', '.codex', 'out', 'tests'):
                self.assertFalse((destination / name).exists())

    def test_source_symlinks_cannot_escape_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            root.mkdir()
            self.fixture(root)
            (root / 'image/link').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(builder.BuildError, 'symlinks'):
                builder.copy_source(root, Path(temporary) / 'snapshot')

    def test_mounts_preserve_spaces_and_commas(self):
        fields = next(csv.reader([builder.mount('/path with spaces/a,b', '/source', True)]))
        self.assertEqual(fields, ['type=bind', 'source=/path with spaces/a,b', 'target=/source', 'readonly'])

    def test_rootless_engines_are_detected(self):
        for engine, response in (('docker', b'["name=rootless"]'), ('podman', b'true\n')):
            with self.subTest(engine=engine), patch.object(builder.subprocess, 'check_output', return_value=response):
                self.assertTrue(builder.engine_is_rootless(engine))

    def test_rootless_podman_automatically_selects_the_unprivileged_vm(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'artifacts'
            def execute(command, **kwargs):
                if command[1] == 'cp' and command[-1] == str(output):
                    data = b'fake VM artifact'
                    (output / 'kiosk-live.iso').write_bytes(data)
                    (output / 'kiosk-live.iso.sha256').write_text(
                        hashlib.sha256(data).hexdigest() + '  kiosk-live.iso\n')
                return subprocess.CompletedProcess(command, 0)

            with patch.object(builder.platform, 'system', return_value='Linux'), \
                 patch.object(builder.subprocess, 'check_output', return_value=b'true\n'), \
                 patch.object(builder.subprocess, 'run', side_effect=execute) as run, \
                 patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(builder.main(['--engine', 'podman', str(output)]), 0)
            build, create, upload, start, download, cleanup = [call.args[0] for call in run.call_args_list]
            self.assertIn(str(ROOT / 'build/Containerfile.vm'), build)
            self.assertNotIn('--platform', build)
            self.assertEqual(create[-1], builder.VM_BUILDER_IMAGE)
            self.assertIn('--cap-drop=all', create)
            for restricted in ('--privileged', '--userns=host', '--device', '--mount'):
                self.assertNotIn(restricted, create)
            self.assertTrue(upload[-1].endswith(':/source'))
            self.assertEqual(start[1:3], ['start', '--attach'])
            self.assertTrue(download[-2].endswith(':/output/.'))
            self.assertIn('--volumes', cleanup)

    def test_checksum_is_verified_after_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            data = b'disposable ISO test fixture'
            (output / 'kiosk-live.iso').write_bytes(data)
            checksum = hashlib.sha256(data).hexdigest()
            (output / 'kiosk-live.iso.sha256').write_text(checksum + '  kiosk-live.iso\n')
            builder.verify_copy(output)
            (output / 'kiosk-live.iso').write_bytes(b'tampered')
            with self.assertRaisesRegex(builder.BuildError, 'mismatch'):
                builder.verify_copy(output)

    def test_linux_and_macos_dry_runs_need_no_engine_or_downloads(self):
        for system in ('Linux', 'Darwin'):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temporary, \
                 patch.object(builder.platform, 'system', return_value=system), \
                 patch.object(builder.shutil, 'which', return_value=None), \
                 patch.object(builder.subprocess, 'run') as run, \
                 patch('sys.stdout', new_callable=io.StringIO) as stream:
                output = Path(temporary) / 'artifacts'
                self.assertEqual(builder.main(['--dry-run', str(output)]), 0)
                commands = json.loads(stream.getvalue())
                self.assertIn('--pull', commands['build'])
                self.assertIn('linux/amd64', commands['run'])
                self.assertNotIn('qemu-system-x86_64', commands['run'])
                run.assert_not_called()
                self.assertFalse(output.exists())

    def test_windows_is_out_of_scope(self):
        with patch.object(builder.platform, 'system', return_value='Windows'):
            with self.assertRaisesRegex(builder.BuildError, 'Windows'):
                builder.main(['--dry-run'])

    def test_frontend_exports_artifacts_and_cleans_up_its_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'artifacts'
            commands, snapshots = [], []

            def execute(command, **kwargs):
                commands.append(command)
                if command[1] == 'run':
                    mounts = {}
                    for index, value in enumerate(command):
                        if value == '--mount':
                            fields = dict(field.split('=', 1) for field in next(csv.reader([command[index + 1]])) if '=' in field)
                            mounts[fields['target']] = Path(fields['source'])
                    snapshot = mounts['/source']
                    snapshots.append(snapshot)
                    self.assertTrue((snapshot / 'build/Containerfile').is_file())
                    self.assertFalse((snapshot / '.git').exists())
                    data = b'fake engine artifact'
                    (mounts['/output'] / 'kiosk-live.iso').write_bytes(data)
                    (mounts['/output'] / 'kiosk-live.iso.sha256').write_text(
                        hashlib.sha256(data).hexdigest() + '  kiosk-live.iso\n')
                return subprocess.CompletedProcess(command, 0)

            with patch.object(builder.platform, 'system', return_value='Linux'), \
                 patch.object(builder.subprocess, 'check_output', return_value=b'[]'), \
                 patch.object(builder.subprocess, 'run', side_effect=execute), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(builder.main(['--engine', 'docker', str(output)]), 0)
            self.assertFalse(snapshots[0].exists())
            self.assertEqual(snapshots[0].parent, output.parent)
            self.assertEqual([command[1] for command in commands], ['build', 'run', 'rm'])
            self.assertIn('--volumes', commands[-1])

    def test_failed_container_still_gets_cleaned_up(self):
        commands = []

        def execute(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 17 if command[1] == 'run' else 0)

        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(builder.platform, 'system', return_value='Linux'), \
             patch.object(builder.subprocess, 'check_output', return_value=b'[]'), \
             patch.object(builder.subprocess, 'run', side_effect=execute), patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(builder.BuildError, 'builder.log'):
                builder.main(['--engine', 'docker', str(Path(temporary) / 'out')])
        self.assertEqual(commands[-1][1], 'rm')


if __name__ == '__main__':
    unittest.main()
