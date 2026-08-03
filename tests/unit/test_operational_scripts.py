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
    assert '"command_type":"start"' in start_script
    assert '"confirmation":"CONFIRM START"' in start_script
    assert '--config "${start_request_config}"' in start_script
    assert '"${command_status}" == "completed"' in start_script
    assert '"${experiment_status}" == "running"' in start_script
    assert "uv run maais daily-supervisor" in start_script
    assert '"scheduler_pid":$scheduler_pid' in start_script
    assert "MANIFEST RESTORE_VERIFICATION QUALIFICATION_BUNDLE" in start_script
    assert '--qualification "${qualification_path}"' in start_script
    assert "qualification:$qualification" in start_script


def test_recovery_script_is_fail_closed_and_audited() -> None:
    recovery_script = (REPOSITORY_ROOT / "scripts" / "recover-paper-week.sh").read_text()

    assert 'if kill -0 "${target_pid}"' in recovery_script
    assert "uv run maais verify-ledger" in recovery_script
    assert "lease_epoch" in recovery_script
    assert "recovery-evidence" in recovery_script
    assert "ENVIRONMENT=production" in recovery_script
    assert '"${service}" != "scheduler"' in recovery_script
    assert "uv run maais daily-supervisor" in recovery_script
    assert ".scheduler_pid=$scheduler_pid" in recovery_script


def test_daily_operations_atomically_record_the_immutable_report_bundle() -> None:
    daily_script = (REPOSITORY_ROOT / "scripts" / "daily-paper-ops.sh").read_text()

    assert 'current_state="${state_dir}/current.json"' in daily_script
    assert "report_directory=\"$(jq -er '.directory'" in daily_script
    assert "report_id=\"$(jq -er '.report_id'" in daily_script
    assert ".daily_reports" in daily_script
    assert 'mv "${temporary_state}" "${current_state}"' in daily_script
    assert "paper_acquire_operation_lock" in daily_script
    assert "--resume-existing" in daily_script


def test_stop_script_uses_the_audited_stop_command_before_process_signals() -> None:
    stop_script = (REPOSITORY_ROOT / "scripts" / "stop-paper-week.sh").read_text()

    assert '"command_type":"stop"' in stop_script
    assert '"confirmation":"CONFIRM STOP"' in stop_script
    assert '"${command_status}" == "rejected"' in stop_script
    assert '"${command_status}" == "completed"' in stop_script
    assert stop_script.index('"command_type":"stop"') < stop_script.index(
        'paper_signal_process_tree "${worker_pid}"'
    )
    assert stop_script.index('paper_signal_process_tree "${scheduler_pid}"') < stop_script.index(
        '"command_type":"stop"'
    )


def test_browser_smoke_uses_a_real_local_browser_and_fails_on_console_errors() -> None:
    browser_script = (REPOSITORY_ROOT / "scripts" / "browser-smoke.sh").read_text()

    assert "playwright-cli" in browser_script
    assert "vite preview --host 127.0.0.1" in browser_script
    assert "No paper experiments yet" in browser_script
    assert "console error --json" in browser_script
    assert "Errors: 0, Warnings: 0" in browser_script
