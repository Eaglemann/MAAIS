from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from maais.operations.database_identity import collect_database_identity

pytestmark = pytest.mark.integration


async def test_database_identity_reports_the_connected_postgresql_cluster(
    db_engine: AsyncEngine,
) -> None:
    identity = await collect_database_identity(db_engine)

    assert identity["database"].endswith("_test")
    assert identity["system_identifier"].isdigit()
    assert int(identity["system_identifier"]) > 0
    assert identity["server_port"] == 5432
