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
    # Pinned equal to --memory so the ceiling is HARD: without it Docker grants swap up
    # to 2x --memory and a runaway allocation escapes the limit instead of being
    # OOM-killed. The script header called this load-bearing; nothing enforced it, and
    # deleting the line left the suite at 13 passed (cordon#13).
    '--memory-swap "$CORDON_MEMORY"',
]

# Every `-flag` the `docker run` invocation may carry, classified. This list is the
# audit's denominator, and it used to be whatever someone had noticed — so the rows of
# CONTRACT.md's posture table that nobody transcribed were unguarded, and a NEW flag
# arrived unguarded by default. `test_every_docker_flag_is_classified` inverts that: an
# unclassified flag is a failure, so widening the invocation forces a decision here.
FIXED_POSTURE_DOCKER_FLAGS = {
    "--name",        # so the cleanup trap can reap a container the timeout orphaned
    "--rm",
    "--network",
    "--read-only",
    "--tmpfs",
    "--cap-drop",
    "--security-opt",
    "--memory-swap",  # not a ceiling: it exists to stop the ceiling being soft
    "-u",
    "-v",
    "-w",
}
TUNABLE_CEILING_DOCKER_FLAGS = {"--memory", "--cpus", "--pids-limit"}


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


def _docker_run_block():
    """The text of the `docker run …` invocation, up to the accept command."""
    text = SCRIPT.read_text()
    end = text.index('"${ACCEPT_CMD[@]}"')
    # rindex, not index: the header comment mentions `docker run` too, and starting there
    # would sweep the timeout wrapper's own options in as if they were docker flags.
    start = text.rindex("docker run", 0, end)
    return text[start:end]


def test_every_docker_flag_is_classified():
    """No unclassified flag may reach `docker run`.

    The audit's coverage claim ("enforces both halves") was only ever true of the flags
    someone had listed — a denominator taken from the observations. This takes it from
    the invocation itself, so a flag that arrives without a decision here fails rather
    than passing silently.

    `${GIT_MOUNT[@]…}` expands to `-v <path>:ro` at runtime and is not a literal token;
    it has its own tests (`test_rc1_git_dir_is_mounted_read_only`,
    `test_rc1_git_mount_is_conditional_on_git_existing`).
    """
    known = FIXED_POSTURE_DOCKER_FLAGS | TUNABLE_CEILING_DOCKER_FLAGS
    flags = {tok for tok in _docker_run_block().split() if tok.startswith("-")}
    unclassified = flags - known
    assert not unclassified, (
        f"unclassified docker flags: {sorted(unclassified)} — add each to "
        "FIXED_POSTURE_DOCKER_FLAGS or TUNABLE_CEILING_DOCKER_FLAGS, and to "
        "CONTRACT.md's posture table if it is part of the guarantee"
    )
    # The control: the classification is only meaningful if the flags are really there.
    # An invocation stripped to `docker run img cmd` would satisfy the assertion above.
    missing = FIXED_POSTURE_DOCKER_FLAGS - flags
    assert not missing, f"fixed-posture flags absent from the invocation: {sorted(missing)}"


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


# ── The wall-clock bound (cordon#13) ────────────────────────────────────────────
#
# This is the half of the posture table nothing tested. Deleting the entire `timeout`
# wrapper, or the 124 classification, each left the suite at 13 passed — while the
# script's own header calls the wrapper "what makes 'never an unbounded hang' true"
# and the CONTRACT reserves 124 as the one signal that distinguishes a deadline breach
# from a 137 OOM-kill.
#
# Behavioural, not textual: the stubs record what the script actually invoked and what
# it actually exited with. A text assertion here would be satisfied by a script that
# carries the right characters in the wrong order — the failure mode already recorded
# above for the root refusal.


