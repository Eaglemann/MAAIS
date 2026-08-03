from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_generated_runtime_state_is_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text()
    for entry in (".superpowers/", ".playwright-cli/", "artifacts/", "backups/", "data/"):
        assert entry in ignored


def test_readme_does_not_claim_paper_readiness() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    assert "not yet ready for the seven-day paper experiment" in readme


def test_ci_enforces_critical_coverage_and_a_real_browser_with_one_postgres_service() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert workflow.count("image: postgres:16-alpine") == 1
    assert "--cov-branch" in workflow
    assert "--cov-fail-under=80" in workflow
    assert "install-browser chrome-for-testing" in workflow
    assert "scripts/browser-smoke.sh" in workflow
