from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, inspect
from sqlalchemy.ext.asyncio import AsyncConnection

from maais.db.connection import Base
from maais.db.models.platform import (
    PlatformCandidateModel,
    RunInstanceModel,
    ServiceInstanceModel,
)

pytestmark = pytest.mark.integration

PLATFORM_TABLES = (
    "service_instances",
    "run_instances",
    "platform_candidates",
)

EXPECTED_COLUMNS = {
    "platform_candidates": (
        "descriptor_hash",
        "git_sha",
        "schema_revision",
        "descriptor_json",
        "status",
        "creator_deployment_id",
        "registered_at",
        "qualifying_at",
        "qualified_at",
        "qualification_evidence_hash",
    ),
    "run_instances": (
        "id",
        "experiment_id",
        "candidate_hash",
        "manifest_hash",
        "database_system_identifier",
        "railway_environment_id",
        "purpose",
        "status",
        "requested_operator_command_id",
        "activating_worker_boot_id",
        "continuity_invalidated",
        "started_at",
        "invalidated_at",
        "invalidation_reason",
        "created_at",
    ),
    "service_instances": (
        "boot_id",
        "run_id",
        "project_id",
        "environment_id",
        "service_id",
        "deployment_id",
        "snapshot_id",
        "replica_id",
        "region",
        "service_role",
        "candidate_hash",
        "runtime_identity_json",
        "started_at",
        "first_seen_at",
        "last_heartbeat_at",
        "heartbeat_sequence",
        "stopped_at",
        "terminal_reason",
    ),
}

EXPECTED_CHECKS = {
    "platform_candidates": {
        "ck_platform_candidate_hashes",
        "ck_platform_candidate_git_sha",
        "ck_platform_candidate_schema_revision",
        "ck_platform_candidate_json",
        "ck_platform_candidate_identity_fields",
        "ck_platform_candidate_status",
        "ck_platform_candidate_lifecycle",
        "ck_platform_candidate_time_order",
    },
    "run_instances": {
        "ck_run_instance_hashes",
        "ck_run_instance_database_identity",
        "ck_run_instance_purpose",
        "ck_run_instance_status",
        "ck_run_instance_lifecycle",
        "ck_run_instance_time_order",
    },
    "service_instances": {
        "ck_service_instance_candidate_hash",
        "ck_service_instance_role",
        "ck_service_instance_identity_fields",
        "ck_service_instance_runtime_json",
        "ck_service_instance_heartbeat_sequence",
        "ck_service_instance_time_order",
        "ck_service_instance_terminal_state",
    },
}

EXPECTED_UNIQUES = {
    "platform_candidates": {},
    "run_instances": {
        "uq_run_instance_experiment": ("experiment_id",),
        "uq_run_instance_operator_command": ("requested_operator_command_id",),
        "uq_run_instance_activating_boot": ("activating_worker_boot_id",),
    },
    "service_instances": {
        "uq_service_instance_boot_run": ("boot_id", "run_id"),
    },
}

EXPECTED_INDEXES = {
    "platform_candidates": {
        "ix_platform_candidates_status_registered": (("status", "registered_at"), False, False),
    },
    "run_instances": {
        "ix_run_instances_environment_status": (
            ("railway_environment_id", "status", "created_at"),
            False,
            False,
        ),
        "uq_run_instances_active_environment": (
            ("railway_environment_id",),
            True,
            True,
        ),
    },
    "service_instances": {
        "ix_service_instances_run_role_heartbeat": (
            ("run_id", "service_role", "last_heartbeat_at"),
            False,
            False,
        ),
    },
}


