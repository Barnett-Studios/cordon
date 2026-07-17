"""Structural test for bin/cordon-run.sh — asserts the ADR-0040 hardened recipe's
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
        assert flag in text, f"missing ADR-0040 security flag: {flag!r}"


def test_script_mounts_the_worktree_readwrite():
    text = SCRIPT.read_text()
    assert ":/work:rw" in text and "-w /work" in text


def test_resource_ceilings_are_env_overridable_with_adr0040_defaults():
    """The three resource ceilings are the contract's tunable `limits`: env-driven
    with the ADR-0040 hardened defaults (2g / 2 cpus / 512 pids)."""
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
