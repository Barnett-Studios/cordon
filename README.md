# cordon

**Run one command inside a hardened, ephemeral, network-isolated container — so a
runaway or buggy process becomes a bounded, classified failure, not a hang or an escape.**

cordon runs a single command (a build, a test, a grep — a code-generation loop's `accept`
check) in a throwaway container with no network, dropped capabilities, a read-only root,
and hard memory/cpu/pid ceilings. A fork bomb gets pid-killed; a runaway allocation gets
OOM-killed; a phone-home fails deterministically under `--network none`. Every one is a
plain non-zero exit the caller already knows how to classify — never an unbounded hang
that takes the whole batch down with it.

It is a *right-sized* boundary for a **single-user** harness running code its **own**
cascade generated against the user's **own** disposable repo — not a microVM built to
defend against a hostile co-tenant (see [`CONTRACT.md`](CONTRACT.md) → threat model, and
ADR-0040).

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
(defaults are the ADR-0040 hardened values):

```sh
CORDON_MEMORY=4g CORDON_CPUS=4 CORDON_PIDS=1024 \
  bin/cordon-run.sh /path/to/worktree cordon-runtime:local cargo test
```

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
