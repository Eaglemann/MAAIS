"""Fail-closed loading for the local Mission Control bearer token."""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path

_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")


def load_control_token(path: Path | None) -> str | None:
    if path is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("Mission Control token path must be a regular file") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Mission Control token path must be a regular file")
        permissions = stat.S_IMODE(metadata.st_mode)
        if not permissions & stat.S_IRUSR or permissions & 0o077:
            raise PermissionError("Mission Control token file must be owner-readable only")
        with os.fdopen(descriptor, "r", encoding="ascii") as handle:
            descriptor = -1
            token = handle.read(66).strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("Mission Control token must be 64 lowercase hexadecimal characters")
    return token
