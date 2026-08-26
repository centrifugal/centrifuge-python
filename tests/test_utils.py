import asyncio
import unittest

from centrifuge import codes, utils

_ConnectingCode = codes._ConnectingCode
_ErrorCode = codes._ErrorCode
_backoff = utils._backoff
_code_message = utils._code_message
_code_number = utils._code_number
_is_token_expired = utils._is_token_expired
_wait_for_future = utils._wait_for_future

# utils.py holds small pure helpers used throughout client.py (reconnect delay
# computation, error code formatting, future waiting). They had no direct unit
# coverage - only exercised indirectly via the integration suite.


class TestBackoff(unittest.TestCase):
    def test_within_bounds_for_various_steps(self):
        min_value, max_value = 0.5, 10.0
        for step in (0, 1, 2, 5, 10, 31, 100):
            for _ in range(20):
                delay = _backoff(step, min_value, max_value)
                self.assertGreaterEqual(delay, min_value)
                self.assertLessEqual(delay, max_value)

    def test_step_beyond_max_step_is_capped(self):
        # step=31 and step=1000 must behave identically since _backoff clamps
        # internally to MAX_STEP to avoid an overflowing 2**step shift.
        min_value, max_value = 1.0, 100.0
        for _ in range(20):
            delay = _backoff(1000, min_value, max_value)
            self.assertGreaterEqual(delay, min_value)
            self.assertLessEqual(delay, max_value)

    def test_zero_step_stays_close_to_min(self):
        min_value, max_value = 1.0, 100.0
        for _ in range(20):
            delay = _backoff(0, min_value, max_value)
            self.assertGreaterEqual(delay, min_value)
            self.assertLessEqual(delay, min_value * 2)


class TestCodeHelpers(unittest.TestCase):
    def test_code_message_lowercases_and_replaces_underscores(self):
        self.assertEqual(_code_message(_ConnectingCode.NO_PING), "no ping")
        self.assertEqual(_code_message(_ErrorCode.TOKEN_EXPIRED), "token expired")

    def test_code_number_returns_int_value(self):
        self.assertEqual(_code_number(_ConnectingCode.TRANSPORT_CLOSED), 1)
        self.assertEqual(_code_number(_ErrorCode.TOKEN_EXPIRED), 109)

    def test_is_token_expired(self):
        self.assertTrue(_is_token_expired(_ErrorCode.TOKEN_EXPIRED.value))
        self.assertFalse(_is_token_expired(_ErrorCode.TIMEOUT.value))


class TestWaitForFuture(unittest.IsolatedAsyncioTestCase):
    async def test_returns_true_when_future_completes_first(self):
        future = asyncio.get_event_loop().create_future()
        future.set_result("done")
        done = await _wait_for_future(future, timeout=1)
        self.assertTrue(done)

    async def test_returns_false_on_timeout_without_cancelling_future(self):
        future = asyncio.get_event_loop().create_future()
        done = await _wait_for_future(future, timeout=0.01)
        self.assertFalse(done)
        self.assertFalse(future.cancelled())
        self.assertFalse(future.done())


if __name__ == "__main__":
    unittest.main()
