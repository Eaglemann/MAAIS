import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maais.operations.backups import BackupMetadata
from maais.operations.restores import (
    load_verified_backup,
    restore_archive,
    validate_restore_target,
    write_restore_verification,
)

DATABASE_URL = "postgresql+psycopg://maais:local-password@localhost:5432/maais"


def _backup_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "backup"
    bundle.mkdir()
    dump = bundle / "database.dump"
    dump.write_bytes(b"valid-custom-archive")
    manifest = {
        "backup_schema_version": 1,
        "database_name": "maais",
        "schema_revision": "0015",
        "table_counts": {"decision_cycles": 10, "domain_events": 210},
        "ledger": {"ok": True, "error_count": 0, "errors": []},
        "dump": {
            "filename": "database.dump",
            "bytes": dump.stat().st_size,
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "format": "postgresql_custom",
        },
    }
    (bundle / "backup-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return bundle


def test_restore_target_requires_exact_confirmation_and_safe_suffix() -> None:
    validate_restore_target("maais", "maais_week_restore", "maais_week_restore")

    with pytest.raises(ValueError, match="confirmation"):
        validate_restore_target("maais", "maais_week_restore", "wrong_restore")
    with pytest.raises(ValueError, match="_restore or _test"):
        validate_restore_target("maais", "maais_copy", "maais_copy")
    with pytest.raises(ValueError, match="must differ"):
        validate_restore_target("maais", "maais", "maais")


def test_verified_backup_rejects_archive_tampering(tmp_path: Path) -> None:
    bundle = _backup_bundle(tmp_path)
    verified = load_verified_backup(bundle)
    assert verified.source_database == "maais"

    verified.dump_path.write_bytes(b"X" * verified.dump_path.stat().st_size)
    with pytest.raises(ValueError, match="SHA-256"):
        load_verified_backup(bundle)


def test_restore_archive_uses_new_target_without_password_in_arguments(tmp_path: Path) -> None:
    backup = load_verified_backup(_backup_bundle(tmp_path))
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    restore_archive(
        DATABASE_URL,
        backup,
        target_database="maais_week_restore",
        confirmation="maais_week_restore",
        runner=fake_run,
    )

    assert commands[0][0] == "createdb"
    assert commands[0][-1] == "maais_week_restore"
    assert commands[1][0:4] == [
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
    ]
    assert commands[1][commands[1].index("--dbname") + 1] == "maais_week_restore"
    assert "local-password" not in " ".join(part for command in commands for part in command)


def test_restore_verification_records_exact_inventory_match(tmp_path: Path) -> None:
    backup = load_verified_backup(_backup_bundle(tmp_path))
    restored = BackupMetadata(
        database_name="maais_week_restore",
        schema_revision="0015",
        database_size_bytes=123456,
        table_counts={"decision_cycles": 10, "domain_events": 210},
        ledger={"ok": True, "error_count": 0, "errors": []},
    )

    paths, passed = write_restore_verification(
        backup,
        restored,
        tmp_path / "verification",
        generated_at=datetime(2026, 8, 2, 21, 0, tzinfo=timezone.utc),
    )

    result = json.loads(paths.result_path.read_text(encoding="utf-8"))
    assert passed
    assert result["passed"] is True
    assert result["schema_revision_match"] is True
    assert result["table_counts_match"] is True
    assert result["ledger"]["ok"] is True
