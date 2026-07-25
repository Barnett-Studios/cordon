"""Opt-in live-Docker smoke for bin/cordon-run.sh.

Needs a running Docker daemon plus the cordon runtime image
(docker/runtime.Dockerfile — built locally as `cordon-runtime:smoke`, or a custom
tag via CORDON_IMAGE). Skipped unless CORDON_LIVE_SMOKES=1; never part of the
default gate.

Proves two boundary claims by observing container behavior, not by
re-reading the script's flags (test_cordon_run_script.py covers that statically):

  (a) --network none blocks an egress attempt deterministically — a plain non-zero
      exit, fast, not a hang.
  (b) a command that breaches the memory ceiling is killed by Docker's cgroup limit
      — a bounded non-zero/timeout exit, not an unbounded hang.
"""
import os
import pathlib
import shutil
import subprocess
import time

import pytest

MODULE_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = MODULE_ROOT / "bin" / "cordon-run.sh"
DOCKERFILE = MODULE_ROOT / "docker" / "runtime.Dockerfile"
IMAGE = os.environ.get("CORDON_IMAGE", "cordon-runtime:smoke")

pytestmark = pytest.mark.skipif(
    os.environ.get("CORDON_LIVE_SMOKES") != "1",
    reason="opt-in live-Docker smoke — set CORDON_LIVE_SMOKES=1 to run",
)


def _docker_available():
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


