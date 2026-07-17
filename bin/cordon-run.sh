#!/usr/bin/env bash
# cordon — run one command inside a hardened, ephemeral, network-isolated container.
#
# The Sandbox component of the Barnett Studios agentic-harness toolkit. Transcribes
# the ADR-0040 hardened-container recipe: a runaway or buggy self-generated process
# is turned into a bounded, classified failure (OOM-kill / pid-limit / blocked
# egress) instead of hanging or escaping the batch.
#
# Contract:  run(worktree, cmd, limits) -> {exit, out}
#   cordon-run.sh <worktree-path> <runtime-image> <command...>
#   stdout/stderr = the command's output; exit code = the command's exit code.
#
# The SECURITY posture is fixed and non-negotiable (that is the whole point):
#   --network none · --read-only · --tmpfs /tmp · --cap-drop ALL
#   --security-opt no-new-privileges · -u 1000:1000
# Only the RESOURCE ceilings ("limits" in the contract) are tunable, via env, with
# the ADR-0040 hardened defaults:
#   CORDON_MEMORY (default 2g) · CORDON_CPUS (default 2) · CORDON_PIDS (default 512)
# --memory-swap is pinned equal to --memory so the memory ceiling is a HARD limit:
# without it Docker grants swap up to 2x --memory, letting a runaway allocation
# escape the ceiling (up to 2x) on any host with swap instead of being OOM-killed.

set -euo pipefail

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

docker run \
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
  -u 1000:1000 \
  -v "$WORKTREE":/work:rw \
  -w /work \
  "$RUNTIME_IMAGE" \
  "${ACCEPT_CMD[@]}"
