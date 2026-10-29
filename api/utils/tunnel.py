"""Utility for getting the cloudflared tunnel URL at runtime."""

import asyncio
import re
from time import monotonic
from typing import Optional

import aiohttp
from loguru import logger


class TunnelURLCache:
    """Optional tunnel metadata for health responses; never probe on a read.

    Each API worker refreshes independently of request traffic. Failed discovery
    clears the URL, and expiry also prevents a stalled refresh from advertising
    an old tunnel indefinitely. Call routing still uses live discovery below.
    """

    REFRESH_INTERVAL = 30.0
    MAX_AGE = 60.0

    def __init__(self):
        self._url: str | None = None
        self._expires_at = 0.0
        self._task: asyncio.Task | None = None

    def get_url(self) -> str | None:
        return self._url if monotonic() < self._expires_at else None

    def start(self) -> None:
        """Start once per worker from lifespan, without awaiting discovery."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh(), name="tunnel-url-cache")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._url = None
        self._expires_at = 0.0

    async def _refresh(self) -> None:
        while True:
            try:
                urls = await TunnelURLProvider.get_tunnel_urls()
                self._url = urls[0] if urls else None
            except (ValueError, OSError, aiohttp.ClientError):
                # Discovery already reports its diagnostic. An absent tunnel is
                # normal for local deployments and must not affect API health.
                self._url = None
            self._expires_at = monotonic() + self.MAX_AGE
            await asyncio.sleep(self.REFRESH_INTERVAL)


tunnel_url_cache = TunnelURLCache()


class TunnelURLProvider:
    """Provider for getting tunnel URLs from cloudflared service."""

    @classmethod
    async def get_tunnel_urls(cls) -> tuple[str, str]:
        """
        Get the tunnel URLs for external access.

        Returns:
            tuple[str, str]: (https_url, wss_url) - Both URLs include full protocol

        Raises:
            ValueError: If no tunnel URL can be determined
        """

        try:
            # Try to get URL from cloudflared metrics
            urls = await cls._get_cloudflared_urls()
            if urls:
                return urls
        except Exception as e:
            logger.warning(f"Failed to get tunnel URL from cloudflared: {e}")

        raise ValueError(
            "No tunnel URL available. Please set BACKEND_API_ENDPOINT environment "
            "variable or ensure cloudflared service is running."
        )

    @classmethod
    async def _get_cloudflared_urls(cls) -> Optional[tuple[str, str]]:
        """
        Query cloudflared metrics endpoint to get the tunnel URLs.

        Returns:
            Optional[tuple[str, str]]: (https_url, wss_url) with full protocols, or None if not found
        """
        try:
            # Try to connect to cloudflared metrics endpoint
            # The service name in docker-compose is 'cloudflared'
            metrics_url = "http://cloudflared:2000/metrics"

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    metrics_url, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            f"Cloudflared metrics returned status {response.status}"
                        )
                        return None

                    text = await response.text()

                    # Look for the tunnel URL in metrics
                    # Cloudflared exposes this in the userHostname metric
                    match = re.search(r'userHostname="([^"]+)"', text)
                    if match:
                        hostname = match.group(1)
                        # Remove https:// or wss:// if present
                        hostname = hostname.replace("https://", "").replace(
                            "wss://", ""
                        )
                        return "https://" + hostname, "wss://" + hostname

                    # Alternative: Look for trycloudflare.com domain
                    match = re.search(r"([a-z0-9-]+\.trycloudflare\.com)", text)
                    if match:
                        hostname = match.group(1)
                        hostname = hostname.replace("https://", "").replace(
                            "wss://", ""
                        )
                        return f"https://{hostname}", f"wss://{hostname}"

                    logger.warning("Could not find tunnel URL in cloudflared metrics")
                    return None

        except asyncio.TimeoutError:
            logger.warning("Timeout connecting to cloudflared metrics endpoint")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"Error connecting to cloudflared: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting cloudflared URL: {e}")
            return None
