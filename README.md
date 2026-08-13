# cordon

[![CI](https://github.com/Barnett-Studios/cordon/actions/workflows/ci.yml/badge.svg)](https://github.com/Barnett-Studios/cordon/actions/workflows/ci.yml)
[![Container](https://img.shields.io/badge/ghcr.io-cordon-blue?logo=docker)](https://github.com/Barnett-Studios/cordon/pkgs/container/cordon)
[![License](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg)](#license)

**Executor: isolation · Stable** — feature-complete; maintenance only. The scope is finished,
not abandoned. See the [component map](https://github.com/Barnett-Studios) for how this fits the rest.

## Why this exists

**The moment you dispatch work to a generated executor, you have to bound its writes and its
lifetime — and the failure you actually get is a hang, not an escape.** A model-written build
script that loops forever does not crash; it sits there holding the batch. A runaway allocation
takes the host down with it. Neither produces an error your caller can classify, and a harness that
cannot classify a failure cannot make progress past one.

cordon converts every one of those into a plain non-zero exit. A fork bomb gets pid-killed; a
runaway allocation gets OOM-killed; a busy loop that trips neither is killed at the
`CORDON_TIMEOUT` deadline (exit `124`); a phone-home fails deterministically under
`--network none`. The caller already knows how to handle a non-zero exit. It has no answer for a
process that never returns.

**What it does not bound is writes to your working tree.** That is mounted read-write, because
modifying it is the command's entire job. cordon isolates the *process* — its network, its
capabilities, its resource ceilings, its lifetime — not your working copy. Give it a tree you can
throw away.

The one carve-out is the worktree's own `.git`, shadowed by a read-only mount. A process that can
write `.git/hooks/*`, or set `core.hooksPath` or a filter driver in `.git/config`, gets host code
execution the next time *you* run git — an escape that happens outside the container, long after
it exited, and that none of the boundaries above would ever see.

**That closes host execution via git for the repository cordon was handed — that one, and no
other.** The mount is a single path, `<worktree>/.git`. A repository *nested* inside the tree — a
vendored clone at `sub/` with its own `.git` — keeps a fully writable one, and a container that
appends `[core] hooksPath` to `sub/.git/config` gets host execution the next time you run git in
`sub/`. A local `core.hooksPath` outranks a global one, so the `~/.git-hooks` that dotclaude
installs in every repo does not cover this. Submodules land in between: the real gitdir lives at
`<worktree>/.git/modules/<name>/` and *is* under the read-only mount, but the `vendor/.git`
pointer file in the tree is writable and can be repointed — no working execution has been
demonstrated through that, and none is claimed either way.

Widening the mount to every nested `.git` would close it and is deliberately not done: it costs a
full tree walk before every run and an unbounded mount count, to shut a path that only matters
against someone *looking* for the nested repo — and that someone already has the writable worktree
below, which is cheaper. Tracked as [#11](https://github.com/Barnett-Studios/cordon/issues/11).

The worktree is still writable, so a
container can leave behind a `conftest.py`, a `build.rs`, a `Makefile`, a `package.json` script or
an `.envrc` that runs the next time *you* run a build or test tool in that tree. Closing that class
needs a host-side `git reset --hard && git clean -fd` before any host tooling touches it, and
**cordon cannot do that for you** — it is your side of the boundary.
[`CONTRACT.md`](CONTRACT.md) states the residual in full rather than implying it away.

**It is deliberately not a microVM.** It is right-sized for a **single-user** harness running code
its **own** cascade generated against the user's **own** disposable repo. It is not built to
withstand a hostile co-tenant, and the threat model in [`CONTRACT.md`](CONTRACT.md) says so
explicitly rather than leaving you to infer the boundary.

## What it does

cordon runs a single command (a build, a test, a grep — a code-generation loop's `accept`
check) in a throwaway container with no network, dropped capabilities, a read-only root,
hard memory/cpu/pid ceilings, and a wall-clock deadline.

> Part of the Barnett Studios agentic-harness toolkit → cxpak · commitward · abproof ·
> cascadr · **cordon** · …

## Use

```sh
# build the generic runtime image once
docker build -f docker/runtime.Dockerfile -t cordon-runtime:local .

# run a command against a work tree, isolated
bin/cordon-run.sh /path/to/worktree cordon-runtime:local sh -c "pytest -q"
```

stdout/stderr and the exit code are the command's own. Tune the ceilings via env
(defaults are the hardened values):

```sh
CORDON_MEMORY=4g CORDON_CPUS=4 CORDON_PIDS=1024 CORDON_TIMEOUT=600 \
  bin/cordon-run.sh /path/to/worktree cordon-runtime:local cargo test
```

The wall-clock bound (`CORDON_TIMEOUT`, default `300`s; `CORDON_KILL_AFTER` grace default
`10`s) is enforced with coreutils `timeout` — a deadline breach exits `124` and the
container is reaped by a cleanup trap, so a busy loop can never hang the batch. `timeout`
must be on `PATH` (macOS: `brew install coreutils` provides `gtimeout`, which cordon
auto-detects).

The security posture (`--network none`, `--cap-drop ALL`, read-only root, non-root uid, …)
is **fixed** — only the resource ceilings tune. See [`CONTRACT.md`](CONTRACT.md).

## Install

The sandbox is `bin/cordon-run.sh`.

> **Releases up to and including `v0.1.2` carry no assets** — publishing the script is new, so
> the first release cut *after* that change is the first one to have it. Until then, take the
> script from a checkout of the tag you want. Check the
> [releases page](https://github.com/Barnett-Studios/cordon/releases) for the assets before
> using the commands below.

Each release that has them ships `cordon-run.sh` plus a SHA-256, a cosign signature and its
certificate:

```sh
V=<a release whose assets include cordon-run.sh>
base="https://github.com/Barnett-Studios/cordon/releases/download/v$V"
curl -fsSLO "$base/cordon-run.sh" \
     -O "$base/cordon-run.sh.sha256" \
     -O "$base/cordon-run.sh.sig" \
     -O "$base/cordon-run.sh.pem"

sha256sum -c cordon-run.sh.sha256      # macOS: shasum -a 256 -c

# Provenance, not just integrity — the checksum is uploaded by whoever could also
# tamper with the script, so verify the signature, not only the digest.
cosign verify-blob cordon-run.sh \
  --signature cordon-run.sh.sig \
  --certificate cordon-run.sh.pem \
  --certificate-identity-regexp '^https://github\.com/Barnett-Studios/cordon/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

chmod +x cordon-run.sh
```

## Runtime image — *not* the sandbox

`ghcr.io/barnett-studios/cordon` is the swappable `<runtime>` argument, **not** the
security boundary. It is a deliberately generic `git + python3 + build-essential` image
with no isolation properties of its own; pulling it and running it directly gives you
none of cordon's guarantees.

Every guarantee — `--network none`, `--cap-drop ALL`, read-only root, non-root uid, the
pid/memory/cpu ceilings, the wall-clock reaper, the read-only `.git` mount — is supplied
by `cordon-run.sh` at `docker run` time. That is why the script is the artifact to install
and verify.

The script is not baked into the image as an entrypoint: it is the thing that *invokes*
`docker run`, so running it inside the container it launches would mean docker-in-docker.

A language-specific variant is a tag swap, not a fork. Bring your own image — any image
works (cordon supplies `-u $(id -u):$(id -g)` itself, so the image needs no baked-in user).

## Tests

- `tests/test_cordon_run_script.py` — static: every security flag present, the resource
  seam wired, no security flag parameterized away. No Docker needed.
- `tests/test_release_publishes_sandbox.py` — static: the release actually publishes the
  audited script plus its checksum. A correct sandbox nobody receives is not a shipped
  control.
- `tests/test_cordon_live_smoke.py` — opt-in (`CORDON_LIVE_SMOKES=1`): against a live
  daemon, observes a blocked egress, a bounded OOM-kill, and that a hook or a
  `core.hooksPath` planted inside the container does **not** execute on the host. One
  test pins the *limit* of that — a nested repository's `.git` stays writable — so the
  mount cannot widen without the docs above following it.

## License

Licensed under either of [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE) at your option.
Unless you explicitly state otherwise, any contribution you intentionally submit for
inclusion in the work shall be dual-licensed as above, without any additional terms.

---

Built by [Barnett Studios](https://barnett-studios.com/) — part of the agentic-harness
toolkit: [cxpak](https://github.com/Barnett-Studios/cxpak) ·
[commitward](https://github.com/Barnett-Studios/commitward) ·
[cascadr](https://github.com/Barnett-Studios/cascadr) ·
[abproof](https://github.com/Barnett-Studios/abproof) · **cordon** ·
[slicr](https://github.com/Barnett-Studios/slicr).
