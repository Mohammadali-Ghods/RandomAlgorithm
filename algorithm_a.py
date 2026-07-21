"""AlgorithmA

Given two decimal bounds, return 10 cryptographically-random values inside the
range [low, high], sorted from low to high.

Randomness comes from the operating system's CSPRNG via the `secrets` module,
the same source used for tokens and keys. Its output is not reproducible and
cannot be predicted or reverse-engineered from previous outputs, so no one can
"find" the sequence the algorithm will produce.
"""

from __future__ import annotations

import secrets
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import List, Union

Number = Union[int, float, str, Decimal]

# Bits of entropy used per sampled value. 256 bits gives resolution far finer
# than any float and removes modulo bias for any practical range.
_ENTROPY_BITS = 256
_ENTROPY_DEN = 1 << _ENTROPY_BITS  # 2**256

# Output is rounded to at most this many decimal places.
_DECIMALS = 5
_QUANTUM = Decimal(1).scaleb(-_DECIMALS)  # 0.00001

# Give Decimal enough precision to represent the sampled fractions faithfully.
getcontext().prec = 90


def _uniform(low: Decimal, high: Decimal) -> Decimal:
    """Draw one uniform Decimal in [low, high] from the OS CSPRNG,
    rounded to at most _DECIMALS decimal places."""
    # secrets.randbelow returns an unbiased int in [0, _ENTROPY_DEN).
    fraction = Decimal(secrets.randbelow(_ENTROPY_DEN)) / Decimal(_ENTROPY_DEN)
    value = low + (high - low) * fraction
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def algorithm_a(a: Number, b: Number, count: int = 10) -> List[Decimal]:
    """Return `count` (default 10) CSPRNG-random values in [min(a,b), max(a,b)],
    sorted ascending.

    The two inputs are the range bounds and may be passed in any order. Results
    are Decimals to preserve the precision of decimal inputs; the spacing between
    values is left entirely to chance — they may cluster or spread out.
    """
    if count < 0:
        raise ValueError("count must be non-negative")

    low, high = Decimal(str(a)), Decimal(str(b))
    if low > high:
        low, high = high, low

    values = [_uniform(low, high) for _ in range(count)]
    values.sort()
    return values


if __name__ == "__main__":
    for v in algorithm_a(1.5, 9.75):
        print(v)
