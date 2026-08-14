"""Artifact distribution — packaged install PR2.

These four routes sit at the root, outside ``/api/v1``, and are **deliberately
unauthenticated**: they are what a machine hits *before* it has any credential,
with a one-line ``curl … | bash``. That makes this the remote-code-delivery
surface of AMS, so every route here is read-only, serves nothing outside one
configured directory, and never touches the database or a credential.

What keeps it safe:

* the artifact name is a whitelist match (``_ARTIFACT_NAME``) *and* the resolved
  path must still sit under the resolved artifacts directory. Either check alone
  would do for the attacks we know about; both are cheap. ``Path.resolve``
  follows symlinks before the containment test, so a symlink in the directory is
  only served when its target is also inside it (``tsamx-latest.whl`` → the
  real wheel is the intended case);
* ``manifest.json`` goes out in an envelope signed with the AMS Ed25519 key —
  the same key the gRPC control plane signs commands with, which the agent has
  already pinned at enroll. The signature covers the **file bytes verbatim**, so
  a verifier hashes what it received rather than a re-serialisation of it;
* nothing here emits key material, and the only request-derived value that
  reaches a log line is the artifact name, after it passed the whitelist.

Distribution is off unless ``AMX_ARTIFACTS_DIR`` names a directory; while off,
every route answers 404 rather than advertising that a build output is missing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.config import get_settings
from app.core.errors import ApiError
from app.grpc.signing import Signer

logger = logging.getLogger(__name__)

router = APIRouter(tags=["artifacts"], include_in_schema=False)

# Leading character is alphanumeric, so ".", ".." and dotfiles are rejected by
# the pattern itself and never reach the filesystem.
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MANIFEST_NAME = "manifest.json"

# Domain separation for the manifest signature. The AMS Ed25519 key also signs
# gRPC commands, and both sign raw bytes; without a prefix, "a valid signature
# by AMS" is the only thing either verifier learns, and the two protocols stay
# apart only because their byte shapes happen not to collide today. The prefix
# makes that structural: a command signature can never verify as a manifest one.
# It is committed to before install.sh exists (PR3), so the verifier hashes
# MANIFEST_SIG_DOMAIN + <manifest bytes>. The envelope's `algorithm` names it.
MANIFEST_SIG_DOMAIN = b"amx-manifest-v1\x00"
MANIFEST_SIG_ALGORITHM = "ed25519:amx-manifest-v1"

# A sha256 hex digest, and nothing else. Enforced before the value reaches a
# response header: it comes out of a file on disk, and a header value carrying
# CR/LF would be a header-injection primitive (Starlette raises on it, which
# would turn every download of that artifact into a 500).
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# Opaque on purpose: absent file, unreadable file, traversal attempt and
# distribution-disabled are one indistinguishable answer.
def _gone() -> ApiError:
    return ApiError(404, "Not Found", "artifact.not_found", "No such artifact.")


def _artifacts_root() -> Path:
    configured = get_settings().artifacts_dir
    if not configured:
        raise _gone()
    root = Path(configured).resolve()
    if not root.is_dir():
        raise _gone()
    return root


def _resolve_in(root: Path, name: str) -> Path:
    """The file ``name`` inside ``root``, or 404. Never escapes ``root``."""
    if not _ARTIFACT_NAME.fullmatch(name):
        raise _gone()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise _gone()
    return candidate


def _manifest_sha256(root: Path, name: str) -> str | None:
    """The recorded sha256 of ``name``, read from manifest.json.

    Used as the ETag so a 17 MB binary is not rehashed on every request. A name
    the manifest does not list (``tsamx-latest.whl``, whose real file is listed
    under its versioned name) simply gets no ETag from us — Starlette still
    derives its own from size and mtime. Same for a digest that is not 64 hex
    characters: the download proceeds without an ETag rather than putting an
    unvalidated file-sourced string into a response header.
    """
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_bytes())
        digest = manifest["artifacts"][name]["sha256"]
    except Exception:  # noqa: BLE001 - a missing/broken manifest must not 500 a download
        return None
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        return None
    return digest


def _signer_or_none() -> Signer | None:
    """The signer, but only when a fixed seed is configured.

    ``Signer.from_env_or_generate`` invents a key when ``AMX_SIGNING_KEY`` is
    unset. Signing a manifest with an invented key would hand out a signature no
    agent can verify — worse than none, because it looks verified. So the
    keyless case is refused instead.
    """
    if not os.environ.get("AMX_SIGNING_KEY", "").strip():
        return None
    return Signer.from_env_or_generate()


@router.get("/download/manifest.json")
def get_manifest() -> dict[str, str]:
    """The build manifest plus a detached Ed25519 signature over its bytes.

    The envelope is ``{"manifest": <file text>, "signature": <base64>,
    "algorithm": "ed25519:amx-manifest-v1"}``. ``manifest`` is the file's exact
    contents as a string, not a re-encoded object: the verifier checks the
    signature over ``MANIFEST_SIG_DOMAIN + manifest.encode("utf-8")`` and any
    key reordering or whitespace change would break that, so no canonicalisation
    step is needed on either side.

    The public half is not in the envelope on purpose — a client that took the
    key from the same response it is verifying would be verifying nothing. It
    comes from enroll, pinned.
    """
    root = _artifacts_root()
    path = _resolve_in(root, MANIFEST_NAME)
    signer = _signer_or_none()
    if signer is None:
        raise ApiError(
            503,
            "Service Unavailable",
            "artifact.signing_unavailable",
            "This AMS has no fixed signing key, so the manifest cannot be signed.",
        )
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A half-written or corrupted manifest is a broken build output, not a
        # bad request: refuse it rather than 500, and say nothing about the bytes.
        raise ApiError(
            503,
            "Service Unavailable",
            "artifact.manifest_unreadable",
            "The build manifest on this server is not readable as UTF-8.",
        ) from None
    return {
        "manifest": text,
        "signature": base64.b64encode(signer.sign(MANIFEST_SIG_DOMAIN + raw)).decode(),
        "algorithm": MANIFEST_SIG_ALGORITHM,
    }


@router.get("/download/{artifact}")
def get_artifact(artifact: str) -> FileResponse:
    """One file out of the artifacts directory, streamed."""
    root = _artifacts_root()
    path = _resolve_in(root, artifact)
    logger.info("artifact download: %s", path.name)
    headers = {"X-Content-Type-Options": "nosniff"}
    digest = _manifest_sha256(root, path.name)
    if digest:
        headers["ETag"] = f'"{digest}"'
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers=headers,
    )


def _install_script(name: str) -> FileResponse:
    configured = get_settings().install_scripts_dir
    if not configured:
        raise _gone()
    root = Path(configured).resolve()
    if not root.is_dir():
        raise _gone()
    path = _resolve_in(root, name)
    # text/plain so a browser shows the script instead of downloading it; the
    # installer pipes it either way.
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/install.sh")
def get_install_sh() -> FileResponse:
    return _install_script("install.sh")


@router.get("/install.ps1")
def get_install_ps1() -> FileResponse:
    return _install_script("install.ps1")
