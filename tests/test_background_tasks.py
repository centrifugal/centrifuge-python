import asyncio
import contextlib
import logging
import unittest

from centrifuge import Client

# The SDK starts tasks nobody awaits - reconnects, token refreshes,
# resubscribes. They must be referenced while they run, since the event loop
# only keeps weak references to tasks, and their failures must be visible
# instead of surfacing as a late "Task exception was never retrieved".


class TestBackgroundTasks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Client("ws://localhost:8000/connection/websocket")

    async def test_task_is_referenced_until_it_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def work():
            started.set()
            await release.wait()

        task = self.client._spawn(work())
        await started.wait()
        self.assertIn(task, self.client._background_tasks)

        release.set()
        await task
        # No leak: the reference is dropped once the task is done.
        self.assertNotIn(task, self.client._background_tasks)

    async def test_failure_is_logged(self):
        async def boom():
            raise RuntimeError("boom")

        with self.assertLogs("centrifuge", level=logging.ERROR) as logs:
            task = self.client._spawn(boom())
            with contextlib.suppress(RuntimeError):
                await task
            await asyncio.sleep(0)  # let the done callback run

        self.assertTrue(any("background task" in message for message in logs.output))
        self.assertTrue(any("RuntimeError: boom" in message for message in logs.output))
        self.assertNotIn(task, self.client._background_tasks)

    async def test_cancellation_is_not_reported_as_failure(self):
        async def sleeper():
            await asyncio.sleep(10)

        with self.assertNoLogs("centrifuge", level=logging.ERROR):
            task = self.client._spawn(sleeper())
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)  # let the done callback run

        self.assertNotIn(task, self.client._background_tasks)
