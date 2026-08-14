# cordon — Contract

cordon is the **Sandbox** component: `run(worktree, cmd, limits) → {exit, out}`. One
command, run inside a hardened, ephemeral, network-isolated container, so a runaway or
buggy self-generated process becomes a *bounded, classified* failure instead of a hang
or a batch-wide blast.

## Versioning

`VERSION` holds this component's version and every release carries a matching `v<version>` tag.
The number was previously carried by the git tag alone, so nothing in a checkout said which
version it was — a consumer holding a working copy, or an image built from one, had no in-tree
way to answer that.

Under the 0.x convention the **minor** is the breaking position: a change to this component's
request/response contract or CLI surface is a minor bump, and behaviour-preserving fixes are
patches. The version in `VERSION`, the git tag, and the published ghcr image tag are the same
number by construction — dotclaude#63 is what a drift between those looks like.

## Interface

```
cordon-run.sh <worktree-path> <runtime-image> <command...>
```

- **worktree** — a host directory, mounted read-write at `/work` (the command's cwd),
  **except its own top-level `.git`, which is mounted read-only** — a repository nested
  inside the tree keeps a writable one (see *The worktree is a trust boundary*).
  The intended input is a disposable per-node git work tree (one clean baseline commit),
  the same volume shape a driving harness provisions.
- **command** — the node's `accept` check (compile / test / grep). Its stdout+stderr are
  cordon's stdout+stderr; its exit code is cordon's exit code. No transform, no wrapper.
- **limits** — the resource ceilings, via env: `CORDON_MEMORY` (default `2g`),
  `CORDON_CPUS` (default `2`), `CORDON_PIDS` (default `512`), plus a wall-clock bound
  `CORDON_TIMEOUT` (default `300` seconds, with `CORDON_KILL_AFTER` grace default `10`).

### The artifact that carries the guarantees is the script, not the image

`<runtime-image>` is a **swappable argument**, not the security boundary.
`ghcr.io/barnett-studios/cordon` is a generic `debian:bookworm-slim` + toolchain image
with no isolation properties of its own — pulled and run directly, it provides none of
the invariants below. Every one of them is applied by `cordon-run.sh` at `docker run`
time.

So **`cordon-run.sh` is the distributed artifact**: it ships as an asset on each GitHub
Release with a SHA-256 checksum beside it, and it is published byte-for-byte as the file
`tests/test_cordon_run_script.py` audits — an audit of a file consumers do not receive
would guarantee nothing (cordon#4).

It is deliberately not baked into the image as an entrypoint: the script *invokes*
`docker run`, so running it inside the container it launches would require
docker-in-docker.

## The security posture is fixed (the invariant)

These flags are **not** parameterizable — they are the component's reason to exist:

| Flag | Why |
|---|---|
| `--network none` | the `accept` check is local; egress is removed, not filtered — a phone-home fails deterministically instead of silently succeeding against an unintended dependency |
| `--memory / --cpus / --pids-limit` | a runaway test (fork bomb, unbounded alloc) is OOM-/pid-killed — a bounded non-zero exit, never an unbounded hang |
| `--memory-swap` pinned equal to `--memory` | without it Docker grants swap up to 2x `--memory`, so a runaway allocation escapes the ceiling on any host with swap instead of being OOM-killed. Not a ceiling — the thing that stops the ceiling being soft, which is why it is here and not in `limits` |
| wall-clock `timeout` (`CORDON_TIMEOUT`, default 300s) | a busy loop that never trips memory/pid limits (`--cpus` only throttles it) is killed at the deadline via coreutils `timeout`; the container is reaped by a `docker rm -f` cleanup trap. cordon exits **124** (the coreutils timeout convention) — distinct from Docker's 137 OOM/SIGKILL |
| `--read-only --tmpfs /tmp` | the only writable surface is the disposable work tree + scratch |
| `--cap-drop ALL --security-opt no-new-privileges -u <invoking uid>:<invoking gid>` | every capability dropped, no escalation, non-root. The uid is read from `id -u`/`id -g`, not fixed: the container writes into a bind-mounted host work tree, so any uid other than the one that owns that tree is a permission error on Linux — where the fixed `1000:1000` only ever worked because Docker Desktop for macOS virtualizes bind-mount ownership. It is not operator-settable, and uid 0 is refused outright rather than run |
| `--rm` (ephemeral, per command) | no state leaks between runs — matches the per-node ephemeral work tree it mounts |
| the worktree's own `.git` mounted `:ro` (when present) | `.git` is not data — it is a directory of things the **host** later executes. Writable `.git/hooks/*`, or `core.hooksPath`/`core.fsmonitor`/filter drivers in `.git/config`, give a sandboxed process code execution on the host at the next host-side git operation, outside every flag above. One path is mounted, so a *nested* repository keeps a writable `.git` — scoped below and in cordon#11 |

Only the resource *ceilings* tune (the contract's `limits`); the isolation flags stay
literal. `tests/test_cordon_run_script.py` enforces both halves without Docker — the fixed
flags verbatim, the ceilings via their env seam and defaults, and the wall-clock rows
behaviourally, by stubbing `timeout` and `docker` and asserting what the script actually
invoked and exited with.

That claim used to be false in the direction that does not show up in a green run. The audit's
flag list was hand-written from what someone had noticed, so the whole wall-clock row and
`--memory-swap` were outside it: deleting the `timeout` wrapper, or the 124 classification, or
`--memory-swap` each left the suite fully green (cordon#13). The list is now an **allowlist with
a refusal** — every `-flag` in the `docker run` invocation must be classified as fixed posture or
tunable ceiling, and an unclassified one fails the audit, so a widened invocation forces the
decision instead of inheriting a pass.

## Failure classification

An isolation failure stays inside the classes a caller already handles: a Docker
OOM-kill or a fired pid-limit surfaces as a non-zero exit from the command — the same
shape as any other command failure. The one reserved signal is the **wall-clock
timeout: exit `124`** (the coreutils `timeout` convention), so a caller can tell a
deadline breach apart from an ordinary non-zero exit or a 137 OOM-kill. It is still the
"non-zero exit" class every caller already handles — just with a recognizable code —
so cordon invents no genuinely new failure class; that is deliberate.

## The worktree is a trust boundary

The worktree is writable **by design** — the command has to build and test in it. That
makes anything the container leaves behind *untrusted host input*, not a result.

`.git` is the part of it the host executes, so it is mounted read-only whenever it exists
(as a directory, or as a file for a linked `git worktree`). The mount is **conditional**:
`docker run -v` creates a missing bind source, so mounting unconditionally would
materialize a spurious `.git/` in a non-repo worktree — turning a plain directory into a
broken repo and severing it from any enclosing one. For a worktree that is not a repo,
cordon adds no mount and makes no claim.

**What this does not cover — 1: any other repository.** The mount is a single path,
`<worktree>/.git`. Closing it closes host execution via git *for the repository cordon was
handed*, and for no other. A vendored clone at `sub/` keeps a writable `sub/.git`, and a
container that appends `[core] hooksPath` there gets host execution the next time the host runs
git inside `sub/`. A **local** `core.hooksPath` outranks a global one, so a `~/.git-hooks`
installed repo-wide — as dotclaude installs one — does not mitigate the config variant.
Submodules are partly covered: the real gitdir at `<worktree>/.git/modules/<name>/` *is* under
the read-only mount and rejects writes, while the `vendor/.git` pointer file in the tree is
writable and can be repointed. No working execution has been demonstrated through the pointer
file, and none is claimed. Widening the mount to every nested `.git` is the alternative and was
not taken — it costs a full tree walk before every run and an unbounded mount count, against a
path that only matters to someone deliberately looking for the nested repo, who already has the
writable worktree named below. Tracked as cordon#11.
`tests/test_cordon_live_smoke.py` pins both halves of this boundary so the mount cannot widen
without this paragraph following it.

**What this does not cover — 2: the worktree itself.** Closing `.git` does not
close host execution via the writable worktree. If the host later runs a build or
test tool in a cordon-touched worktree, a container can still plant `conftest.py` (pytest
auto-imports it), `pyproject.toml` `[tool.pytest.ini_options] addopts`, `build.rs`
(executed by `cargo test`), a `Makefile`, `package.json` `scripts`, or `.envrc`. Closing
that class needs either a read-only worktree — which would break the accept contract — or
a host-side `git reset --hard && git clean -fd` before any host tool touches the tree.
**A caller that runs host tooling in a cordon-touched worktree must do that reset itself.**
Within the threat model below — non-adversarial self-generated code — sealing the git path
is proportionate; the residual is stated rather than implied away.

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
security posture above (or strengthens it) — **including the read-only `.git`**, which is
part of that posture, not an implementation detail of the Docker backend. The contract is the CLI signature + the
exit-code passthrough + the no-egress / bounded-resource guarantees; the isolation
technology behind it is swappable.
