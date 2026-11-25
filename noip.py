#!/usr/bin/env python3
"""Keep Cloudflare A records synchronized with this host's public IPv4 address."""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable


CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
DEFAULT_IP_SERVICES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)
USER_AGENT = "noip-cloudflare-ddns/2.0"
AUTO_TTL = 1


class UpdateError(RuntimeError):
    """A safe-to-display update failure."""


@dataclass(frozen=True)
class DNSRecord:
    record_id: str
    name: str
    record_type: str
    content: str
    ttl: int
    proxied: bool

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "DNSRecord":
        try:
            return cls(
                record_id=str(value["id"]),
                name=str(value["name"]),
                record_type=str(value["type"]),
                content=str(value["content"]),
                ttl=int(value["ttl"]),
                proxied=bool(value.get("proxied", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateError("Cloudflare returned a malformed DNS record") from exc


class HTTPClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            response_body = exc.read(2048).decode("utf-8", "replace").strip()
            detail = f": {response_body}" if response_body else ""
            raise UpdateError(f"HTTP {exc.code} from {request.host}{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"request to {request.host} failed: {exc}") from exc


def read_secret(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UpdateError(f"cannot open token file {path}: {exc.strerror}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UpdateError(f"token file {path} is not a regular file")
        if metadata.st_mode & 0o077:
            raise UpdateError(f"token file {path} must not be readable by group or others")
        with os.fdopen(descriptor, "r", encoding="utf-8") as token_file:
            descriptor = -1
            token = token_file.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not token or "\n" in token or "\r" in token:
        raise UpdateError(f"token file {path} must contain exactly one non-empty line")
    return token


def validate_public_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise UpdateError("public IP service returned an invalid address") from exc
    if address.version != 4 or not address.is_global:
        raise UpdateError("public IP service did not return a public IPv4 address")
    return str(address)


def discover_public_ipv4(
    http: HTTPClient, services: Iterable[str] = DEFAULT_IP_SERVICES
) -> str:
    errors: list[str] = []
    for service in services:
        try:
            _, body = http.request(service)
            return validate_public_ipv4(body.decode("ascii", "strict"))
        except (UpdateError, UnicodeDecodeError) as exc:
            errors.append(f"{urllib.parse.urlsplit(service).hostname}: {exc}")
    raise UpdateError("all public IP services failed (" + "; ".join(errors) + ")")


def record_hostname(zone: str, name: str) -> str:
    zone = zone.rstrip(".").lower()
    return zone if name == "@" else f"{name.rstrip('.').lower()}.{zone}"


def describe_ttl(ttl: int) -> str:
    return "TTL auto" if ttl == AUTO_TTL else f"TTL {ttl}"


class CloudflareDNS:
    def __init__(self, token: str, http: HTTPClient, api_base: str = CLOUDFLARE_API):
        self.http = http
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        self._zone_ids: dict[str, str] = {}

    def _call(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = dict(self.headers)
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        _, raw = self.http.request(url, method=method, headers=headers, body=body)
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UpdateError("Cloudflare returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise UpdateError("Cloudflare returned an unexpected response")
        if response.get("success") is not True:
            messages = [
                str(error.get("message", error))
                for error in response.get("errors") or []
                if isinstance(error, dict)
            ]
            detail = f": {'; '.join(messages)}" if messages else ""
            raise UpdateError(f"Cloudflare rejected the request{detail}")
        return response.get("result")

    def zone_id(self, zone: str) -> str:
        zone = zone.rstrip(".").lower()
        if zone not in self._zone_ids:
            result = self._call("/zones", query={"name": zone, "per_page": 50})
            if not isinstance(result, list):
                raise UpdateError("Cloudflare returned a malformed zone list")
            matches = [
                item for item in result
                if isinstance(item, dict) and str(item.get("name", "")).lower() == zone
            ]
            if not matches:
                raise UpdateError(f"zone {zone} not found or not accessible with this token")
            if len(matches) > 1:
                raise UpdateError(f"refusing to guess: Cloudflare returned {len(matches)} zones named {zone}")
            zone_id = matches[0].get("id")
            if not isinstance(zone_id, str) or not zone_id:
                raise UpdateError("Cloudflare returned a malformed zone")
            self._zone_ids[zone] = zone_id
        return self._zone_ids[zone]

    def _records_path(self, zone_id: str) -> str:
        return f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records"

    def get_a_records(self, zone_id: str, hostname: str) -> list[DNSRecord]:
        result = self._call(
            self._records_path(zone_id),
            query={"type": "A", "name": hostname, "per_page": 100},
        )
        if not isinstance(result, list):
            raise UpdateError("Cloudflare returned a malformed record list")
        records = [DNSRecord.from_api(item) for item in result]
        return [
            record for record in records
            if record.record_type == "A" and record.name.lower() == hostname
        ]

    def create_a_record(
        self, zone_id: str, hostname: str, address: str, ttl: int, proxied: bool
    ) -> None:
        self._call(
            self._records_path(zone_id),
            method="POST",
            payload={
                "type": "A",
                "name": hostname,
                "content": address,
                "ttl": ttl,
                "proxied": proxied,
            },
        )

    def update_address(self, zone_id: str, record: DNSRecord, address: str) -> None:
        # PATCH changes only the address; TTL, proxy status and comments stay as they are.
        record_id = urllib.parse.quote(record.record_id, safe="")
        self._call(
            f"{self._records_path(zone_id)}/{record_id}",
            method="PATCH",
            payload={"content": address},
        )


def synchronize(
    dns: CloudflareDNS,
    zone: str,
    name: str,
    address: str,
    create_ttl: int,
    *,
    proxied: bool = False,
    dry_run: bool = False,
) -> str:
    hostname = record_hostname(zone, name)
    zone_id = dns.zone_id(zone)
    records = dns.get_a_records(zone_id, hostname)
    if not records:
        ttl = AUTO_TTL if proxied else create_ttl
        details = f"{describe_ttl(ttl)}{', proxied' if proxied else ''}"
        if dry_run:
            return f"would create {hostname} A record with {address} ({details})"
        dns.create_a_record(zone_id, hostname, address, ttl, proxied)
        return f"created {hostname} A record with {address} ({details})"
    if len(records) > 1:
        raise UpdateError(
            f"refusing to modify {hostname}: Cloudflare returned {len(records)} A records"
        )

    record = records[0]
    if record.content == address:
        return f"{hostname} already points to {address}" if dry_run else ""
    old_address = record.content
    if dry_run:
        return f"would update {hostname} from {old_address} to {address}"
    dns.update_address(zone_id, record, address)
    return f"updated {hostname} from {old_address} to {address}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", default="gluglu.app")
    parser.add_argument(
        "--record",
        action="append",
        dest="records",
        help="record name to synchronize (default: daemon); may be repeated",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=AUTO_TTL,
        help="TTL used when creating a record: 1 (automatic) or 60-86400",
    )
    parser.add_argument(
        "--proxied",
        action="store_true",
        help="proxy newly created records through Cloudflare; existing records keep their setting",
    )
    parser.add_argument("--token-file", default="/etc/noip/cloudflare.token")
    parser.add_argument("--lock-file", default="/run/lock/noip-cloudflare.lock")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true", help="report without changing DNS")
    args = parser.parse_args(argv)
    if args.ttl != AUTO_TTL and not 60 <= args.ttl <= 86400:
        parser.error("--ttl must be 1 (automatic) or between 60 and 86400")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not args.records:
        args.records = ["daemon"]
    args.records = list(dict.fromkeys(args.records))
    return args


def run(args: argparse.Namespace, http_factory: Callable[[float], HTTPClient] = HTTPClient) -> str:
    os.makedirs(os.path.dirname(args.lock_file), mode=0o755, exist_ok=True)
    with open(args.lock_file, "w", encoding="ascii") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""
        token = read_secret(args.token_file)
        http = http_factory(args.timeout)
        address = discover_public_ipv4(http)
        dns = CloudflareDNS(token, http)
        messages = [
            synchronize(
                dns,
                args.zone,
                record,
                address,
                args.ttl,
                proxied=args.proxied,
                dry_run=args.dry_run,
            )
            for record in args.records
        ]
        return "\n".join(message for message in messages if message)


def main(argv: list[str] | None = None) -> int:
    try:
        message = run(parse_args(argv))
        if message:
            print(message)
        return 0
    except UpdateError as exc:
        print(f"noip: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
