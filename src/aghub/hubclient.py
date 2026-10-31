import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote


class HubError(Exception):
    """The hub is unreachable or returned an error."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class HubConflict(HubError):
    """The hub rejected a lease because someone else holds it."""


class _RetryableHubError(HubError):
    """A hub was unreachable or returned a transient gateway error."""


_warned = False


def warn_once(message):
    global _warned
    if _warned:
        return
    _warned = True
    print(f"aghub: warning: {message}", file=sys.stderr)


def _token(token=None):
    return token or os.environ.get("AGHUB_HUB_TOKEN")


def _request_one(base, method, path, payload=None, token=None, timeout=5, raw=False):
    url = base.rstrip("/") + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    outgoing = _token(token)
    if outgoing:
        req.add_header("Authorization", f"Bearer {outgoing}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            if raw:
                return body
            try:
                return json.loads(body) if body else {}
            except ValueError as exc:
                raise _RetryableHubError(f"invalid JSON response: {exc}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except ValueError:
            detail = {}
        message = detail.get("error") or body or f"HTTP {exc.code}"
        if exc.code == 409:
            raise HubConflict(message, status=409, payload=detail)
        if exc.code in (502, 503, 504):
            raise _RetryableHubError(f"{exc.code}: {message}")
        raise HubError(f"{exc.code}: {message}", status=exc.code, payload=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _RetryableHubError(str(getattr(exc, "reason", exc)))


def request(base, method, path, payload=None, token=None, timeout=5, raw=False):
    """Call a hub, failing over across a list of URLs on transport errors.

    A 409, 401, 403, 429 or other 4xx is authoritative and is never retried
    against another hub. Only connection failures and 502/503/504 fail over.
    """
    if isinstance(base, str):
        candidates = [base] if base else []
    else:
        candidates = [str(item) for item in (base or []) if item]
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise HubError("no hub configured")
    if len(candidates) == 1:
        try:
            return _request_one(
                candidates[0], method, path, payload, token, timeout, raw
            )
        except _RetryableHubError as exc:
            raise HubError(str(exc))
    failures = []
    for candidate in candidates:
        try:
            return _request_one(candidate, method, path, payload, token, timeout, raw)
        except _RetryableHubError as exc:
            failures.append(f"{candidate}: {exc}")
    raise HubError("all hubs failed: " + "; ".join(failures))


def health(base, token=None):
    return request(base, "GET", "/api/health", token=token, timeout=3)


def projects(base, token=None):
    return request(base, "GET", "/api/projects", token=token)


def overview(base, token=None):
    return request(base, "GET", "/api/overview", token=token)


def audit(base, project=None, after=0, limit=100, actor=None, token=None):
    query = f"?after={int(after)}&limit={int(limit)}"
    if project:
        query += "&project=" + quote(project)
    if actor:
        query += "&actor=" + quote(actor)
    return request(base, "GET", "/api/audit" + query, token=token)


def audit_export(
    base, project=None, actor=None, after=0, limit=1000, fmt="jsonl", token=None
):
    query = f"?format={quote(fmt)}&after={int(after)}&limit={int(limit)}"
    if project:
        query += "&project=" + quote(project)
    if actor:
        query += "&actor=" + quote(actor)
    return request(
        base, "GET", "/api/audit/export" + query, token=token, raw=True
    )


def board(base, project=None, token=None):
    path = "/api/board"
    if project:
        path += "?project=" + quote(project)
    return request(base, "GET", path, token=token)


def board_lock_status(base, project=None, token=None):
    path = "/api/board/lock"
    if project:
        path += "?project=" + quote(project)
    return request(base, "GET", path, token=token)


def board_lock(base, project, holder, ttl=60, force=False, token=None):
    return request(
        base,
        "POST",
        "/api/board/lock",
        {
            "project": project,
            "holder": holder,
            "ttl": ttl,
            "force": force,
        },
        token=token,
    )


def board_unlock(base, project, holder, fencing=None, force=False, token=None):
    return request(
        base,
        "POST",
        "/api/board/unlock",
        {
            "project": project,
            "holder": holder,
            "fencing": fencing,
            "force": force,
        },
        token=token,
    )


def history(base, project=None, after=0, limit=50, token=None):
    query = f"?after={int(after)}&limit={int(limit)}"
    if project:
        query += "&project=" + quote(project)
    return request(base, "GET", "/api/history" + query, token=token)


def publish(base, project, snapshot, token=None, fencing=None):
    payload = {"project": project, "snapshot": snapshot}
    if fencing is not None:
        payload["fencing"] = fencing
    return request(base, "POST", "/api/snapshot", payload, token=token)


def publish_review_packet(base, project, task, packet, token=None):
    return request(
        base,
        "POST",
        "/api/review/packet",
        {"project": project, "task": task, "packet": packet},
        token=token,
    )


def review_packet(base, project, task, token=None):
    query = "?project=" + quote(project) + "&task=" + quote(task)
    return request(base, "GET", "/api/review/packet" + query, token=token)


def review_packets(base, project, token=None):
    query = "?project=" + quote(project)
    return request(base, "GET", "/api/review/packets" + query, token=token)


def review_requests(base, project=None, status="pending", limit=100, token=None):
    query = f"?status={quote(status)}&limit={int(limit)}"
    if project:
        query += "&project=" + quote(project)
    return request(base, "GET", "/api/review/requests" + query, token=token)


def request_review(
    base, project, task, decision, note="", head=None, reviewer=None, token=None
):
    return request(
        base,
        "POST",
        "/api/review/requests",
        {
            "project": project,
            "task": task,
            "decision": decision,
            "note": note,
            "head": head,
            "reviewer": reviewer,
        },
        token=token,
    )


def claim_review_request(base, request_id, token=None):
    return request(
        base, "POST", "/api/review/claim", {"id": request_id}, token=token
    )


def resolve_review_request(
    base, request_id, status, record_id=None, error=None, token=None
):
    return request(
        base,
        "POST",
        "/api/review/resolve",
        {
            "id": request_id,
            "status": status,
            "record_id": record_id,
            "error": error,
        },
        token=token,
    )


def cancel_review_request(base, request_id, token=None):
    return request(
        base, "POST", "/api/review/cancel", {"id": request_id}, token=token
    )


def heartbeat(base, project, agent, task=None, branch=None, ttl=600, token=None, machine=None):
    return request(
        base,
        "POST",
        "/api/heartbeat",
        {
            "project": project,
            "agent": agent,
            "task": task,
            "branch": branch,
            "ttl": ttl,
            "machine": machine,
        },
        token=token,
    )


def claim(base, project, task, agent, paths, ttl=600, steal=False, force=False, token=None):
    return request(
        base,
        "POST",
        "/api/claim",
        {
            "project": project,
            "task": task,
            "agent": agent,
            "paths": paths,
            "ttl": ttl,
            "steal": steal,
            "force": force,
        },
        token=token,
    )


def release(base, project, task, agent, force=False, token=None):
    return request(
        base,
        "POST",
        "/api/release",
        {"project": project, "task": task, "agent": agent, "force": force},
        token=token,
    )


def expire(base, project, agent, ttl=600, token=None):
    return request(
        base,
        "POST",
        "/api/debug/expire",
        {"project": project, "agent": agent, "ttl": ttl},
        token=token,
    )
