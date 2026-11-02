"""GitHub bridge: put aghub review state where humans already work.

The bridge is deliberately small and dependency-free. It reads event payloads
from GitHub Actions or a verified webhook delivery, renders one review comment,
and publishes one check run. Approval decisions come from pull-request reviews.

Two authentication paths are supported:

* a workflow-scoped ``GITHUB_TOKEN`` (the original Actions path), and
* a GitHub App: a short-lived RS256 JWT proves the app identity, and is
  exchanged for a cached installation token scoped to the repository.

Webhook deliveries are authenticated with the app's webhook secret
(``X-Hub-Signature-256``). The receiver runs on a trusted clone; the hub never
holds the app key and never writes reviews itself.
"""

import base64
import binascii
import calendar
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, service
from .errors import AghubError

API_URL = "https://api.github.com"
MARKER = "<!-- aghub:review -->"
CHECK_NAME = "aghub/review"
DEFAULT_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
WEBHOOK_PATH = "/github/webhook"
MAX_WEBHOOK_BYTES = 2 * 1024 * 1024


# -- GitHub App authentication ----------------------------------------------
#
# GitHub Apps sign a short JWT with an RSA private key, then exchange it for an
# installation token. The project has no crypto dependencies, so the small
# PKCS#1 v1.5 signer below is pure Python. It is a signing primitive for a
# trusted local clone, not constant-time code for hostile co-tenants.

_SHA256_DIGEST_INFO = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)


def _der_item(data, offset=0):
    if offset + 2 > len(data):
        raise AghubError("invalid private key: truncated DER")
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + count > len(data):
            raise AghubError("invalid private key: bad DER length")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise AghubError("invalid private key: truncated DER value")
    return tag, data[offset:end], end


def _der_items(data):
    items = []
    offset = 0
    while offset < len(data):
        tag, value, offset = _der_item(data, offset)
        items.append((tag, value))
    return items


def _pem_body(pem, label):
    pattern = (
        r"-----BEGIN " + label + r"-----(.*?)-----END " + label + r"-----"
    )
    match = re.search(pattern, pem, re.DOTALL)
    if not match:
        raise AghubError(f"invalid private key: missing {label} PEM block")
    try:
        return base64.b64decode("".join(match.group(1).split()))
    except (ValueError, binascii.Error) as exc:
        raise AghubError(f"invalid private key: bad PEM base64 ({exc})")


def _int_items(values):
    return [int.from_bytes(value, "big") for _tag, value in values]


def _rsa_numbers(key):
    """Return RSA (n, e, d) from a PEM private or public key."""
    if isinstance(key, bytes):
        key = key.decode("utf-8", errors="replace")
    key = str(key or "")
    if "BEGIN RSA PRIVATE KEY" in key:
        _tag, sequence, _end = _der_item(_pem_body(key, "RSA PRIVATE KEY"))
        values = _int_items(_der_items(sequence))
        if len(values) < 3:
            raise AghubError("invalid RSA private key")
        return values[1], values[2], values[3] if len(values) > 3 else None
    if "BEGIN PRIVATE KEY" in key:
        _tag, sequence, _end = _der_item(_pem_body(key, "PRIVATE KEY"))
        for tag, value in _der_items(sequence):
            if tag != 0x04:
                continue
            _inner_tag, inner, _inner_end = _der_item(value)
            values = _int_items(_der_items(inner))
            if len(values) >= 4:
                return values[1], values[2], values[3]
        raise AghubError("invalid PKCS#8 RSA private key")
    if "BEGIN RSA PUBLIC KEY" in key:
        _tag, sequence, _end = _der_item(_pem_body(key, "RSA PUBLIC KEY"))
        values = _int_items(_der_items(sequence))
        if len(values) < 2:
            raise AghubError("invalid RSA public key")
        return values[0], values[1], None
    if "BEGIN PUBLIC KEY" in key:
        _tag, sequence, _end = _der_item(_pem_body(key, "PUBLIC KEY"))
        for tag, value in _der_items(sequence):
            if tag != 0x03:
                continue
            _inner_tag, inner, _inner_end = _der_item(value[1:])
            values = _int_items(_der_items(inner))
            if len(values) >= 2:
                return values[0], values[1], None
        raise AghubError("invalid RSA public key")
    raise AghubError("invalid key: expected a PEM RSA key")


