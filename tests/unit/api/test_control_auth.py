from pathlib import Path

import pytest
from pydantic import SecretStr

from maais.api.app import create_app
from maais.api.auth import load_control_token
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.security.passwords import hash_operator_password


def test_control_token_file_must_be_private_regular_and_high_entropy(tmp_path: Path) -> None:
    token_file = tmp_path / "mission-control.token"
    token = "a1" * 32
    token_file.write_text(f"{token}\n", encoding="ascii")
    token_file.chmod(0o600)

    assert load_control_token(token_file) == token

    token_file.chmod(0o640)
    with pytest.raises(PermissionError, match="owner-readable only"):
        load_control_token(token_file)


def test_control_token_loader_rejects_symlinks_and_malformed_secrets(tmp_path: Path) -> None:
    target = tmp_path / "target.token"
    target.write_text(f"{'b2' * 32}\n", encoding="ascii")
    target.chmod(0o600)
    symlink = tmp_path / "linked.token"
    symlink.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        load_control_token(symlink)

    target.write_text("short-token\n", encoding="ascii")
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        load_control_token(target)


def test_direct_control_token_rejects_weak_or_whitespace_values() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        create_app(control_token="short")
    with pytest.raises(ValueError, match="trimmed"):
        create_app(control_token=f" {'a' * 32}")


def test_cloud_session_mode_rejects_bearer_token_configuration() -> None:
    security = SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(
            hash_operator_password(
                "paper-only operator passphrase"  # pragma: allowlist secret
            )
        ),
        session_pepper=SecretStr("s" * 43),
        csrf_pepper=SecretStr("c" * 43),
        monitor_token=SecretStr("m" * 43),
        secure_cookies=True,
        public_origin="https://mission-control.test",
    )

    with pytest.raises(ValueError, match="local control token"):
        create_app(control_token="a" * 32, security_settings=security)
