import errno
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maais.operations.backups import BackupMetadata, create_database_backup

DATABASE_URL = "postgresql+psycopg://maais:maais@localhost:5432/maais"


def test_disk_full_leaves_no_partial_backup_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text

    def fail_manifest_write(path: Path, *args: object, **kwargs: object) -> int:
        if path.name == "backup-manifest.json":
            raise OSError(errno.ENOSPC, "simulated disk full")
        return original_write_text(path, *args, **kwargs)  # type: ignore[arg-type]

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "pg_dump" and "--version" not in command:
            Path(command[command.index("--file") + 1]).write_bytes(b"valid archive")
        stdout = "pg_dump (PostgreSQL) 16.14" if "--version" in command else "archive list"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(Path, "write_text", fail_manifest_write)
    with pytest.raises(OSError, match="simulated disk full"):
        create_database_backup(
            DATABASE_URL,
            tmp_path,
            BackupMetadata(
                database_name="maais",
                schema_revision="0015",
                database_size_bytes=1,
                table_counts={"domain_events": 0},
                ledger={"ok": True, "error_count": 0, "errors": []},
            ),
            generated_at=datetime(2026, 8, 2, 20, tzinfo=timezone.utc),
            runner=fake_run,
        )

    assert list(tmp_path.iterdir()) == []
