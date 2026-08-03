"""Deterministic paper execution and official account projections.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    money = sa.Numeric(38, 18)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "order_intents",
        sa.Column("id", uuid, nullable=False),
        sa.Column("proposal_id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("position_effect", sa.String(16), nullable=False),
        sa.Column("order_type", sa.String(32), nullable=False),
        sa.Column("quantity", money, nullable=False),
        sa.Column("filled_quantity", money, nullable=False),
        sa.Column("limit_price", money),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("exchange_filter_snapshot_json", jsonb, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_quantity_positive"),
        sa.CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_order_filled_quantity",
        ),
        sa.CheckConstraint("created_at < expires_at", name="ck_order_time_order"),
        sa.CheckConstraint("version > 0", name="ck_order_version_positive"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_order_side"),
        sa.CheckConstraint(
            "order_type IN ('market', 'limit', 'stop_market')", name="ck_order_type"
        ),
        sa.CheckConstraint(
            "position_effect IN ('open', 'reduce')", name="ck_order_position_effect"
        ),
        sa.CheckConstraint(
            "status IN ('created', 'authorized', 'accepted', 'partially_filled', "
            "'filled', 'canceled', 'rejected', 'expired')",
            name="ck_order_status",
        ),
        sa.CheckConstraint(
            "(order_type = 'limit' AND limit_price IS NOT NULL) OR "
            "(order_type <> 'limit' AND limit_price IS NULL)",
            name="ck_order_limit_price",
        ),
        sa.CheckConstraint(
            "(position_effect = 'reduce' AND reduce_only) OR "
            "(position_effect = 'open' AND NOT reduce_only)",
            name="ck_order_reduce_only",
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["trade_proposals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "client_order_id", name="uq_order_client_identity"),
    )
    op.create_index(
        "ix_order_intents_experiment_status",
        "order_intents",
        ["experiment_id", "status", "created_at"],
    )
    op.create_table(
        "order_events",
        sa.Column("id", uuid, nullable=False),
        sa.Column("order_intent_id", uuid, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_frame_id", uuid),
        sa.Column("payload_json", jsonb, nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_order_event_sequence_positive"),
        sa.ForeignKeyConstraint(["order_intent_id"], ["order_intents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["market_frame_id"], ["market_frames.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_intent_id", "sequence", name="uq_order_event_sequence"),
    )
    op.create_table(
        "fills",
        sa.Column("id", uuid, nullable=False),
        sa.Column("order_intent_id", uuid, nullable=False),
        sa.Column("market_event_id", sa.String(128), nullable=False),
        sa.Column("fill_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", money, nullable=False),
        sa.Column("price", money, nullable=False),
        sa.Column("liquidity_role", sa.String(16), nullable=False),
        sa.Column("fee", money, nullable=False),
        sa.Column("fee_asset", sa.String(16), nullable=False),
        sa.Column("spread_cost", money, nullable=False),
        sa.Column("depth_slippage", money, nullable=False),
        sa.Column("latency_slippage", money, nullable=False),
        sa.Column("total_slippage", money, nullable=False),
        sa.Column("market_snapshot_json", jsonb, nullable=False),
        sa.CheckConstraint("quantity > 0 AND price > 0", name="ck_fill_positive"),
        sa.CheckConstraint("fee >= 0", name="ck_fill_fee_nonnegative"),
        sa.CheckConstraint("liquidity_role IN ('maker', 'taker')", name="ck_fill_liquidity_role"),
        sa.ForeignKeyConstraint(["order_intent_id"], ["order_intents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_intent_id", "market_event_id", name="uq_fill_order_market_event"
        ),
    )
    op.create_index("ix_fills_order_time", "fills", ["order_intent_id", "fill_at"])
    op.create_table(
        "positions",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("quantity", money, nullable=False),
        sa.Column("average_entry", money, nullable=False),
        sa.Column("mark_price", money, nullable=False),
        sa.Column("initial_margin", money, nullable=False),
        sa.Column("maintenance_margin", money, nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("unrealized_pnl", money, nullable=False),
        sa.Column("realized_pnl", money, nullable=False),
        sa.Column("fees", money, nullable=False),
        sa.Column("funding", money, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_position_quantity_nonnegative"),
        sa.CheckConstraint("leverage BETWEEN 1 AND 5", name="ck_position_leverage"),
        sa.CheckConstraint("side IN ('long', 'short', 'neutral')", name="ck_position_side"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_position_status"),
        sa.CheckConstraint(
            "(status = 'open' AND quantity > 0 AND side <> 'neutral' AND closed_at IS NULL) OR "
            "(status = 'closed' AND quantity = 0 AND side = 'neutral' AND closed_at IS NOT NULL)",
            name="ck_position_open_closed_state",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_position_one_open_symbol",
        "positions",
        ["experiment_id", "symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "position_lots",
        sa.Column("id", uuid, nullable=False),
        sa.Column("position_id", uuid, nullable=False),
        sa.Column("opening_fill_id", uuid, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", money, nullable=False),
        sa.Column("original_quantity", money, nullable=False),
        sa.Column("remaining_quantity", money, nullable=False),
        sa.Column("opening_fee", money, nullable=False),
        sa.Column("remaining_opening_fee", money, nullable=False),
        sa.Column("funding", money, nullable=False),
        sa.CheckConstraint(
            "original_quantity > 0 AND remaining_quantity >= 0 AND "
            "remaining_quantity <= original_quantity",
            name="ck_lot_quantities",
        ),
        sa.CheckConstraint(
            "opening_fee >= 0 AND remaining_opening_fee >= 0 AND "
            "remaining_opening_fee <= opening_fee",
            name="ck_lot_fees",
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["opening_fill_id"], ["fills.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opening_fill_id", name="uq_lot_opening_fill"),
    )
    op.create_table(
        "exit_plans",
        sa.Column("id", uuid, nullable=False),
        sa.Column("position_id", uuid, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("quantity", money, nullable=False),
        sa.Column("average_entry", money, nullable=False),
        sa.Column("expected_loss_fraction", money, nullable=False),
        sa.Column("expected_gain_fraction", money, nullable=False),
        sa.Column("stop_price", money, nullable=False),
        sa.Column("target_price", money, nullable=False),
        sa.Column("maximum_bars", sa.Integer(), nullable=False),
        sa.Column("bars_elapsed", sa.Integer(), nullable=False),
        sa.Column("opposite_signal_streak", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_exit_quantity_positive"),
        sa.CheckConstraint("maximum_bars > 0", name="ck_exit_maximum_bars"),
        sa.CheckConstraint("bars_elapsed >= 0", name="ck_exit_bars_elapsed"),
        sa.CheckConstraint("opposite_signal_streak >= 0", name="ck_exit_opposite_streak"),
        sa.CheckConstraint(
            "status IN ('active', 'triggered', 'superseded', 'closed')",
            name="ck_exit_status",
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_exit_one_active_position",
        "exit_plans",
        ["position_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "account_snapshots",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash_balance", money, nullable=False),
        sa.Column("equity", money, nullable=False),
        sa.Column("used_margin", money, nullable=False),
        sa.Column("free_margin", money, nullable=False),
        sa.Column("gross_notional", money, nullable=False),
        sa.Column("risk_at_stop", money, nullable=False),
        sa.Column("unrealized_pnl", money, nullable=False),
        sa.Column("realized_pnl", money, nullable=False),
        sa.Column("fees", money, nullable=False),
        sa.Column("funding", money, nullable=False),
        sa.Column("peak_equity", money, nullable=False),
        sa.Column("drawdown", money, nullable=False),
        sa.CheckConstraint("account_version >= 0", name="ck_account_version_nonnegative"),
        sa.CheckConstraint("drawdown >= 0", name="ck_account_drawdown_nonnegative"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "account_version", name="uq_account_snapshot_version"),
    )
    op.create_index(
        "ix_account_snapshots_experiment_time",
        "account_snapshots",
        ["experiment_id", "snapshot_at"],
    )
    op.create_table(
        "funding_entries",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("position_id", uuid, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate", money, nullable=False),
        sa.Column("notional", money, nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.Column("market_event_id", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "market_event_id", name="uq_funding_market_event"),
    )
    op.create_table(
        "execution_sensitivities",
        sa.Column("id", uuid, nullable=False),
        sa.Column("order_intent_id", uuid, nullable=False),
        sa.Column("scenario", sa.String(16), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome_json", jsonb, nullable=False),
        sa.CheckConstraint(
            "scenario IN ('optimistic', 'conservative', 'stress')",
            name="ck_sensitivity_scenario",
        ),
        sa.ForeignKeyConstraint(["order_intent_id"], ["order_intents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_intent_id", "scenario", name="uq_sensitivity_order_scenario"),
    )


def downgrade() -> None:
    op.drop_table("execution_sensitivities")
    op.drop_table("funding_entries")
    op.drop_index("ix_account_snapshots_experiment_time", table_name="account_snapshots")
    op.drop_table("account_snapshots")
    op.drop_index("uq_exit_one_active_position", table_name="exit_plans")
    op.drop_table("exit_plans")
    op.drop_table("position_lots")
    op.drop_index("uq_position_one_open_symbol", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_fills_order_time", table_name="fills")
    op.drop_table("fills")
    op.drop_table("order_events")
    op.drop_index("ix_order_intents_experiment_status", table_name="order_intents")
    op.drop_table("order_intents")
