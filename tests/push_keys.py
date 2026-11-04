"""Structurally valid WebPush subscription keys for tests.

``P256DH`` is a base64url 65-byte uncompressed P-256 point (0x04 prefix; not a
real curve point, which validation does not need), ``AUTH`` 16 base64url bytes.
"""

import base64

P256DH = base64.urlsafe_b64encode(b"\x04" + bytes(range(1, 65))).decode().rstrip("=")
AUTH = base64.urlsafe_b64encode(bytes(range(16))).decode().rstrip("=")