def load_private_key(value):
    """Accept PEM text, a path, or an env value with escaped newlines."""
    if not value:
        raise AghubError("GitHub App private key is required")
    value = str(value)
    if "BEGIN" in value:
        return value.replace("\\n", "\n")
    path = os.path.expanduser(value)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise AghubError(f"cannot read GitHub App private key: {exc}")


def _rsa_sign(key, message):
    n, e, d = _rsa_numbers(key)
    if d is None:
        raise AghubError("invalid private key: no private exponent")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    size = (n.bit_length() + 7) // 8
    padding = size - len(digest_info) - 3
    if padding < 8:
        raise AghubError("RSA key is too small for SHA-256 signing")
    encoded = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), d, n)
    return signature.to_bytes(size, "big")


def _rsa_verify(key, message, signature):
    n, e, _d = _rsa_numbers(key)
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    try:
        recovered = pow(int.from_bytes(signature, "big"), e, n)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(recovered.to_bytes(size, "big"), expected)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id, private_key, now=None, ttl=600):
    """Create the short-lived RS256 JWT that proves a GitHub App identity."""
    if not app_id:
        raise AghubError("GitHub App id is required")
    stamp = int(time.time() if now is None else now)
    issuer = int(app_id) if str(app_id).isdigit() else str(app_id)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": stamp - 60, "exp": stamp - 60 + int(ttl), "iss": issuer}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )
    signature = _rsa_sign(private_key, signing_input.encode("ascii"))
    return signing_input + "." + _b64url(signature)


def installation_token(
    app_id, private_key, installation_id, api=None, timeout=20, now=None
):
    """Exchange an app JWT for a repository-scoped installation token."""
    if not installation_id:
        raise AghubError("GitHub App installation id is required")
    jwt = app_jwt(app_id, private_key, now=now)
    path = "/app/installations/{}/access_tokens".format(
        urllib.parse.quote(str(installation_id), safe="")
    )
    result = _request(
        "POST", path, {}, token=jwt, api=api, timeout=timeout
    )
    token = result.get("token") if isinstance(result, dict) else None
    if not token:
        raise AghubError("GitHub did not return an installation token")
    return result


def _github_time(value, fallback):
    if not value:
        return fallback
    try:
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return fallback


