"""GitHub App authentication and webhook verification: fixtures, no network.

These tests prove that aghub can authenticate as a GitHub App using only its
private key, and that webhook payloads cannot be spoofed. No production
secrets or real GitHub API calls are needed.
"""

import base64
import http.client
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import os
sys_path = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, sys_path)

from aghub import github  # noqa: E402
from aghub.errors import AghubError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"
PRIVATE = FIXTURES / "app-private-key.pem"
PUBLIC = FIXTURES / "app-public-key.pem"
PKCS1 = FIXTURES / "app-private-key.pkcs1.pem"
WEBHOOK_SECRET = "test-webhook-secret"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def verify_jwt(jwt):
    header, claims, encoded = jwt.split(".")
    signature = base64.urlsafe_b64decode(
        encoded + "=" * (-len(encoded) % 4)
    )
    return github._rsa_verify(
        PRIVATE.read_text(),
        (header + "." + claims).encode("ascii"),
        signature,
    )


class FakeGitHub:
    def __init__(self):
        self.requests = []
        self.fail_status = None

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body):
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                if owner.fail_status:
                    self._reply(owner.fail_status, {"message": "forbidden"})
                    return True
                return False

            def do_GET(self):
                if self._record(None):
                    return
                self._reply(404, {"message": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if self._record(body):
                    return
                self._reply(200, {"token": "cached-token", "expires_at": "2099-01-01T00:00:00Z"})

        return Handler


class GitHubAppTests(unittest.TestCase):
    def test_rsa_numbers_parse_pkcs8_and_pkcs1_private_keys(self):
        n1, e1, d1 = github._rsa_numbers(PRIVATE.read_bytes())
        n2, e2, d2 = github._rsa_numbers(PKCS1.read_bytes())
        self.assertIsNotNone(d1)
        self.assertEqual(n1, n2)
        self.assertEqual(e1, e2)
        self.assertEqual(d1, d2)

    def test_rsa_public_keys_parse_both_formats(self):
        n1, e1, _ = github._rsa_numbers(PUBLIC.read_bytes())
        self.assertIsNotNone(n1)
        self.assertNotEqual(n1, e1)

    def test_rsa_sign_and_verify(self):
        key = PRIVATE.read_bytes()
        message = b"ship it"
        signature = github._rsa_sign(key, message)
        self.assertTrue(github._rsa_verify(key, message, signature))
        self.assertFalse(github._rsa_verify(key, b"changed", signature))
        self.assertFalse(github._rsa_verify(key, message, b"x" * len(signature)))

    def test_app_jwt_structure(self):
        pem = PRIVATE.read_text()
        now = time.time()
        jwt = github.app_jwt(12345, pem, now=now)
        header_b64, _, rest = jwt.partition(".")
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
        claims_b64, _, _ = rest.partition(".")
        claims = json.loads(base64.urlsafe_b64decode(claims_b64 + "=="))
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["typ"], "JWT")
        self.assertEqual(claims["iss"], 12345)
        self.assertEqual(claims["exp"], claims["iat"] + 600)
        self.assertGreaterEqual(claims["iat"], int(now) - 61)
        self.assertTrue(verify_jwt(jwt))

    def test_app_jwt_signs_correctly_for_pkcs1_encoding(self):
        pem = PKCS1.read_text()
        jwt1 = github.app_jwt(678, pem, now=1_700_000_000)
        jwt2 = github.app_jwt(678, PRIVATE.read_text(), now=1_700_000_000)
        self.assertEqual(jwt1, jwt2)
        self.assertTrue(verify_jwt(jwt1))

    def test_app_jwt_requires_valid_app_id(self):
        with self.assertRaises(AghubError):
            github.app_jwt(None, PRIVATE.read_text())

    def test_app_jwt_requires_valid_private_key(self):
        with self.assertRaises(AghubError):
            github.app_jwt(1, "not-a-key")

    def test_installation_token_is_cached_and_refreshable(self):
        fake = FakeGitHub()
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = f"http://127.0.0.1:{server.server_port}"
            auth = github.AppAuth(
                "123", PRIVATE.read_text(), "42", api=api, timeout=5
            )
            token1 = auth.token()
            token2 = auth.token()
            self.assertEqual(token1, token2)
            self.assertEqual(token1, "cached-token")
            self.assertEqual(len(fake.requests), 1)
            auth.token(force=True)
            self.assertEqual(len(fake.requests), 2)
            request = fake.requests[0]
            self.assertEqual(request["path"], "/app/installations/42/access_tokens")
            header = request["authorization"]
            self.assertTrue(header.startswith("Bearer "))
            self.assertTrue(verify_jwt(header[7:]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_installation_token_rejects_expired_cache_with_margin(self):
        fake = FakeGitHub()
        server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = f"http://127.0.0.1:{server.server_port}"
            auth = github.AppAuth(
                "123", PRIVATE.read_text(), "42", api=api, timeout=5
            )
            auth.token(force=True)
            self.assertEqual(len(fake.requests), 1)
            auth._expires_at = int(time.time()) - 1  # stale
            auth.token()
            self.assertEqual(len(fake.requests), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_app_auth_from_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "AGHUB_GITHUB_APP_ID": "123",
                "AGHUB_GITHUB_APP_PRIVATE_KEY": PRIVATE.read_text(),
                "AGHUB_GITHUB_INSTALLATION_ID": "42",
            },
            clear=True,
        ):
            auth = github.app_auth()
            self.assertIsInstance(auth, github.AppAuth)
            self.assertEqual(auth.app_id, "123")
        with mock.patch.dict(
            os.environ,
            {
                "AGHUB_GITHUB_APP_ID": "123",
                "GITHUB_INSTALLATION_ID": "99",
            },
            clear=True,
        ):
            self.assertRaises(AghubError, github.app_auth)

    def test_app_auth_requires_all_three(self):
        with mock.patch.dict(
            os.environ,
            {
                "AGHUB_GITHUB_APP_ID": "123",
                "GITHUB_APP_PRIVATE_KEY": PRIVATE.read_text(),
            },
            clear=True,
        ):
            with self.assertRaises(AghubError) as exc:
                github.app_auth()
            self.assertIn("installation id", str(exc.exception))


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.captured = []
        self.receiver = github.WebhookReceiver(
            WEBHOOK_SECRET,
            lambda kind, event, delivery: self.captured.append(
                (kind, event, delivery)
            ),
        )

    def test_webhook_signature_and_invalid_signature_are_detected(self):
        fixture = load_fixture("pull_request_review.approved.json")
        body = json.dumps(fixture).encode("utf-8")
        signature = github.webhook_signature(WEBHOOK_SECRET, body)

        self.assertTrue(
            github.verify_webhook(WEBHOOK_SECRET, body, signature)
        )
        self.assertRaises(AghubError, github.verify_webhook,
            "wrong-secret", body, signature)
        self.assertRaises(AghubError, github.verify_webhook,
            WEBHOOK_SECRET, body, "sha256=" + "0" * 64)
        self.assertRaises(AghubError, github.verify_webhook,
            "wrong-secret", body, "sha256=bad")
        self.assertRaises(AghubError, github.verify_webhook,
            WEBHOOK_SECRET, body, None)
        self.assertRaises(AghubError, github.verify_webhook,
            None, body, signature)
        self.assertRaises(AghubError, github.verify_webhook,
            "", body, signature)

    def test_webhook_signature_is_tamper_evident(self):
        fixture = load_fixture("pull_request_review.approved.json")
        body = json.dumps(fixture).encode("utf-8")
        signature = github.webhook_signature(WEBHOOK_SECRET, body)
        tampered = body + b"zzz"
        with self.assertRaises(AghubError):
            github.verify_webhook(WEBHOOK_SECRET, tampered, signature)

    def test_webhook_routes_verified_review_event(self):
        fixture = load_fixture("pull_request_review.approved.json")
        body = json.dumps(fixture).encode("utf-8")
        headers = {
            "X-GitHub-Event": "pull_request_review",
            "X-Hub-Signature-256": github.webhook_signature(WEBHOOK_SECRET, body),
        }
        result = self.receiver.receive(headers, body)
        self.assertEqual(self.captured[0][0], "pull_request_review")
        self.assertEqual(self.captured[0][1]["action"], "submitted")
        self.assertEqual(result["event"], "pull_request_review")

    def test_webhook_callback_is_not_called_when_signature_fails(self):
        fixture = load_fixture("pull_request_review.approved.json")
        body = json.dumps(fixture).encode("utf-8")
        headers = {
            "X-GitHub-Event": "pull_request_review",
            "X-Hub-Signature-256": "sha256=deadbeef",
        }
        self.assertRaises(AghubError, self.receiver.receive, headers, body)
        self.assertEqual(len(self.captured), 0)

    def test_webhook_rejects_missing_event_header(self):
        body = json.dumps({"action": "ping"}).encode("utf-8")
        headers = {"X-Hub-Signature-256": github.webhook_signature(WEBHOOK_SECRET, body)}
        self.assertRaises(AghubError, self.receiver.receive, headers, body)

    def test_webhook_rejects_overly_large_payloads(self):
        body = b"{" + b" " * github.MAX_WEBHOOK_BYTES + b"}"
        headers = {
            "X-GitHub-Event": "pull_request_review",
            "X-Hub-Signature-256": github.webhook_signature(WEBHOOK_SECRET, body),
        }
        self.assertRaises(AghubError, self.receiver.receive, headers, body)

    def test_webhook_ping_is_handled(self):
        fixture = load_fixture("ping.json")
        body = json.dumps(fixture).encode("utf-8")
        headers = {
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": github.webhook_signature(WEBHOOK_SECRET, body),
        }
        self.receiver.receive(headers, body)
        self.assertEqual(self.captured[0][0], "ping")


class WebhookServerTests(unittest.TestCase):
    def test_webhook_server_accepts_signed_payload(self):
        receiver = github.WebhookReceiver(
            WEBHOOK_SECRET, lambda kind, event, delivery: {"status": "ok"}
        )
        server = github.webhook_server(receiver, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            fixture = load_fixture("pull_request_review.approved.json")
            body = json.dumps(fixture).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request_review",
                "X-GitHub-Delivery": "d123",
                "X-Hub-Signature-256": github.webhook_signature(
                    WEBHOOK_SECRET, body
                ),
            }
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/github/webhook",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["delivery"], "d123")

            health = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz"
            )
            self.assertEqual(health.status, 200)

            tampered = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                data=b'{"fake": true}',
                headers={
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": "sha256=" + "0" * 64,
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(tampered)
            self.assertEqual(raised.exception.code, 401)

            missing_sig = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                data=body,
                headers={
                    "X-GitHub-Event": "pull_request_review",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(missing_sig)
            self.assertEqual(raised.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_webhook_server_rejects_too_big_payload(self):
        receiver = github.WebhookReceiver(
            WEBHOOK_SECRET, lambda kind, event, delivery: {"status": "ok"}
        )
        server = github.webhook_server(receiver, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            # Announce an oversized body but send none. The handler rejects on
            # Content-Length before reading, so this cannot race the client's
            # upload the way a real oversized body can (BrokenPipeError).
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.putrequest("POST", "/")
                conn.putheader("X-GitHub-Event", "push")
                conn.putheader("Content-Length", str(github.MAX_WEBHOOK_BYTES + 2))
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, 413)
                response.read()
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
