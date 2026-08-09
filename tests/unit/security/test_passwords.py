from __future__ import annotations

from io import StringIO

import pytest
from argon2 import PasswordHasher, Type

import maais.cli as cli
from maais.cli import generate_secret_token_command, operator_password_hash_command
from maais.security.passwords import (
    INVALID_CREDENTIALS,
    PASSWORD_HASHER,
    hash_operator_password,
    validate_operator_passphrase,
    validate_operator_password_hash,
    verify_operator_password,
)

PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret


def test_operator_password_hash_uses_frozen_argon2id_policy() -> None:
    encoded = hash_operator_password(PASSPHRASE)
    parameters = validate_operator_password_hash(encoded)

    assert encoded.startswith("$argon2id$")
    assert parameters.type is Type.ID
    assert parameters.memory_cost >= 65_536
    assert parameters.time_cost >= 3
    assert parameters.parallelism >= 4
    assert parameters.hash_len >= 32
    assert parameters.salt_len >= 16
    assert PASSWORD_HASHER.verify(encoded, PASSPHRASE)


def test_password_verification_is_uniform_for_mismatch_and_malformed_hash() -> None:
    encoded = hash_operator_password(PASSPHRASE)

    mismatch = verify_operator_password("not-the-passphrase", encoded)
    malformed = verify_operator_password(PASSPHRASE, "not-an-argon2-hash")

    assert mismatch.valid is malformed.valid is False
    assert mismatch.public_error_code == malformed.public_error_code == INVALID_CREDENTIALS
    assert mismatch.needs_rehash is malformed.needs_rehash is False


def test_valid_stronger_hash_is_accepted_and_rehash_state_is_separate() -> None:
    stronger = PasswordHasher(
        time_cost=4,
        memory_cost=131_072,
        parallelism=4,
        hash_len=48,
        salt_len=24,
        type=Type.ID,
    ).hash(PASSPHRASE)

    parameters = validate_operator_password_hash(stronger)
    result = verify_operator_password(PASSPHRASE, stronger)

    assert parameters.memory_cost == 131_072
    assert result.valid is True
    assert result.public_error_code is None
    assert isinstance(result.needs_rehash, bool)


@pytest.mark.parametrize("value", ("short", " " * 20, "x" * 257))
def test_operator_passphrase_policy_is_bounded(value: str) -> None:
    with pytest.raises(ValueError, match="passphrase"):
        validate_operator_passphrase(value)


def test_password_hash_helper_requires_tty_confirmation_and_never_echoes_passphrase() -> None:
    prompts: list[str] = []
    values = iter((PASSPHRASE, PASSPHRASE))
    output = StringIO()

    result = operator_password_hash_command(
        reader=lambda prompt: (prompts.append(prompt), next(values))[1],
        output=output.write,
        input_is_tty=True,
    )

    rendered = output.getvalue()
    assert result == 0
    assert PASSPHRASE not in rendered
    assert PASSPHRASE not in " ".join(prompts)
    assert rendered.startswith("$argon2id$")
    assert PASSWORD_HASHER.verify(rendered.strip(), PASSPHRASE)

    mismatched_values = iter((PASSPHRASE, "different passphrase"))
    with pytest.raises(ValueError, match="confirmation"):
        operator_password_hash_command(
            reader=lambda _prompt: next(mismatched_values),
            output=lambda _value: None,
            input_is_tty=True,
        )
    with pytest.raises(RuntimeError, match="TTY"):
        operator_password_hash_command(
            reader=lambda _prompt: PASSPHRASE,
            output=lambda _value: None,
            input_is_tty=False,
        )


def test_secret_helper_emits_one_high_entropy_token_without_accepting_input() -> None:
    output = StringIO()

    result = generate_secret_token_command(output=output.write)
    token = output.getvalue().strip()

    assert result == 0
    assert len(token) >= 43
    assert token.isascii()


@pytest.mark.parametrize("command", ("operator-password-hash", "generate-secret-token"))
def test_secret_helpers_reject_command_line_secret_arguments(command: str) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([command, "--password", "must-not-enter-process-arguments"])


@pytest.mark.parametrize(
    ("command", "handler_name"),
    (
        ("operator-password-hash", "operator_password_hash_command"),
        ("generate-secret-token", "generate_secret_token_command"),
    ),
)
def test_secret_helpers_run_before_settings_and_logging(
    command: str,
    handler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("secret helper crossed the settings or logging boundary")

    monkeypatch.setattr(cli, handler_name, lambda: 0)
    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "configure_logging", forbidden)

    assert cli.main([command]) == 0
