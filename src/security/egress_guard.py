from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import anyio
import httpcore
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._exceptions import ConnectError, ConnectTimeout, map_exceptions
import httpx


class EgressBlockedError(Exception):
    """Raised when an outbound request violates the egress policy or DNS verification."""
    pass


class PinnedNetworkBackend(AnyIOBackend):
    """
    Network backend that connects directly to pre-verified public IP addresses
    while preserving original hostnames for TLS SNI and HTTP Host verification,
    preventing Time-of-Check to Time-of-Use DNS rebinding attacks.
    """

    def __init__(self, pinned_map: Optional[Dict[str, str]] = None, fail_closed: bool = False):
        super().__init__()
        self.pinned_map: Dict[str, str] = pinned_map if pinned_map is not None else {}
        self.fail_closed: bool = fail_closed
        self.last_connect_target: Optional[str] = None

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Any] = None,
    ) -> httpcore.AsyncNetworkStream:
        if self.fail_closed and host not in self.pinned_map:
            raise ConnectError(f"DNS pinning policy failure: domain '{host}' has no verified pinned IP address")

        target_ip = self.pinned_map.get(host, host)
        self.last_connect_target = target_ip

        if socket_options is None:
            socket_options = []

        exc_map = {
            TimeoutError: ConnectTimeout,
            OSError: ConnectError,
            anyio.BrokenResourceError: ConnectError,
        }
        with map_exceptions(exc_map):
            with anyio.fail_after(timeout):
                stream = await anyio.connect_tcp(
                    remote_host=target_ip,
                    remote_port=port,
                    local_host=local_address,
                )
                for opt in socket_options:
                    try:
                        stream._raw_socket.setsockopt(*opt)
                    except Exception:
                        pass
        return AnyIOStream(stream)


