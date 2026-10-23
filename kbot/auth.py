"""Kalshi request signing (docs.kalshi.com/getting_started/api_keys).

Signed message = timestamp_ms + METHOD + path_without_query.
Ed25519 signs directly. RSA uses PSS, SHA-256, MGF1(SHA-256), salt = 32 bytes.
"""
from __future__ import annotations

import base64
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


class Signer:
    def __init__(self, key_id: str, private_key):
        if not key_id:
            raise ValueError("missing API key id")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_file(cls, key_id: str, path: str) -> "Signer":
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"private key file not found: {p}")
        if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"{p} is readable by other users; run: chmod 600 {p}")
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        if not isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
            raise TypeError("private key must be Ed25519 or RSA")
        return cls(key_id, key)

    def sign(self, message: str) -> str:
        data = message.encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            sig = self._key.sign(data)
        else:
            sig = self._key.sign(
                data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, url_or_path: str, ts_ms: int | None = None) -> dict[str, str]:
        path = urlsplit(url_or_path).path
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method.upper() + path),
        }

    def __repr__(self) -> str:  # never print key material
        return f"Signer(key_id={self.key_id[:4]}…)"
