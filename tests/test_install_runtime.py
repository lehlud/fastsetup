"""Cancellation must stop Anaconda; no real installer is ever launched here."""
import asyncio
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'image/usr/local/lib'))
from kiosk import install
from kiosk.common import Failure
from kiosk.install_progress import InstallProgress

ROOT = Path(__file__).resolve().parents[1]


class ShutdownTests(unittest.TestCase):
    def request_shutdown(self, status):
        # Override commands in this shell only, so it cannot power off the host.
        fixture = r'''
set +o posix
shutdown_status=$2
function rm() { :; }
function /usr/bin/timeout() {
    printf '%s\n' "$*"
    if [[ "$2" == /usr/bin/systemctl ]]; then return "$shutdown_status"; fi
}
function exec() { printf '%s\n' "$*"; }
source "$1"
'''
        return subprocess.check_output(['bash', '-ec', fixture, 'fixture',
                                        str(ROOT / 'image/usr/local/bin/kiosk-poweroff'), str(status)],
                                       text=True).splitlines()

    def test_accepted_shutdown_request_leaves_unmounting_to_systemd(self):
        self.assertEqual(self.request_shutdown(0),
                         ['1 /usr/bin/systemctl --no-block --check-inhibitors=no poweroff'])

    def test_failed_shutdown_request_syncs_before_forced_fallback(self):
        self.assertEqual(self.request_shutdown(124)[1:], ['0.5 /usr/bin/sync', '/usr/sbin/poweroff -f'])


def installer_output(data=b''):
    stream = asyncio.StreamReader()
    stream.feed_data(data)
    stream.feed_eof()
    return stream


class InstallationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_selinux_enforcement_is_restored_after_installer_failure(self):
        progress = InstallProgress()
        with patch('kiosk.install.output', return_value='Enforcing'), patch('kiosk.install.run') as run, \
             patch('kiosk.install.run_anaconda_process', new=AsyncMock(side_effect=Failure('Install failed'))):
            with self.assertRaisesRegex(Failure, 'Install failed'):
                await install.run_anaconda(progress)
        self.assertEqual(run.call_args_list, [call(['/usr/sbin/setenforce', '0'], timeout=5),
                                              call(['/usr/sbin/setenforce', '1'], timeout=5)])

    async def test_successful_anaconda_is_not_terminated(self):
        proc = Mock(returncode=0, stdout=installer_output(b'...Configuring storage\n...Complete!\n'))
        proc.wait = AsyncMock(return_value=0)
        progress = InstallProgress()
        with patch('kiosk.install.asyncio.create_subprocess_exec', new=AsyncMock(return_value=proc)) as launch, \
             patch('kiosk.install_progress.log'):
            self.assertEqual(await install.run_anaconda_process(progress), 0)
        self.assertTrue(progress.completed)
        self.assertEqual(launch.call_args.args[0], '/usr/local/bin/kiosk-anaconda')
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    async def test_abort_terminates_running_anaconda_before_returning(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        proc = Mock(returncode=None, stdout=installer_output())
        async def wait():
            started.set()
            await stopped.wait()
            return proc.returncode
        def terminate():
            proc.returncode = -15
            stopped.set()
        proc.wait = wait
        proc.terminate.side_effect = terminate
        with patch('kiosk.install.asyncio.create_subprocess_exec', new=AsyncMock(return_value=proc)):
            task = asyncio.create_task(install.run_anaconda_process(InstallProgress()))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    async def test_deadline_is_failure_and_stops_the_process(self):
        proc = Mock(returncode=None, stdout=installer_output())
        proc.wait = AsyncMock(side_effect=[TimeoutError, 0])
        with patch('kiosk.install.asyncio.create_subprocess_exec', new=AsyncMock(return_value=proc)):
            with self.assertRaisesRegex(Failure, 'no progress for 15 minutes'):
                await install.run_anaconda_process(InstallProgress())
        proc.terminate.assert_called_once()

    async def test_progress_exposes_package_counts_and_ignores_raw_secrets(self):
        messages = []
        stream = installer_output(b'Starting automated install....\n'
                                  b'..Downloading 300 RPMs, 20 MiB / 100 MiB (20%) done.\n'
                                  b'Installing kernel-core.x86_64 (125/250)\n'
                                  b'Line: network --wpakey=fixture-only-secret\n'
                                  b'The following mandatory spokes are not completed:\nInstallation source\n')
        progress = InstallProgress(messages.append)
        with patch('kiosk.install_progress.log') as log:
            await install.read_progress(stream, progress)
        self.assertEqual(messages, ['Checking repositories and the target disk',
                                    'Download 300 RPMs   20 MiB / 100 MiB (20%)',
                                    'Install 125/250 (50%)   kernel-core.x86_64'])
        self.assertEqual(progress.missing, ['Installation source'])
        self.assertNotIn('fixture-only-secret', str(log.call_args_list))
        self.assertFalse(progress.completed)

    async def test_only_real_progress_refreshes_installation_deadline(self):
        stream = installer_output(b'Installing kernel-core.x86_64 (125/250)\n'
                                  b'Installing kernel-core.x86_64 (125/250)\n'
                                  b'random output\nInstalling chrony.x86_64 (126/250)\n')
        refresh = Mock()
        with patch('kiosk.install_progress.log'):
            await install.read_progress(stream, InstallProgress(), refresh)
        self.assertEqual(refresh.call_count, 2)

    def test_wrapper_reapplies_live_policy_after_anaconda_options(self):
        conf = Mock(system=SimpleNamespace(can_reboot=True, provides_system_bus=True))
        def apply_options(_):
            conf.system.can_reboot = True
        def load_kiosk_config(_):
            conf.system.can_reboot = False
        conf.set_from_opts.side_effect = apply_options
        conf.set_from_files.side_effect = load_kiosk_config
        run = runpy.run_path
        opts = object()
        def run_anaconda(path, *, run_name):
            self.assertEqual((path, run_name), ('/usr/sbin/anaconda', '__main__'))
            for _ in range(2):
                conf.set_from_opts(opts)
                self.assertFalse(conf.system.can_reboot)
        with patch.dict(sys.modules, {'pyanaconda.core.configuration.anaconda': SimpleNamespace(conf=conf)}), \
             patch('runpy.run_path', side_effect=run_anaconda):
            run(str(ROOT / 'image/usr/local/bin/kiosk-anaconda'), run_name='__main__')
        self.assertEqual(conf.set_from_files.call_count, 2)
