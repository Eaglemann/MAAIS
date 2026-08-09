from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import urllib.request
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
RESOLVER = ROOT / "scripts" / "resolve-base-image-digests.py"
SHA256_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def _instructions() -> tuple[str, ...]:
    raw = DOCKERFILE.read_text(encoding="utf-8")
    instructions: list[str] = []
    current = ""
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        instructions.append(current)
        current = ""
    if current:
        instructions.append(current)
    return tuple(instructions)


def test_every_base_image_is_tagged_digest_pinned_and_reused_for_runtime() -> None:
    instructions = _instructions()
    from_lines = tuple(line for line in instructions if line.upper().startswith("FROM "))

    assert len(from_lines) == 3
    references: dict[str, str] = {}
    for line in from_lines:
        match = re.fullmatch(r"FROM\s+(\S+)\s+AS\s+([a-z0-9-]+)", line, re.IGNORECASE)
        assert match is not None, line
        reference, stage = match.groups()
        assert SHA256_REFERENCE.fullmatch(reference), reference
        assert ":" in reference.split("@", 1)[0], reference
        assert ":latest" not in reference
        references[stage.lower()] = reference

    assert set(references) == {"dashboard-build", "python-build", "runtime"}
    assert references["python-build"] == references["runtime"]
    assert references["dashboard-build"].startswith("node:22-bookworm-slim@sha256:")
    assert references["runtime"].startswith("python:3.12-slim-bookworm@sha256:")


def test_locked_builds_generate_the_candidate_from_exact_source_inputs() -> None:
    instructions = _instructions()
    joined = "\n".join(instructions)

    assert any(re.search(r"\bRUN\s+npm ci(?:\s|$)", line) for line in instructions)
    assert 'RUN python -m pip install --no-cache-dir "uv==0.11.16"' in instructions
    sync_lines = tuple(line for line in instructions if "uv sync" in line)
    assert sync_lines
    assert all("--locked" in line and "--no-dev" in line for line in sync_lines)
    assert all("--no-editable" in line for line in sync_lines)
    assert "ARG RAILWAY_GIT_COMMIT_SHA" in instructions
    assert "ARG MAAIS_SOURCE_CLEAN" in instructions
    assert 'test "$MAAIS_SOURCE_CLEAN" = "true"' in joined
    assert "^[0-9a-f]{40}$" in joined
    assert "uv run maais candidate-descriptor" in joined
    assert '--git-sha "$RAILWAY_GIT_COMMIT_SHA"' in joined
    assert '--source-clean "$MAAIS_SOURCE_CLEAN"' in joined
    assert "--output /build/candidate.json" in joined
    assert "COPY --from=dashboard-build /src/dashboard/dist /src/dashboard/dist" in joined


def test_final_stage_is_fixed_non_root_minimal_and_paper_only() -> None:
    instructions = _instructions()
    final_stage = instructions[
        max(i for i, line in enumerate(instructions) if line.startswith("FROM ")) :
    ]
    joined = "\n".join(final_stage)

    user_lines = [line for line in final_stage if line.startswith("USER ")]
    assert user_lines == ["USER 10001:10001"]
    entrypoint_lines = [line for line in final_stage if line.startswith("ENTRYPOINT ")]
    assert len(entrypoint_lines) == 1
    assert json.loads(entrypoint_lines[0].removeprefix("ENTRYPOINT ")) == [
        "/opt/maais/.venv/bin/maais"
    ]
    assert not any(line.startswith("CMD ") for line in final_stage)
    assert "/app/candidate.json" in joined
    assert "/app/dashboard" in joined
    assert "chmod -R a-w /app /opt/maais" in joined
    assert 'org.opencontainers.image.revision="$RAILWAY_GIT_COMMIT_SHA"' in joined
    assert 'io.maais.candidate.schema="1"' in joined
    assert 'io.maais.safety.paper-only="true"' in joined
    assert "apt-get install" not in joined
    assert "apk add" not in joined
    assert "python -m pip install" not in joined
    assert "uv sync" not in joined
    assert "npm ci" not in joined
    assert "rm -rf /usr/local/lib/python3.12/ensurepip" in joined
    assert "find /usr/local/lib/python3.12 /opt/maais /app" in joined
    assert "-name __pycache__ -o -name test -o -name tests" in joined
    assert "-prune -exec rm -rf '{}' +" in joined
    assert "-type f \\( -name '*.pyc' -o -name '*.pyo' \\) -delete" in joined
    assert "SENTRY_AUTH_TOKEN" not in joined
    assert "DATABASE_URL" not in joined
    assert "BINANCE_DEMO_API" not in joined


