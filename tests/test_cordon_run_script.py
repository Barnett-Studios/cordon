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
    '-u "$CONTAINER_UID:$CONTAINER_GID"',
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
    # The uid is derived, not fixed — but derived from `id`, never from the environment.
    # A fixed 1000 only worked because Docker Desktop for macOS virtualizes bind-mount
    # ownership; on Linux it is a permission-denied accept failure (cordon#3). What the
    # guard still has to hold is that an OPERATOR cannot choose the uid: `${CORDON_UID}`
    # here would be the regression, and `$(id -u)` is not.
    assert "CONTAINER_UID=$(id -u)" in text
    assert "CONTAINER_GID=$(id -g)" in text
    assert not re.search(r"CONTAINER_UID=\$\{?[A-Z_]*CORDON", text), (
        "the container uid must not be operator-settable"
    )
    # The flag as it appears in the `docker run` invocation, not in the header comment —
    # matching the first `-u ...` in the file found the prose and passed on it.
    assert '\n    -u "$CONTAINER_UID:$CONTAINER_GID" \\\n' in text
    assert not re.search(r"^\s*-u \d+:\d+", text, re.M), "no fixed uid may survive"


def test_root_is_refused_rather_than_run_as_uid_0():
    """Matching the mount and staying non-root are the same requirement everywhere
    except when the invoker IS root, where they conflict. cordon refuses: uid 0 inside
    the sandbox would drop a posture the header calls non-negotiable, and doing it
    silently is worse than failing.

    Text-level companion to `test_root_refusal_happens_before_docker_runs`, which is the
    one with teeth. Kept for the message it gives on a rename, not as the guard."""
    text = SCRIPT.read_text()
    assert 'if [[ "$CONTAINER_UID" -eq 0 ]]; then' in text
    assert "refusing to run as root" in text


# ── The refusal is behaviour, not a string ─────────────────────────────────────
#
# The two assertions above only require those characters to exist SOMEWHERE. The
# property is that the refusal happens BEFORE `docker run`, and nothing above tests
# position: moving the block verbatim to the end of the script leaves both satisfied,
# and the sandbox then runs as root with a writable bind mount. Measured — 21 passed
# with the mutant in place, and the resulting invocation carried `-u 0:0`.
#
# That is the same shape as the `-u \d+:\d+` guard fixed in this same change: an
# assertion matching text rather than behaviour, satisfied by a file that does the
# wrong thing.
#
# Root is the one case that exits before `docker run`, so it needs no Docker to test —
# only stubs first on PATH. This file is already named in CI's structural step, so
# these run there without a workflow change.

_STUBS = {
    # Records the argv it was handed, so the assertion is on what the container would
    # actually get rather than on the source text that computes it.
    "docker": '#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$CORDON_TEST_LOG"\nexit 0\n',
    # The script requires coreutils `timeout` and refuses without it; it execs the rest
    # of its argv, so `timeout N docker run …` reaches the docker stub.
    "timeout": '#!/bin/sh\nshift 3\nexec "$@"\n',
}


def _run_with_uid(tmp_path, uid):
    """Invoke the real script with `id` stubbed to `uid`. Returns (rc, docker_argv)."""
    import os
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    for name, body in _STUBS.items():
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    idstub = bindir / "id"
    idstub.write_text(f'#!/bin/sh\nprintf \'%s\\n\' {uid}\n')
    idstub.chmod(0o755)

    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CORDON_TEST_LOG"] = str(log)
    proc = subprocess.run(
        [str(SCRIPT), str(work), "img:latest", "true"],
        env=env,
        capture_output=True,
        text=True,
    )
    argv = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, argv


def test_root_refusal_happens_before_docker_runs(tmp_path):
    rc, argv = _run_with_uid(tmp_path, 0)
    assert rc == 1, "an invocation as root must fail, not proceed"
    # `docker run`, not `docker` — the cleanup trap fires on every exit path and calls
    # `docker rm -f` on a container that was never created. Asserting docker was never
    # invoked AT ALL fails on that legitimate cleanup, which would make this guard a
    # nuisance rather than a control, and the first thing anyone deleted.
    assert "run" not in argv, (
        "docker run was reached as root — the container would run as uid 0 with a "
        f"writable bind mount, the exact posture the refusal exists to prevent; argv={argv}"
    )


