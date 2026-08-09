"""Encryption, masking, and the refuse-to-start configuration rules (§7)."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import ConfigError, load_settings
from app.core import crypto

_TID = uuid.uuid4()


def test_encrypt_decrypt_round_trip(app_env):
    # Flag off (conftest default): legacy Fernet path, db unused.
    plaintext = '{"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}}'
    ciphertext = crypto.encrypt_secret(plaintext, tenant_id=_TID, db=None)
    assert plaintext not in ciphertext
    assert not ciphertext.startswith("v2:")
    assert crypto.decrypt_secret(ciphertext, tenant_id=_TID, db=None) == plaintext


def test_ciphertext_from_another_key_does_not_open(app_env):
    foreign = Fernet(Fernet.generate_key()).encrypt(b"secret").decode()
    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt_secret(foreign, tenant_id=_TID, db=None)


def test_mask_reveals_nothing_of_the_secret(app_env):
    secret = "sk-ant-oat01-supersecrettail"
    masked = crypto.mask_secret("oauth", secret)
    assert masked.startswith("oauth:…")
    # A suffix-based mask would put real characters on every console page.
    assert secret[-8:] not in masked
    assert crypto.mask_secret("oauth", secret) == masked
    assert crypto.mask_secret("oauth", secret + "x") != masked


def test_enroll_tokens_are_stored_only_as_hashes():
    token = crypto.new_token()
    digest = crypto.hash_token(token)
    assert digest.startswith("sha256:")
    assert token not in digest
    assert crypto.hash_token(token) == digest


@pytest.mark.parametrize(
    "env,message",
    [
        ({"AMX_ENCRYPTION_KEY": ""}, "AMX_ENCRYPTION_KEY"),
        ({"AMX_ENCRYPTION_KEY": "not-a-fernet-key"}, "valid Fernet key"),
        ({"AMX_ADMIN_TOKEN": ""}, "AMX_ADMIN_TOKEN"),
        ({"AMX_ADMIN_TOKEN": "short"}, "at least 16"),
        ({"AMX_DATABASE_URL": ""}, "AMX_DATABASE_URL"),
    ],
)
def test_missing_or_weak_configuration_refuses_to_load(app_env, monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert message in str(exc.value)


def test_the_app_will_not_construct_without_an_admin_token(app_env):
    """Startup, not first request — an AMS with no admin token must not serve."""
    script = (
        "import app.main, app.config;"
        "app.config.get_settings.cache_clear();"
        "app.main.create_app()"
    )
    env = dict(os.environ, AMX_ADMIN_TOKEN="")
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "AMX_ADMIN_TOKEN" in result.stderr
