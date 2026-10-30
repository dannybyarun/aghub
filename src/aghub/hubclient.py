import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote


class HubError(Exception):
    """The hub is unreachable or returned an error."""


class HubConflict(HubError):
    """The hub rejected a lease because someone else holds it."""


_warned = False


def warn_once(message):
    global _warned
    if _warned:
        return
    _warned = True
    print(f"aghub: warning: {message}", file=sys.stderr)


def _token(token=None):
    return token or os.environ.get("AGHUB_HUB_TOKEN")


def request(base, method, path, payload=None, token=None, timeout=5):
    if not base:
        raise HubError("no hub configured")
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
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except ValueError:
            detail = {}
        message = detail.get("error") or body or f"HTTP {exc.code}"
        if exc.code == 409:
            raise HubConflict(message)
        raise HubError(f"{exc.code}: {message}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HubError(str(getattr(exc, "reason", exc)))


def health(base, token=None):
    return request(base, "GET", "/api/health", token=token, timeout=3)


def projects(base, token=None):
    return request(base, "GET", "/api/projects", token=token)


def overview(base, token=None):
    return request(base, "GET", "/api/overview", token=token)


def audit(base, project=None, after=0, limit=100, token=None):
    query = f"?after={int(after)}&limit={int(limit)}"
    if project:
        query += "&project=" + quote(project)
    return request(base, "GET", "/api/audit" + query, token=token)


def board(base, project=None, token=None):
    path = "/api/board"
    if project:
        path += "?project=" + quote(project)
    return request(base, "GET", path, token=token)


def history(base, project=None, after=0, limit=50, token=None):
    query = f"?after={int(after)}&limit={int(limit)}"
    if project:
        query += "&project=" + quote(project)
    return request(base, "GET", "/api/history" + query, token=token)


def publish(base, project, snapshot, token=None):
    return request(base, "POST", "/api/snapshot", {"project": project, "snapshot": snapshot}, token=token)


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
