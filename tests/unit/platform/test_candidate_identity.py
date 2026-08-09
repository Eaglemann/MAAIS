from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from maais.cli import main
from maais.config.constants import ALL_AGENTS
from maais.platform.candidate import (
    build_candidate_descriptor,
    build_dashboard_asset_manifest,
    write_candidate_descriptor,
)
from maais.platform.identity import CandidateDescriptor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "platform"


def _candidate(**overrides: object) -> CandidateDescriptor:
    values: dict[str, object] = {
        "git_sha": "a" * 40,
        "source_clean": True,
        "uv_lock_sha256": "b" * 64,
        "dashboard_lock_sha256": "c" * 64,
        "schema_revision": "0018",
        "agent_implementation_hashes": {
            name: f"{index + 1:064x}" for index, name in enumerate(ALL_AGENTS)
        },
        "dashboard_asset_manifest_sha256": "d" * 64,
        "build_definition_sha256": "e" * 64,
    }
    values.update(overrides)
    return CandidateDescriptor.build(**values)  # type: ignore[arg-type]


def test_candidate_descriptor_hash_is_canonical_and_covers_every_input() -> None:
    ordered = {name: f"{index + 1:064x}" for index, name in enumerate(ALL_AGENTS)}
    reversed_order = dict(reversed(tuple(ordered.items())))

    assert (
        _candidate(agent_implementation_hashes=ordered).descriptor_hash
        == _candidate(agent_implementation_hashes=reversed_order).descriptor_hash
    )

    changes = (
        {"git_sha": "f" + "a" * 39},
        {"uv_lock_sha256": "f" + "b" * 63},
        {"dashboard_lock_sha256": "f" + "c" * 63},
        {"schema_revision": "0019"},
        {"dashboard_asset_manifest_sha256": "f" + "d" * 63},
        {"build_definition_sha256": "f" + "e" * 63},
        {
            "agent_implementation_hashes": {
                **ordered,
                ALL_AGENTS[0]: "f" + ordered[ALL_AGENTS[0]][1:],
            }
        },
    )
    original = _candidate()
    assert all(
        _candidate(**change).descriptor_hash != original.descriptor_hash for change in changes
    )


def test_candidate_descriptor_is_strict_and_deeply_immutable() -> None:
    with pytest.raises(ValueError, match="clean source"):
        _candidate(source_clean=False)
    with pytest.raises(ValueError, match="git_sha"):
        _candidate(git_sha="A" * 40)
    with pytest.raises(ValueError, match="schema_revision"):
        _candidate(schema_revision="18")
    with pytest.raises(ValueError, match="exact agent"):
        _candidate(agent_implementation_hashes={ALL_AGENTS[0]: "1" * 64})

    descriptor = _candidate()
    with pytest.raises(TypeError):
        descriptor.agent_implementation_hashes[ALL_AGENTS[0]] = "0" * 64  # type: ignore[index]


def test_candidate_descriptor_round_trip_rejects_tampering_and_schema_drift(
    tmp_path: Path,
) -> None:
    descriptor = _candidate()
    path = tmp_path / "candidate.json"
    write_candidate_descriptor(descriptor, path)

    assert CandidateDescriptor.from_path(path) == descriptor
    assert stat.S_IMODE(path.stat().st_mode) == 0o644

    payload = json.loads(path.read_text())
    payload["git_sha"] = "f" * 40
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="descriptor_hash"):
        CandidateDescriptor.from_path(path)

    payload = descriptor.to_json_data()
    payload["unknown"] = "rejected"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="exact keys"):
        CandidateDescriptor.from_path(path)

    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="duplicate key"):
        CandidateDescriptor.from_path(path)

    external = tmp_path / "external.json"
    external.write_text("must remain untouched")
    symlink = tmp_path / "candidate-link.json"
    symlink.symlink_to(external)
    with pytest.raises(ValueError, match="symbolic link"):
        write_candidate_descriptor(descriptor, symlink)
    assert external.read_text() == "must remain untouched"

    del payload["unknown"]
    del payload["schema_revision"]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="exact keys"):
        CandidateDescriptor.from_path(path)


def test_dashboard_asset_manifest_is_content_addressed_and_rejects_bad_inventory(
    tmp_path: Path,
) -> None:
    dashboard = tmp_path / "dist"
    (dashboard / "assets").mkdir(parents=True)
    (dashboard / "assets" / "app.js").write_bytes(b"")
    (dashboard / "index.html").write_bytes(b"")

    expected = json.loads((FIXTURES / "dashboard-assets.json").read_text())
    manifest, manifest_hash = build_dashboard_asset_manifest(dashboard)
    assert manifest == expected
    assert len(manifest_hash) == 64

    (dashboard / "index.html").write_bytes(b"x")
    assert build_dashboard_asset_manifest(dashboard)[1] != manifest_hash

    with pytest.raises(ValueError, match="duplicate dashboard asset path"):
        build_dashboard_asset_manifest(
            dashboard,
            asset_paths=(dashboard / "index.html", dashboard / "index.html"),
        )
    with pytest.raises(ValueError, match="at least one regular file"):
        build_dashboard_asset_manifest(tmp_path / "empty")