def _ensure_image():
    """Use the image if already built; otherwise build it from the module's own
    Dockerfile. Returns False (never raises) so the caller can skip gracefully."""
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return True
    if not DOCKERFILE.exists():
        return False
    build = subprocess.run(
        ["docker", "build", "-q", "-f", str(DOCKERFILE), "-t", IMAGE, str(MODULE_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return build.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_docker_and_image():
    if not _docker_available():
        pytest.skip("Docker daemon not available")
    if not _ensure_image():
        pytest.skip(f"cordon runtime image {IMAGE!r} unavailable and could not be built")


def _sandboxed_worktree(tmp_path):
    # cordon-run.sh runs the container as -u 1000:1000; make the mounted worktree
    # world-accessible so that uid works regardless of the host uid.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    os.chmod(worktree, 0o777)
    return worktree


def test_network_egress_is_blocked_deterministically_and_fast(tmp_path):
    worktree = _sandboxed_worktree(tmp_path)
    accept_cmd = (
        "python3 -c \"import urllib.request; "
        "urllib.request.urlopen('http://1.1.1.1', timeout=3)\""
    )
    t0 = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT), str(worktree), IMAGE, "sh", "-c", accept_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - t0
    assert result.returncode != 0, (
        "egress must be blocked under --network none, not silently allowed: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert elapsed < 20, f"a blocked egress attempt should fail fast, took {elapsed:.1f}s (a hang, not a bounded failure)"


def test_memory_ceiling_breach_is_a_bounded_kill_not_a_hang(tmp_path):
    worktree = _sandboxed_worktree(tmp_path)
    # Allocate well past the sandbox's --memory 2g ceiling; bytearray() forces the
    # pages to be committed (not merely reserved), so the cgroup limit is actually
    # exercised rather than optimistically overcommitted.
    accept_cmd = "python3 -c \"b = bytearray(3 * 1024**3); print(len(b))\""
    t0 = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT), str(worktree), IMAGE, "sh", "-c", accept_cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.monotonic() - t0
    assert result.returncode != 0, (
        "a 3GiB allocation must be killed under the 2g cgroup ceiling, not succeed: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert elapsed < 45, f"the OOM-kill should be bounded, not a hang, took {elapsed:.1f}s"


# ── RC-1 / cordon#2: the worktree is a trust boundary — .git is outside it ──────
#
# These assert the property that matters: **the host does not execute** what the
# container wrote. They do NOT assert that a mount flag is present (that is the
# static test's job, and a flag assertion passes even if the flag does nothing).
#
# Three false-green traps are neutralized explicitly. Without them these tests pass
# on UNFIXED code on a typical developer machine, proving nothing:
#
#   1. **The bind mount must actually work.** On Docker Desktop for macOS a bind
#      whose source is outside the shared paths (pytest's `tmp_path` lives under
#      /private/var/folders/...) is silently created empty inside the VM as
#      root:root — so the container can write nothing, and an "attack failed" result
#      is vacuous. `_bindable_worktree` roots the tree under $HOME and a probe
#      SKIPS (never passes) if the container still cannot write.
#   2. **A global core.hooksPath** redirects hook lookup away from .git/hooks/
#      entirely. dotclaude installs one (`~/.git-hooks`, "active in every repo"), so
#      a planted .git/hooks/post-checkout would never fire regardless of the
#      sandbox. Every host git call runs with GIT_CONFIG_GLOBAL=/dev/null and the
#      fixture sets core.hooksPath=.git/hooks explicitly.
#   3. **The exec bit** — git silently skips a non-executable hook.
#
# The .git/config vector (a) is the one that actually fires in this framework's
# deployment: rewriting .git/config overrides both the global hooksPath AND any
# consumer-side core.hooksPath=/dev/null mitigation, because that setting lives in
# the very file being rewritten. The classic .git/hooks vector (b) is the dead one.

HOST_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _host_git(worktree, *args):
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, text=True, timeout=60, env=HOST_GIT_ENV,
    )


def _run_cordon(worktree, cmd):
    return subprocess.run(
        ["bash", str(SCRIPT), str(worktree), IMAGE, "sh", "-c", cmd],
        capture_output=True, text=True, timeout=120,
    )


@pytest.fixture
def bindable_worktree(tmp_path):
    """A worktree the container can genuinely write, or a SKIP.

    Rooted under $HOME because Docker Desktop for macOS only bind-mounts shared
    paths; `tmp_path` is not one, and a non-shared source yields an empty root-owned
    directory in the VM instead of a mount. Yields the path; removes it afterwards.
    """
    import tempfile
    base = pathlib.Path(tempfile.mkdtemp(prefix="cordon-rc1-", dir=pathlib.Path.home()))
    worktree = base / "worktree"
    worktree.mkdir()
    os.chmod(base, 0o777)
    os.chmod(worktree, 0o777)
    probe = subprocess.run(
        ["docker", "run", "--rm", "-u", "1000:1000", "-v", f"{worktree}:/work:rw",
         "-w", "/work", IMAGE, "sh", "-c", "touch /work/.probe"],
        capture_output=True, text=True, timeout=120,
    )
    if probe.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(
            "container cannot write the bind-mounted worktree "
            f"(uid mapping / unshared path): {probe.stderr.strip()!r} — the escape "
            "tests would be vacuous here, so they are skipped rather than passed"
        )
    (worktree / ".probe").unlink(missing_ok=True)
    try:
        yield worktree
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _make_repo(worktree):
    """Plain `git init` with hooks ENABLED — deliberately not a NodeWorkspace, which
    sets core.hooksPath=/dev/null and would mask the escape."""
    _host_git(worktree, "init", "-q")
    _host_git(worktree, "config", "user.email", "t@example.com")
    _host_git(worktree, "config", "user.name", "t")
    _host_git(worktree, "config", "core.hooksPath", ".git/hooks")
    (worktree / "seed.txt").write_text("seed\n")
    _host_git(worktree, "add", "-A")
    _host_git(worktree, "commit", "-q", "-m", "seed")
    for p in [worktree, *worktree.rglob("*")]:
        try:
            os.chmod(p, 0o777)
        except OSError:
            pass
    return worktree


def test_rc1_planted_git_config_does_not_execute_on_host(bindable_worktree):
    """(a) THE LIVE VECTOR — a container that can rewrite .git/config points
    core.hooksPath at a directory it also controls."""
    worktree = _make_repo(bindable_worktree)
    sentinel = worktree / "PWNED_VIA_CONFIG"
    _run_cordon(worktree, (
        "mkdir -p /work/evilhooks && "
        "printf '#!/bin/sh\\ntouch /work/PWNED_VIA_CONFIG\\n' > /work/evilhooks/post-checkout && "
        "chmod +x /work/evilhooks/post-checkout && "
        "printf '[core]\\n\\thooksPath = /work/evilhooks\\n' >> /work/.git/config"
    ))
    assert "evilhooks" not in (worktree / ".git" / "config").read_text(), (
        ".git/config was rewritten from inside the sandbox — the container can "
        "redirect hook lookup to a directory it controls"
    )
    _host_git(worktree, "checkout", "-b", "other")
    _host_git(worktree, "checkout", "-")
    assert not sentinel.exists(), (
        "SANDBOX ESCAPE: a hook planted inside the container executed on the host"
    )


def test_rc1_planted_git_hook_does_not_execute_on_host(bindable_worktree):
    """(b) The classic vector, kept honest by re-enabling .git/hooks in the fixture."""
    worktree = _make_repo(bindable_worktree)
    sentinel = worktree / "PWNED_VIA_HOOK"
    _run_cordon(worktree, (
        "printf '#!/bin/sh\\ntouch /work/PWNED_VIA_HOOK\\n' > /work/.git/hooks/post-checkout && "
        "chmod +x /work/.git/hooks/post-checkout"
    ))
    assert not (worktree / ".git" / "hooks" / "post-checkout").exists(), (
        ".git/hooks/post-checkout was written from inside the sandbox"
    )
    _host_git(worktree, "checkout", "-b", "other")
    _host_git(worktree, "checkout", "-")
    assert not sentinel.exists(), (
        "SANDBOX ESCAPE: a hook planted inside the container executed on the host"
    )


def test_rc1_worktree_stays_writable(bindable_worktree):
    """Regression guard on the fix: only `.git` is sealed. A read-only worktree would
    break the accept contract — which is why exclusion lost to a read-only overlay."""
    worktree = _make_repo(bindable_worktree)
    result = _run_cordon(worktree, "echo ok > /work/written.txt")
    assert result.returncode == 0, f"worktree must stay writable: {result.stderr!r}"
    assert (worktree / "written.txt").exists()


def test_rc1_non_repo_worktree_gets_no_spurious_git_dir(bindable_worktree):
    """`docker run -v` creates a missing bind source, so an unconditional .git mount
    would manufacture the broken repo it exists to prevent."""
    result = _run_cordon(bindable_worktree, "echo ok > /work/out.txt")
    assert result.returncode == 0, f"a non-repo worktree must still run: {result.stderr!r}"
    assert not (bindable_worktree / ".git").exists(), (
        "cordon created a spurious .git in a non-repo worktree"
    )
