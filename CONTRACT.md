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
  It must be a directory the invoking user can enter; cordon resolves it to an absolute
  real path and **refuses with exit 1** otherwise. `docker run -v` would otherwise create
  a missing source as an empty directory and read a relative one as a *named volume* —
  either way the command gets an empty `/work`, so an absence-shaped accept check ("no
  TODO markers", "lint is clean") passes vacuously and the command's writes are
  discarded (cordon#15).
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
| the worktree's own `.git` mounted `:ro` (when present) | `.git` is not data — it is a directory of things the **host** later executes. Writable `.git/hooks/*`, or `core.hooksPath`/`core.fsmonitor`/filter drivers in `.git/config`, give a sandboxed process code execution on the host at the next host-side git operation, outside every flag above. Only the paths reachable from `<worktree>/.git` are mounted — that file or directory, and for a linked worktree the parent repository it points back to — so a repository *nested* anywhere inside the tree keeps a writable `.git` and is not discovered. Scoped below and in cordon#11 |

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

**cordon classifies a run as a breach only when the run lasted the window.** Neither
code identifies one on its own: Docker returns 137 for an OOM-kill that can fire well
before the deadline, and 124 is what a command that bounds *itself* exits — wrapping a
check in coreutils `timeout` is the ordinary way a test script does that, one level
below cordon and using the same code for the same meaning. So the clock decides, and a
command's own 124 passes through as its exit with nothing added to its stderr. A
genuine breach cannot be shorter than its own window, so this costs a real timeout
nothing (cordon#16).

## The worktree is a trust boundary

The worktree is writable **by design** — the command has to build and test in it. That
makes anything the container leaves behind *untrusted host input*, not a result.

`.git` is the part of it the host executes, so it is mounted read-only whenever it exists
(as a directory, or as a file for a linked `git worktree`). The mount is **conditional**:
`docker run -v` creates a missing bind source, so mounting unconditionally would
materialize a spurious `.git/` in a non-repo worktree — turning a plain directory into a
broken repo and severing it from any enclosing one. For a worktree that is not a repo,
cordon adds no mount and makes no claim.

**A linked worktree also mounts its parent repository's `.git`, read-only.** For a
`git worktree add` worktree, `.git` is a *file* holding `gitdir: <absolute host path>`. Mounting
that file alone put the pointer in the container and nothing it points at, so git answered
`fatal: not a git repository: …/worktrees/<name>` and no accept could read git state — the
guarantee above ("read-only rather than excluded, so an accept that reads git state still works")
did not hold for that shape. The objects live in the parent repository, and
`worktrees/<name>/commondir` points back to it, so mounting only `worktrees/<name>` is not enough.
cordon now binds the resolved common git directory at its **own host path**, read-only, because
the pointer inside the file is absolute (cordon#17).

**The cost, stated because it is a real widening of the read surface.** A sandboxed command in a
linked worktree can read the *whole* repository's git directory. On a harness that provisions
each node as a linked worktree of one shared repository, every node's baseline is in that one
object store, so **every other node's content is readable from inside any node's sandbox** —
`git log --all` inside one worktree lists a sibling worktree's commits. This is measured, not
inferred. Read-only does not narrow it and nothing narrower works. If a consumer needs a node to
see its own baseline and nothing else, provision it as a standalone repository (`git init` into a
fresh directory with one baseline commit), which is what the known driving harness does and which
mounts nothing beyond that node.

What the widening does *not* cost is the seal. Every added mount is `:ro`, including the
**shared** `hooks/` directory a linked worktree uses — which lives in the parent and is now
explicitly read-only rather than merely unreachable; a write to it is refused by the filesystem.
Only `/work` is writable, in either shape.

**"Its parent repository" is verified, not assumed.** The `.git` pointer file lives in `/work` —
the one **writable** mount — so a sandboxed command that ran over a directory with no `.git` can
write one, and a later run would otherwise bind whatever it named. Requiring only the marks of a
git directory (`HEAD`, `objects/`) does not close that: the forged target *is* a git directory.

So cordon requires the **back-pointer**. `git worktree add` writes both halves —
`<worktree>/.git` names `<parent>/.git/worktrees/<name>`, and
`<parent>/.git/worktrees/<name>/gitdir` names `<worktree>/.git` back — and cordon mounts only when
they agree. The forward half is writable from inside the sandbox; the back half is not, because
writing it means already holding write access to the repository being named.

cordon also still refuses when the pointer dangles (`docker run -v` would create the missing
source, inside someone's repository) and when the resolved common directory does not look like a
git directory, which keeps a `commondir` rewritten to `/` from naming the host root even for a
worktree whose back-pointer is genuine.

**What this costs: `--separate-git-dir` gets the pointer file and nothing more.**
`git init --separate-git-dir=<path>` writes no back-pointer in `<path>` and no `core.worktree` in
its config, so from the host that shape is **indistinguishable from a forgery** — it is exactly
what a forgery imitates. cordon does not widen the read surface for it: `.git` is mounted read-only
as the file it is, and a command needing git state in that shape sees the same
`fatal: not a git repository` a linked worktree saw before cordon#17. Stated here rather than left
to be discovered, and asserted in `tests/test_cordon_run_script.py` — including the fixture premise,
so if a future git starts writing that back-pointer the test fails rather than quietly recording a
limitation that no longer exists.

One boundary, stated rather than left to be discovered: the mount source is a **resolved real
path**, because `-v` is resolved by the daemon in its own filesystem namespace. git writes the
resolved path into `gitdir:` itself — a worktree created through a symlinked directory records the
real path, measured — so the mount and the path git looks for agree for every worktree git
creates. A `.git` file *hand-edited* to name a symlinked path is outside that agreement: git looks
for the symlinked path, which is not what was mounted, and the run fails exactly as it did before
this fix. cordon makes no claim there.

**What this does not cover — 1: any other repository.** What is mounted is reached from
`<worktree>/.git` and nothing else — that file or directory, plus the parent repository a linked
worktree's pointer names. Nothing walks the tree looking for other repositories. Closing this
closes host execution via git *for the repository cordon was handed*, and for no other. A vendored clone at `sub/` keeps a writable `sub/.git`, and a
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