class AppAuth:
    """Mint and cache one installation token for a trusted process."""

    def __init__(
        self,
        app_id,
        private_key,
        installation_id,
        api=None,
        timeout=20,
    ):
        self.app_id = str(app_id)
        self.private_key = load_private_key(private_key)
        self.installation_id = str(installation_id)
        self.api = api
        self.timeout = timeout
        self._token = None
        self._expires_at = 0

    def token(self, force=False, now=None):
        stamp = time.time() if now is None else float(now)
        if not force and self._token and self._expires_at - stamp > 60:
            return self._token
        result = installation_token(
            self.app_id,
            self.private_key,
            self.installation_id,
            api=self.api,
            timeout=self.timeout,
            now=stamp,
        )
        self._token = result["token"]
        try:
            expires_in = float(result.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._expires_at = _github_time(
            result.get("expires_at"), stamp + expires_in
        )
        return self._token

    def header(self, force=False, now=None):
        return f"Bearer {self.token(force=force, now=now)}"


def app_auth(
    app_id=None,
    private_key=None,
    installation_id=None,
    api=None,
):
    """Build AppAuth from flags/env, or return None when no app is configured."""
    app_id = app_id or os.environ.get("AGHUB_GITHUB_APP_ID") or os.environ.get(
        "GITHUB_APP_ID"
    )
    private_key = (
        private_key
        or os.environ.get("AGHUB_GITHUB_APP_PRIVATE_KEY")
        or os.environ.get("GITHUB_APP_PRIVATE_KEY")
        or os.environ.get("AGHUB_GITHUB_APP_PRIVATE_KEY_PATH")
        or os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    )
    installation_id = (
        installation_id
        or os.environ.get("AGHUB_GITHUB_INSTALLATION_ID")
        or os.environ.get("GITHUB_INSTALLATION_ID")
    )
    if not (app_id or private_key or installation_id):
        return None
    missing = [
        name
        for name, value in (
            ("app id", app_id),
            ("private key", private_key),
            ("installation id", installation_id),
        )
        if not value
    ]
    if missing:
        raise AghubError(
            "GitHub App configuration is incomplete: missing " + ", ".join(missing)
        )
    return AppAuth(app_id, private_key, installation_id, api=api)


# -- webhook verification ----------------------------------------------------


def webhook_signature(secret, body):
    """Return the X-Hub-Signature-256 value for a payload."""
    if not secret:
        raise AghubError("GitHub webhook secret is required")
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hmac.new(
        str(secret).encode("utf-8"), body or b"", hashlib.sha256
    ).hexdigest()
    return "sha256=" + digest


def verify_webhook(secret, body, signature):
    """Constant-time check of a GitHub webhook signature."""
    if not secret:
        raise AghubError("GitHub webhook secret is required")
    if not signature:
        raise AghubError("GitHub webhook signature is missing")
    expected = webhook_signature(secret, body)
    if not hmac.compare_digest(expected, str(signature).strip()):
        raise AghubError("GitHub webhook signature is invalid")
    return True


def _header(headers, name):
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value:
            return value
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value
    return ""


class WebhookReceiver:
    """Verify and route one raw webhook delivery.

    ``on_event(kind, payload, delivery)`` is only called after the signature
    verifies and the JSON payload parses. The callback owns board writes.
    """

    def __init__(self, secret, on_event, max_bytes=MAX_WEBHOOK_BYTES):
        if not secret:
            raise AghubError("GitHub webhook secret is required")
        if not callable(on_event):
            raise AghubError("webhook receiver needs an event callback")
        self.secret = secret
        self.on_event = on_event
        self.max_bytes = max(1, int(max_bytes))

    def receive(self, headers, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        body = body or b""
        if len(body) > self.max_bytes:
            raise AghubError(
                f"webhook payload is too large ({len(body)} > {self.max_bytes} bytes)"
            )
        verify_webhook(
            self.secret, body, _header(headers, "X-Hub-Signature-256")
        )
        kind = _header(headers, "X-GitHub-Event")
        if not kind:
            raise AghubError("GitHub webhook event header is missing")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AghubError(f"invalid GitHub webhook payload: {exc}")
        if not isinstance(payload, dict):
            raise AghubError("invalid GitHub webhook payload: expected an object")
        delivery = _header(headers, "X-GitHub-Delivery")
        result = self.on_event(kind, payload, delivery)
        return {"event": kind, "delivery": delivery, "result": result}


def webhook_server(receiver, host="127.0.0.1", port=8787):
    """A tiny HTTP server for one verified webhook receiver."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload, sort_keys=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/healthz", "/health"):
                self._send(200, {"ok": True, "webhook": WEBHOOK_PATH})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path not in ("/", WEBHOOK_PATH):
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length < 0 or length > receiver.max_bytes:
                self._send(413, {"error": "payload too large"})
                return
            body = self.rfile.read(length) if length else b""
            try:
                result = receiver.receive(self.headers, body)
            except AghubError as exc:
                message = str(exc)
                status = 401 if "signature" in message.lower() else 400
                self._send(status, {"error": message})
                return
            except Exception as exc:  # keep the receiver alive on callback bugs
                self._send(500, {"error": str(exc)})
                return
            self._send(200, result)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server((host, int(port)), Handler)
    server.receiver = receiver
    return server



def load_event(path=None):
    path = path or os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise AghubError(f"cannot read GitHub event payload: {exc}")
    except ValueError as exc:
        raise AghubError(f"invalid GitHub event payload: {exc}")


def event_kind(event=None, name=None):
    name = name or os.environ.get("GITHUB_EVENT_NAME") or ""
    if name:
        return name
    event = event or {}
    if "review" in event:
        return "pull_request_review"
    if "pull_request" in event:
        return "pull_request"
    return ""


def pull_request_info(event):
    """Normalize the PR fields we need from pull_request(_review) payloads."""
    pr = event.get("pull_request") or {}
    if not pr:
        return {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    info = {
        "number": event.get("number") or pr.get("number"),
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "branch": head.get("ref") or "",
        "sha": head.get("sha") or "",
        "base": base.get("ref") or "",
        "url": pr.get("html_url") or "",
        "author": (pr.get("user") or {}).get("login") or "",
        "draft": bool(pr.get("draft")),
        "head_repo": head_repo,
        "action": event.get("action") or "",
    }
    review = event.get("review")
    if review:
        info["review"] = {
            "id": str(review.get("id") or ""),
            "state": (review.get("state") or "").lower(),
            "login": (review.get("user") or {}).get("login") or "",
            "association": review.get("author_association") or "",
            "body": review.get("body") or "",
            "url": review.get("html_url") or "",
        }
    return info


def find_task(tasks, branch=None, text=""):
    """Resolve a PR to a board task by branch, marker, or explicit task id."""
    branch = branch or ""
    text = text or ""
    for task in tasks.values():
        if task.get("branch") and task.get("branch") == branch:
            return task
    marked = re.search(r"aghub-task:\s*(T-\d+)", text, re.IGNORECASE)
    if marked and marked.group(1) in tasks:
        return tasks[marked.group(1)]
    suffix = re.search(r"(?:^|/)(T-\d+)$", branch)
    if suffix and suffix.group(1) in tasks:
        return tasks[suffix.group(1)]
    for match in re.finditer(r"\bT-\d+\b", text):
        if match.group(0) in tasks:
            return tasks[match.group(0)]
    return None


def _repo(repo=None):
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        raise AghubError("GITHUB_REPOSITORY must be owner/name")
    owner, name = repo.split("/", 1)
    return (
        urllib.parse.quote(owner, safe="") + "/" + urllib.parse.quote(name, safe="")
    )


def _token(token=None):
    return token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _request(method, path, payload=None, token=None, api=None, timeout=20):
    token = _token(token)
    if not token:
        raise AghubError("GITHUB_TOKEN is required for GitHub sync")
    api = (api or os.environ.get("GITHUB_API_URL") or API_URL).rstrip("/")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(api + path, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", f"aghub/{__version__}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("message")
        except ValueError:
            detail = body
        raise AghubError(f"GitHub API {exc.code}: {detail or exc.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AghubError(f"cannot reach GitHub: {getattr(exc, 'reason', exc)}")


def upsert_comment(repo=None, number=None, body="", token=None, api=None):
    """Create one marker comment, or update the existing one in place."""
    path = f"/repos/{_repo(repo)}/issues/{int(number)}/comments"
    for page in range(1, 21):
        existing = _request(
            "GET",
            f"{path}?per_page=100&page={page}",
            token=token,
            api=api,
        )
        if not isinstance(existing, list):
            break
        for comment in existing:
            if MARKER in str(comment.get("body") or ""):
                updated = _request(
                    "PATCH",
                    f"/repos/{_repo(repo)}/issues/comments/{comment['id']}",
                    {"body": body},
                    token=token,
                    api=api,
                )
                return {"id": updated.get("id"), "updated": True}
        if len(existing) < 100:
            break
    created = _request(
        "POST", path, {"body": body}, token=token, api=api
    )
    return {"id": created.get("id"), "updated": False}


def upsert_check(repo=None, sha=None, check=None, token=None, api=None):
    """Create the aghub check run, or update the one for this commit."""
    if not sha:
        raise AghubError("pull request head sha is missing")
    repo_path = _repo(repo)
    payload = {"name": CHECK_NAME, "status": "completed", **check}
    listing = _request(
        "GET",
        f"/repos/{repo_path}/commits/{urllib.parse.quote(str(sha), safe='')}"
        f"/check-runs?check_name={urllib.parse.quote(CHECK_NAME, safe='')}&per_page=1",
        token=token,
        api=api,
    )
    runs = listing.get("check_runs") if isinstance(listing, dict) else None
    if runs:
        updated = _request(
            "PATCH",
            f"/repos/{repo_path}/check-runs/{runs[0]['id']}",
            payload,
            token=token,
            api=api,
        )
        return {"id": updated.get("id"), "updated": True}
    created = _request(
        "POST",
        f"/repos/{repo_path}/check-runs",
        {"head_sha": str(sha), **payload},
        token=token,
        api=api,
    )
    return {"id": created.get("id"), "updated": False}


def mark_stale_approval(packet, head_sha):
    """Flag an approval that was recorded for a different commit."""
    approval = packet.get("approval") or {}
    approval["stale"] = bool(
        approval.get("stale")
        or (
            approval.get("decision") == "approved"
            and (approval.get("source") == "github" or approval.get("binding") == "commit")
            and approval.get("commit")
            and head_sha
            and not service.same_commit(approval["commit"], head_sha)
        )
    )
    return approval


def _code(value):
    return "`" + str(value).replace("`", "'") + "`"


def _approval_line(approval):
    line = f"**approval** {approval['decision']}"
    required = int(approval.get("required_approvals") or 1)
    approved = int(approval.get("approved_count") or 0)
    if approval.get("required") and (required > 1 or approved > 1):
        line += f" ({approved}/{required})"
    if approval.get("reviewer"):
        line += f" by {_code(approval['reviewer'])}"
    if approval.get("stale"):
        line += " (for an older commit)"
    if approval.get("expired"):
        line += f" ({len(approval['expired'])} expired)"
    if approval.get("ineligible"):
        line += f" ({len(approval['ineligible'])} ineligible)"
    return line


def _policy_lines(task, approval):
    lines = []
    required = int(approval.get("required_approvals") or 1)
    approved = int(approval.get("approved_count") or 0)
    bits = []
    if required > 1:
        bits.append(f"{approved}/{required} approvals")
    if approval.get("ttl_seconds"):
        bits.append(f"approval ttl {approval['ttl_seconds']}s")
    groups = approval.get("required_groups") or []
    if groups:
        missing = set(approval.get("missing_groups") or [])
        bits.append(
            "groups "
            + ", ".join(
                _code(name) + (" (missing)" if name in missing else "")
                for name in groups
            )
        )
    if approval.get("reviewers"):
        bits.append(
            "reviewers " + ", ".join(_code(name) for name in approval["reviewers"])
        )
    if bits:
        lines.append("**review policy** " + " · ".join(bits))
    missing = approval.get("missing_groups") or []
    if missing:
        hints = []
        for name in missing:
            group = (approval.get("groups") or {}).get(name) or {}
            members = group.get("members") or []
            if members:
                hints.append(f"{_code(name)} (member: {', '.join(_code(item) for item in members)})")
            else:
                hints.append(f"{_code(name)} (undefined)")
        lines.append("**waiting on** " + " · ".join(hints))
    return lines


def comment_body(packet):
    """Markdown for the single sticky review comment on a PR."""
    task = packet["task"]
    approval = packet["approval"]
    lines = [
        MARKER,
        f"## aghub · {task['id']} {task.get('title') or ''}",
        "",
        f"**status** {packet['status']} · **owner** "
        f"{_code(task.get('owner') or '—')} · **branch** "
        f"{_code(task.get('branch') or '—')}",
        "",
        _approval_line(approval),
    ]
    lines.extend(_policy_lines(task, approval))
    if approval.get("note"):
        lines.append(f"> {approval['note']}")
    if task.get("paths"):
        lines.append(
            "**paths** " + ", ".join(_code(path) for path in task["paths"])
        )
    if packet["contracts"]:
        lines.append(
            "**contracts** "
            + ", ".join(
                _code(item["ref"])
                + (" (newer frozen version available)" if item.get("stale") else "")
                for item in packet["contracts"]
            )
        )

    if packet["queue"]:
        lines.extend(["", "### Queue", ""])
        for entry in packet["queue"][-3:]:
            reason = f" — {entry.get('reason')}" if entry.get("reason") else ""
            lines.append(
                f"- {_code(entry.get('id'))} {entry.get('status')}{reason}"
            )
    else:
        lines.extend(["", "_Not submitted to the merge queue yet._"])

    changes = packet["changes"]
    if not changes.get("error") and changes.get("files"):
        lines.extend(["", "### Changes", ""])
        lines.append(
            f"{_code(changes['base'])} → {_code(changes['branch'])} · "
            f"{len(changes['files'])} file(s)"
        )
        if changes.get("stat"):
            lines.extend(["", "```", changes["stat"], "```"])

    if packet.get("failure"):
        trace = packet["failure"]
        lines.extend(
            [
                "",
                "### Failure",
                "",
                f"**{trace.get('kind') or 'failure'}** — {trace.get('reason') or ''}",
            ]
        )

    if approval.get("stale"):
        lines.extend(
            [
                "",
                "> This approval was recorded for an older commit. Re-approve the "
                "current head before merging.",
            ]
        )
    elif approval.get("satisfied") is False and approval.get("reason"):
        lines.extend(
            [
                "",
                f"> {approval['reason']}",
            ]
        )
    elif approval["required"] and approval["decision"] == "changes_requested":
        lines.extend(
            [
                "",
                "> Changes were requested. Address the review, then approve.",
            ]
        )
    elif approval["required"] and approval["decision"] != "approved":
        lines.extend(
            [
                "",
                "> A human approval is required. Approve the pull request, or run "
                f"`aghub review {task['id']} --approve --note ...`.",
            ]
        )
    lines.extend(
        [
            "",
            f"<sub>updated {packet['generated_at']} · aghub {__version__}</sub>",
        ]
    )
    return "\n".join(lines)


def check_payload(packet, details_url=None):
    """Turn a review packet into one check-run conclusion + summary."""
    task = packet["task"]
    approval = packet["approval"]
    queue = packet["queue"]
    latest = queue[-1] if queue else None
    if task.get("status") == "done" or (latest and latest.get("status") == "merged"):
        conclusion = "success"
        label = "merged"
    elif latest and latest.get("status") == "rejected":
        conclusion = "failure"
        label = "queue rejected"
    elif approval.get("stale"):
        conclusion = "action_required"
        label = "approval is for an older commit"
    elif approval.get("satisfied") is False:
        conclusion = "action_required"
        label = "review policy not satisfied"
    elif approval["decision"] == "changes_requested":
        conclusion = "action_required"
        label = "changes requested"
    elif approval["required"] and approval["decision"] != "approved":
        conclusion = "action_required"
        label = "approval required"
    elif latest and latest.get("status") in ("waiting", "merging"):
        conclusion = "success"
        label = "ready for the queue"
    else:
        conclusion = "neutral"
        label = "in progress"

    details = [
        f"**{label}**",
        "",
        f"- task: {_code(task['id'])} {task.get('title') or ''}",
        f"- owner: {_code(task.get('owner') or '—')}",
        f"- branch: {_code(task.get('branch') or '—')}",
        f"- approval: {approval['decision']}"
        + (" (stale; re-approve this head)" if approval.get("stale") else ""),
    ]
    required = int(approval.get("required_approvals") or 1)
    approved = int(approval.get("approved_count") or 0)
    if approval.get("required") and (required > 1 or approved > 1):
        details.append(f"- approvals: {approved}/{required}")
    if approval.get("required_groups"):
        missing = set(approval.get("missing_groups") or [])
        details.append(
            "- review groups: "
            + ", ".join(
                name + (" (missing)" if name in missing else "")
                for name in approval["required_groups"]
            )
        )
    if approval.get("ttl_seconds"):
        details.append(f"- approval ttl: {approval['ttl_seconds']}s")
    if approval.get("expired"):
        details.append(f"- expired reviews: {len(approval['expired'])}")
    if approval.get("ineligible"):
        details.append(f"- ineligible reviews: {len(approval['ineligible'])}")
    if approval.get("reason"):
        details.append(f"- policy: {approval['reason']}")
    if approval.get("note"):
        details.append(f"- review note: {approval['note']}")
    if latest:
        details.append(
            f"- queue: {latest.get('id')} {latest.get('status')}"
            + (f" — {latest.get('reason')}" if latest.get("reason") else "")
        )
    else:
        details.append("- queue: not submitted")
    if packet.get("failure"):
        trace = packet["failure"]
        details.append(
            f"- failure: {trace.get('kind') or 'unknown'} — "
            f"{trace.get('reason') or ''}"
        )
    payload = {
        "name": CHECK_NAME,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": f"{task['id']} {task.get('title') or ''}".strip()[:255],
            "summary": "\n".join(details),
        },
    }
    if details_url:
        payload["details_url"] = details_url
    return payload
