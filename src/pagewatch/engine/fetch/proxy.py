"""Which proxy a check goes through, and the login it needs (spec: Politeness and network).

A bookmark's own proxy wins over the global one. The address is stored without credentials:
the proxy's user name sits beside it and the password is a secret-store entry, read here at
check time and never written anywhere (not the database, the log, or an error message).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from pagewatch.engine.secrets import SecretStore
from pagewatch.models import FetchConfig, Settings


class ProxyError(RuntimeError):
    """The proxy cannot be used (its password is not in the secret store)."""


@dataclass(frozen=True, slots=True)
class ProxySpec:
    server: str  # scheme://host:port, no credentials
    username: str | None = None
    password: str | None = None

    @property
    def scheme(self) -> str:
        return urlsplit(self.server).scheme.lower()

    @property
    def is_socks(self) -> bool:
        return self.scheme.startswith("socks")

    @property
    def label(self) -> str:
        """Safe to log and show: the address only."""
        return self.server

    def url(self) -> str:
        """The address with its login inside, for httpx. Keep it out of logs."""
        if not self.username:
            return self.server
        parts = urlsplit(self.server)
        login = quote(self.username, safe="")
        if self.password is not None:
            login += ":" + quote(self.password, safe="")
        return f"{parts.scheme}://{login}@{parts.netloc}"

    def playwright(self) -> dict[str, str]:
        out = {"server": self.server}
        if self.username:
            out["username"] = self.username
            out["password"] = self.password or ""
        return out


async def resolve_proxy(
    cfg: FetchConfig, settings: Settings, secrets: SecretStore
) -> ProxySpec | None:
    """The proxy for this check, with its password looked up (off the event loop: the
    credential store can block)."""
    if cfg.proxy:
        server, user, key = cfg.proxy, cfg.proxy_username, cfg.proxy_secret_key
    elif settings.global_proxy:
        server = settings.global_proxy
        user, key = settings.global_proxy_username, settings.global_proxy_secret_key
    else:
        return None
    if not user or not key:
        return ProxySpec(server)
    password = await asyncio.to_thread(secrets.get, key)
    if password is None:
        raise ProxyError(f"no stored secret named {key!r} for the proxy login")
    return ProxySpec(server, user, password)
