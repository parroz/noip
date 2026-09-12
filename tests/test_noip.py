import json
import os
import tempfile
import unittest

import noip


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


class GoDaddyTests(unittest.TestCase):
    def test_create_when_record_is_missing(self):
        http = FakeHTTP([(200, b'{"items":[]}'), (201, b'{}')])
        dns = noip.GoDaddyDNS("not-a-real-token", http)
        message = noip.synchronize(dns, "gluglu.app", "@", "8.8.8.8", 600)

        self.assertIn("created", message)
        url, request = http.requests[1]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(json.loads(request["body"])["data"], "8.8.8.8")
        self.assertNotIn("not-a-real-token", request["body"].decode())

    def test_update_preserves_existing_ttl(self):
        record = {
            "items": [
                {
                    "recordId": "abc123",
                    "name": "@",
                    "type": "A",
                    "data": "1.1.1.1",
                    "ttl": 3600,
                }
            ]
        }
        http = FakeHTTP([(200, json.dumps(record).encode()), (200, b'{}')])
        dns = noip.GoDaddyDNS("token", http)
        message = noip.synchronize(dns, "gluglu.app", "@", "8.8.8.8", 600)

        self.assertIn("updated", message)
        _, request = http.requests[1]
        self.assertEqual(request["method"], "PUT")
        self.assertEqual(json.loads(request["body"])["ttl"], 3600)

    def test_unchanged_record_does_not_write(self):
        record = {
            "items": [
                {
                    "recordId": "abc123",
                    "name": "@",
                    "type": "A",
                    "data": "8.8.8.8",
                    "ttl": 600,
                }
            ]
        }
        http = FakeHTTP([(200, json.dumps(record).encode())])
        dns = noip.GoDaddyDNS("token", http)

        self.assertEqual(noip.synchronize(dns, "gluglu.app", "@", "8.8.8.8", 600), "")
        self.assertEqual(len(http.requests), 1)

    def test_multiple_records_fail_safely(self):
        records = {
            "items": [
                {"recordId": "1", "name": "@", "type": "A", "data": "1.1.1.1", "ttl": 600},
                {"recordId": "2", "name": "@", "type": "A", "data": "8.8.8.8", "ttl": 600},
            ]
        }
        http = FakeHTTP([(200, json.dumps(records).encode())])
        dns = noip.GoDaddyDNS("token", http)

        with self.assertRaisesRegex(noip.UpdateError, "refusing"):
            noip.synchronize(dns, "gluglu.app", "@", "9.9.9.9", 600)
        self.assertEqual(len(http.requests), 1)

    def test_dry_run_reports_without_writing(self):
        record = {
            "items": [
                {
                    "recordId": "abc123",
                    "name": "@",
                    "type": "A",
                    "data": "1.1.1.1",
                    "ttl": 600,
                }
            ]
        }
        http = FakeHTTP([(200, json.dumps(record).encode())])
        dns = noip.GoDaddyDNS("token", http)

        message = noip.synchronize(
            dns, "gluglu.app", "@", "8.8.8.8", 600, dry_run=True
        )
        self.assertEqual(message, "would update gluglu.app from 1.1.1.1 to 8.8.8.8")
        self.assertEqual(len(http.requests), 1)


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
    def test_default_records(self):
        args = noip.parse_args([])
        self.assertEqual(args.records, ["@", "pandora", "auth"])

    def test_records_can_be_repeated_and_duplicates_are_removed(self):
        args = noip.parse_args(
            ["--record", "@", "--record", "pandora", "--record", "@"]
        )
        self.assertEqual(args.records, ["@", "pandora"])


if __name__ == "__main__":
    unittest.main()
