#!/usr/bin/env python3
"""Keep a GoDaddy A record synchronized with this host's public IPv4 address."""

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
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable


GODADDY_API = "https://api.godaddy.com/v3/domains"
DEFAULT_IP_SERVICES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)
USER_AGENT = "noip-godaddy-ddns/1.0"


class UpdateError(RuntimeError):
    """A safe-to-display update failure."""


@dataclass(frozen=True)
class DNSRecord:
    record_id: str
    name: str
    record_type: str
    data: str
    ttl: int

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "DNSRecord":
        try:
            return cls(
                record_id=str(value["recordId"]),
                name=str(value["name"]),
                record_type=str(value["type"]),
                data=str(value["data"]),
                ttl=int(value["ttl"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateError("GoDaddy returned a malformed DNS record") from exc


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


class GoDaddyDNS:
    def __init__(self, token: str, http: HTTPClient, api_base: str = GODADDY_API):
        self.http = http
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _zone_url(self, zone: str) -> str:
        encoded_zone = urllib.parse.quote(zone, safe="")
        return f"{self.api_base}/zones/{encoded_zone}/dns-records"

    def get_a_records(self, zone: str, name: str) -> list[DNSRecord]:
        query = urllib.parse.urlencode({"type": "A", "name": name, "pageSize": 100})
        _, body = self.http.request(f"{self._zone_url(zone)}?{query}", headers=self.headers)
        try:
            response = json.loads(body)
            items = response["items"]
            if not isinstance(items, list):
                raise TypeError
            return [DNSRecord.from_api(item) for item in items]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise UpdateError("GoDaddy returned a malformed record list") from exc

    def create_a_record(self, zone: str, name: str, address: str, ttl: int) -> None:
        self._write(
            self._zone_url(zone),
            method="POST",
            payload={"name": name, "type": "A", "data": address, "ttl": ttl},
        )

    def replace_a_record(self, zone: str, record: DNSRecord, address: str) -> None:
        record_id = urllib.parse.quote(record.record_id, safe="")
        self._write(
            f"{self._zone_url(zone)}/{record_id}",
            method="PUT",
            payload={
                "name": record.name,
                "type": "A",
                "data": address,
                "ttl": record.ttl,
            },
        )

    def _write(self, url: str, *, method: str, payload: dict[str, Any]) -> None:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
        }
        self.http.request(
            url,
            method=method,
            headers=headers,
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )


def synchronize(
    dns: GoDaddyDNS,
    zone: str,
    name: str,
    address: str,
    create_ttl: int,
    *,
    dry_run: bool = False,
) -> str:
    hostname = zone if name == "@" else f"{name}.{zone}"
    records = dns.get_a_records(zone, name)
    if not records:
        if dry_run:
            return f"would create {hostname} A record with {address} (TTL {create_ttl})"
        dns.create_a_record(zone, name, address, create_ttl)
        return f"created {hostname} A record with {address} (TTL {create_ttl})"
    if len(records) > 1:
        raise UpdateError(
            f"refusing to modify {hostname}: GoDaddy returned {len(records)} A records"
        )

    record = records[0]
    if record.data == address:
        return f"{hostname} already points to {address}" if dry_run else ""
    old_address = record.data
    if dry_run:
        return f"would update {hostname} from {old_address} to {address}"
    dns.replace_a_record(zone, record, address)
    return f"updated {hostname} from {old_address} to {address}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", default="gluglu.app")
    parser.add_argument("--record", default="@")
    parser.add_argument("--ttl", type=int, default=600, help="TTL used when creating a record")
    parser.add_argument("--token-file", default="/etc/noip/godaddy.pat")
    parser.add_argument("--lock-file", default="/run/lock/noip-godaddy.lock")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true", help="report without changing DNS")
    args = parser.parse_args(argv)
    if not 600 <= args.ttl <= 86400:
        parser.error("--ttl must be between 600 and 86400")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
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
        dns = GoDaddyDNS(token, http)
        return synchronize(
            dns, args.zone, args.record, address, args.ttl, dry_run=args.dry_run
        )


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
