"""Пул HTTP/SOCKS-прокси для движка репортов.

Технология — по мотивам FastProxyPool: тянем бесплатные списки,
валидируем параллельно, крутим round-robin, битые помечаем.
Переписано под httpx (уже есть в зависимостях) и под Telethon.

Режимы (PROXY_MODE):
  off   — прокси выключены, всегда прямое соединение;
  auto  — пробуем прокси, не вышло — идём напрямую (задание не встанет);
  force — только через прокси, без рабочих проксей сессия пропускается.

Формат для Telethon: ("http", host, port) / ("socks5", host, port).
"""
import asyncio
import logging
import random
import re
import time

import httpx

log = logging.getLogger("darkbot.proxy")

_IP_PORT_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}")

VALIDATE_URL = "http://httpbin.org/ip"
VALIDATE_TIMEOUT = 6
VALIDATE_CONCURRENCY = 50
MIN_WORKING = 5


def _extract(text: str, scheme: str = "http") -> list[str]:
    return [f"{scheme}://{m}" for m in _IP_PORT_RE.findall(text or "")]


async def _fetch_source(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        r = await client.get(url, timeout=15)
        if r.status_code == 200:
            return _extract(r.text)
    except Exception as e:
        log.debug("proxy source failed [%s]: %s", url, e)
    return []


async def _check_one(
    sem: asyncio.Semaphore, proxy: str
) -> str | None:
    async with sem:
        try:
            async with httpx.AsyncClient(
                proxy=proxy, timeout=VALIDATE_TIMEOUT
            ) as client:
                r = await client.get(VALIDATE_URL)
                if r.status_code in (200, 301, 302):
                    return proxy
        except Exception:
            pass
    return None


async def validate_proxies(proxies: list[str]) -> list[str]:
    if not proxies:
        return []
    sem = asyncio.Semaphore(VALIDATE_CONCURRENCY)
    tasks = [asyncio.create_task(_check_one(sem, p)) for p in proxies]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    working = [r for r in results if isinstance(r, str)]
    log.info("proxy validate: %d -> %d working", len(proxies), len(working))
    return working


def parse_proxy_tuple(proxy_url: str) -> tuple | None:
    """'http://1.2.3.4:8080' -> ('http', '1.2.3.4', 8080)."""
    try:
        scheme, rest = proxy_url.split("://", 1)
        host, port = rest.rsplit(":", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "socks5", "socks4"):
            return None
        return (scheme, host, int(port))
    except (ValueError, AttributeError):
        return None


class ProxyPool:
    def __init__(
        self,
        mode: str = "auto",
        sources: list[str] | None = None,
        refresh_sec: int = 900,
    ):
        self.mode = mode if mode in ("off", "auto", "force") else "auto"
        self.sources = [s for s in (sources or []) if s]
        self.refresh_sec = max(60, int(refresh_sec or 900))
        self._working: list[str] = []
        self._bad: set[str] = set()
        self._idx = 0
        self._last_refresh = 0.0
        self._refreshing = False
        self._lock = asyncio.Lock()

    def enabled(self) -> bool:
        return self.mode != "off"

    async def initialize(self) -> None:
        if self.mode == "off" or not self.sources:
            return
        asyncio.create_task(self._do_refresh())

    async def get_proxy(self) -> str | None:
        if self.mode == "off":
            return None
        await self._trigger_refresh_if_needed()
        async with self._lock:
            if not self._working:
                if self.mode == "force":
                    raise RuntimeError("proxy mode=force, no working proxies")
                return None
            proxy = self._working[self._idx % len(self._working)]
            self._idx = (self._idx + 1) % len(self._working)
            return proxy

    def mark_bad(self, proxy: str | None) -> None:
        if not proxy:
            return
        self._bad.add(proxy)
        try:
            self._working.remove(proxy)
        except ValueError:
            pass

    def mark_good(self, proxy: str | None) -> None:
        if not proxy or proxy in self._bad:
            return
        if proxy in self._working:
            self._working.remove(proxy)
        self._working.insert(0, proxy)

    @property
    def stats(self) -> dict:
        return {
            "working": len(self._working),
            "bad": len(self._bad),
            "mode": self.mode,
        }

    async def _trigger_refresh_if_needed(self) -> None:
        now = time.monotonic()
        low = len(self._working) < MIN_WORKING
        stale = (now - self._last_refresh) >= self.refresh_sec
        if (low or stale) and not self._refreshing:
            asyncio.create_task(self._do_refresh())

    async def _do_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            async with httpx.AsyncClient() as client:
                tasks = [_fetch_source(client, u) for u in self.sources]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            seen, raw = set(), []
            for res in results:
                if not isinstance(res, list):
                    continue
                for p in res:
                    if p not in seen and p not in self._bad:
                        seen.add(p)
                        raw.append(p)
            working = await validate_proxies(raw)
            async with self._lock:
                # Пустой рефреш не должен убивать старый рабочий пул.
                if working:
                    self._working = [p for p in working if p not in self._bad]
                    random.shuffle(self._working)
                    self._idx = 0
                self._last_refresh = time.monotonic()
            log.info("proxy refresh done: %d working", len(self._working))
        except Exception as e:
            log.error("proxy refresh error: %s", e)
        finally:
            self._refreshing = False
