"""Structural test for bin/cordon-run.sh — asserts the hardened recipe's
security flags are present verbatim and that the resource-ceiling env seam is wired
without weakening any security flag. No Docker needed to run this test."""
import pathlib
import re

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cordon-run.sh"

# The fixed security posture — must appear verbatim, never parameterized away.
REQUIRED_SECURITY_FLAGS = [
    "--network none",
    "--read-only",
    "--tmpfs /tmp",
    "--cap-drop ALL",
    "--security-opt no-new-privileges",
    "-u 1000:1000",
    "docker run",
    "--rm",
]


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "bin/cordon-run.sh must exist"
    assert SCRIPT.stat().st_mode & 0o111, "script must be executable"


def test_script_has_shell_safety_header():
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text or ("set -e" in text and "set -u" in text)


def test_script_contains_every_security_flag():
    text = SCRIPT.read_text()
    for flag in REQUIRED_SECURITY_FLAGS:
        assert flag in text, f"missing security flag: {flag!r}"


def test_script_mounts_the_worktree_readwrite():
    text = SCRIPT.read_text()
    assert ":/work:rw" in text and "-w /work" in text


def test_resource_ceilings_are_env_overridable_with_hardened_defaults():
    """The three resource ceilings are the contract's tunable `limits`: env-driven
    with the hardened defaults (2g / 2 cpus / 512 pids)."""
    text = SCRIPT.read_text()
    for var, default in [("CORDON_MEMORY", "2g"), ("CORDON_CPUS", "2"), ("CORDON_PIDS", "512")]:
        assert re.search(rf'{var}="\$\{{{var}:-{re.escape(default)}\}}"', text), (
            f"{var} must default to {default!r} via ${{{var}:-{default}}}"
        )
    # The docker flags must consume the vars, not hardcoded numbers.
    assert '--memory "$CORDON_MEMORY"' in text
    assert '--cpus "$CORDON_CPUS"' in text
    assert '--pids-limit "$CORDON_PIDS"' in text


def test_security_flags_are_not_env_parameterized():
    """Regression guard on the env seam: no security flag may be swapped for a
    variable — only the resource ceilings are tunable."""
    text = SCRIPT.read_text()
    # --network, capability, privilege, and uid flags must be literals.
    assert "--network none" in text and "--network none" == re.search(
        r"--network \S+", text
    ).group(0)
    assert "--cap-drop ALL" in text
    assert re.search(r"-u \d+:\d+", text).group(0) == "-u 1000:1000"


# ── RC-1 / cordon#2: .git is outside the sandbox's writable surface ─────────────

def test_rc1_git_dir_is_mounted_read_only():
    """The worktree stays rw (that is the contract), but `.git` must be shadowed by
    a read-only bind so a sandboxed process cannot plant content the HOST later
    executes. Docker orders bind mounts by path depth, so the deeper `/work/.git`
    mount lands on top of `/work`."""
    text = SCRIPT.read_text()
    assert re.search(r'/\.git["\']?:/work/\.git:ro', text), (
        "bin/cordon-run.sh must bind $WORKTREE/.git at /work/.git read-only"
    )
    # Still rw at the top level — read-only-everything would break the accept contract.
    assert ":/work:rw" in text


def test_rc1_git_mount_is_conditional_on_git_existing():
    """`docker run -v` CREATES a missing bind source, so an unconditional mount would
    materialize a spurious `.git/` in a non-repo worktree — turning a plain directory
    into a broken repo and severing it from any enclosing repo. The mount must be
    guarded by an existence test."""
    text = SCRIPT.read_text()
    assert re.search(r'if\s+\[\[\s+-[ed]\s+"\$WORKTREE/\.git"', text), (
        "the .git mount must be conditional on $WORKTREE/.git existing"
    )
