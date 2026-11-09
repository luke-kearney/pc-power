"""Client for the PC Power agent."""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import aiohttp


class AgentError(Exception):
    """Agent unreachable or returned garbage."""


class AgentAuthError(AgentError):
    """Token rejected."""


class AgentClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, timeout: float, json: dict | None = None) -> dict:
        try:
            async with self._session.request(
                method, self._base + path, headers=self._headers, json=json,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    raise AgentAuthError("invalid token")
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise AgentError(data.get("error") if isinstance(data, dict) else f"HTTP {resp.status}")
        except AgentError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise AgentError(str(err) or type(err).__name__) from err
        if not isinstance(data, dict):
            raise AgentError("unexpected response")
        return data

    async def metrics(self, timeout: float = 5.0) -> dict:
        data = await self._request("GET", "/v1/metrics", timeout)
        if data.get("api") != 1:
            raise AgentError("unsupported agent API")
        return data

    async def power(self, action: str) -> dict:
        return await self._request("POST", f"/v1/power/{action}", 10.0)

    async def lock(self) -> dict:
        return await self._request("POST", "/v1/session/lock", 10.0)

    async def set_brightness(self, bus: int, value: int) -> dict:
        return await self._request("POST", f"/v1/monitors/{bus}/brightness", 20.0, {"value": value})


def send_magic_packet(macs: list[str], ipv4_cidrs: list[str], port: int = 9) -> int:
    """Blocking: send WoL magic packets to the limited broadcast and each known subnet broadcast."""
    targets = {"255.255.255.255"}
    for cidr in ipv4_cidrs:
        try:
            targets.add(str(ipaddress.IPv4Interface(cidr).network.broadcast_address))
        except ValueError:
            continue
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
            if len(raw) != 6:
                continue
            packet = b"\xff" * 6 + raw * 16
            for target in targets:
                try:
                    sock.sendto(packet, (target, port))
                    sent += 1
                except OSError:
                    continue
    return sent
