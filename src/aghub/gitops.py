import hashlib
import os
import subprocess

from .errors import AghubError


def git(args, cwd=None, check=True, input=None, timeout=180):
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise AghubError("git is not installed or not on PATH")
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AghubError(f"git {' '.join(args)} failed: {detail}")
    return proc


def out(args, cwd=None):
    return git(args, cwd).stdout.strip()


def repo_root(cwd=None):
    proc = git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if proc.returncode != 0:
        raise AghubError("not inside a git repository")
    return proc.stdout.strip()


def common_dir(repo):
    return out(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)


def has_remote(repo, name):
    if not name:
        return False
    return git(["remote", "get-url", name], repo, check=False).returncode == 0


def remote_branch_exists(repo, remote, branch):
    if not has_remote(repo, remote):
        return False
    return (
        git(["ls-remote", "--exit-code", "--heads", remote, branch], repo, check=False).returncode == 0
    )


def ref_exists(repo, ref):
    return git(["rev-parse", "--verify", "--quiet", ref], repo, check=False).returncode == 0


def short(repo, ref):
    proc = git(["rev-parse", "--short", ref], repo, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else "-"


def is_ancestor(repo, ancestor, descendant):
    return (
        git(["merge-base", "--is-ancestor", ancestor, descendant], repo, check=False).returncode == 0
    )


def current_branch(repo):
    proc = git(["rev-parse", "--abbrev-ref", "HEAD"], repo, check=False)
    if proc.returncode != 0:
        return None
    name = proc.stdout.strip()
    return None if name == "HEAD" else name


def worktree_fingerprint(repo):
    """Content hash of uncommitted and untracked (non-ignored) input files.

    Evals execute a snapshot of the checkout, not just ``HEAD``. Returns
    ``None`` for a clean worktree so fingerprints from older boards still
    compare equal.
    """
    digest = hashlib.sha256()
    changed = False
    diff = git(["diff", "HEAD", "--binary", "--no-ext-diff"], repo, check=False)
    if diff.returncode == 0 and diff.stdout:
        changed = True
        digest.update(diff.stdout.encode("utf-8", "surrogateescape"))
    listing = git(["ls-files", "--others", "--exclude-standard", "-z"], repo, check=False)
    for name in sorted(filter(None, (listing.stdout or "").split("\0"))):
        path = os.path.join(repo, name)
        digest.update(b"\0" + name.encode("utf-8", "surrogateescape"))
        changed = True
        if os.path.islink(path):
            digest.update(b"link:" + os.readlink(path).encode("utf-8", "surrogateescape"))
        elif os.path.isfile(path):
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest() if changed else None
