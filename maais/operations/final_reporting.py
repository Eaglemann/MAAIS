"""Immutable aggregation of verified daily paper-report bundles."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast
from uuid import UUID

from maais.domain.json import content_hash, to_json_data
from maais.operations.reporting import REPORT_SCHEMA_VERSION, berlin_daily_window

FINAL_REPORT_SCHEMA_VERSION = 2
_DAILY_ARTIFACTS = frozenset(
    {
        "report.json",
        "report.md",
        "decisions.csv",
        "decisions.parquet",
        "execution.csv",
        "execution.parquet",
    }
)
_EXPERIMENT_IDENTITY = (
    "id",
    "name",
    "mode",
    "git_sha",
    "worktree_hash",
    "lock_hash",
    "schema_revision",
    "config_hash",
    "manifest_hash",
    "started_at",
)


class FinalReportValidationError(ValueError):
    """A daily bundle cannot be trusted as final-report evidence."""


@dataclass(frozen=True, slots=True)
class FinalReportBundlePaths:
    directory: Path
    json_path: Path
    markdown_path: Path
    daily_reports_csv_path: Path
    manifest_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FinalReportValidationError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinalReportValidationError(f"{name} must be a nonnegative integer")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise FinalReportValidationError(f"{name} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinalReportValidationError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise FinalReportValidationError(f"{name} must be a finite decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise FinalReportValidationError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalReportValidationError(f"{name} must be an ISO timestamp") from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise FinalReportValidationError(f"{name} must be UTC")
    return parsed


def _verify_bundle(directory: Path) -> tuple[dict[str, object], dict[str, object]]:
    report_path = directory / "report.json"
    manifest_path = directory / "bundle-manifest.json"
    if not report_path.is_file() or not manifest_path.is_file():
        raise FinalReportValidationError(f"daily bundle is incomplete: {directory.name}")
    try:
        report = _object(json.loads(report_path.read_text(encoding="utf-8")), "daily report")
        manifest = _object(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            "daily bundle manifest",
        )
    except json.JSONDecodeError as exc:
        raise FinalReportValidationError(
            f"daily bundle contains invalid JSON: {directory.name}"
        ) from exc
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or manifest.get("report_id") != report_id:
        raise FinalReportValidationError(f"daily report identity mismatch: {directory.name}")
    artifacts = _object(manifest.get("artifacts"), "daily bundle artifacts")
    if set(artifacts) != _DAILY_ARTIFACTS:
        raise FinalReportValidationError(
            f"daily bundle artifact inventory mismatch: {directory.name}"
        )
    for filename in sorted(_DAILY_ARTIFACTS):
        metadata = _object(artifacts[filename], f"daily artifact {filename}")
        artifact_path = directory / filename
        if not artifact_path.is_file():
            raise FinalReportValidationError(f"daily artifact is missing: {artifact_path}")
        if metadata.get("bytes") != artifact_path.stat().st_size:
            raise FinalReportValidationError(f"daily artifact byte size mismatch: {artifact_path}")
        if metadata.get("sha256") != _sha256(artifact_path):
            raise FinalReportValidationError(f"daily artifact SHA-256 mismatch: {artifact_path}")
    return report, {
        "directory": directory.name,
        "report_json_sha256": _sha256(report_path),
        "bundle_manifest_sha256": _sha256(manifest_path),
    }


def _matching_reports(
    reports_directory: Path,
    experiment_id: UUID,
    report_date: date,
) -> list[tuple[Path, dict[str, object], dict[str, object]]]:
    matches: list[tuple[Path, dict[str, object], dict[str, object]]] = []
    if not reports_directory.is_dir():
        raise FinalReportValidationError(
            f"daily reports directory does not exist: {reports_directory}"
        )
    for directory in sorted(path for path in reports_directory.iterdir() if path.is_dir()):
        report_path = directory / "report.json"
        if not report_path.is_file():
            continue
        try:
            candidate = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FinalReportValidationError(
                f"daily report contains invalid JSON: {directory.name}"
            ) from exc
        if not isinstance(candidate, dict):
            continue
        experiment = candidate.get("experiment")
        if (
            candidate.get("report_date") != report_date.isoformat()
            or not isinstance(experiment, dict)
            or experiment.get("id") != str(experiment_id)
            or candidate.get("complete_day") is not True
        ):
            continue
        report, evidence = _verify_bundle(directory)
        matches.append((directory, report, evidence))
    return matches


def _verify_daily_report(
    report: dict[str, object],
    *,
    expected_date: date,
    experiment_id: UUID,
    generated_at: datetime,
) -> None:
    if (
        report.get("report_type") != "daily"
        or report.get("report_schema_version") != REPORT_SCHEMA_VERSION
        or report.get("report_date") != expected_date.isoformat()
        or report.get("complete_day") is not True
    ):
        raise FinalReportValidationError(
            f"daily report is not a complete supported day: {expected_date.isoformat()}"
        )
    safety = _object(report.get("safety"), "daily report safety")
    if (
        safety.get("paper_trading_only") is not True
        or safety.get("live_money") is not False
        or safety.get("authenticated_exchange_credentials_used") is not False
    ):
        raise FinalReportValidationError("daily report violates the paper-only safety boundary")
    experiment = _object(report.get("experiment"), "daily report experiment")
    if experiment.get("id") != str(experiment_id) or experiment.get("mode") != "paper_live":
        raise FinalReportValidationError("daily report experiment identity is invalid")
    _parse_utc(experiment.get("started_at"), "daily report experiment.started_at")
    window = _object(report.get("window"), "daily report window")
    if window.get("timezone") != "Europe/Berlin" or window.get("cutoff_utc") != window.get(
        "end_utc"
    ):
        raise FinalReportValidationError("daily report did not freeze the complete Berlin day")
    expected_window = berlin_daily_window(expected_date)
    expected_values = {
        "start_local": expected_window.start_local.isoformat(),
        "end_local": expected_window.end_local.isoformat(),
        "start_utc": expected_window.start_utc.isoformat().replace("+00:00", "Z"),
        "end_utc": expected_window.end_utc.isoformat().replace("+00:00", "Z"),
        "cutoff_utc": expected_window.end_utc.isoformat().replace("+00:00", "Z"),
    }
    if any(window.get(name) != value for name, value in expected_values.items()):
        raise FinalReportValidationError(
            f"daily report Berlin window mismatch: {expected_date.isoformat()}"
        )
    end_utc = _parse_utc(window.get("end_utc"), "daily report end_utc")
    generated = _parse_utc(report.get("generated_at"), "daily report generated_at")
    if generated < end_utc or generated_at < generated:
        raise FinalReportValidationError("daily report generation time is outside the final window")
    reconciliation = _object(report.get("reconciliation"), "daily report reconciliation")
    if reconciliation.get("ledger_ok") is not True or reconciliation.get("ledger_error_count") != 0:
        raise FinalReportValidationError("daily report ledger reconciliation did not pass")
    account = _object(report.get("account"), "daily report account")
    starting_equity = _decimal(account.get("starting_equity"), "account.starting_equity")
    ending_equity = _decimal(account.get("ending_equity"), "account.ending_equity")
    net_change = _decimal(account.get("net_change"), "account.net_change")
    if ending_equity - starting_equity != net_change:
        raise FinalReportValidationError(
            f"daily account net change mismatch: {expected_date.isoformat()}"
        )
    operator_actions = _object(report.get("operator_actions"), "daily operator actions")
    action_index = report.get("operator_action_index")
    if not isinstance(action_index, list):
        raise FinalReportValidationError("daily operator action index must be a list")
    event_count = _integer(operator_actions.get("events"), "operator_actions.events")
    if event_count != len(action_index):
        raise FinalReportValidationError("daily operator action index is incomplete")
    for key in ("requests", "rejections", "recoveries"):
        if _integer(operator_actions.get(key), f"operator_actions.{key}") > event_count:
            raise FinalReportValidationError(f"operator_actions.{key} exceeds event count")
    _object(operator_actions.get("by_event_type"), "operator_actions.by_event_type")
    _object(operator_actions.get("by_command_type"), "operator_actions.by_command_type")
    _object(operator_actions.get("by_status"), "operator_actions.by_status")


def verify_daily_report_bundle(
    directory: Path,
    *,
    expected_date: date,
    experiment_id: UUID,
    generated_at: datetime,
) -> dict[str, object]:
    """Verify one immutable complete-day bundle and expose soak-gate evidence."""
    report, bundle_evidence = _verify_bundle(directory)
    _verify_daily_report(
        report,
        expected_date=expected_date,
        experiment_id=experiment_id,
        generated_at=generated_at,
    )
    reconciliation = _object(report.get("reconciliation"), "daily report reconciliation")
    decisions = _object(report.get("decisions"), "daily report decisions")
    operator_actions = _object(report.get("operator_actions"), "daily operator actions")
    return {
        **bundle_evidence,
        "passed": True,
        "directory": str(directory.resolve()),
        "report_date": expected_date.isoformat(),
        "experiment_id": str(experiment_id),
        "report_id": report.get("report_id"),
        "complete_day": report.get("complete_day"),
        "ledger_ok": reconciliation.get("ledger_ok"),
        "ledger_error_count": reconciliation.get("ledger_error_count"),
        "authoritative_hash": reconciliation.get("authoritative_hash"),
        "report_hash": reconciliation.get("report_hash"),
        "decision_cycles": _integer(decisions.get("total"), "decisions.total"),
        "operator_action_events": _integer(
            operator_actions.get("events"), "operator_actions.events"
        ),
    }


def resolve_existing_daily_report_bundle(
    reports_directory: Path,
    *,
    expected_date: date,
    experiment_id: UUID,
    generated_at: datetime,
) -> dict[str, object] | None:
    """Return one verified complete bundle so an interrupted close can resume safely."""
    if not reports_directory.exists():
        return None
    matches = _matching_reports(reports_directory, experiment_id, expected_date)
    if len(matches) > 1:
        raise FinalReportValidationError(
            "expected at most one complete daily report for "
            f"{expected_date.isoformat()}, found {len(matches)}"
        )
    if not matches:
        return None

    directory, _report, _bundle_evidence = matches[0]
    evidence = verify_daily_report_bundle(
        directory,
        expected_date=expected_date,
        experiment_id=experiment_id,
        generated_at=generated_at,
    )
    return {
        "report_id": evidence["report_id"],
        "directory": str(directory),
        "json": str(directory / "report.json"),
        "markdown": str(directory / "report.md"),
        "decisions_csv": str(directory / "decisions.csv"),
        "decisions_parquet": str(directory / "decisions.parquet"),
        "execution_csv": str(directory / "execution.csv"),
        "execution_parquet": str(directory / "execution.parquet"),
        "bundle_manifest": str(directory / "bundle-manifest.json"),
        "resumed": True,
    }


def _same_identity(first: Mapping[str, object], candidate: Mapping[str, object]) -> bool:
    return all(first.get(name) == candidate.get(name) for name in _EXPERIMENT_IDENTITY)


def _sum_counts(reports: Sequence[dict[str, object]], section: str, key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for report in reports:
        values = _object(_object(report.get(section), section).get(key), f"{section}.{key}")
        for name, value in values.items():
            totals[name] = totals.get(name, 0) + _integer(value, f"{section}.{key}.{name}")
    return dict(sorted(totals.items()))


def _sum_integers(reports: Sequence[dict[str, object]], section: str, key: str) -> int:
    return sum(
        _integer(_object(report.get(section), section).get(key), f"{section}.{key}")
        for report in reports
    )


def _sum_decimals(reports: Sequence[dict[str, object]], section: str, key: str) -> str:
    return _decimal_text(
        sum(
            (
                _decimal(_object(report.get(section), section).get(key), f"{section}.{key}")
                for report in reports
            ),
            Decimal("0"),
        )
    )


def _maximum_decimal(reports: Sequence[dict[str, object]], section: str, key: str) -> str:
    return _decimal_text(
        max(
            _decimal(_object(report.get(section), section).get(key), f"{section}.{key}")
            for report in reports
        )
    )


def build_final_report_from_bundles(
    reports_directory: Path,
    *,
    experiment_id: UUID,
    start_date: date,
    days: int,
    generated_at: datetime,
) -> dict[str, object]:
    if days != 7:
        raise FinalReportValidationError("the official final report requires exactly seven days")
    offset = generated_at.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise FinalReportValidationError("final report generated_at must be UTC")

    reports: list[dict[str, object]] = []
    evidence_rows: list[dict[str, object]] = []
    experiment_identity: dict[str, object] | None = None
    for index in range(days):
        report_date = start_date.fromordinal(start_date.toordinal() + index)
        matches = _matching_reports(reports_directory, experiment_id, report_date)
        if len(matches) != 1:
            raise FinalReportValidationError(
                f"expected exactly one complete daily bundle for {report_date.isoformat()}, "
                f"found {len(matches)}"
            )
        directory, report, evidence = matches[0]
        _verify_daily_report(
            report,
            expected_date=report_date,
            experiment_id=experiment_id,
            generated_at=generated_at,
        )
        identity = _object(report.get("experiment"), "daily report experiment")
        if experiment_identity is None:
            experiment_identity = {name: identity.get(name) for name in _EXPERIMENT_IDENTITY}
        elif not _same_identity(experiment_identity, identity):
            raise FinalReportValidationError(
                f"daily report experiment identity changed on {report_date.isoformat()}"
            )
        reconciliation = _object(report.get("reconciliation"), "daily report reconciliation")
        evidence_rows.append(
            {
                "report_date": report_date.isoformat(),
                "report_id": report.get("report_id"),
                **evidence,
                "authoritative_hash": reconciliation.get("authoritative_hash"),
                "report_hash": reconciliation.get("report_hash"),
            }
        )
        reports.append(report)

    assert experiment_identity is not None
    experiment_started_at = _parse_utc(
        experiment_identity.get("started_at"),
        "daily report experiment.started_at",
    )
    first_window = berlin_daily_window(start_date)
    start_delay = experiment_started_at - first_window.start_utc
    if not timedelta(0) <= start_delay < timedelta(minutes=1):
        raise FinalReportValidationError(
            "official seven-day experiment did not start within the first Berlin minute"
        )
    for previous, current in zip(reports, reports[1:]):
        previous_account = _object(previous.get("account"), "account")
        current_account = _object(current.get("account"), "account")
        previous_end = _decimal(previous_account.get("ending_equity"), "ending_equity")
        current_start = _decimal(current_account.get("starting_equity"), "starting_equity")
        if previous_end != current_start:
            raise FinalReportValidationError(
                "daily account equity discontinuity: "
                f"{previous.get('report_date')} ended at {_decimal_text(previous_end)} but "
                f"{current.get('report_date')} started at {_decimal_text(current_start)}"
            )
    first_account = _object(reports[0].get("account"), "account")
    last_account = _object(reports[-1].get("account"), "account")
    starting_equity = _decimal(first_account.get("starting_equity"), "starting_equity")
    ending_equity = _decimal(last_account.get("ending_equity"), "ending_equity")
    account = {
        "starting_equity": _decimal_text(starting_equity),
        "ending_equity": _decimal_text(ending_equity),
        "net_change": _decimal_text(ending_equity - starting_equity),
        "realized_pnl": _sum_decimals(reports, "account", "realized_pnl"),
        "fees": _sum_decimals(reports, "account", "fees"),
        "funding": _sum_decimals(reports, "account", "funding"),
        "maximum_drawdown": _maximum_decimal(reports, "account", "maximum_drawdown"),
        "peak_exposure": _maximum_decimal(reports, "account", "peak_exposure"),
        "peak_risk_at_stop": _maximum_decimal(reports, "account", "peak_risk_at_stop"),
        "peak_used_margin": _maximum_decimal(reports, "account", "peak_used_margin"),
    }
    end_date = start_date.fromordinal(start_date.toordinal() + days - 1)
    report: dict[str, object] = {
        "report_type": "final",
        "report_schema_version": FINAL_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "safety": {
            "paper_trading_only": True,
            "live_money": False,
            "authenticated_exchange_credentials_used": False,
        },
        "experiment": experiment_identity,
        "period": {
            "timezone": "Europe/Berlin",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "calendar_days": days,
        },
        "account": account,
        "decisions": {
            "total": _sum_integers(reports, "decisions", "total"),
            "by_status": _sum_counts(reports, "decisions", "by_status"),
            "by_disposition": _sum_counts(reports, "decisions", "by_disposition"),
            "by_direction": _sum_counts(reports, "decisions", "by_direction"),
            "by_reason": _sum_counts(reports, "decisions", "by_reason"),
            "by_symbol": _sum_counts(reports, "decisions", "by_symbol"),
            "by_regime": _sum_counts(reports, "decisions", "by_regime"),
        },
        "agents": {
            "evaluations": _sum_integers(reports, "agents", "evaluations"),
            "by_name": _sum_counts(reports, "agents", "by_name"),
            "by_maturity": _sum_counts(reports, "agents", "by_maturity"),
            "by_direction": _sum_counts(reports, "agents", "by_direction"),
            "by_reason": _sum_counts(reports, "agents", "by_reason"),
            "incompatible": _sum_integers(reports, "agents", "incompatible"),
            "disabled": _sum_integers(reports, "agents", "disabled"),
        },
        "gates": {
            "evaluations": _sum_integers(reports, "gates", "evaluations"),
            "passed": _sum_integers(reports, "gates", "passed"),
            "failed": _sum_integers(reports, "gates", "failed"),
            "by_type": _sum_counts(reports, "gates", "by_type"),
            "failures_by_reason": _sum_counts(reports, "gates", "failures_by_reason"),
        },
        "data_quality": {
            "evaluations": _sum_integers(reports, "data_quality", "evaluations"),
            "by_status": _sum_counts(reports, "data_quality", "by_status"),
            "by_check": _sum_counts(reports, "data_quality", "by_check"),
            "by_reason": _sum_counts(reports, "data_quality", "by_reason"),
            "failed_required": _sum_integers(reports, "data_quality", "failed_required"),
        },
        "execution": {
            "proposals": _sum_integers(reports, "execution", "proposals"),
            "proposals_by_status": _sum_counts(reports, "execution", "proposals_by_status"),
            "orders_created": _sum_integers(reports, "execution", "orders_created"),
            "orders_by_status": _sum_counts(reports, "execution", "orders_by_status"),
            "order_events": _sum_integers(reports, "execution", "order_events"),
            "order_events_by_type": _sum_counts(reports, "execution", "order_events_by_type"),
            "fills": _sum_integers(reports, "execution", "fills"),
            "filled_quantity": _sum_decimals(reports, "execution", "filled_quantity"),
            "fees": _sum_decimals(reports, "execution", "fees"),
            "spread_cost": _sum_decimals(reports, "execution", "spread_cost"),
            "depth_slippage": _sum_decimals(reports, "execution", "depth_slippage"),
            "latency_slippage": _sum_decimals(reports, "execution", "latency_slippage"),
            "total_slippage": _sum_decimals(reports, "execution", "total_slippage"),
            "funding_entries": _sum_integers(reports, "execution", "funding_entries"),
            "funding_amount": _sum_decimals(reports, "execution", "funding_amount"),
        },
        "counterfactuals": {
            "created": _sum_integers(reports, "counterfactuals", "created"),
            "by_status": _sum_counts(reports, "counterfactuals", "by_status"),
            "by_rejection_gate": _sum_counts(reports, "counterfactuals", "by_rejection_gate"),
            "resolved_pnl": _sum_decimals(reports, "counterfactuals", "resolved_pnl"),
        },
        "operations": {
            "incidents_detected": _sum_integers(reports, "operations", "incidents_detected"),
            "incidents_by_severity": _sum_counts(reports, "operations", "incidents_by_severity"),
            "incidents_by_reason": _sum_counts(reports, "operations", "incidents_by_reason"),
            "operator_review_open_daily_sum": _sum_integers(
                reports, "operations", "operator_review_open"
            ),
            "data_quality_failed_required": _sum_integers(
                reports, "operations", "data_quality_failed_required"
            ),
            "recoveries_started": _sum_integers(reports, "operations", "recoveries_started"),
            "recoveries_by_status": _sum_counts(reports, "operations", "recoveries_by_status"),
            "worker_restarts": _sum_integers(reports, "operations", "worker_restarts"),
        },
        "operator_actions": {
            "events": _sum_integers(reports, "operator_actions", "events"),
            "requests": _sum_integers(reports, "operator_actions", "requests"),
            "rejections": _sum_integers(reports, "operator_actions", "rejections"),
            "recoveries": _sum_integers(reports, "operator_actions", "recoveries"),
            "by_event_type": _sum_counts(reports, "operator_actions", "by_event_type"),
            "by_command_type": _sum_counts(reports, "operator_actions", "by_command_type"),
            "by_status": _sum_counts(reports, "operator_actions", "by_status"),
        },
        "daily_reports": evidence_rows,
        "reconciliation": {
            "verified_daily_bundles": len(reports),
            "all_daily_ledgers_ok": True,
            "daily_evidence_chain_hash": content_hash(evidence_rows),
        },
    }
    report["report_id"] = content_hash(
        {
            "experiment": experiment_identity,
            "period": report["period"],
            "daily_evidence_chain_hash": cast(dict[str, object], report["reconciliation"])[
                "daily_evidence_chain_hash"
            ],
        }
    )
    normalized = to_json_data(report)
    if not isinstance(normalized, dict):
        raise TypeError("final report must normalize to a JSON object")
    return cast(dict[str, object], normalized)


def write_final_report_bundle(
    report: dict[str, object],
    output_directory: Path,
) -> FinalReportBundlePaths:
    if (
        report.get("report_type") != "final"
        or report.get("report_schema_version") != FINAL_REPORT_SCHEMA_VERSION
    ):
        raise FinalReportValidationError("unsupported final report")
    report_id = report.get("report_id")
    if (
        not isinstance(report_id, str)
        or len(report_id) != 64
        or any(character not in "0123456789abcdef" for character in report_id)
    ):
        raise FinalReportValidationError("final report_id must be a lowercase SHA-256 digest")
    experiment = _object(report.get("experiment"), "final report experiment")
    experiment_id = UUID(str(experiment.get("id")))
    if experiment_id.int == 0:
        raise FinalReportValidationError("final report experiment ID cannot be zero")
    period = _object(report.get("period"), "final report period")
    start_date = date.fromisoformat(str(period.get("start_date")))
    end_date = date.fromisoformat(str(period.get("end_date")))
    if period.get("calendar_days") != 7 or end_date.toordinal() - start_date.toordinal() != 6:
        raise FinalReportValidationError("final report period must contain seven contiguous days")
    daily_reports = report.get("daily_reports")
    if not isinstance(daily_reports, list) or len(daily_reports) != 7:
        raise FinalReportValidationError("final report requires seven daily evidence rows")

    bundle_name = (
        f"{start_date.isoformat()}-to-{end_date.isoformat()}-"
        f"{str(experiment_id)[:8]}-{report_id[:12]}"
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / bundle_name
    if target.exists():
        raise FileExistsError(f"final report bundle already exists: {target}")
    with tempfile.TemporaryDirectory(
        prefix=".maais-final-report-", dir=output_directory
    ) as temporary:
        temporary_path = Path(temporary)
        json_path = temporary_path / "report.json"
        markdown_path = temporary_path / "report.md"
        csv_path = temporary_path / "daily-reports.csv"
        manifest_path = temporary_path / "bundle-manifest.json"
        json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(render_final_report_markdown(report), encoding="utf-8")
        columns = (
            "report_date",
            "report_id",
            "directory",
            "report_json_sha256",
            "bundle_manifest_sha256",
            "authoritative_hash",
            "report_hash",
        )
        with csv_path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in daily_reports:
                if not isinstance(row, dict):
                    raise FinalReportValidationError("daily evidence rows must be objects")
                writer.writerow({column: row.get(column) for column in columns})
        artifacts = (json_path, markdown_path, csv_path)
        manifest_path.write_text(
            json.dumps(
                {
                    "report_id": report_id,
                    "report_schema_version": FINAL_REPORT_SCHEMA_VERSION,
                    "artifacts": {
                        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                        for path in artifacts
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(target)
    return FinalReportBundlePaths(
        directory=target,
        json_path=target / "report.json",
        markdown_path=target / "report.md",
        daily_reports_csv_path=target / "daily-reports.csv",
        manifest_path=target / "bundle-manifest.json",
    )


def _markdown_counts(values: object) -> str:
    if not isinstance(values, dict) or not values:
        return "_None_"
    return "\n".join(f"| {name} | {count} |" for name, count in sorted(values.items()))


def render_final_report_markdown(report: dict[str, object]) -> str:
    experiment = _object(report.get("experiment"), "final report experiment")
    period = _object(report.get("period"), "final report period")
    account = _object(report.get("account"), "final report account")
    decisions = _object(report.get("decisions"), "final report decisions")
    execution = _object(report.get("execution"), "final report execution")
    operations = _object(report.get("operations"), "final report operations")
    operator_actions = _object(report.get("operator_actions"), "final operator actions")
    reconciliation = _object(report.get("reconciliation"), "final report reconciliation")
    daily_reports = report.get("daily_reports")
    if not isinstance(daily_reports, list):
        raise FinalReportValidationError("daily_reports must be a list")
    daily_rows = "\n".join(
        "| {report_date} | `{report_id}` | `{report_json_sha256}` |".format(**row)
        for row in daily_reports
        if isinstance(row, dict)
    )
    return f"""# MAAIS Seven-Day Paper Report

