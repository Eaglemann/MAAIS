import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maais.operations.backups import (
    BackupMetadata,
    create_database_backup,
    postgres_cli_connection,
)

DATABASE_URL = "postgresql+psycopg://maais:local-password@localhost:5432/maais"


def _metadata() -> BackupMetadata:
    return BackupMetadata(
        database_name="maais",
        schema_revision="0015",
        database_size_bytes=123456,
        table_counts={"decision_cycles": 10, "domain_events": 210},
        ledger={"ok": True, "error_count": 0, "errors": []},
    )


def test_postgres_cli_connection_keeps_password_out_of_arguments() -> None:
    arguments, environment = postgres_cli_connection(DATABASE_URL)

    assert arguments == [
        "--host",
        "localhost",
        "--port",
        "5432",
        "--username",
        "maais",
        "--dbname",
        "maais",
    ]
    assert "local-password" not in " ".join(arguments)
    assert environment["PGPASSWORD"] == "local-password"


def test_database_backup_is_immutable_validated_and_hashed(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "pg_dump" and "--version" not in command:
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"postgres-custom-archive")
        stdout = "pg_dump (PostgreSQL) 16.14" if "--version" in command else "archive list"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    paths = create_database_backup(
        DATABASE_URL,
        tmp_path,
        _metadata(),
        generated_at=datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc),
        runner=fake_run,
    )

    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    assert paths.dump_path.is_file()
    assert len(manifest["dump"]["sha256"]) == 64
    assert manifest["table_counts"] == {"decision_cycles": 10, "domain_events": 210}
    assert manifest["ledger"] == {"ok": True, "error_count": 0, "errors": []}
    assert commands[1][0:2] == ["pg_dump", "--format=custom"]
    assert commands[2][0:2] == ["pg_restore", "--list"]
    assert "local-password" not in " ".join(part for command in commands for part in command)

    with pytest.raises(FileExistsError, match="backup bundle already exists"):
        create_database_backup(
            DATABASE_URL,
            tmp_path,
            _metadata(),
            generated_at=datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc),
            runner=fake_run,
        )


def test_database_backup_refuses_inconsistent_ledger(tmp_path: Path) -> None:
    metadata = _metadata()
    bad = BackupMetadata(
        database_name=metadata.database_name,
        schema_revision=metadata.schema_revision,
        database_size_bytes=metadata.database_size_bytes,
        table_counts=metadata.table_counts,
        ledger={"ok": False, "error_count": 1, "errors": [{"code": "stream_gap"}]},
    )

    with pytest.raises(ValueError, match="ledger verification must pass"):
        create_database_backup(
            DATABASE_URL,
            tmp_path,
            bad,
            generated_at=datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc),
        )