def test_platform_model_contract_is_exact() -> None:
    assert {model.__table__.name for model in _MODELS} == set(PLATFORM_TABLES)
    for table_name in PLATFORM_TABLES:
        table = Base.metadata.tables[table_name]
        assert tuple(column.name for column in table.columns) == EXPECTED_COLUMNS[table_name]
        assert _constraint_names(table.constraints, CheckConstraint) == EXPECTED_CHECKS[table_name]
        assert _unique_contract(table.constraints) == EXPECTED_UNIQUES[table_name]
        assert _index_contract(table.indexes) == EXPECTED_INDEXES[table_name]
    assert _foreign_key_contract() == {
        "platform_candidates": {},
        "run_instances": {
            "fk_run_instance_activating_worker_boot": (
                ("activating_worker_boot_id", "id"),
                "service_instances",
                ("boot_id", "run_id"),
                "RESTRICT",
            ),
            "fk_run_instance_candidate": (
                ("candidate_hash",),
                "platform_candidates",
                ("descriptor_hash",),
                "RESTRICT",
            ),
            "fk_run_instance_experiment": (
                ("experiment_id",),
                "experiments",
                ("id",),
                "RESTRICT",
            ),
            "fk_run_instance_operator_command": (
                ("requested_operator_command_id",),
                "operator_commands",
                ("id",),
                "RESTRICT",
            ),
        },
        "service_instances": {
            "fk_service_instance_candidate": (
                ("candidate_hash",),
                "platform_candidates",
                ("descriptor_hash",),
                "RESTRICT",
            ),
            "fk_service_instance_run": (
                ("run_id",),
                "run_instances",
                ("id",),
                "RESTRICT",
            ),
        },
    }


async def test_platform_schema_matches_models(db_connection: AsyncConnection) -> None:
    def compare(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        assert set(PLATFORM_TABLES) <= set(inspector.get_table_names())
        for table_name in PLATFORM_TABLES:
            table = Base.metadata.tables[table_name]
            assert tuple(column["name"] for column in inspector.get_columns(table_name)) == tuple(
                column.name for column in table.columns
            )
            assert set(inspector.get_pk_constraint(table_name)["constrained_columns"]) == {
                column.name for column in table.primary_key.columns
            }
            assert _inspected_foreign_keys(inspector, table_name) == _table_foreign_keys(table)
            assert {
                item["name"]: tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table_name)
            } == EXPECTED_UNIQUES[table_name]
            assert {item["name"] for item in inspector.get_check_constraints(table_name)} == (
                EXPECTED_CHECKS[table_name]
            )
            assert _inspected_indexes(inspector, table_name) == EXPECTED_INDEXES[table_name]

    await db_connection.run_sync(compare)


_MODELS = (PlatformCandidateModel, RunInstanceModel, ServiceInstanceModel)


def _constraint_names(
    constraints: set[object],
    constraint_type: type[CheckConstraint],
) -> set[str]:
    return {
        str(constraint.name)
        for constraint in constraints
        if isinstance(constraint, constraint_type)
    }


def _unique_contract(constraints: set[object]) -> dict[str, tuple[str, ...]]:
    return {
        str(constraint.name): tuple(constraint.columns.keys())
        for constraint in constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _index_contract(indexes: set[Index]) -> dict[str, tuple[tuple[str, ...], bool, bool]]:
    return {
        str(index.name): (
            tuple(column.name for column in index.columns),
            bool(index.unique),
            index.dialect_options["postgresql"].get("where") is not None,
        )
        for index in indexes
    }


def _foreign_key_contract() -> dict[
    str, dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]
]:
    return {
        table_name: _table_foreign_keys(Base.metadata.tables[table_name])
        for table_name in PLATFORM_TABLES
    }


def _table_foreign_keys(
    table: object,
) -> dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    constraints = table.foreign_key_constraints  # type: ignore[attr-defined]
    return {
        str(constraint.name): (
            tuple(constraint.column_keys),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
            str(constraint.ondelete),
        )
        for constraint in constraints
    }


def _inspected_foreign_keys(
    inspector: object,
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    values = inspector.get_foreign_keys(table_name)  # type: ignore[attr-defined]
    return {
        str(item["name"]): (
            tuple(item["constrained_columns"]),
            str(item["referred_table"]),
            tuple(item["referred_columns"]),
            str(item["options"].get("ondelete")),
        )
        for item in values
    }


def _inspected_indexes(
    inspector: object,
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], bool, bool]]:
    values = inspector.get_indexes(table_name)  # type: ignore[attr-defined]
    return {
        str(item["name"]): (
            tuple(item["column_names"]),
            bool(item["unique"]),
            bool(item.get("dialect_options", {}).get("postgresql_where")),
        )
        for item in values
        if not item.get("duplicates_constraint")
    }
