#!/usr/bin/env bash
# cordon — run one command inside a hardened, ephemeral, network-isolated container.
#
# The Sandbox component of the Barnett Studios agentic-harness toolkit. Transcribes
# the hardened-container recipe: a runaway or buggy self-generated process
# is turned into a bounded, classified failure (OOM-kill / pid-limit / blocked
# egress) instead of hanging or escaping the batch.
#
# Contract:  run(worktree, cmd, limits) -> {exit, out}
#   cordon-run.sh <worktree-path> <runtime-image> <command...>
#   stdout/stderr = the command's output; exit code = the command's exit code.
#
# The SECURITY posture is fixed and non-negotiable (that is the whole point):
#   --network none · --read-only · --tmpfs /tmp · --cap-drop ALL
#   --security-opt no-new-privileges · -u <invoking uid>:<invoking gid>, never root
# Only the RESOURCE ceilings ("limits" in the contract) are tunable, via env, with
# the hardened defaults:
#   CORDON_MEMORY (default 2g) · CORDON_CPUS (default 2) · CORDON_PIDS (default 512)
#   CORDON_TIMEOUT (default 300s, wall-clock) · CORDON_KILL_AFTER (default 10s grace)
# --memory-swap is pinned equal to --memory so the memory ceiling is a HARD limit:
# without it Docker grants swap up to 2x --memory, letting a runaway allocation
# escape the ceiling (up to 2x) on any host with swap instead of being OOM-killed.
#
# WALL-CLOCK BOUND — this is what makes "never an unbounded hang" true. A busy loop
# (`while true: pass`) is throttled by --cpus but never OOM-/pid-killed, so without a
# clock it would spin forever. The run is wrapped in coreutils `timeout`, and a fixed
# --name + a docker rm -f cleanup trap reaps the container (killing the `docker run`
# client alone leaves the daemon-owned container running). On timeout, cordon exits
# CORDON_EXIT_TIMEOUT (124, the coreutils convention) — distinct from an OOM/pid kill.

set -euo pipefail

# Exit code reserved for a wall-clock timeout — distinct from the command's own codes
# and from Docker's 137 OOM/SIGKILL. Matches the coreutils `timeout` convention.
readonly CORDON_EXIT_TIMEOUT=124

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <worktree-path> <runtime-image> <command...>" >&2
  exit 1
fi

WORKTREE="$1"
RUNTIME_IMAGE="$2"
shift 2
ACCEPT_CMD=("$@")

# Resource ceilings — overridable; security flags below are not.
CORDON_MEMORY="${CORDON_MEMORY:-2g}"
CORDON_CPUS="${CORDON_CPUS:-2}"
CORDON_PIDS="${CORDON_PIDS:-512}"
# Wall-clock ceiling — overridable; bounds a runaway that never trips memory/pid limits.
CORDON_TIMEOUT="${CORDON_TIMEOUT:-300}"
CORDON_KILL_AFTER="${CORDON_KILL_AFTER:-10}"

# coreutils `timeout` is required to honor the wall-clock bound. macOS ships it as
# `gtimeout` (brew install coreutils). Without it the "never an unbounded hang"
# contract cannot be kept, so fail loudly rather than silently drop the guarantee.
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
if [[ -z "$TIMEOUT_BIN" ]]; then
  echo "cordon: 'timeout' (coreutils) not found — required to bound wall-clock runtime;" \
       "install coreutils (e.g. 'brew install coreutils' for gtimeout)" >&2
  exit 1
fi

