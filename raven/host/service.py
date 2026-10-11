"""Host server plus LAN advertisement, started and stopped together."""

from __future__ import annotations

import logging

from raven.config import SettingsStore, config_dir
from raven.discovery import Advertiser
from raven.host.server import EventCallback, HostServer

log = logging.getLogger(__name__)


class HostService:
    def __init__(self, store: SettingsStore, on_event: EventCallback | None = None, **server_kwargs):
        self.store = store
        self.server = HostServer(store, config_dir(), on_event=on_event, **server_kwargs)
        self.advertiser = Advertiser()

    @property
    def fingerprint(self) -> str:
        return self.server.fingerprint

    @property
    def running(self) -> bool:
        return self.server.running

    async def start(self) -> None:
        await self.server.start()
        if self.store.settings.host.advertise:
            await self.advertiser.start(self.store.settings.host.name, self.server.port, self.fingerprint)

    async def stop(self) -> None:
        await self.advertiser.stop()
        await self.server.disconnect_all()
        await self.server.stop()

    async def restart(self) -> None:
        await self.stop()
        await self.start()
