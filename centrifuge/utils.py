import asyncio
import random
from enum import Enum

from centrifuge.codes import _ErrorCode

MAX_STEP = 31


def _backoff(step: int, min_value: float, max_value: float) -> float:
    """Implements exponential backoff with jitter.
    Using full jitter technique - see
    https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    """
    if step > MAX_STEP:
        step = MAX_STEP
    interval = random.uniform(0, min(max_value, min_value * 2**step))  # noqa: S311
    return min(max_value, min_value + interval)


def _code_message(code: Enum) -> str:
    return str(code.name).lower().replace("_", " ")


def _code_number(code: Enum) -> int:
    return int(code.value)


def _is_token_expired(code: int) -> bool:
    return code == _ErrorCode.TOKEN_EXPIRED.value


async def _wait_for_future(
    future: asyncio.Future,
    timeout: float,
) -> bool:
    """asyncio.wait_for() cancels the future if the timeout expires. This function does not."""
    future_task = asyncio.ensure_future(future)
    # Create a task that completes after a timeout
    timeout_task = asyncio.ensure_future(asyncio.sleep(timeout))

    # Wait for either the future to complete or the timeout
    done, pending = await asyncio.wait(
        {future_task, timeout_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel the timeout task if it's still pending (i.e., the future completed first)
    if timeout_task in pending:
        timeout_task.cancel()

    # Check if the future is done
    return future_task in done
