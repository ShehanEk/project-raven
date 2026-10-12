"""Persistent settings stored as JSON in the per-user config directory."""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from raven import DEFAULT_PORT
from raven import security


def config_dir() -> Path:
    override = os.environ.get("RAVEN_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "Raven"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Raven"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "raven"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def default_device_name() -> str:
    name = platform.node() or socket.gethostname() or "Computer"
    return name.removesuffix(".local")


@dataclass
class HostSettings:
    enabled: bool = False
    name: str = field(default_factory=default_device_name)
    port: int = DEFAULT_PORT
    password_salt: str = ""
    password_verifier: str = ""
    allow_input: bool = True
    allow_power: bool = True
    allow_files: bool = True
    advertise: bool = True

    @property
    def has_password(self) -> bool:
        return bool(self.password_salt and self.password_verifier)

    def set_password(self, password: str) -> None:
        salt = security.new_salt()
        self.password_salt = salt.hex()
        self.password_verifier = security.derive_key(password, salt).hex()


@dataclass
class RemoteHost:
    """A computer this app knows how to reach and control."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    address: str = ""
    port: int = DEFAULT_PORT
    os: str = ""
    mac: str = ""
    fingerprint: str = ""
    # PBKDF2-derived key (not the password) when the user chose "remember".
    saved_key: str = ""
    saved_salt: str = ""


@dataclass
class Settings:
    host: HostSettings = field(default_factory=HostSettings)
    remotes: list[RemoteHost] = field(default_factory=list)

    def find_remote(self, remote_id: str) -> RemoteHost | None:
        return next((r for r in self.remotes if r.id == remote_id), None)


class SettingsStore:
    """Thread-safe load/save of :class:`Settings`."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "settings.json"
        self._lock = threading.RLock()
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Settings()
        host_fields = HostSettings.__dataclass_fields__
        remote_fields = RemoteHost.__dataclass_fields__
        host = HostSettings(**{k: v for k, v in raw.get("host", {}).items() if k in host_fields})
        remotes = [
            RemoteHost(**{k: v for k, v in r.items() if k in remote_fields})
            for r in raw.get("remotes", [])
            if isinstance(r, dict)
        ]
        return Settings(host=host, remotes=remotes)

    def save(self) -> None:
        with self._lock:
            data = json.dumps(asdict(self.settings), indent=2)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
