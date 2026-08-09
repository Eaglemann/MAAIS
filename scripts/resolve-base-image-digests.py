#!/usr/bin/env python3
"""Resolve official Docker Hub tags to verified anonymous OCI index digests."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

REGISTRY = "registry-1.docker.io"
TOKEN_SERVICE = "https://auth.docker.io/token"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
TARGETS = (
    ("python", "library/python", "3.12-slim-bookworm"),
    ("node", "library/node", "22-bookworm-slim"),
)
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


class Response(Protocol):
    headers: Any

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> Response: ...

    def __exit__(self, *_args: object) -> None: ...


OpenUrl = Callable[..., Response]


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    image: str
    repository: str
    tag: str
    digest: str

    @property
    def reference(self) -> str:
        return f"{self.image}:{self.tag}@{self.digest}"


def resolve_image(
    *,
    image: str,
    repository: str,
    tag: str,
    open_url: OpenUrl = urllib.request.urlopen,
) -> ResolvedImage:
    _validate_target(image=image, repository=repository, tag=tag)
    token = _anonymous_pull_token(repository, open_url=open_url)
    request = urllib.request.Request(
        f"https://{REGISTRY}/v2/{repository}/manifests/{urllib.parse.quote(tag, safe='')}",
        headers={
            "Accept": ACCEPT,
            "Authorization": f"Bearer {token}",
            "User-Agent": "maais-base-digest-resolver/1",
        },
        method="GET",
    )
    body, headers = _read_bounded(request, open_url=open_url)
    observed = f"sha256:{hashlib.sha256(body).hexdigest()}"
    declared = headers.get("Docker-Content-Digest")
    if not isinstance(declared, str) or SHA256_DIGEST.fullmatch(declared) is None:
        raise RuntimeError("registry response omitted a valid content digest")
    if declared != observed:
        raise RuntimeError("registry content digest does not match manifest bytes")
    document = _json_object(body, "registry manifest")
    if document.get("schemaVersion") != 2 or not isinstance(document.get("manifests"), list):
        raise RuntimeError("base tag did not resolve to a multi-platform OCI index")
    platforms = {
        (platform.get("os"), platform.get("architecture"))
        for descriptor in document["manifests"]
        if isinstance(descriptor, dict)
        and isinstance((platform := descriptor.get("platform")), dict)
    }
    if ("linux", "amd64") not in platforms or ("linux", "arm64") not in platforms:
        raise RuntimeError("base image index lacks required Linux platforms")
    return ResolvedImage(
        image=image,
        repository=repository,
        tag=tag,
        digest=declared,
    )


def resolve_targets(*, open_url: OpenUrl = urllib.request.urlopen) -> tuple[ResolvedImage, ...]:
    return tuple(
        resolve_image(
            image=image,
            repository=repository,
            tag=tag,
            open_url=open_url,
        )
        for image, repository, tag in TARGETS
    )


def _anonymous_pull_token(repository: str, *, open_url: OpenUrl) -> str:
    query = urllib.parse.urlencode(
        {
            "service": "registry.docker.io",
            "scope": f"repository:{repository}:pull",
        }
    )
    request = urllib.request.Request(
        f"{TOKEN_SERVICE}?{query}",
        headers={"Accept": "application/json", "User-Agent": "maais-base-digest-resolver/1"},
        method="GET",
    )
    body, _headers = _read_bounded(request, open_url=open_url)
    document = _json_object(body, "anonymous registry token")
    token = document.get("token")
    if not isinstance(token, str) or BEARER_TOKEN.fullmatch(token) is None:
        raise RuntimeError("anonymous registry token is invalid")
    return token


def _read_bounded(
    request: urllib.request.Request,
    *,
    open_url: OpenUrl,
) -> tuple[bytes, Any]:
    with open_url(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        headers = response.headers
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("registry response exceeds the size limit")
    return body, headers


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _validate_target(*, image: str, repository: str, tag: str) -> None:
    component = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
    if component.fullmatch(image) is None:
        raise ValueError("image name is invalid")
    if not repository.startswith("library/") or any(
        component.fullmatch(part) is None for part in repository.split("/")
    ):
        raise ValueError("only official Docker Hub library repositories are supported")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag) is None:
        raise ValueError("image tag is invalid")


def main() -> int:
    for resolved in resolve_targets():
        print(resolved.reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
