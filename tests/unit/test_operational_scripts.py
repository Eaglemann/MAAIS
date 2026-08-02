from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_paper_services_start_with_structured_production_logging() -> None:
    start_script = (REPOSITORY_ROOT / "scripts" / "start-paper-week.sh").read_text()

    assert (
        "exec env RUN_MODE=paper_live ENVIRONMENT=production "
        "MISSION_CONTROL_TOKEN_FILE=%q uv run maais mission-control" in start_script
    )
    assert (
        "exec env RUN_MODE=paper_live ENVIRONMENT=production "
        "uv run maais paper-live" in start_script
    )


def test_recovery_script_is_fail_closed_and_audited() -> None:
    recovery_script = (REPOSITORY_ROOT / "scripts" / "recover-paper-week.sh").read_text()

    assert 'if kill -0 "${target_pid}"' in recovery_script
    assert "uv run maais verify-ledger" in recovery_script
    assert "lease_epoch" in recovery_script
    assert "recovery-evidence" in recovery_script
    assert "ENVIRONMENT=production" in recovery_script


def test_daily_operations_atomically_record_the_immutable_report_bundle() -> None:
    daily_script = (REPOSITORY_ROOT / "scripts" / "daily-paper-ops.sh").read_text()

    assert 'current_state="${state_dir}/current.json"' in daily_script
    assert "report_directory=\"$(jq -er '.directory'" in daily_script
    assert "report_id=\"$(jq -er '.report_id'" in daily_script
    assert ".daily_reports" in daily_script
    assert 'mv "${temporary_state}" "${current_state}"' in daily_script
    assert "paper_acquire_operation_lock" in daily_script
    assert "--resume-existing" in daily_script
