"""Cancellation must stop Anaconda; no real installer is ever launched here."""
import asyncio
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'image/usr/local/lib'))
from kiosk import install

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


class InstallationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_anaconda_is_not_terminated(self):
        proc = Mock(returncode=0)
        proc.wait = AsyncMock(return_value=0)
        with patch('kiosk.install.asyncio.create_subprocess_exec', new=AsyncMock(return_value=proc)):
            self.assertEqual(await install.run_anaconda(), 0)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    async def test_abort_terminates_running_anaconda_before_returning(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        proc = Mock(returncode=None)
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
            task = asyncio.create_task(install.run_anaconda())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    async def test_deadline_is_failure_and_stops_the_process(self):
        proc = Mock(returncode=None)
        proc.wait = AsyncMock(side_effect=[TimeoutError, 0])
        with patch('kiosk.install.asyncio.create_subprocess_exec', new=AsyncMock(return_value=proc)):
            self.assertEqual(await install.run_anaconda(), -1)
        proc.terminate.assert_called_once()
