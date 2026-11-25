import contextlib
import io
import json
import os
import tempfile
import unittest

import noip


ZONE_ID = "023e105f4ecef8ad9ca31a8372d0c353"


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok(result):
    return 200, json.dumps(
        {"success": True, "errors": [], "messages": [], "result": result}
    ).encode()


def zone_response(name="gluglu.app"):
    return ok([{"id": ZONE_ID, "name": name}])


def a_record(name="gluglu.app", content="1.1.1.1", ttl=1, proxied=False, record_id="abc123"):
    return {
        "id": record_id,
        "name": name,
        "type": "A",
        "content": content,
        "ttl": ttl,
        "proxied": proxied,
    }


class PublicIPTests(unittest.TestCase):
    def test_falls_back_to_second_service(self):
        http = FakeHTTP(
            [noip.UpdateError("offline"), (200, b"8.8.8.8\n")]
        )
        self.assertEqual(
            noip.discover_public_ipv4(http, ("https://one.test", "https://two.test")),
            "8.8.8.8",
        )

    def test_rejects_private_address(self):
        with self.assertRaises(noip.UpdateError):
            noip.validate_public_ipv4("192.168.1.2")


class CloudflareTests(unittest.TestCase):
    def test_create_when_record_is_missing(self):
        http = FakeHTTP([zone_response(), ok([]), ok(a_record(content="8.8.8.8"))])
        dns = noip.CloudflareDNS("not-a-real-token", http)
        message = noip.synchronize(dns, "gluglu.app", "pandora", "8.8.8.8", 1)

        self.assertEqual(
            message, "created pandora.gluglu.app A record with 8.8.8.8 (TTL auto)"
        )
        lookup_url, _ = http.requests[1]
        self.assertIn(f"/zones/{ZONE_ID}/dns_records?", lookup_url)
        self.assertIn("name=pandora.gluglu.app", lookup_url)
        url, request = http.requests[2]
        self.assertTrue(url.endswith(f"/zones/{ZONE_ID}/dns_records"))
        self.assertEqual(request["method"], "POST")
        body = json.loads(request["body"])
        self.assertEqual(
            body,
            {
                "type": "A",
                "name": "pandora.gluglu.app",
                "content": "8.8.8.8",
                "ttl": 1,
                "proxied": False,
            },
        )
        self.assertEqual(request["headers"]["Authorization"], "Bearer not-a-real-token")
        self.assertNotIn("not-a-real-token", request["body"].decode())

    def test_create_proxied_record_uses_automatic_ttl(self):
        http = FakeHTTP([zone_response(), ok([]), ok({})])
        dns = noip.CloudflareDNS("token", http)
        message = noip.synchronize(
            dns, "gluglu.app", "@", "8.8.8.8", 600, proxied=True
        )

        self.assertIn("proxied", message)
        body = json.loads(http.requests[2][1]["body"])
        self.assertEqual(body["ttl"], 1)
        self.assertIs(body["proxied"], True)

    def test_update_changes_only_the_address(self):
        record = a_record(content="1.1.1.1", ttl=3600, proxied=True)
        http = FakeHTTP([zone_response(), ok([record]), ok(record)])
        dns = noip.CloudflareDNS("token", http)
        message = noip.synchronize(dns, "gluglu.app", "@", "8.8.8.8", 1)

        self.assertEqual(message, "updated gluglu.app from 1.1.1.1 to 8.8.8.8")
        url, request = http.requests[2]
        self.assertTrue(url.endswith(f"/zones/{ZONE_ID}/dns_records/abc123"))
        self.assertEqual(request["method"], "PATCH")
        self.assertEqual(json.loads(request["body"]), {"content": "8.8.8.8"})

    def test_unchanged_record_does_not_write(self):
        http = FakeHTTP([zone_response(), ok([a_record(content="8.8.8.8")])])
        dns = noip.CloudflareDNS("token", http)

        self.assertEqual(noip.synchronize(dns, "gluglu.app", "@", "8.8.8.8", 1), "")
        self.assertEqual(len(http.requests), 2)

    def test_multiple_records_fail_safely(self):
        records = [
            a_record(content="1.1.1.1", record_id="1"),
            a_record(content="8.8.8.8", record_id="2"),
        ]
        http = FakeHTTP([zone_response(), ok(records)])
        dns = noip.CloudflareDNS("token", http)

        with self.assertRaisesRegex(noip.UpdateError, "refusing"):
            noip.synchronize(dns, "gluglu.app", "@", "9.9.9.9", 1)
        self.assertEqual(len(http.requests), 2)

    def test_ignores_records_with_other_names(self):
        records = [a_record(name="other.gluglu.app", content="1.1.1.1")]
        http = FakeHTTP([zone_response(), ok(records)])
        dns = noip.CloudflareDNS("token", http)

        message = noip.synchronize(
            dns, "gluglu.app", "auth", "8.8.8.8", 1, dry_run=True
        )
        self.assertEqual(
            message, "would create auth.gluglu.app A record with 8.8.8.8 (TTL auto)"
        )

    def test_dry_run_reports_without_writing(self):
        http = FakeHTTP([zone_response(), ok([a_record(content="1.1.1.1")])])
        dns = noip.CloudflareDNS("token", http)

        message = noip.synchronize(
            dns, "gluglu.app", "@", "8.8.8.8", 1, dry_run=True
        )
        self.assertEqual(message, "would update gluglu.app from 1.1.1.1 to 8.8.8.8")
        self.assertEqual(len(http.requests), 2)

    def test_zone_id_is_looked_up_once(self):
        http = FakeHTTP(
            [
                zone_response(),
                ok([a_record(content="8.8.8.8")]),
                ok([a_record(name="pandora.gluglu.app", content="8.8.8.8")]),
            ]
        )
        dns = noip.CloudflareDNS("token", http)
        for name in ("@", "pandora"):
            noip.synchronize(dns, "gluglu.app", name, "8.8.8.8", 1)

        zone_lookups = [url for url, _ in http.requests if "/dns_records" not in url]
        self.assertEqual(len(zone_lookups), 1)

    def test_missing_zone_fails(self):
        http = FakeHTTP([ok([])])
        dns = noip.CloudflareDNS("token", http)

        with self.assertRaisesRegex(noip.UpdateError, "not found"):
            noip.synchronize(dns, "gluglu.app", "@", "8.8.8.8", 1)

    def test_unsuccessful_response_reports_cloudflare_errors(self):
        body = json.dumps(
            {"success": False, "errors": [{"code": 9109, "message": "Invalid access token"}]}
        ).encode()
        http = FakeHTTP([(200, body)])
        dns = noip.CloudflareDNS("token", http)

        with self.assertRaisesRegex(noip.UpdateError, "Invalid access token"):
            dns.zone_id("gluglu.app")


class SecretTests(unittest.TestCase):
    def test_secret_file_must_be_private(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as token_file:
            token_file.write("secret-token\n")
            path = token_file.name
        try:
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(noip.UpdateError, "must not be readable"):
                noip.read_secret(path)
            os.chmod(path, 0o600)
            self.assertEqual(noip.read_secret(path), "secret-token")
        finally:
            os.unlink(path)


class ArgumentTests(unittest.TestCase):
    def test_defaults(self):
        args = noip.parse_args([])
        self.assertEqual(args.records, ["daemon"])
        self.assertEqual(args.ttl, 1)
        self.assertFalse(args.proxied)
        self.assertEqual(args.token_file, "/etc/noip/cloudflare.token")

    def test_records_can_be_repeated_and_duplicates_are_removed(self):
        args = noip.parse_args(
            ["--record", "@", "--record", "pandora", "--record", "@"]
        )
        self.assertEqual(args.records, ["@", "pandora"])

    def test_ttl_must_be_auto_or_in_range(self):
        self.assertEqual(noip.parse_args(["--ttl", "60"]).ttl, 60)
        for value in ("0", "30", "86401"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    noip.parse_args(["--ttl", value])


if __name__ == "__main__":
    unittest.main()
