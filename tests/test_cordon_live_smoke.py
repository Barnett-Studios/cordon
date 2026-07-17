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
