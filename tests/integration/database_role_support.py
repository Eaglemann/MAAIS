from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from maais.db.roles import DatabaseRolePasswords


def integration_role_passwords() -> DatabaseRolePasswords:
    return DatabaseRolePasswords(
        migrator="migrator-integration-password",  # pragma: allowlist secret
        worker="worker-integration-password",  # pragma: allowlist secret
        web="web-integration-password",  # pragma: allowlist secret
        operations="operations-integration-password",  # pragma: allowlist secret
        verifier="verifier-integration-password",  # pragma: allowlist secret
    )


async def cleanup_database_roles(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "DROP FUNCTION IF EXISTS public.maais_heartbeat_service_instance("
                "uuid, integer, timestamp with time zone)"
            )
        )
        await connection.execute(
            text(
                "DROP FUNCTION IF EXISTS public.maais_register_service_instance("
                "uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, "
                "timestamp with time zone, timestamp with time zone)"
            )
        )
        await connection.execute(
            text(
                "DROP FUNCTION IF EXISTS public.maais_enqueue_operator_command("
                "uuid, uuid, text, text, text, text, jsonb, boolean, timestamp with time zone)"
            )
        )
        existing = set(
            await connection.scalars(
                text(
                    "SELECT rolname FROM pg_roles WHERE rolname IN "
                    "('maais_migrator','maais_worker','maais_web','maais_ops','maais_verifier')"
                )
            )
        )
        if "maais_migrator" in existing:
            await connection.execute(text("REASSIGN OWNED BY maais_migrator TO maais"))
        for role_name in (
            "maais_worker",
            "maais_web",
            "maais_ops",
            "maais_verifier",
            "maais_migrator",
        ):
            if role_name in existing:
                await connection.execute(text(f"DROP OWNED BY {role_name}"))
                await connection.execute(text(f"DROP ROLE {role_name}"))
        await connection.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
        await connection.execute(
            text(
                "DO $restore_connect$ BEGIN EXECUTE format("
                "'GRANT CONNECT ON DATABASE %I TO PUBLIC', current_database()); "
                "END $restore_connect$"
            )
        )
