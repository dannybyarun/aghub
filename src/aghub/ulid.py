"""Minimal ULID generator: 48-bit millisecond time + 80 bits of randomness.

ULIDs sort lexicographically by creation time, which is what the board relies on
for ordering events and handoffs without a shared counter.
"""

import os
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value, length):
    out = []
    for _ in range(length):
        out.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_ulid():
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(millis, 10) + _encode(randomness, 16)
