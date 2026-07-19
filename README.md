# cordon

[![CI](https://github.com/Barnett-Studios/cordon/actions/workflows/ci.yml/badge.svg)](https://github.com/Barnett-Studios/cordon/actions/workflows/ci.yml)
[![Container](https://img.shields.io/badge/ghcr.io-cordon-blue?logo=docker)](https://github.com/Barnett-Studios/cordon/pkgs/container/cordon)
[![License](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg)](#license)

**Run one command inside a hardened, ephemeral, network-isolated container — so a
runaway or buggy process becomes a bounded, classified failure, not a hang or an escape.**

cordon runs a single command (a build, a test, a grep — a code-generation loop's `accept`
check) in a throwaway container with no network, dropped capabilities, a read-only root,
hard memory/cpu/pid ceilings, and a wall-clock deadline. A fork bomb gets pid-killed; a
runaway allocation gets OOM-killed; a busy loop that trips none of those is killed at the
`CORDON_TIMEOUT` deadline (exit `124`); a phone-home fails deterministically under
`--network none`. Every one is a plain non-zero exit the caller already knows how to
classify — never an unbounded hang that takes the whole batch down with it.

It is a *right-sized* boundary for a **single-user** harness running code its **own**
cascade generated against the user's **own** disposable repo — not a microVM built to
defend against a hostile co-tenant (see [`CONTRACT.md`](CONTRACT.md) → threat model).

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

## Runtime image

`docker/runtime.Dockerfile` is a deliberately generic `git + python3 + build-essential`
image. A language-specific variant is a tag swap, not a fork. Bring your own image — any
image works, cordon only supplies the isolation flags at `docker run` time (including the
non-root `-u 1000:1000`, so the image needs no baked-in user).

## Tests

- `tests/test_cordon_run_script.py` — static: every security flag present, the resource
  seam wired, no security flag parameterized away. No Docker needed.
- `tests/test_cordon_live_smoke.py` — opt-in (`CORDON_LIVE_SMOKES=1`): observes a blocked
  egress and a bounded OOM-kill against a live daemon.

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
