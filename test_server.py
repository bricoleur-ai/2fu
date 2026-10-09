import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

import server

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ParseCodeTest(unittest.TestCase):
    CASES = [
        ("Your verification code is 123456", "123456"),
        ("123456 is your Facebook code", "123456"),
        ("G-482910 is your Google verification code.", "482910"),
        ("Use code 123 456 to log in", "123456"),
        ("Use code 123-456 to log in", "123456"),
        ("Your code expires in 10 minutes: 482910", "482910"),
        (
            "Chase: your one-time code is 12345678. "
            "Call 1-800-935-9935 if this wasn't you.",
            "12345678",
        ),
        ("Call 800-935-9935. Your PIN: 4821", "4821"),
        ("Your OTP is 9041", "9041"),
        ("Your passcode:\n771203", "771203"),
        ("Order 123456789 has shipped", None),
        ("Hello, see you at lunch", None),
        ("", None),
    ]

    def test_examples(self):
        for message, want in self.CASES:
            with self.subTest(message=message):
                self.assertEqual(server.parse_code(message), want)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.server = server.make_server(
            port=0, clients={"alice"}, ttl=300, clock=self.clock
        )
        self.port = self.server.server_address[1]
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None):
        """Send one request; return (status, body bytes, headers)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body)
            response = conn.getresponse()
            return response.status, response.read(), response.headers
        finally:
            conn.close()

    def send_sms(self, message):
        body = json.dumps({"message": message}).encode()
        return self.request("POST", "/sms", body)[0]

    def fetch(self, client="alice"):
        status, body, _ = self.request("GET", f"/code?client={client}")
        return status, body

    def test_stored_code_is_served_to_a_known_client(self):
        self.assertEqual(self.send_sms("Your code is 123456"), 204)

        status, body = self.fetch()

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"code": "123456"})

    def test_code_is_dropped_after_it_is_served(self):
        self.send_sms("Your code is 123456")
        self.fetch()

        self.assertEqual(self.fetch(), (404, b""))

    def test_unknown_client_is_refused_and_the_code_survives(self):
        self.send_sms("Your code is 123456")

        self.assertEqual(self.fetch("mallory"), (403, b""))

        self.assertEqual(self.fetch()[0], 200)

    def test_missing_client_is_refused(self):
        self.send_sms("Your code is 123456")

        self.assertEqual(self.request("GET", "/code")[0], 403)
        self.assertEqual(self.request("GET", "/code?client=")[0], 403)

    def test_client_name_is_case_sensitive(self):
        self.send_sms("Your code is 123456")

        self.assertEqual(self.fetch("Alice")[0], 403)

    def test_unknown_client_cannot_tell_whether_a_code_is_pending(self):
        self.assertEqual(self.fetch("mallory")[0], 403)

    def test_fetch_with_nothing_stored_is_not_found(self):
        self.assertEqual(self.fetch(), (404, b""))

    def test_newer_message_replaces_an_unfetched_code(self):
        self.send_sms("Your code is 111111")
        self.send_sms("Your code is 222222")

        self.assertEqual(json.loads(self.fetch()[1]), {"code": "222222"})
        self.assertEqual(self.fetch()[0], 404)

    def test_code_is_served_until_the_ttl_runs_out(self):
        self.send_sms("Your code is 123456")
        self.clock.now += 299

        self.assertEqual(self.fetch()[0], 200)

    def test_expired_code_is_not_served(self):
        self.send_sms("Your code is 123456")
        self.clock.now += 301

        self.assertEqual(self.fetch()[0], 404)

    def test_malformed_sms_body_is_rejected(self):
        bodies = [
            b"",
            b"not json",
            b"\xff\xfe",
            b"[]",
            b'"Your code is 123456"',
            b'{"text": "Your code is 123456"}',
            b'{"message": 123456}',
            b"[" * 5000,
        ]
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.assertEqual(self.request("POST", "/sms", body)[0], 400)
        self.assertEqual(self.fetch()[0], 404)

    def test_message_without_a_code_leaves_the_pending_code_alone(self):
        self.send_sms("Your code is 123456")

        self.assertEqual(self.send_sms("Hello, see you at lunch"), 422)

        self.assertEqual(json.loads(self.fetch()[1]), {"code": "123456"})

    def test_oversized_sms_body_is_rejected(self):
        body = json.dumps({"message": "Your code is 123456 " + "x" * 17000}).encode()

        self.assertEqual(self.request("POST", "/sms", body)[0], 413)

        self.assertEqual(self.fetch()[0], 404)

    def test_other_paths_are_not_found(self):
        self.send_sms("Your code is 123456")

        self.assertEqual(self.request("GET", "/")[0], 404)
        self.assertEqual(self.request("GET", "/sms?client=alice")[0], 404)
        self.assertEqual(self.request("POST", "/code?client=alice", b"{}")[0], 404)

    def test_code_response_is_not_cacheable(self):
        self.send_sms("Your code is 123456")

        _, _, headers = self.request("GET", "/code?client=alice")

        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_simultaneous_fetches_serve_the_code_once(self):
        self.send_sms("Your code is 123456")
        start = threading.Barrier(16)
        statuses = []

        def fetch():
            start.wait()
            statuses.append(self.fetch()[0])

        threads = [threading.Thread(target=fetch) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(statuses), [200] + [404] * 15)

    def test_code_and_message_text_are_never_logged(self):
        with self.assertLogs("2fu", level="DEBUG") as logs:
            self.send_sms("Your Acme code is 123456")
            self.fetch("mallory")
            self.fetch()
            self.send_sms("Hello, see you at lunch")

        output = "\n".join(logs.output)
        self.assertNotIn("123456", output)
        self.assertNotIn("Acme", output)
        self.assertNotIn("lunch", output)
        self.assertIn("alice", output)
        self.assertIn("mallory", output)


class SettingsTest(unittest.TestCase):
    DEFAULTS = (frozenset(server.CLIENTS), server.PORT, server.TTL_SECONDS)

    def test_defaults_when_nothing_is_set(self):
        self.assertEqual(server.settings_from_env({}), self.DEFAULTS)

    def test_empty_values_count_as_unset(self):
        env = {"TWOFU_CLIENTS": " ", "TWOFU_PORT": "", "TWOFU_TTL_SECONDS": ""}
        self.assertEqual(server.settings_from_env(env), self.DEFAULTS)

    def test_values_from_the_environment(self):
        env = {"TWOFU_CLIENTS": "laptop, browser ,cron", "TWOFU_PORT": "9000", "TWOFU_TTL_SECONDS": "60"}
        self.assertEqual(
            server.settings_from_env(env),
            (frozenset({"laptop", "browser", "cron"}), 9000, 60),
        )

    def test_unusable_values_are_refused_by_name(self):
        cases = [
            ("TWOFU_CLIENTS", ",,"),
            ("TWOFU_PORT", "http"),
            ("TWOFU_PORT", "0"),
            ("TWOFU_PORT", "65536"),
            ("TWOFU_TTL_SECONDS", "0"),
            ("TWOFU_TTL_SECONDS", "-5"),
            ("TWOFU_TTL_SECONDS", "1.5"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, "^" + name):
                    server.settings_from_env({name: value})


class StartupTest(unittest.TestCase):
    def test_an_unusable_setting_stops_the_server_with_its_name(self):
        env = {**os.environ, "TWOFU_PORT": "http"}
        result = subprocess.run(
            [sys.executable, SERVER], env=env, capture_output=True, text=True, timeout=10
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TWOFU_PORT", result.stderr)

    def test_the_server_listens_and_allows_what_the_environment_says(self):
        port = _free_port()
        env = {**os.environ, "TWOFU_PORT": str(port), "TWOFU_CLIENTS": "laptop", "TWOFU_TTL_SECONDS": "60"}
        proc = subprocess.Popen([sys.executable, SERVER], env=env, stderr=subprocess.PIPE, text=True)
        try:
            status = {}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not status:
                try:
                    for client in ("laptop", "my-client"):
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        conn.request("GET", "/code?client=" + client)
                        status[client] = conn.getresponse().status
                        conn.close()
                except ConnectionRefusedError:
                    status = {}
                    time.sleep(0.05)
            self.assertEqual(status, {"laptop": 404, "my-client": 403})
        finally:
            proc.terminate()
            _, err = proc.communicate(timeout=5)
        self.assertIn(f"listening on port {port}, clients: laptop, codes live 60s", err)


if __name__ == "__main__":
    unittest.main()
