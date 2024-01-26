from enum import Enum
import random

from centrifuge.codes import _ErrorCode


def _backoff(step: int, min_value: float, max_value: float):
    """
    Implements exponential backoff with jitter.
    Using full jitter technique - see https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    """
    if step > 31:
        step = 31
    interval = random.uniform(0, min(max_value, min_value * 2 ** step))
    return min(max_value, min_value + interval)


def _code_message(code: Enum):
    return str(code.name).lower().replace("_", " ")


def _code_number(code: Enum):
    return int(code.value)


def _is_token_expired(code: int):
    return code == _ErrorCode.TOKEN_EXPIRED.value
