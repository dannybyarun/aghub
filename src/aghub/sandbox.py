"""Local eval execution with a best-effort, dependency-free sandbox.

The default is a snapshot of the checkout run under Bubblewrap when it is
available: no host network, a read-only host filesystem, and a writable copy of
the repository. Without Bubblewrap, the command still runs against a snapshot
with a scrubbed environment and POSIX resource limits, but it can see the host
filesystem and network. `require` refuses that fallback.
"""

import functools
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass

from .errors import AghubError

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None

DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
SAFE_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
)
FSIZE_LIMIT = 64 * 1024 * 1024
NOFILE_LIMIT = 1024
NPROC_LIMIT = 256


@dataclass
class EvalRun:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    sandbox: dict


def _bwrap_ok(path):
    probe = subprocess.run(
        [path, "--unshare-all", "--ro-bind", "/", "/", "--", "/bin/true"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return probe.returncode == 0


@functools.lru_cache(maxsize=1)
def bubblewrap():
    path = shutil.which("bwrap")
    if not path:
        return None
    try:
        return path if _bwrap_ok(path) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _snapshot_root():
    candidates = [
        os.environ.get("AGHUB_EVAL_ROOT"),
        "/var/tmp",
        tempfile.gettempdir(),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return None


def _copy_file(source, target):
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if os.path.islink(source):
        link = os.readlink(source)
        if os.path.lexists(target):
            os.unlink(target)
        os.symlink(link, target)
        return
    if os.path.isdir(source):
        shutil.copytree(
            source,
            target,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
            dirs_exist_ok=True,
        )
        return
    shutil.copy2(source, target)


def snapshot(repo):
    """Copy tracked and untracked, non-ignored files into a private directory."""
    root = _snapshot_root()
    target = tempfile.mkdtemp(prefix="aghub-eval-", dir=root)
    listing = subprocess.run(
        ["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=False,
    )
    if listing.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        fallback = tempfile.mkdtemp(prefix="aghub-eval-", dir=root)
        os.rmdir(fallback)
        return shutil.copytree(
            repo,
            fallback,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
        )
    try:
        for raw in listing.stdout.split(b"\0"):
            if not raw:
                continue
            relative = raw.decode("utf-8", "surrogateescape")
            parts = relative.split("/")
            if any(part in ("", ".", "..") for part in parts):
                continue
            source = os.path.join(repo, relative)
            if not os.path.lexists(source):
                continue
            _copy_file(source, os.path.join(target, relative))
        sandbox_dir = os.path.join(target, ".aghub-sandbox")
        os.makedirs(os.path.join(sandbox_dir, "home"), exist_ok=True)
        os.makedirs(os.path.join(sandbox_dir, "tmp"), exist_ok=True)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _safe_env(home, tmp):
    env = {
        "PATH": os.environ.get("PATH") or DEFAULT_PATH,
        "HOME": home,
        "TMPDIR": tmp,
    }
    for key in SAFE_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _limits(timeout):
    def apply():
        if resource is None:
            return
        seconds = max(1, int(timeout) + 1)
        for name, value in (
            ("RLIMIT_CPU", (seconds, seconds + 1)),
            ("RLIMIT_FSIZE", (FSIZE_LIMIT, FSIZE_LIMIT)),
            ("RLIMIT_NOFILE", (NOFILE_LIMIT, NOFILE_LIMIT)),
            ("RLIMIT_NPROC", (NPROC_LIMIT, NPROC_LIMIT)),
        ):
            limit = getattr(resource, name, None)
            if limit is None:
                continue
            try:
                resource.setrlimit(limit, value)
            except (OSError, ValueError):
                pass

    return apply


def _execute(argv, cwd, env, timeout, sandbox, preexec=None, cleanup=None):
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=preexec,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate()
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return EvalRun(
        exit_code=124 if timed_out else proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        sandbox=sandbox,
    )


def run(command, repo, timeout=300, mode="auto"):
    """Run a shell command under the requested sandbox policy."""
    if mode not in ("auto", "off", "require"):
        raise AghubError(f"unknown sandbox mode '{mode}'")
    if mode == "off":
        return _execute(
            ["/bin/sh", "-c", command],
            repo,
            None,
            timeout,
            {"mode": "off", "backend": "none", "network": "host", "snapshot": False},
        )

    bwrap = bubblewrap()
    if bwrap:
        work = snapshot(repo)
        home = os.path.join(work, ".aghub-sandbox", "home")
        tmp = os.path.join(work, ".aghub-sandbox", "tmp")
        args = [
            bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        for hidden in ("/home", "/root"):
            if os.path.isdir(hidden):
                args.extend(["--tmpfs", hidden])
        args.extend(
            [
                "--bind",
                work,
                work,
                "--bind",
                tmp,
                "/tmp",
                "--chdir",
                work,
                "--setenv",
                "PATH",
                os.environ.get("PATH") or DEFAULT_PATH,
                "--setenv",
                "HOME",
                home,
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
            ]
        )
        for key in SAFE_KEYS:
            if key in os.environ:
                args.extend(["--setenv", key, os.environ[key]])
        args.extend(["--", "/bin/sh", "-c", command])
        metadata = {
            "mode": mode,
            "backend": "bubblewrap",
            "network": "none",
            "snapshot": True,
        }
        try:
            return _execute(
                args, None, None, timeout, metadata, cleanup=work
            )
        except OSError as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise AghubError(f"could not start bubblewrap: {exc}")

    if mode == "require":
        raise AghubError(
            "sandbox=require needs bubblewrap (`bwrap`) on PATH; "
            "install it or use --sandbox auto"
        )

    work = snapshot(repo)
    env = _safe_env(
        os.path.join(work, ".aghub-sandbox", "home"),
        os.path.join(work, ".aghub-sandbox", "tmp"),
    )
    metadata = {
        "mode": mode,
        "backend": "copy",
        "network": "host",
        "snapshot": True,
        "limits": bool(resource),
    }
    try:
        return _execute(
            ["/bin/sh", "-c", command],
            work,
            env,
            timeout,
            metadata,
            preexec=_limits(timeout),
            cleanup=work,
        )
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise AghubError(f"could not start the eval command: {exc}")
