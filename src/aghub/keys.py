"""Small, dependency-free Ed25519 implementation for local agent keys.

This follows the compact reference algorithm from RFC 8032. It is intended for
signing local eval scorecards, not for high-volume or hostile-input crypto. The
private key never leaves the clone; only the public key is published on the
board.
"""

import hashlib
import secrets

B = 256
Q = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493


def _sha512(data):
    return hashlib.sha512(data).digest()


def _expmod(base, exponent, modulus):
    return pow(base, exponent, modulus)


def _inv(value):
    return _expmod(value, Q - 2, Q)


_D = -121665 * _inv(121666) % Q
_I = _expmod(2, (Q - 1) // 4, Q)


def _recover_x(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = _expmod(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * _I) % Q
    if x % 2 != 0:
        x = Q - x
    return x


_BY = 4 * _inv(5) % Q
_BX = _recover_x(_BY)
_IDENTITY = (0, 1, 1, 0)
_BASE = (_BX % Q, _BY % Q, 1, (_BX * _BY) % Q)


def _add(point, other):
    """Add two points in extended twisted Edwards coordinates."""
    x1, y1, z1, t1 = point
    x2, y2, z2, t2 = other
    a = ((y1 - x1) * (y2 - x2)) % Q
    b = ((y1 + x1) * (y2 + x2)) % Q
    c = (2 * _D * t1 * t2) % Q
    d = (2 * z1 * z2) % Q
    e = (b - a) % Q
    f = (d - c) % Q
    g = (d + c) % Q
    h = (b + a) % Q
    return ((e * f) % Q, (g * h) % Q, (f * g) % Q, (e * h) % Q)


def _double(point):
    x1, y1, z1, _ = point
    a = (x1 * x1) % Q
    b = (y1 * y1) % Q
    c = (2 * z1 * z1) % Q
    d = (-a) % Q
    e = ((x1 + y1) * (x1 + y1) - a - b) % Q
    g = (d + b) % Q
    f = (g - c) % Q
    h = (d - b) % Q
    return ((e * f) % Q, (g * h) % Q, (f * g) % Q, (e * h) % Q)


def _scalar_mult(point, scalar):
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _double(addend)
        scalar >>= 1
    return result


def _same_point(point, other):
    x1, y1, z1, _ = point
    x2, y2, z2, _ = other
    return (x1 * z2) % Q == (x2 * z1) % Q and (y1 * z2) % Q == (y2 * z1) % Q


def _encode_extended(point):
    x, y, z, _ = point
    inverse = _inv(z)
    return _encode_point(((x * inverse) % Q, (y * inverse) % Q))


def _encode_int(value):
    return value.to_bytes(32, "little")


def _encode_point(point):
    x, y = point
    data = bytearray(y.to_bytes(32, "little"))
    data[31] = (data[31] & 0x7F) | ((x & 1) << 7)
    return bytes(data)


def _decode_point(data):
    if len(data) != 32:
        raise ValueError("invalid point length")
    raw = bytearray(data)
    sign = raw[31] >> 7
    raw[31] &= 0x7F
    y = int.from_bytes(raw, "little")
    if y >= Q:
        raise ValueError("invalid point")
    x = _recover_x(y)
    if (-x * x + y * y - 1 - _D * x * x * y * y) % Q != 0:
        raise ValueError("point is not on the curve")
    if x & 1 != sign:
        x = Q - x
    return (x, y, 1, (x * y) % Q)


def _bit(data, index):
    return (data[index // 8] >> (index % 8)) & 1


def _secret_scalar(seed):
    digest = _sha512(seed)
    scalar = 2 ** (B - 2)
    scalar += sum(2**i * _bit(digest, i) for i in range(3, B - 2))
    return digest, scalar


def _hash_int(data):
    digest = _sha512(data)
    return sum(2**i * _bit(digest, i) for i in range(2 * B))


def public_key(seed):
    _, scalar = _secret_scalar(seed)
    return _encode_extended(_scalar_mult(_BASE, scalar))


def generate():
    """Return (private seed hex, public key hex)."""
    seed = secrets.token_bytes(32)
    return seed.hex(), public_key(seed).hex()


def key_id(public_hex):
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


def sign(seed_hex, message):
    seed = bytes.fromhex(seed_hex)
    digest, scalar = _secret_scalar(seed)
    prefix = digest[32:]
    nonce = _hash_int(prefix + message)
    point = _encode_extended(_scalar_mult(_BASE, nonce))
    public = _encode_extended(_scalar_mult(_BASE, scalar))
    challenge = _hash_int(point + public + message)
    signature = (nonce + challenge * scalar) % L
    return (point + _encode_int(signature)).hex()


def verify(public_hex, message, signature_hex):
    try:
        public = bytes.fromhex(public_hex)
        signature = bytes.fromhex(signature_hex)
        if len(public) != 32 or len(signature) != 64:
            return False
        point = _decode_point(signature[:32])
        scalar = int.from_bytes(signature[32:], "little")
        if scalar >= L:
            return False
        challenge = _hash_int(signature[:32] + public + message)
        return _same_point(
            _scalar_mult(_BASE, scalar),
            _add(point, _scalar_mult(_decode_point(public), challenge)),
        )
    except (TypeError, ValueError):
        return False
