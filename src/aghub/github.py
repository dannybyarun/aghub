"""GitHub bridge: put aghub review state where humans already work.

The bridge is deliberately small and dependency-free. It reads the event payload
that GitHub Actions already writes, renders one review comment, and publishes one
check run. Approval decisions come from pull-request reviews, not from a public
webhook endpoint, so there is no OAuth server to operate.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .errors import AghubError

API_URL = "https://api.github.com"
MARKER = "<!-- aghub:review -->"
CHECK_NAME = "aghub/review"
DEFAULT_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


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
    """Flag a GitHub approval that was recorded for a different PR head."""
    approval = packet.get("approval") or {}
    approval["stale"] = bool(
        approval.get("decision") == "approved"
        and approval.get("source") == "github"
        and approval.get("commit")
        and head_sha
        and str(approval["commit"]) != str(head_sha)
    )
    return approval


def _code(value):
    return "`" + str(value).replace("`", "'") + "`"


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
        f"**approval** {approval['decision']}"
        + (f" by {_code(approval['reviewer'])}" if approval.get("reviewer") else "")
        + (" (for an older commit)" if approval.get("stale") else ""),
    ]
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
