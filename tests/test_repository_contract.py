from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_generated_runtime_state_is_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text()
    for entry in (".superpowers/", ".playwright-cli/", "artifacts/", "backups/", "data/"):
        assert entry in ignored


def test_readme_does_not_claim_paper_readiness() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    assert "not yet ready for the seven-day paper experiment" in readme