> **PAPER TRADING / NO LIVE MONEY** — All execution and P&L in this report are simulated.

Experiment: `{experiment["id"]}`  
Candidate: `{experiment["name"]}`  
Manifest: `{experiment["manifest_hash"]}`  
Git commit: `{experiment["git_sha"]}`  
Berlin period: **{period["start_date"]} through {period["end_date"]}**  
Generated: `{report["generated_at"]}`

## Account result

| Metric | Value |
|---|---:|
| Starting equity | {account["starting_equity"]} |
| Ending equity | {account["ending_equity"]} |
| Net change | {account["net_change"]} |
| Realized P&L | {account["realized_pnl"]} |
| Fees | {account["fees"]} |
| Funding | {account["funding"]} |
| Maximum drawdown | {account["maximum_drawdown"]} |
| Peak exposure | {account["peak_exposure"]} |

## Decisions

Total cycles: **{decisions["total"]}**

| Disposition | Count |
|---|---:|
{_markdown_counts(decisions.get("by_disposition"))}

| Reason | Count |
|---|---:|
{_markdown_counts(decisions.get("by_reason"))}

## Paper execution

| Metric | Value |
|---|---:|
| Proposals | {execution["proposals"]} |
| Orders | {execution["orders_created"]} |
| Fills | {execution["fills"]} |
| Filled quantity | {execution["filled_quantity"]} |
| Execution fees | {execution["fees"]} |
| Total modeled slippage | {execution["total_slippage"]} |
| Funding entries | {execution["funding_entries"]} |

## Operations and reconciliation

| Metric | Value |
|---|---:|
| Incidents detected | {operations["incidents_detected"]} |
| Recoveries started | {operations["recoveries_started"]} |
| Worker starts/restarts | {operations["worker_restarts"]} |
| Required data-quality failures | {operations["data_quality_failed_required"]} |
| Operator lifecycle events | {operator_actions["events"]} |
| Operator requests | {operator_actions["requests"]} |
| Operator rejections | {operator_actions["rejections"]} |
| Operator crash recoveries | {operator_actions["recoveries"]} |
| Verified daily bundles | {reconciliation["verified_daily_bundles"]} |
| All daily ledgers passed | {reconciliation["all_daily_ledgers_ok"]} |
| Daily evidence chain | `{reconciliation["daily_evidence_chain_hash"]}` |

## Daily evidence

| Berlin date | Report ID | Report JSON SHA-256 |
|---|---|---|
{daily_rows}

## Interpretation boundary

This seven-day sample evaluates operational correctness, traceability, data quality, and
simulated execution behavior. It does not establish durable profitability or live-market
execution performance. Decisions remain attributable to their immutable daily bundles and
the authoritative PostgreSQL ledger.
"""
