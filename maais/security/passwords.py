from __future__ import annotations

from dataclasses import dataclass

from argon2 import Parameters, PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ARGON2_MEMORY_COST_KIB = 65_536
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 4
ARGON2_HASH_LENGTH = 32
ARGON2_SALT_LENGTH = 16
MINIMUM_PASSPHRASE_LENGTH = 16
MAXIMUM_PASSPHRASE_LENGTH = 256
INVALID_CREDENTIALS = "invalid_credentials"

PASSWORD_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_LENGTH,
    salt_len=ARGON2_SALT_LENGTH,
    type=Type.ID,
)


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    valid: bool
    needs_rehash: bool
    public_error_code: str | None


def validate_operator_passphrase(passphrase: str) -> str:
    if not isinstance(passphrase, str):
        raise ValueError("operator passphrase must be text")
    if not MINIMUM_PASSPHRASE_LENGTH <= len(passphrase) <= MAXIMUM_PASSPHRASE_LENGTH:
        raise ValueError("operator passphrase must contain between 16 and 256 characters")
    if not passphrase.strip():
        raise ValueError("operator passphrase must not be blank")
    return passphrase


def hash_operator_password(passphrase: str) -> str:
    return PASSWORD_HASHER.hash(validate_operator_passphrase(passphrase))


def validate_operator_password_hash(encoded_hash: str) -> Parameters:
    try:
        parameters = extract_parameters(encoded_hash)
    except InvalidHashError as error:
        raise ValueError("operator password hash must be valid Argon2id") from error
    if (
        parameters.type is not Type.ID
        or parameters.memory_cost < ARGON2_MEMORY_COST_KIB
        or parameters.time_cost < ARGON2_TIME_COST
        or parameters.parallelism < ARGON2_PARALLELISM
        or parameters.hash_len < ARGON2_HASH_LENGTH
        or parameters.salt_len < ARGON2_SALT_LENGTH
    ):
        raise ValueError("operator password hash must satisfy the frozen Argon2id policy")
    return parameters


def verify_operator_password(passphrase: str, encoded_hash: str) -> PasswordVerification:
    try:
        valid = PASSWORD_HASHER.verify(encoded_hash, passphrase)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return PasswordVerification(
            valid=False,
            needs_rehash=False,
            public_error_code=INVALID_CREDENTIALS,
        )
    if not valid:  # pragma: no cover - argon2 raises on mismatch
        return PasswordVerification(False, False, INVALID_CREDENTIALS)
    return PasswordVerification(
        valid=True,
        needs_rehash=PASSWORD_HASHER.check_needs_rehash(encoded_hash),
        public_error_code=None,
    )