# Fixed, per-invocation container name so the cleanup trap can reap the container even
# when the timeout kills only the `docker run` client. $$/$RANDOM avoid collisions
# across concurrent runs (a bare fixed name would clash).
# THE WORKTREE IS A TRUST BOUNDARY, AND .git IS OUTSIDE IT.
# The worktree is mounted rw by contract — the command must be able to build and
# write. But `.git` is not a data directory: it is a directory of things the HOST
# later executes. A process in the sandbox that can write `.git/hooks/*`, or set
# `core.hooksPath` / `core.fsmonitor` / a filter driver in `.git/config`, gets code
# execution on the host at the next host-side git operation — outside every flag
# above. Shadowing `.git` with a read-only bind closes that: Docker orders bind
# mounts by path depth, so the deeper /work/.git mount lands on top of /work.
#
# Read-only rather than excluded, so an accept that *reads* git state still works;
# only the write that causes the escape fails.
#
# CONDITIONAL, because `docker run -v` CREATES a missing bind source. Mounting
# unconditionally would materialize a spurious `.git/` in a non-repo worktree —
# turning a plain directory into a broken repo, severing it from any enclosing
# repo, and (as root, on native Linux) leaving something the invoking user cannot
# remove. The mitigation would manufacture the artifact it exists to prevent.
# -e, not -d: a linked `git worktree` stores `.git` as a FILE.
GIT_MOUNT=()
if [[ -e "$WORKTREE/.git" ]]; then
  GIT_MOUNT=(-v "$WORKTREE/.git":/work/.git:ro)
fi

CONTAINER_NAME="cordon-run-$$-${RANDOM}"
# shellcheck disable=SC2317,SC2329  # invoked indirectly via the trap below
cleanup() { docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

# The container user must MATCH the bind-mounted worktree's owner, not a fixed number.
# `-u 1000:1000` worked only because Docker Desktop for macOS virtualizes bind-mount
# ownership; on Linux — the roadmap platform, and every VPS — uid 1000 reading a worktree
# owned by uid 501 or 1001 gets permission denied, and the accept command fails for a
# reason that has nothing to do with the code under test (cordon#3). The same mismatch also
# makes `git` inside the sandbox refuse the worktree as "dubious ownership".
#
# This is NOT an env seam: the value comes from `id`, so an operator cannot weaken the
# posture by exporting something. The posture it must preserve is *non-root*, and matching
# the mount cannot deliver that when the invoker IS root — so that case is refused rather
# than silently run as uid 0 with the mount readable and the posture gone.
CONTAINER_UID=$(id -u)
CONTAINER_GID=$(id -g)
if [[ "$CONTAINER_UID" -eq 0 ]]; then
  echo "cordon: refusing to run as root — the container user matches the invoking user," >&2
  echo "cordon: and uid 0 inside the sandbox is not a posture cordon will take. Invoke as" >&2
  echo "cordon: a non-root user that owns $WORKTREE." >&2
  exit 1
fi

start=$(date +%s)
rc=0
"$TIMEOUT_BIN" --signal=TERM --kill-after="$CORDON_KILL_AFTER" "$CORDON_TIMEOUT" \
  docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    --network none \
    --read-only \
    --tmpfs /tmp \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --memory "$CORDON_MEMORY" \
    --memory-swap "$CORDON_MEMORY" \
    --cpus "$CORDON_CPUS" \
    --pids-limit "$CORDON_PIDS" \
    -u "$CONTAINER_UID:$CONTAINER_GID" \
    -v "$WORKTREE":/work:rw \
    ${GIT_MOUNT[@]+"${GIT_MOUNT[@]}"} \
    -w /work \
    "$RUNTIME_IMAGE" \
    "${ACCEPT_CMD[@]}" || rc=$?
elapsed=$(( $(date +%s) - start ))

# Classify a timeout distinctly. `timeout` returns 124 when the deadline is reached and
# the process dies on TERM; 137 (128+9) when the --kill-after KILL escalation is needed.
# Docker also returns 137 for an OOM/SIGKILL that can fire well before the deadline, so
# 137 is only a timeout when the run actually lasted the full window.
if [[ "$rc" -eq 124 ]] || { [[ "$rc" -eq 137 ]] && [[ "$elapsed" -ge "$CORDON_TIMEOUT" ]]; }; then
  echo "cordon: command exceeded ${CORDON_TIMEOUT}s wall-clock timeout — container killed" >&2
  exit "$CORDON_EXIT_TIMEOUT"
fi
exit "$rc"
