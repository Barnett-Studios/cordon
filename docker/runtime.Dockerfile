# cordon generic per-node runtime image (ADR-0040).
#
# The swappable `<runtime-image>` argument to bin/cordon-run.sh. Intentionally
# generic (git + python3 + build-essential) rather than per-language — a
# language-specific variant is a future build-arg/tag swap, not a fork of this file.
#
# No user is created or switched here on purpose: cordon-run.sh supplies
# `-u 1000:1000` at `docker run` time, so the same image works regardless of the
# invoking host's uid mapping. Do not bake a fixed non-root user into this image —
# that would fight the runtime -u flag instead of cooperating with it.
#
# No secrets, no `claude` CLI, no network-dependent behavior at container-run time
# (cordon always runs with --network none); apt-get here runs only at image build
# time, not at command-execution time.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        python3 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
