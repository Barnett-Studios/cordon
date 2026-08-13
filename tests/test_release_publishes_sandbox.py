"""cordon#4 — the thing that IS the sandbox must actually ship.

`docker/runtime.Dockerfile` is `debian:bookworm-slim` plus a toolchain. The isolation
lives entirely in `bin/cordon-run.sh` — `--network none`, `--cap-drop ALL`, `--read-only`,
`-u $(id -u):$(id -g)`, the pid/memory/cpu ceilings, the wall-clock reaper. The release workflow
built and signed only the image, so `docker pull ghcr.io/barnett-studios/cordon` handed a
consumer a generic toolchain image with **zero** isolation properties, and the sandbox
itself was reachable only by cloning the repo.

`tests/test_cordon_run_script.py` proves the script is hardened. These tests prove the
hardened script is the one that gets published — a correct artifact nobody receives is not
a shipped security control. No Docker or network needed.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"
SCRIPT_PATH = "bin/cordon-run.sh"


def _release_yml() -> str:
    assert RELEASE_YML.is_file(), f"missing {RELEASE_YML}"
    return RELEASE_YML.read_text()


def test_release_publishes_the_sandbox_script():
    text = _release_yml()
    assert SCRIPT_PATH in text, (
        f"{SCRIPT_PATH} appears nowhere in the release workflow — the component that IS "
        "the sandbox ships in no artifact, so `docker pull` yields isolation-free "
        "toolchain image only (cordon#4)"
    )
    assert "gh release" in text, (
        "the script must be attached to the GitHub Release, not merely copied around "
        "inside the job"
    )


def test_the_published_script_is_the_one_the_security_tests_validate():
    # The artifact is staged by copying `bin/cordon-run.sh` verbatim. If a future edit
    # generates, templates, or rewrites the script on the way to the release, every
    # guarantee asserted by test_cordon_run_script.py stops applying to what consumers
    # actually download.
    text = _release_yml()
    assert re.search(rf"cp\s+{re.escape(SCRIPT_PATH)}\s", text), (
        f"the released artifact must be a verbatim copy of {SCRIPT_PATH}; anything else "
        "means the audited file and the shipped file can diverge"
    )


def test_release_publishes_a_checksum_beside_the_script():
    text = _release_yml()
    assert "sha256sum" in text, (
        "a security-critical shell script downloaded over HTTPS needs a checksum a "
        "consumer can verify against; the image is signed with cosign, the script had "
        "nothing"
    )
    assert "cordon-run.sh.sha256" in text, "the checksum must ship as its own asset"


def test_the_script_is_signed_at_parity_with_the_image():
    # The image in this same workflow is cosign-signed. A bare checksum on the script is
    # corruption-detection, not provenance: it is uploaded by the same actor that could
    # tamper with the artifact. The script is the MORE security-sensitive of the two — it is
    # what runs untrusted code — so it must not ship with the weaker guarantee.
    text = _release_yml()
    assert "cosign sign-blob" in text, (
        "the sandbox script must be cosign-signed, not merely checksummed"
    )
    for asset in ("cordon-run.sh.sig", "cordon-run.sh.pem"):
        assert asset in text, f"{asset} must be uploaded alongside the script"


def test_install_docs_do_not_pin_an_assetless_release():
    # README told consumers to curl the script from v0.1.2, which carries no assets —
    # publishing the script postdates that release, so the documented command 404s. Docs must
    # not hardcode a version; the reader picks a release that actually has the asset.
    readme = (ROOT / "README.md").read_text()
    assert "releases/download/v$V" in readme, "the install snippet should stay version-agnostic"
    assert "V=0.1.2" not in readme, (
        "README pins v0.1.2, a release with zero assets — the documented curl 404s"
    )
    assert "carry no assets" in readme, (
        "the gap between the docs and the first asset-bearing release must be stated, not implied"
    )


def test_docs_tell_consumers_to_verify_provenance():
    readme = (ROOT / "README.md").read_text()
    assert "cosign verify-blob" in readme, (
        "shipping a signature the docs never tell anyone to check is theatre"
    )


def test_keyless_signing_has_the_oidc_scope_it_needs():
    # cosign keyless signing mints its certificate from a GitHub OIDC token, which requires
    # `id-token: write`. Without it the job fails only at tag time — after a release has
    # already been cut — so assert it here rather than discovering it in production.
    text = _release_yml()
    assert text.count("id-token: write") >= 2, (
        "both the image job and the sandbox job sign with cosign, so both need "
        "`id-token: write`"
    )


def test_the_release_job_may_write_releases():
    # A `contents: write` scope somewhere in the file — without it the upload fails at
    # run time, which is a failure nobody sees until a tag is already pushed.
    text = _release_yml()
    assert re.search(r"contents:\s*write", text), (
        "publishing a release asset needs `permissions: contents: write`"
    )


def test_default_permissions_stay_read_only():
    # Guard: the tests above must not be satisfiable by widening the whole workflow. The
    # top-level default stays read-only; only the job that publishes gets write.
    text = _release_yml()
    top_level = text.split("jobs:", 1)[0]
    assert re.search(r"contents:\s*read", top_level), (
        "the workflow-level default permission must remain `contents: read`; only the "
        "publishing job escalates"
    )


def test_every_action_is_pinned_to_a_commit_sha():
    # Guard: cordon pins actions to immutable SHAs rather than mutable tags. Adding a
    # release step must not be the thing that quietly breaks that rule — this is the
    # sandbox component, so its own supply chain is part of the product.
    text = _release_yml()
    unpinned = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"uses:\s*\S+@(?!\s*[0-9a-f]{40})", line)
    ]
    assert not unpinned, f"actions must be pinned to a 40-char commit SHA; got {unpinned}"
