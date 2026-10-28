from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlparse
import httpx


class EgressBlockedError(Exception):
    """Raised when an outbound request violates the egress policy."""
    pass


class EgressGuard:
    """
    Zero-Trust HTTP client wrapper.
    Enforces strict domain whitelisting, blocks SSRF/loopback access,
    disables redirects (follow_redirects=False), and tracks outbound traffic size.
    """

    def __init__(
        self,
        whitelist_domains: List[str],
        timeout_seconds: float = 60.0,
        max_request_bytes: int = 10485760,  # 10MB
        block_private_ips: bool = True,
    ):
        self.whitelist_domains = [d.lower().strip() for d in whitelist_domains]
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_request_bytes = max_request_bytes
        self.block_private_ips = block_private_ips

        # Internal client with strict settings
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,  # CRITICAL: Prevent SSRF via redirection
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=100),
        )

        # Internal metrics
        self.total_outbound_bytes: int = 0
        self.total_inbound_bytes: int = 0
        self.total_requests: int = 0

    def _is_private_ip(self, ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            )
        except ValueError:
            return False

    def validate_url(self, url: str) -> None:
        """Validate destination host against whitelist and SSRF rules."""
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            raise EgressBlockedError(f"Malformed destination URL: {url}")

        if parsed.scheme not in ("http", "https"):
            raise EgressBlockedError(f"Invalid URL scheme: {parsed.scheme}")

        # Check domain whitelist
        matched = False
        for allowed in self.whitelist_domains:
            if hostname == allowed or hostname.endswith("." + allowed):
                matched = True
                break

        if not matched:
            raise EgressBlockedError(
                f"Destination domain '{hostname}' is not in the egress whitelist! (Zero-Trust Block)"
            )

        # Block private IPs and loopbacks to prevent SSRF
        if self.block_private_ips:
            if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                raise EgressBlockedError(f"Access to localhost/loopback '{hostname}' is strictly forbidden.")

            if self._is_private_ip(hostname):
                raise EgressBlockedError(f"Direct IP access to private/internal subnet '{hostname}' is blocked.")

    async def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        self.validate_url(url)

        # Calculate payload size
        payload_bytes = 0
        if content:
            payload_bytes = len(content)
        elif json_data is not None:
            import json
            payload_bytes = len(json.dumps(json_data).encode("utf-8"))

        if payload_bytes > self.max_request_bytes:
            raise EgressBlockedError(
                f"Payload size {payload_bytes} bytes exceeds maximum allowed limit {self.max_request_bytes} bytes"
            )

        self.total_outbound_bytes += payload_bytes
        self.total_requests += 1

        start_time = time.perf_counter()
        response = await self._client.post(
            url,
            headers=headers,
            json=json_data,
            content=content,
        )
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        self.total_inbound_bytes += len(response.content)
        # Attach latency attribute for caller
        response.latency_ms = latency_ms  # type: ignore[attr-defined]
        return response

    async def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        self.validate_url(url)
        self.total_requests += 1

        start_time = time.perf_counter()
        response = await self._client.get(
            url,
            headers=headers,
            params=params,
        )
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        self.total_inbound_bytes += len(response.content)
        response.latency_ms = latency_ms  # type: ignore[attr-defined]
        return response

    async def stream_post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
    ) -> AsyncIterator[bytes]:
        """
        Stream response data using Server-Sent Events (SSE) while keeping egress guards active.
        """
        self.validate_url(url)
        self.total_requests += 1

        req = self._client.build_request("POST", url, headers=headers, json=json_data)
        response = await self._client.send(req, stream=True)

        try:
            if response.status_code >= 400:
                body = await response.aread()
                raise httpx.HTTPStatusError(
                    f"Provider returned HTTP {response.status_code}: {body.decode('utf-8', errors='ignore')}",
                    request=req,
                    response=response,
                )

            async for chunk in response.aiter_bytes():
                self.total_inbound_bytes += len(chunk)
                yield chunk
        finally:
            await response.aclose()

    async def close(self) -> None:
        await self._client.aclose()
