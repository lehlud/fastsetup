"""Cancellation must stop Anaconda; no real installer is ever launched here."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'image/usr/local/lib'))
from kiosk import install


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
