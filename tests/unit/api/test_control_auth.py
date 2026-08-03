from pathlib import Path

import pytest

from maais.api.app import create_app
from maais.api.auth import load_control_token


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
