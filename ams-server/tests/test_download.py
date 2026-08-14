"""Artifact distribution routes (packaged install PR2).

The unauthenticated /download surface is where remote code leaves AMS, so the
cases that matter are the ones that would let a caller name a file we never
meant to serve, and the one that proves a downloaded manifest is the manifest
AMS produced.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

from app.api.download import MANIFEST_SIG_ALGORITHM, MANIFEST_SIG_DOMAIN
from app.config import get_settings
from app.grpc.signing import verify

BINARY = b"\x7fELF" + bytes(range(256)) * 4
MANIFEST_TEXT = json.dumps(
    {
        "version": {
            "commit": "deadbeef",
            "builtAt": "2026-08-14T00:00:00Z",
            "wheel": "tsamx-9.9.9-py3-none-any.whl",
        },
        "artifacts": {
            "ama-linux-amd64": {"sha256": "0" * 64, "size": len(BINARY)},
            "tsamx-9.9.9-py3-none-any.whl": {"sha256": "1" * 64, "size": 3},
        },
    },
    indent=2,
)
# A fixed url-safe base64 32-byte seed. Test-only: it signs nothing real.
SIGNING_SEED = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


@pytest.fixture()
def dist(tmp_path, app_env, monkeypatch):
    """A populated artifacts directory with distribution enabled."""
    root = tmp_path / "dist"
    root.mkdir()
    (root / "ama-linux-amd64").write_bytes(BINARY)
    (root / "manifest.json").write_text(MANIFEST_TEXT)
    (root / "install.sh").write_text("#!/bin/sh\n# placeholder (PR3)\n")
    (root / "tsamx-latest.whl").symlink_to(root / "ama-linux-amd64")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours")
    (root / "escape.whl").symlink_to(outside / "secret.txt")

    monkeypatch.setenv("AMX_ARTIFACTS_DIR", str(root))
    monkeypatch.setenv("AMX_SIGNING_KEY", SIGNING_SEED)
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture()
def dl(dist):
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def disabled(app_env, monkeypatch):
    """Distribution off: no AMX_ARTIFACTS_DIR at all."""
    monkeypatch.delenv("AMX_ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("AMX_INSTALL_SCRIPTS_DIR", raising=False)
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_existing_artifact_is_served_byte_for_byte(dl):
    response = dl.get("/download/ama-linux-amd64")
    assert response.status_code == 200
    assert response.content == BINARY
    assert response.headers["x-content-type-options"] == "nosniff"


def test_symlink_inside_the_directory_is_served(dl):
    assert dl.get("/download/tsamx-latest.whl").content == BINARY


def test_symlink_pointing_outside_is_refused(dl):
    assert dl.get("/download/escape.whl").status_code == 404


def test_no_authorization_header_is_required(dl):
    dl.headers.pop("Authorization", None)
    assert dl.get("/download/ama-linux-amd64").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/download/../../etc/passwd",          # literal traversal
        "/download/..%2f..%2fetc%2fpasswd",    # encoded separator
        "/download/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/download/..",
        "/download/%2e%2e",
        "/download//etc/passwd",               # absolute-looking
        "/download/%2fetc%2fpasswd",
        "/download/ama-linux-amd64%00.txt",    # NUL byte
        "/download/.hidden",
        "/download/sub/dir",
    ],
)
def test_path_traversal_attempts_are_refused(dl, path):
    assert dl.get(path).status_code in (404, 422)


def test_traversal_cannot_reach_a_file_that_exists_outside(dl, dist):
    # The concrete target of the traversal attempts above really is readable on
    # disk, so a 404 here is a refusal and not just an absent file.
    outside = dist.parent / "outside" / "secret.txt"
    assert outside.is_file()
    for attempt in ("../outside/secret.txt", "..%2foutside%2fsecret.txt", str(outside)):
        assert dl.get(f"/download/{attempt}").status_code in (404, 422)


def test_unknown_name_that_passes_the_whitelist_is_404(dl):
    assert dl.get("/download/ama-linux-riscv64").status_code == 404


def test_manifest_signature_verifies_against_the_ams_public_key(dl):
    response = dl.get("/download/manifest.json")
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["algorithm"] == MANIFEST_SIG_ALGORITHM
    # Verification is over the file bytes verbatim, which is what the envelope
    # carries as text — no re-serialisation in between.
    assert envelope["manifest"] == MANIFEST_TEXT

    pubkey = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(get_settings().ams_pubkey)
    )
    assert verify(
        pubkey,
        base64.b64decode(envelope["signature"]),
        MANIFEST_SIG_DOMAIN + envelope["manifest"].encode("utf-8"),
    )


def test_manifest_signature_does_not_cover_a_modified_manifest(dl):
    envelope = dl.get("/download/manifest.json").json()
    pubkey = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(get_settings().ams_pubkey)
    )
    tampered = envelope["manifest"].replace("deadbeef", "cafebabe").encode("utf-8")
    assert not verify(
        pubkey, base64.b64decode(envelope["signature"]), MANIFEST_SIG_DOMAIN + tampered
    )


def test_manifest_signature_is_domain_separated_from_command_signing(dl):
    """The same key signs gRPC commands; a manifest signature must not verify
    as a bare-bytes signature, or one protocol's blob could be replayed as the
    other's."""
    envelope = dl.get("/download/manifest.json").json()
    pubkey = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(get_settings().ams_pubkey)
    )
    signature = base64.b64decode(envelope["signature"])
    assert not verify(pubkey, signature, envelope["manifest"].encode("utf-8"))