class EgressGuard:
    """
    Zero-Trust HTTP client wrapper with enforced DNS verification, socket address pinning,
    payload size gating for both JSON & streaming, follow_redirects=False, and disabled env proxies.
    """

    def __init__(
        self,
        whitelist_domains: List[str],
        timeout_seconds: float = 60.0,
        max_request_bytes: int = 10485760,  # 10MB
        max_response_bytes: int = 16777216,  # 16MB
        block_private_ips: bool = True,
        allow_http: bool = False,
        dns_resolver: Optional[Any] = None,
    ):
        self.whitelist_domains = [d.lower().strip() for d in whitelist_domains]
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.block_private_ips = block_private_ips
        self.allow_http = allow_http
        self._dns_resolver = dns_resolver

        self._pinned_hosts: Dict[str, str] = {}
        self._network_backend = PinnedNetworkBackend(self._pinned_hosts, fail_closed=True)

        # Internal connection pool with pinned network backend
        pool = httpcore.AsyncConnectionPool(
            network_backend=self._network_backend,
            max_connections=100,
            max_keepalive_connections=50,
        )
        transport = httpx.AsyncHTTPTransport()
        transport._pool = pool

        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self.timeout,
            follow_redirects=False,  # CRITICAL: Prevent SSRF via redirection
            trust_env=False,         # CRITICAL: Ignore HTTP_PROXY/HTTPS_PROXY from environment
        )

        # Metrics
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
                or ip.is_unspecified
            )
        except ValueError:
            return False

    def validate_url(self, url: str) -> Tuple[str, int, str]:
        """Validate destination host, scheme, userinfo, port, and whitelist."""
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            raise EgressBlockedError(f"Malformed destination URL (missing hostname): {url}")

        # 1. Scheme check
        if parsed.scheme == "http" and not self.allow_http:
            raise EgressBlockedError("HTTP scheme is forbidden. Only HTTPS is allowed by Zero-Trust egress policy.")
        if parsed.scheme not in ("http", "https"):
            raise EgressBlockedError(f"Invalid URL scheme: '{parsed.scheme}'. Only HTTPS is permitted.")

        # 2. Userinfo check
        if parsed.username or parsed.password or "@" in parsed.netloc.split(":")[0]:
            raise EgressBlockedError("URLs containing userinfo are strictly blocked to prevent credential confusion.")

        # 3. Port check
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https" and port != 443:
            raise EgressBlockedError(f"Disallowed port {port} for HTTPS. Only port 443 is permitted.")
        if parsed.scheme == "http" and port != 80:
            raise EgressBlockedError(f"Disallowed port {port} for HTTP. Only port 80 is permitted.")

        # 4. Domain Whitelist (exact match unless explicitly wildcarded with '*.')
        matched = False
        for allowed in self.whitelist_domains:
            if allowed.startswith("*."):
                suffix = allowed[2:]
                if hostname == suffix or hostname.endswith("." + suffix):
                    matched = True
                    break
            else:
                if hostname == allowed:
                    matched = True
                    break

        if not matched:
            raise EgressBlockedError(
                f"Destination domain '{hostname}' is not in the egress whitelist! (Zero-Trust Block)"
            )

        # 5. Block literal private/loopback IPs
        if self.block_private_ips:
            if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                raise EgressBlockedError(f"Access to localhost/loopback '{hostname}' is strictly forbidden.")
            if self._is_private_ip(hostname):
                raise EgressBlockedError(f"Direct IP access to private/internal subnet '{hostname}' is blocked.")

        return hostname, port, parsed.scheme

    async def verify_dns(self, hostname: str, port: int) -> List[str]:
        """
        Asynchronously resolve host and verify that all resolved IPs are public.
        Pins the verified public IP for transport connection to eliminate DNS rebinding.
        """
        if not self.block_private_ips:
            return [hostname]

        # If already literal IP
        try:
            ipaddress.ip_address(hostname)
            if self._is_private_ip(hostname):
                raise EgressBlockedError(f"Private IP address '{hostname}' is blocked.")
            self._pinned_hosts[hostname] = hostname
            return [hostname]
        except ValueError:
            pass

        if self._dns_resolver:
            addrinfo = await self._dns_resolver(hostname, port)
        else:
            loop = asyncio.get_running_loop()
            try:
                addrinfo = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            except Exception as e:
                raise EgressBlockedError(f"DNS resolution failed for '{hostname}': {e}")

        resolved_ips: List[str] = []
        for item in addrinfo:
            sockaddr = item[4]
            ip_str = sockaddr[0]
            resolved_ips.append(ip_str)
            if self._is_private_ip(ip_str):
                raise EgressBlockedError(
                    f"Destination host '{hostname}' resolves to private/non-public IP '{ip_str}'! SSRF blocked."
                )

        if not resolved_ips:
            raise EgressBlockedError(f"No IP addresses resolved for '{hostname}'.")

        # Pin the verified public IP into the transport backend
        self._pinned_hosts[hostname] = resolved_ips[0]
        return resolved_ips

    def _prepare_payload(
        self, json_data: Optional[Any] = None, content: Optional[bytes] = None
    ) -> Tuple[bytes, Dict[str, str]]:
        headers = {}
        if content is not None:
            payload_bytes = content
        elif json_data is not None:
            payload_bytes = json.dumps(json_data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            payload_bytes = b""

        if len(payload_bytes) > self.max_request_bytes:
            raise EgressBlockedError(
                f"Payload size {len(payload_bytes)} bytes exceeds maximum allowed limit {self.max_request_bytes} bytes"
            )

        return payload_bytes, headers

    async def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        hostname, port, _ = self.validate_url(url)
        await self.verify_dns(hostname, port)

        payload_bytes, auto_headers = self._prepare_payload(json_data=json_data, content=content)
        req_headers = dict(auto_headers)
        if headers:
            req_headers.update(headers)

        self.total_outbound_bytes += len(payload_bytes)
        self.total_requests += 1

        start_time = time.perf_counter()
        req = self._client.build_request(
            "POST",
            url,
            headers=req_headers,
            content=payload_bytes if (json_data is not None or content is not None) else None,
        )
        response = await self._client.send(req, stream=True)
        try:
            cl = response.headers.get("content-length")
            if cl:
                try:
                    if int(cl) > self.max_response_bytes:
                        raise EgressBlockedError(
                            f"Response size {cl} bytes exceeds maximum allowed limit {self.max_response_bytes} bytes"
                        )
                except ValueError:
                    pass

            body_chunks = []
            total_inbound = 0
            async for chunk in response.aiter_bytes():
                total_inbound += len(chunk)
                if total_inbound > self.max_response_bytes:
                    raise EgressBlockedError(
                        f"Response size exceeded maximum allowed limit {self.max_response_bytes} bytes"
                    )
                body_chunks.append(chunk)

            full_content = b"".join(body_chunks)
            response._content = full_content
        finally:
            await response.aclose()

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        self.total_inbound_bytes += len(response.content)
        response.latency_ms = latency_ms  # type: ignore[attr-defined]
        return response

    async def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        hostname, port, _ = self.validate_url(url)
        await self.verify_dns(hostname, port)

        self.total_requests += 1

        start_time = time.perf_counter()
        req = self._client.build_request(
            "GET",
            url,
            headers=headers,
            params=params,
        )
        response = await self._client.send(req, stream=True)
        try:
            cl = response.headers.get("content-length")
            if cl:
                try:
                    if int(cl) > self.max_response_bytes:
                        raise EgressBlockedError(
                            f"Response size {cl} bytes exceeds maximum allowed limit {self.max_response_bytes} bytes"
                        )
                except ValueError:
                    pass

            body_chunks = []
            total_inbound = 0
            async for chunk in response.aiter_bytes():
                total_inbound += len(chunk)
                if total_inbound > self.max_response_bytes:
                    raise EgressBlockedError(
                        f"Response size exceeded maximum allowed limit {self.max_response_bytes} bytes"
                    )
                body_chunks.append(chunk)

            full_content = b"".join(body_chunks)
            response._content = full_content
        finally:
            await response.aclose()

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
        hostname, port, _ = self.validate_url(url)
        await self.verify_dns(hostname, port)

        payload_bytes, auto_headers = self._prepare_payload(json_data=json_data)
        req_headers = dict(auto_headers)
        if headers:
            req_headers.update(headers)

        self.total_outbound_bytes += len(payload_bytes)
        self.total_requests += 1

        req = self._client.build_request("POST", url, headers=req_headers, content=payload_bytes)
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
