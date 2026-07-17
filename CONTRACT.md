# cordon — Contract

cordon is the **Sandbox** component: `run(worktree, cmd, limits) → {exit, out}`. One
command, run inside a hardened, ephemeral, network-isolated container, so a runaway or
buggy self-generated process becomes a *bounded, classified* failure instead of a hang
or a batch-wide blast.

## Interface

```
cordon-run.sh <worktree-path> <runtime-image> <command...>
```

- **worktree** — a host directory, mounted read-write at `/work` (the command's cwd).
  The intended input is a disposable per-node git work tree (one clean baseline commit),
  the same volume shape the Proving Ground provisions.
- **command** — the node's `accept` check (compile / test / grep). Its stdout+stderr are
  cordon's stdout+stderr; its exit code is cordon's exit code. No transform, no wrapper.
- **limits** — the three resource ceilings, via env: `CORDON_MEMORY` (default `2g`),
  `CORDON_CPUS` (default `2`), `CORDON_PIDS` (default `512`).

## The security posture is fixed (the invariant)

These flags are **not** parameterizable — they are the component's reason to exist:

| Flag | Why |
|---|---|
| `--network none` | the `accept` check is local; egress is removed, not filtered — a phone-home fails deterministically instead of silently succeeding against an unintended dependency |
| `--memory / --cpus / --pids-limit` | a runaway test (infinite loop, fork bomb, unbounded alloc) is OOM-/pid-killed — a bounded non-zero/timeout exit, never an unbounded hang |
| `--read-only --tmpfs /tmp` | the only writable surface is the disposable work tree + scratch |
| `--cap-drop ALL --security-opt no-new-privileges -u 1000:1000` | every capability dropped, no escalation, non-root |
| `--rm` (ephemeral, per command) | no state leaks between runs — matches the per-node ephemeral work tree it mounts |

Only the resource *ceilings* tune (the contract's `limits`); the isolation flags stay
literal. `tests/test_cordon_run_script.py` enforces both halves statically.

## Failure classification

An isolation failure stays inside the classes a caller already handles: a Docker
OOM-kill or a fired pid-limit surfaces as a non-zero/timeout exit from the command —
the same shape as any other command failure or hang. cordon
invents **no new failure class**; that is deliberate.

## Threat model (scope boundary)

Right-sized for **single-user, self-generated code against the user's own disposable repo**
— a runaway/buggy generation, not a hostile multi-tenant co-tenant. cordon provides **no**
defense against genuinely hostile or third-party untrusted code; do not run such code
through it. If the threat model shifts to "hostile tenant" or a hosted multi-user service,
the microVM/gVisor question reopens on its merits.

## Swap-in (lead + adapter)

cordon is the **lead** reference impl. A stronger isolation backend — microsandbox, gVisor
(runsc), E2B, Modal, Firecracker — drops into the same `run(worktree, cmd, limits) →
{exit, out}` socket by replacing the `docker run` line, provided it preserves the fixed
security posture above (or strengthens it). The contract is the CLI signature + the
exit-code passthrough + the no-egress / bounded-resource guarantees; the isolation
technology behind it is swappable.