def test_manifest_names_the_signed_wheel_rather_than_the_symlink(dl):
    """PR3 must install the manifest-listed wheel. The convenience symlink is
    outside the signature, so a manifest that only named it would leave the
    install path unverifiable."""
    manifest = json.loads(dl.get("/download/manifest.json").json()["manifest"])
    wheel = manifest["version"]["wheel"]
    assert wheel in manifest["artifacts"]
    assert wheel != "tsamx-latest.whl"


def test_manifest_envelope_carries_no_key_material(dl):
    envelope = dl.get("/download/manifest.json").json()
    assert set(envelope) == {"manifest", "signature", "algorithm"}


def test_a_non_utf8_manifest_is_refused_not_a_500(dist, monkeypatch):
    (dist / "manifest.json").write_bytes(b'{"version": {"commit": "\xff\xfe"}}')
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/download/manifest.json")
    assert response.status_code == 503
    assert response.json()["code"] == "artifact.manifest_unreadable"


@pytest.mark.parametrize(
    "digest",
    [
        "aa\r\nX-Injected: yes",   # header injection via CR/LF
        "not-hex" * 8,
        "AB" * 32,                 # uppercase: outside the sha256 hex alphabet
        "0" * 63,
        "0" * 65,
        12345,                     # not even a string
    ],
)
def test_a_bad_manifest_digest_drops_the_etag_but_still_serves(dl, dist, digest):
    manifest = json.loads(MANIFEST_TEXT)
    manifest["artifacts"]["ama-linux-amd64"]["sha256"] = digest
    (dist / "manifest.json").write_text(json.dumps(manifest))

    response = dl.get("/download/ama-linux-amd64")
    assert response.status_code == 200
    assert response.content == BINARY
    assert "X-Injected" not in response.headers
    assert response.headers.get("etag", "") != f'"{digest}"'


def test_a_valid_manifest_digest_becomes_the_etag(dl):
    response = dl.get("/download/ama-linux-amd64")
    assert response.headers["etag"] == '"' + "0" * 64 + '"'


def test_install_script_is_served_when_present(dl):
    response = dl.get("/install.sh")
    assert response.status_code == 200
    assert response.text.startswith("#!/bin/sh")


def test_absent_install_script_is_404(dl):
    # PR3 has not written install.ps1 yet; an absent script is a 404, not a 500.
    assert dl.get("/install.ps1").status_code == 404


def test_everything_is_404_while_distribution_is_disabled(disabled):
    for path in (
        "/download/ama-linux-amd64",
        "/download/manifest.json",
        "/install.sh",
        "/install.ps1",
    ):
        assert disabled.get(path).status_code == 404, path


def test_manifest_is_refused_without_a_fixed_signing_key(dist, monkeypatch):
    monkeypatch.delenv("AMX_SIGNING_KEY", raising=False)
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/download/manifest.json")
    assert response.status_code == 503
    assert response.json()["code"] == "artifact.signing_unavailable"
