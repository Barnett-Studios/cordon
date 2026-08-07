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
#   4. **core.hooksPath must be RELATIVE.** A relative value resolves against the root
#      of the working tree (githooks(5): git chdir's there before invoking a hook), so
#      it names the same directory inside the container and on the host. An absolute
#      container path like `/work/evilhooks` does NOT exist on the host, so host git
#      finds no hook and the sentinel never appears — the escape assertion would pass
#      against unfixed code. Measured on git 2.54.0: relative `evilhooks` FIRES on the
#      host, `/work/evilhooks` does not. `test_rc1_relative_hookspath_is_a_live_host_
#      vector` is the positive control that keeps this honest.
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
    """A worktree the container can genuinely write **through cordon**, or a SKIP.

    Rooted under $HOME because Docker Desktop for macOS only bind-mounts shared
    paths; `tmp_path` is not one, and a non-shared source yields an empty root-owned
    directory in the VM instead of a mount. Yields the path; removes it afterwards.

    The probe deliberately goes through `_run_cordon` — the real script — not a bare
    `docker run`. Any reason cordon cannot execute the payload makes every escape
    assertion below pass without the attack ever being attempted. A direct-docker
    probe misses the most common one: `cordon-run.sh` hard-exits when coreutils
    `timeout` is absent (it is not installed by default on macOS), so on such a host
    the container never starts, nothing is ever written, and "the escape failed" is
    a statement about nothing. Probing through the script covers that and any future
    precondition it grows.
    """
    import tempfile
    base = pathlib.Path(tempfile.mkdtemp(prefix="cordon-rc1-", dir=pathlib.Path.home()))
    worktree = base / "worktree"
    worktree.mkdir()
    os.chmod(base, 0o777)
    os.chmod(worktree, 0o777)
    probe = _run_cordon(worktree, "touch /work/.probe")
    if probe.returncode != 0 or not (worktree / ".probe").exists():
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(
            "cordon cannot write the bind-mounted worktree (missing precondition, "
            f"uid mapping, or unshared path): {probe.stderr.strip()!r} — the escape "
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
    core.hooksPath at a directory it also controls.

    Both the hooksPath and the hook's payload are **relative**, so they name the same
    location inside the container and on the host. Using container-absolute paths
    (`/work/...`) makes the sentinel assertion vacuous: the host cannot resolve them,
    so nothing is ever written whether or not `.git` is sealed.
    """
    worktree = _make_repo(bindable_worktree)
    sentinel = worktree / "PWNED_VIA_CONFIG"
    _run_cordon(worktree, (
        "mkdir -p /work/evilhooks && "
        "printf '#!/bin/sh\\ntouch PWNED_VIA_CONFIG\\n' > /work/evilhooks/post-checkout && "
        "chmod +x /work/evilhooks/post-checkout && "
        "printf '[core]\\n\\thooksPath = evilhooks\\n' >> /work/.git/config"
    ))
    _host_git(worktree, "checkout", "-b", "other")
    _host_git(worktree, "checkout", "-")

    # The security property first: the failure that matters is execution, not config.
    assert not sentinel.exists(), (
        "SANDBOX ESCAPE: a hook planted inside the container executed on the host"
    )
    assert "evilhooks" not in (worktree / ".git" / "config").read_text(), (
        ".git/config was rewritten from inside the sandbox — the container can "
        "redirect hook lookup to a directory it controls"
    )


def test_rc1_planted_git_hook_does_not_execute_on_host(bindable_worktree):
    """(b) The classic vector, kept honest by re-enabling .git/hooks in the fixture.

    The payload is relative for the same reason as (a) — `touch /work/PWNED_VIA_HOOK`
    cannot resolve on the host, which would make the execution assertion vacuous even
    though the file-existence assertion above it still discriminates.
    """
    worktree = _make_repo(bindable_worktree)
    sentinel = worktree / "PWNED_VIA_HOOK"
    _run_cordon(worktree, (
        "printf '#!/bin/sh\\ntouch PWNED_VIA_HOOK\\n' > /work/.git/hooks/post-checkout && "
        "chmod +x /work/.git/hooks/post-checkout"
    ))
    _host_git(worktree, "checkout", "-b", "other")
    _host_git(worktree, "checkout", "-")

    assert not sentinel.exists(), (
        "SANDBOX ESCAPE: a hook planted inside the container executed on the host"
    )
    assert not (worktree / ".git" / "hooks" / "post-checkout").exists(), (
        ".git/hooks/post-checkout was written from inside the sandbox"
    )


def _make_nested_repo(worktree):
    """A second, independent repository inside the worktree — a vendored clone, the
    shape `measurement/corpus` has in the consuming assembly."""
    nested = worktree / "sub"
    nested.mkdir()
    _make_repo(nested)
    for p in [nested, *nested.rglob("*")]:
        try:
            os.chmod(p, 0o777)
        except OSError:
            pass
    return nested


def test_rc1_nested_repo_keeps_a_writable_git_and_the_docs_say_so(bindable_worktree):
    """cordon#11 — the mount is ONE path, so the seal covers the repository cordon was
    handed and no other. This pins the boundary where the code actually draws it.

    It asserts **both halves in one run**, which is what keeps it honest:

      * the outer `.git/config` write must FAIL. Without that half the test passes on a
        host where the container could not write anything at all — the vacuity trap the
        header above names, in its most expensive form, because `bindable_worktree`'s
        probe only proves the *worktree* is writable, not that the mount landed.
      * the nested `sub/.git/config` write must SUCCEED. That is the documented gap. If
        someone widens the mount (cordon#11, option 1), this half reddens and sends them
        to the paragraph in CONTRACT.md that has to change with it.

    Nothing is executed on the host here. A planted `core.hooksPath` in a nested repo
    firing on the host was demonstrated during review of #10 and is recorded in cordon#11;
    re-landing host execution inside the suite buys nothing the write assertion does not
    already give, and leaves a live payload in a test.
    """
    worktree = _make_repo(bindable_worktree)
    nested = _make_nested_repo(worktree)

    result = _run_cordon(worktree, (
        "{ printf '[core]\\n\\thooksPath = outerhooks\\n' >> /work/.git/config && "
        "echo OUTER_WRITABLE; } || echo OUTER_SEALED; "
        "{ printf '[core]\\n\\thooksPath = nestedhooks\\n' >> /work/sub/.git/config && "
        "echo NESTED_WRITABLE; } || echo NESTED_SEALED"
    ))
    out = result.stdout

    assert "OUTER_SEALED" in out, (
        "the read-only .git mount did not take, so the second half of this test proves "
        f"nothing about a nested repo: stdout={out!r} stderr={result.stderr!r}"
    )
    # `outerhooks`, not `hooksPath` — _make_repo sets a legitimate core.hooksPath, so the
    # bare key is present in a clean config and asserting on it fails a working sandbox.
    assert "outerhooks" not in (worktree / ".git" / "config").read_text()

    assert "NESTED_WRITABLE" in out and "nestedhooks" in (nested / ".git" / "config").read_text(), (
        "a nested repository's .git is now sealed. That is an IMPROVEMENT, not a "
        "failure — but README.md and CONTRACT.md both state that it is not, and "
        "cordon#11 is open on the trade. Update them in the same change and delete "
        f"this assertion: stdout={out!r}"
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
