"""TLS certificates, password-proof authentication and LAN address checks.

Security model
--------------
* Every host owns a self-signed TLS certificate generated on first run.
  Controllers pin the certificate's SHA-256 fingerprint the first time they
  connect (trust on first use) and refuse to talk to a host whose certificate
  later changes.
* The password never crosses the wire. The host sends a random nonce and the
  controller answers with HMAC(PBKDF2(password, salt), nonce + fingerprint).
  Mixing the certificate fingerprint into the proof binds it to this TLS
  channel, so a man in the middle cannot relay it.
* Hosts only accept connections from private, loopback or link-local
  addresses, so the service is unusable from the internet even if a port is
  forwarded by mistake.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import ssl
import sys
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PBKDF2_ITERATIONS = 200_000
# A host chooses the iteration count, so controllers refuse anything that
# would make their proof cheap to crack (too low) or hang them (too high).
MIN_PBKDF2_ITERATIONS = PBKDF2_ITERATIONS
MAX_PBKDF2_ITERATIONS = 5_000_000
AUTH_CONTEXT = b"raven-auth-v1|"


# ---------------------------------------------------------------- passwords

def new_salt() -> bytes:
    return secrets.token_bytes(16)


def new_nonce() -> bytes:
    return secrets.token_bytes(32)


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def make_proof(key: bytes, nonce: bytes, fingerprint: str) -> bytes:
    return hmac.new(key, AUTH_CONTEXT + nonce + fingerprint.encode("ascii"), hashlib.sha256).digest()


def verify_proof(key: bytes, nonce: bytes, fingerprint: str, proof: bytes) -> bool:
    return hmac.compare_digest(make_proof(key, nonce, fingerprint), proof)


class AuthRateLimiter:
    """Limits password guessing.

    A login attempt counts against its address from the moment it starts,
    not when the wrong answer comes back, so firing many guesses in parallel
    can't slip past the limit. A host-wide budget also stops an attacker who
    rotates through many LAN addresses.
    """

    def __init__(self, max_failures: int = 5, lockout_seconds: float = 60.0, max_global_failures: int = 30):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.max_global_failures = max_global_failures
        self._failures: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._pending: dict[str, int] = {}

    def _recent(self, address: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(address, []) if now - t < self.lockout_seconds]
        self._failures[address] = recent
        return recent

    def _global_recent(self, now: float) -> list[float]:
        self._global = [t for t in self._global if now - t < self.lockout_seconds]
        return self._global

    def is_locked(self, address: str) -> bool:
        now = time.monotonic()
        if len(self._recent(address, now)) + self._pending.get(address, 0) >= self.max_failures:
            return True
        return len(self._global_recent(now)) + sum(self._pending.values()) >= self.max_global_failures

    def begin(self, address: str) -> bool:
        """Reserve an attempt for ``address``; False means it's locked out."""
        if self.is_locked(address):
            return False
        self._pending[address] = self._pending.get(address, 0) + 1
        return True

    def finish(self, address: str, ok: bool | None) -> None:
        """Release a reserved attempt: True = logged in, False = wrong password,
        None = the client left without answering (no guess was made)."""
        left = self._pending.get(address, 0) - 1
        if left > 0:
            self._pending[address] = left
        else:
            self._pending.pop(address, None)
        if ok:
            self._failures.pop(address, None)
        elif ok is False:
            now = time.monotonic()
            self._recent(address, now).append(now)
            self._global_recent(now).append(now)

    # Kept for callers that record outcomes without reserving first.
    def record_failure(self, address: str) -> None:
        now = time.monotonic()
        self._recent(address, now).append(now)
        self._global_recent(now).append(now)

    def record_success(self, address: str) -> None:
        self._failures.pop(address, None)


_INVISIBLE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]"
)


def strip_invisible(text: str) -> str:
    """Remove control and text-direction characters (e.g. U+202E, which can
    make "gnp.exe" display as "exe.png")."""
    return _INVISIBLE.sub("", text)


def clean_display_name(value: object, max_length: int = 64) -> str:
    """Make a name from another computer safe to show: no control or
    text-direction characters (which can disguise text), trimmed and capped."""
    text = strip_invisible(re.sub(r"\s+", " ", str(value or "")))
    return " ".join(text.split())[:max_length]


# ---------------------------------------------------------------- network

def is_lan_address(address: str) -> bool:
    """True for loopback, RFC 1918 / ULA private and link-local addresses."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _PRIVATE_V4)
    return ip in _ULA_V6


_PRIVATE_V4 = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]
_ULA_V6 = ipaddress.ip_network("fc00::/7")


# ---------------------------------------------------------------- certificates

def fingerprint_der(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def short_fingerprint(fingerprint: str) -> str:
    """First 8 bytes, which is what the UI shows people to compare."""
    return fingerprint[:23]


def ensure_certificate(directory: Path, common_name: str = "Raven host") -> tuple[Path, Path, str]:
    """Return (cert_path, key_path, fingerprint), generating them if needed."""
    cert_path = directory / "host-cert.pem"
    key_path = directory / "host-key.pem"
    if not (cert_path.exists() and key_path.exists()):
        _generate_certificate(cert_path, key_path, common_name)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert_path, key_path, fingerprint_der(cert.public_bytes(serialization.Encoding.DER))


def _generate_certificate(cert_path: Path, key_path: Path, common_name: str) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path.write_bytes(key_bytes)
    if sys.platform != "win32":
        os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    # Certificates are self-signed; identity is checked by pinning the
    # fingerprint after the handshake (see RemoteSession.connect).
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