def test_build_candidate_descriptor_derives_every_hash_from_repository_bytes(
    tmp_path: Path,
) -> None:
    root = _candidate_repository(tmp_path)
    dashboard = root / "dashboard" / "dist"

    first = build_candidate_descriptor(
        repository_root=root,
        dashboard_dist=dashboard,
        git_sha="a" * 40,
        source_clean=True,
    )
    assert first.schema_revision == "0018"
    assert set(first.agent_implementation_hashes) == set(ALL_AGENTS)

    mutations = (
        (root / "uv.lock", b"lock-v2", "uv_lock_sha256"),
        (
            root / "dashboard" / "package-lock.json",
            b"dashboard-lock-v2",
            "dashboard_lock_sha256",
        ),
        (root / "Dockerfile", b"FROM busybox\n", "build_definition_sha256"),
        (
            dashboard / "index.html",
            b"MAAIS v2",
            "dashboard_asset_manifest_sha256",
        ),
        (
            root / "maais" / "agents" / f"{ALL_AGENTS[0]}.py",
            b"NAME = 'changed'\n",
            "agent_implementation_hashes",
        ),
    )
    baseline = first.to_json_data()
    for path, changed_bytes, field in mutations:
        original_bytes = path.read_bytes()
        path.write_bytes(changed_bytes)
        changed = build_candidate_descriptor(
            repository_root=root,
            dashboard_dist=dashboard,
            git_sha="a" * 40,
            source_clean=True,
        )
        assert changed.to_json_data()[field] != baseline[field]
        assert changed.descriptor_hash != first.descriptor_hash
        path.write_bytes(original_bytes)

    with pytest.raises(ValueError, match="git_sha"):
        build_candidate_descriptor(
            repository_root=root,
            dashboard_dist=dashboard,
            git_sha="A" * 40,
            source_clean=True,
        )
    with pytest.raises(ValueError, match="clean source"):
        build_candidate_descriptor(
            repository_root=root,
            dashboard_dist=dashboard,
            git_sha="a" * 40,
            source_clean=False,
        )

    (root / "alembic" / "versions" / "0019_second_head.py").write_text(
        '"""second test head"""\nrevision = "0019"\ndown_revision = None\n'
    )
    with pytest.raises(ValueError, match="exactly one Alembic head"):
        build_candidate_descriptor(
            repository_root=root,
            dashboard_dist=dashboard,
            git_sha="a" * 40,
            source_clean=True,
        )


def test_candidate_descriptor_cli_writes_verified_public_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _candidate_repository(tmp_path)
    output = tmp_path / "image" / "candidate.json"

    assert (
        main(
            [
                "candidate-descriptor",
                "--repository",
                str(root),
                "--dashboard-dir",
                str(root / "dashboard" / "dist"),
                "--git-sha",
                "a" * 40,
                "--source-clean",
                "true",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    descriptor = CandidateDescriptor.from_path(output)
    emitted = json.loads(capsys.readouterr().out)

    assert emitted == {
        "descriptor_hash": descriptor.descriptor_hash,
        "path": str(output),
    }

    with pytest.raises(SystemExit):
        main(
            [
                "candidate-descriptor",
                "--repository",
                str(root),
                "--dashboard-dir",
                str(root / "dashboard" / "dist"),
                "--git-sha",
                "a" * 40,
                "--source-clean",
                "false",
                "--output",
                str(output),
            ]
        )


def _candidate_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "dashboard" / "dist").mkdir(parents=True)
    (root / "dashboard" / "dist" / "index.html").write_text("MAAIS")
    (root / "dashboard" / "package-lock.json").write_bytes(b"dashboard-lock")
    (root / "maais" / "agents").mkdir(parents=True)
    for name in ALL_AGENTS:
        (root / "maais" / "agents" / f"{name}.py").write_text(f"NAME = {name!r}\n")
    (root / "alembic" / "versions").mkdir(parents=True)
    (root / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (root / "alembic" / "versions" / "0018_head.py").write_text(
        '"""test head"""\nrevision = "0018"\ndown_revision = None\n'
    )
    (root / "uv.lock").write_bytes(b"lock-v1")
    (root / "Dockerfile").write_bytes(b"FROM scratch\n")
    return root
