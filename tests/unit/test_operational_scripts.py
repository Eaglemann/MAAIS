from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_paper_services_start_with_structured_production_logging() -> None:
    start_script = (REPOSITORY_ROOT / "scripts" / "start-paper-week.sh").read_text()

    assert (
        "exec env RUN_MODE=paper_live ENVIRONMENT=production "
        "uv run maais mission-control" in start_script
    )
    assert (
        "exec env RUN_MODE=paper_live ENVIRONMENT=production "
        "uv run maais paper-live" in start_script
    )
