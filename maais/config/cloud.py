"""Typed, non-secret deployment identity for local and Railway runtimes."""

from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping

from pydantic import BaseModel, ConfigDict


class DeploymentTarget(StrEnum):
    LOCAL = "local"
    RAILWAY = "railway"


class ServiceRole(StrEnum):
    WEB = "web"
    WORKER = "worker"
    OPERATIONS = "operations"
    VERIFIER = "verifier"
    MIGRATOR = "migrator"


DATABASE_ROLE_BY_SERVICE: Final[Mapping[ServiceRole, str]] = MappingProxyType(
    {
        ServiceRole.WEB: "maais_web",
        ServiceRole.WORKER: "maais_worker",
        ServiceRole.OPERATIONS: "maais_ops",
        ServiceRole.VERIFIER: "maais_verifier",
        ServiceRole.MIGRATOR: "maais_migrator",
    }
)


class CloudSettings(BaseModel):
    """Frozen view of deployment identity after the root settings model validates it."""

    model_config = ConfigDict(frozen=True)

    deployment_target: DeploymentTarget = DeploymentTarget.LOCAL
    service_role: ServiceRole | None = None
    railway_project_id: str = ""
    railway_environment_id: str = ""
    railway_service_id: str = ""
    railway_deployment_id: str = ""
    railway_snapshot_id: str | None = None
    railway_replica_id: str = ""
    railway_region: str = ""
    candidate_descriptor_path: Path = Path("/app/candidate.json")
    expected_schema_revision: str = ""
    database_role_name: str = ""