def test_build_context_is_deny_first_and_never_reincludes_forbidden_state() -> None:
    lines = tuple(
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert lines[0] == "**"
    allow = tuple(line[1:] for line in lines if line.startswith("!"))
    assert "Dockerfile" in allow
    assert "pyproject.toml" in allow
    assert "uv.lock" in allow
    assert "maais/**/*.py" in allow
    assert "alembic/**/*.py" in allow
    assert "dashboard/src/**/*.tsx" in allow
    forbidden_fragments = (
        ".git",
        ".env",
        ".venv",
        "node_modules",
        "dashboard/dist",
        "dist-sourcemaps",
        "tests",
        "artifacts",
        "backups",
        "data",
        ".map",
        "__pycache__",
        ".pyc",
    )
    for pattern in allow:
        assert not any(fragment in pattern for fragment in forbidden_fragments), pattern
    assert "!maais/**" not in lines
    assert "!dashboard/**" not in lines


def test_build_context_re_excludes_frontend_test_sources() -> None:
    lines = tuple(
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    source_allow_index = max(
        index
        for index, line in enumerate(lines)
        if line in {"!dashboard/src/**/*.ts", "!dashboard/src/**/*.tsx"}
    )
    required_denials = {
        "dashboard/src/**/__tests__/",
        "dashboard/src/**/__tests__/**",
        "dashboard/src/**/*.spec.ts",
        "dashboard/src/**/*.spec.tsx",
        "dashboard/src/**/*.test.ts",
        "dashboard/src/**/*.test.tsx",
    }

    assert required_denials.issubset(lines)
    assert all(lines.index(pattern) > source_allow_index for pattern in required_denials)


def test_contract_starts_red_when_packaging_files_are_absent() -> None:
    assert DOCKERFILE.is_file()
    assert DOCKERIGNORE.is_file()


def test_digest_resolver_uses_only_anonymous_registry_requests_and_verifies_bytes() -> None:
    module = _resolver_module()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {"platform": {"os": "linux", "architecture": "amd64"}},
                {"platform": {"os": "linux", "architecture": "arm64"}},
            ],
        },
        separators=(",", ":"),
    ).encode()
    digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
    requests: list[urllib.request.Request] = []

    class FakeResponse:
        def __init__(self, body: bytes, headers: Message | None = None) -> None:
            self.body = body
            self.headers = headers or Message()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, amount: int = -1) -> bytes:
            return self.body[:amount] if amount >= 0 else self.body

    def open_url(request: urllib.request.Request, *, timeout: float):
        requests.append(request)
        assert timeout == 20.0
        if len(requests) == 1:
            return FakeResponse(b'{"token":"anonymous-token"}')
        headers = Message()
        headers["Docker-Content-Digest"] = digest
        return FakeResponse(manifest, headers)

    resolved = module.resolve_image(
        image="python",
        repository="library/python",
        tag="3.12-slim-bookworm",
        open_url=open_url,
    )

    assert resolved.reference == f"python:3.12-slim-bookworm@{digest}"
    assert len(requests) == 2
    assert "auth.docker.io/token" in requests[0].full_url
    assert requests[0].get_header("Authorization") is None
    assert requests[1].get_header("Authorization") == "Bearer anonymous-token"
    source = RESOLVER.read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "keychain" not in source.lower()
    assert "DOCKER_CONFIG" not in source


def _resolver_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("maais_base_digest_resolver", RESOLVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", (DOCKERFILE, DOCKERIGNORE))
def test_packaging_files_are_utf8_and_end_with_one_newline(path: Path) -> None:
    raw = path.read_bytes()
    assert raw.decode("utf-8").encode("utf-8") == raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
