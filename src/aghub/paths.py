"""Glob matching and an approximate overlap test for path claims.

Claims are advisory, so the overlap test only needs to be conservative in the
common cases: `src/web/**` vs `src/api/**` must not collide, while
`src/**` must collide with both.
"""

import re

_CACHE = {}


def _compile(pattern):
    regex = _CACHE.get(pattern)
    if regex is not None:
        return regex
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(char))
        i += 1
    regex = re.compile("^" + "".join(out) + "$")
    _CACHE[pattern] = regex
    return regex


def matches(pattern, path):
    return bool(_compile(pattern).match(path.strip("/")))


def _literal_prefix(pattern):
    cut = len(pattern)
    for index, char in enumerate(pattern):
        if char in "*?[":
            cut = index
            break
    return pattern[:cut].rstrip("/")


def _within(prefix, other):
    return prefix == other or prefix.startswith(other + "/")


def overlaps(left, right):
    a = _literal_prefix(left)
    b = _literal_prefix(right)
    if not a or not b:
        return True
    return _within(a, b) or _within(b, a)