def _run_stubbed(tmp_path, timeout_body, env_extra=None, uid=4242):
    """Invoke the real script with `id`, `docker` and `timeout` stubbed.

    Returns (rc, docker_argv, timeout_argv). `timeout_body` decides what the wrapper
    does, which is how a deadline breach is simulated without waiting for one.
    """
    import os
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir()
    dlog = tmp_path / "docker.log"
    tlog = tmp_path / "timeout.log"

    (bindir / "docker").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$CORDON_TEST_LOG"\nexit 0\n'
    )
    (bindir / "timeout").write_text(timeout_body)
    (bindir / "id").write_text(f'#!/bin/sh\nprintf \'%s\\n\' {uid}\n')
    for name in ("docker", "timeout", "id"):
        (bindir / name).chmod(0o755)

    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CORDON_TEST_LOG"] = str(dlog)
    env["CORDON_TIMEOUT_TEST_LOG"] = str(tlog)
    env.update(env_extra or {})
    proc = subprocess.run(
        [str(SCRIPT), str(work), "img:latest", "true"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return (
        proc.returncode,
        dlog.read_text().splitlines() if dlog.exists() else [],
        tlog.read_text().splitlines() if tlog.exists() else [],
    )


# Records its own argv, then behaves like coreutils `timeout`: drop the three leading
# options and exec the wrapped command.
_TIMEOUT_PASSTHROUGH = (
    '#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$CORDON_TIMEOUT_TEST_LOG"\nshift 3\nexec "$@"\n'
)


def test_docker_run_is_wrapped_in_the_wall_clock_timeout(tmp_path):
    """`docker run` must be the timeout's child, with both ceilings passed to it.

    Red on deleting the wrapper: `timeout` is then never invoked at all, so its log is
    empty and the container has no deadline.
    """
    rc, docker_argv, timeout_argv = _run_stubbed(tmp_path, _TIMEOUT_PASSTHROUGH)
    assert rc == 0, f"the control run must succeed; docker_argv={docker_argv}"
    assert timeout_argv, (
        "coreutils `timeout` was never invoked — the run is unbounded, and a busy loop "
        "that never trips the memory or pid ceiling would spin forever"
    )
    assert timeout_argv[0] == "--signal=TERM", f"timeout argv={timeout_argv}"
    assert timeout_argv[1] == "--kill-after=10", (
        f"CORDON_KILL_AFTER must default to 10s of grace; timeout argv={timeout_argv}"
    )
    assert timeout_argv[2] == "300", (
        f"CORDON_TIMEOUT must default to 300s; timeout argv={timeout_argv}"
    )
    assert timeout_argv[3:5] == ["docker", "run"], (
        f"docker run must be the wrapped command, not a sibling; argv={timeout_argv}"
    )


def test_the_wall_clock_ceilings_are_env_overridable(tmp_path):
    """The control on the two assertions above: they must be reading the vars, not two
    numbers that happen to be 10 and 300."""
    _, _, timeout_argv = _run_stubbed(
        tmp_path, _TIMEOUT_PASSTHROUGH,
        {"CORDON_TIMEOUT": "42", "CORDON_KILL_AFTER": "7"},
    )
    assert timeout_argv[1] == "--kill-after=7", f"timeout argv={timeout_argv}"
    assert timeout_argv[2] == "42", f"timeout argv={timeout_argv}"


def test_a_deadline_breach_exits_124(tmp_path):
    """coreutils returns 124 when the deadline is reached and the process dies on TERM."""
    rc, _, _ = _run_stubbed(tmp_path, '#!/bin/sh\nexit 124\n')
    assert rc == 124, f"a timeout must surface as the reserved 124; got {rc}"


def test_a_kill_escalated_deadline_breach_is_124_not_137(tmp_path):
    """The discriminating case. `timeout` escalates to KILL after the grace period and
    the run comes back as 137 — the same code Docker returns for an OOM-kill. cordon
    must translate that to 124 when the run lasted the full window.

    This is the one that is red on `exit "$rc"` in place of `exit
    "$CORDON_EXIT_TIMEOUT"`: for the plain-124 case above the mutant returns 124 too.
    """
    rc, _, _ = _run_stubbed(
        tmp_path, '#!/bin/sh\nsleep 2\nexit 137\n', {"CORDON_TIMEOUT": "1"}
    )
    assert rc == 124, (
        f"a KILL-escalated deadline breach must still be reported as 124, not {rc} — "
        "a caller cannot otherwise tell it from an OOM-kill"
    )


def test_an_early_137_is_not_reported_as_a_timeout(tmp_path):
    """The other side of it: an OOM-kill fires well before the deadline and must keep
    its own code. Without this, `exit 124` unconditionally on 137 would pass the test
    above while erasing the distinction it exists to make."""
    rc, _, _ = _run_stubbed(
        tmp_path, '#!/bin/sh\nexit 137\n', {"CORDON_TIMEOUT": "300"}
    )
    assert rc == 137, f"a 137 inside the window is an OOM-kill, not a timeout; got {rc}"


def test_the_cleanup_trap_reaps_the_named_container(tmp_path):
    """Killing the `docker run` client leaves the daemon-owned container running, so the
    deadline is only real if the container is reaped by name."""
    _, docker_argv, _ = _run_stubbed(tmp_path, _TIMEOUT_PASSTHROUGH)
    assert "rm" in docker_argv and "-f" in docker_argv, (
        f"the cleanup trap must docker rm -f the container; argv={docker_argv}"
    )
    named = [a for a in docker_argv if a.startswith("cordon-run-")]
    assert named, f"the container must be reaped by its --name; argv={docker_argv}"
    assert docker_argv[docker_argv.index("-f") + 1] == named[0], (
        f"docker rm -f must name the container it created; argv={docker_argv}"
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


# ── The worktree is a mount source, not a name (cordon#15) ─────────────────────
#
# `docker run -v` accepts two things the caller never meant. A MISSING source is
# created as an empty directory; a RELATIVE source is read as a NAMED VOLUME. Either
# way the accept command gets an empty `/work` — so an absence-shaped check ("no TODO
# markers", "lint is clean") passes vacuously, and everything the command writes lands
# somewhere the caller never reads. Measured on v0.1.3: a missing path gave rc=0 and
# materialized the directory on the host; a bare relative name gave rc=0 and created a
# docker volume.
#
# The script already reasons about exactly this Docker behaviour — for `/work/.git`:
#
#     CONDITIONAL, because `docker run -v` CREATES a missing bind source.
#
# Correct there, and never applied to `/work` itself.
#
# These run against the real script with the same docker/timeout stubs as the root
# refusal above, so they assert on the argv the container would receive — not on the
# source text that computes it — and need no Docker.


def _run_with_worktree(tmp_path, worktree_arg, cwd=None):
    """Invoke the real script with docker/timeout/id stubbed. -> (rc, stderr, argv)."""
    import os
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    for name, body in _STUBS.items():
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    # Non-root, so the uid refusal above cannot be what these measure — otherwise a CI
    # runner that happens to be root would turn every assertion below green for the
    # wrong reason.
    idstub = bindir / "id"
    idstub.write_text("#!/bin/sh\nprintf '%s\\n' 4242\n")
    idstub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CORDON_TEST_LOG"] = str(log)
    proc = subprocess.run(
        [str(SCRIPT), str(worktree_arg), "img:latest", "true"],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
    )
    argv = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, proc.stderr, argv


def _work_mount(argv):
    """The `-v` source docker was handed for /work, or None."""
    for i, a in enumerate(argv):
        if a == "-v" and i + 1 < len(argv) and argv[i + 1].endswith(":/work:rw"):
            return argv[i + 1].rsplit(":/work:rw", 1)[0]
    return None


def test_a_missing_worktree_is_refused_before_docker_runs(tmp_path):
    ghost = tmp_path / "ghost-wt"
    rc, err, argv = _run_with_worktree(tmp_path, ghost)
    assert rc == 1, f"a worktree that does not exist must be refused; err={err!r}"
    assert "ghost-wt" in err, f"the refusal must name the offending path: {err!r}"
    # Message contract, and the ONLY thing separating the explicit refusal from letting
    # `set -e` abort on the failed `cd`: that mutant refuses the same inputs with the
    # same code and also names the path, but says it as a line-numbered bash diagnostic
    # about the script's internals rather than as cordon telling the caller what it
    # refused. Every other refusal in this script is branded and states its reason.
    assert "cordon:" in err, f"the refusal must be cordon's own, not a bash trace: {err!r}"
    # `run`, not `docker` — the cleanup trap legitimately calls `docker rm -f` on every
    # exit path, so asserting docker was never invoked at all would fail on that.
    assert "run" not in argv, (
        f"docker run was reached with a source it would create empty; argv={argv}"
    )


def test_a_file_is_not_a_worktree(tmp_path):
    # `-v /path/to/file:/work` binds a FILE over the mount point. The contract says
    # worktree is a host directory, and the guard must be `-d`, not `-e`.
    f = tmp_path / "notadir.txt"
    f.write_text("x\n")
    rc, err, argv = _run_with_worktree(tmp_path, f)
    assert rc == 1, f"a file is not a worktree; err={err!r}"
    assert "notadir.txt" in err, f"the refusal must name the offending path: {err!r}"
    assert "run" not in argv, f"docker run was reached with a file as /work; argv={argv}"


def test_a_relative_worktree_never_reaches_docker_as_a_bare_name(tmp_path):
    # A relative path that EXISTS — so the existence guard alone does not save it.
    # `-v cordplain:/work` is a named volume, and the accept command sees an empty tree.
    wt = tmp_path / "cordplain"
    wt.mkdir()
    (wt / "src.txt").write_text("TODO: unfinished\n")

    rc, err, argv = _run_with_worktree(tmp_path, "cordplain", cwd=tmp_path)
    src = _work_mount(argv)
    assert rc == 0, f"an existing relative worktree must still run; err={err!r}"
    assert src is not None, f"nothing was bound at /work; argv={argv}"
    assert src.startswith("/"), (
        f"a relative worktree reached -v as a bare name — docker reads that as a named "
        f"volume, not this directory; -v {src}:/work:rw"
    )
    assert (pathlib.Path(src) / "src.txt").exists(), (
        f"the resolved mount source is not the directory that was asked for: {src}"
    )


def test_an_absolute_worktree_still_reaches_docker_bound_at_work(tmp_path):
    """The control. Without it, a script that refused every worktree satisfies all
    three assertions above while the sandbox never runs at all."""
    wt = tmp_path / "real-wt"
    wt.mkdir()
    rc, err, argv = _run_with_worktree(tmp_path, wt)
    assert rc == 0, f"a real absolute worktree must run; err={err!r} argv={argv}"
    assert _work_mount(argv) == str(wt.resolve()), (
        f"the worktree must be bound rw at /work; argv={argv}"
    )


def test_an_empty_worktree_is_refused_rather_than_resolved_to_the_callers_cwd(tmp_path):
    """`cd -- ""` is a no-op that SUCCEEDS on bash 3.2 — macOS's /bin/bash, this
    component's supported platform — so a resolution-only guard hands `-v` the caller's own
    cwd and mounts it rw at /work. Measured on the first version of this fix: rc=0, docker
    reached, `-v <caller's cwd>:/work:rw`, with a host file the caller never named sitting
    in it. The #15 shape unchanged, and a REGRESSION: before the guard existed the same
    input reached docker as `-v :/work:rw` and was refused loudly with 125.

    This test discriminates on every bash in service today, CI included — measured on the
    ubuntu-latest base (bash 5.2.21) with the `[[ -d ]]` half dropped: it goes RED there,
    not only here. The hole was open on the runner too.

    The structural assertion below is for the OTHER end of the timeline. bash 5.3 has
    shipped and refuses `cd -- ""` by itself; the day a runner picks it up, this test
    silently becomes a control — passing whether or not the guard exists — with nothing
    announcing the change. That assertion is what will still be killing the mutation then.
    """
    caller = tmp_path / "callercwd"
    caller.mkdir()
    (caller / "PRIVATE-HOST-FILE.txt").write_text("TODO: unfinished\n")
    rc, err, argv = _run_with_worktree(tmp_path, "", cwd=caller)
    assert _work_mount(argv) != str(caller.resolve()), (
        "an empty worktree argument mounted the CALLER'S CWD rw at /work — an "
        f"absence-shaped check then runs against a tree nobody named; argv={argv}"
    )
    assert rc == 1, f"an empty worktree must be refused; rc={rc} err={err!r}"
    assert "run" not in argv, f"docker run was reached with an empty worktree; argv={argv}"


def test_the_worktree_guard_tests_the_argument_and_not_only_cd(tmp_path):
    """Textual, deliberately, and the only assertion in this file that has to be.

    The behavioural test above stops discriminating the moment a runner ships bash 5.3,
    whose `cd -- ""` fails on its own — and it stops silently, still green. This asserts the
    property directly and on every bash: the refusal is cordon's own decision about the
    argument, not a side effect of what some `cd` happens to do with an empty string.
    """
    # Comments stripped first. Written against the whole file, this passed with the guard
    # DELETED — the comment above the guard quotes it, and a text assertion cannot tell a
    # rule from a sentence describing one. Caught by running the mutation; it is the third
    # time in this file that a textual assertion was satisfied by prose.
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert re.search(r'\[\[\s+!\s+-d\s+"\$WORKTREE"\s+\]\]', code), (
        "the worktree guard must test the argument itself; a resolution-only guard "
        "resolves \"\" to the caller's cwd on bash 3.2"
    )


def test_a_symlinked_worktree_is_bound_by_its_real_path(tmp_path):
    # `-v` is resolved by the DAEMON, in its own filesystem namespace — a host symlink
    # is not a path it can be relied on to follow. Resolving here means the mount source
    # is the directory that was asked for.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    rc, err, argv = _run_with_worktree(tmp_path, link)
    assert rc == 0, f"a symlinked worktree must still run; err={err!r}"
    assert _work_mount(argv) == str(real.resolve()), (
        f"the mount source must be the real directory, not the link; argv={argv}"
    )