def test_a_non_root_invocation_still_runs_with_the_invoking_uid(tmp_path):
    """The control. Without it a script that refused EVERYTHING passes the test above
    while the sandbox never runs at all — and it pins the uid on the argv the container
    receives, not in the source text that computes it."""
    rc, argv = _run_with_uid(tmp_path, 4242)
    assert rc == 0, f"a non-root invocation must proceed; argv={argv}"
    assert "run" in argv, f"docker run was never reached; argv={argv}"
    assert "4242:4242" in argv, (
        f"the derived uid must reach the container as -u; argv={argv}"
    )
    assert argv[argv.index("4242:4242") - 1] == "-u", (
        f"the uid must be the argument to -u, not incidental text; argv={argv}"
    )


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


# ── positive control for the live escape smokes ──────────────────────────────────
#
# `test_cordon_live_smoke.py` proves the sandbox escape is blocked by asserting a
# sentinel file does NOT appear. That is only evidence if the sentinel WOULD appear
# when the plant succeeds. Two earlier revisions of those tests were vacuous for
# exactly this reason — they used container-absolute paths (`core.hooksPath =
# /work/evilhooks`, `touch /work/PWNED`) which the host cannot resolve, so nothing was
# ever written whether or not `.git` was sealed.
#
# This lives here, in the always-run suite, rather than beside them: it needs no
# Docker and no cordon, so it keeps guarding the assumption on every CI run, including
# the ones where the live smokes skip.


def _host_git(worktree, *args):
    import os
    import subprocess
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_rc1_relative_hookspath_is_a_live_host_vector(tmp_path):
    """A RELATIVE core.hooksPath + a RELATIVE hook payload really do execute on the
    host, so the live smokes' `assert not sentinel.exists()` genuinely discriminates.

    git resolves a relative `core.hooksPath` against the working-tree root and chdir's
    there before invoking a hook (githooks(5)), which is why the relative form names
    the same location inside the container and on the host.

    If this ever fails, the live escape smokes have gone vacuous — fix them before
    trusting a green run.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _host_git(worktree, "init", "-q")
    _host_git(worktree, "config", "user.email", "t@example.com")
    _host_git(worktree, "config", "user.name", "t")
    (worktree / "seed.txt").write_text("seed\n")
    _host_git(worktree, "add", "-A")
    _host_git(worktree, "commit", "-q", "-m", "seed")

    hooks = worktree / "evilhooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch PWNED_POSITIVE_CONTROL\n")
    hook.chmod(0o755)
    _host_git(worktree, "config", "core.hooksPath", "evilhooks")

    _host_git(worktree, "checkout", "-b", "other")
    _host_git(worktree, "checkout", "-")

    assert (worktree / "PWNED_POSITIVE_CONTROL").exists(), (
        "a relative core.hooksPath hook did NOT execute on this host, so the live "
        "escape smokes cannot discriminate a blocked escape from an impossible one"
    )


def test_rc1_container_absolute_payload_would_be_vacuous(tmp_path):
    """The counter-example, pinned so the regression cannot silently return.

    With a container-absolute hooksPath the host finds no hook, so the sentinel never
    appears — which is precisely why the original smokes passed against unfixed code.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _host_git(worktree, "init", "-q")
    _host_git(worktree, "config", "user.email", "t@example.com")
    _host_git(worktree, "config", "user.name", "t")
    (worktree / "seed.txt").write_text("seed\n")
    _host_git(worktree, "add", "-A")
    _host_git(worktree, "commit", "-q", "-m", "seed")

    hooks = worktree / "evilhooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch PWNED_ABS\n")
    hook.chmod(0o755)
    _host_git(worktree, "config", "core.hooksPath", "/work/evilhooks")

    _host_git(worktree, "checkout", "-b", "other")
    _host_git(worktree, "checkout", "-")

    assert not (worktree / "PWNED_ABS").exists(), (
        "unexpected: a container-absolute hooksPath resolved on the host"
    )
