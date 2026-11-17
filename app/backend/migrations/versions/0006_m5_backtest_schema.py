"""m5 backtest history schema

Revision ID: 0006
Revises: 0005

Mirror of the QuantDinger backtest history (runs / equity / trades / orders / fills /
account points).  Historical results are imported, never re-run.  QD timestamps are
``timestamp without time zone``; they are kept naive rather than re-labelled.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "backtest_run",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("indicator_id", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column("strategy_name", sa.String(length=255), nullable=True),
        sa.Column("run_type", sa.String(length=64), nullable=True),
        sa.Column("market", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("timeframe", sa.String(length=32), nullable=True),
        sa.Column("start_date", sa.String(length=32), nullable=True),
        sa.Column("end_date", sa.String(length=32), nullable=True),
        sa.Column("initial_capital", sa.Numeric(), nullable=True),
        sa.Column("commission", sa.Numeric(), nullable=True),
        sa.Column("slippage", sa.Numeric(), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=True),
        sa.Column("trade_direction", sa.String(length=32), nullable=True),
        _jsonb("strategy_config"),
        _jsonb("config_snapshot"),
        sa.Column("engine_version", sa.String(length=64), nullable=True),
        sa.Column("code_hash", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        _jsonb("result_json"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_backtest_run")),
        schema="research",
    )
    op.create_index(op.f("ix_backtest_run_status"), "backtest_run", ["status"], schema="research")

    op.create_table(
        "backtest_equity",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("point_index", sa.Integer(), nullable=False),
        sa.Column("point_time", sa.String(length=64), nullable=True),
        sa.Column("point_value", sa.Double(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "point_index", name=op.f("pk_backtest_equity")),
        schema="research",
    )
    op.create_table(
        "backtest_trade",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("trade_index", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column("trade_time", sa.String(length=64), nullable=True),
        sa.Column("trade_type", sa.String(length=32), nullable=True),
        sa.Column("side", sa.String(length=16), nullable=True),
        sa.Column("price", sa.Double(), nullable=True),
        sa.Column("amount", sa.Double(), nullable=True),
        sa.Column("profit", sa.Double(), nullable=True),
        sa.Column("balance", sa.Double(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        _jsonb("payload_json"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "trade_index", name=op.f("pk_backtest_trade")),
        schema="research",
    )
    op.create_table(
        "backtest_order",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("order_seq", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("signal_index", sa.Integer(), nullable=True),
        sa.Column("signal_time", sa.String(length=64), nullable=True),
        sa.Column("active_index", sa.Integer(), nullable=True),
        sa.Column("active_time", sa.String(length=64), nullable=True),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("side", sa.String(length=16), nullable=True),
        sa.Column("order_type", sa.String(length=32), nullable=True),
        sa.Column("time_in_force", sa.String(length=32), nullable=True),
        sa.Column("requested_qty", sa.Text(), nullable=True),
        sa.Column("allocation_pct", sa.Text(), nullable=True),
        sa.Column("lot_size", sa.Text(), nullable=True),
        sa.Column("limit_price", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("terminal_index", sa.Integer(), nullable=True),
        sa.Column("terminal_time", sa.String(length=64), nullable=True),
        sa.Column("terminal_code", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        _jsonb("payload_json"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "order_seq", name=op.f("pk_backtest_order")),
        schema="research",
    )
    op.create_table(
        "backtest_fill",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("fill_seq", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("fill_id", sa.String(length=128), nullable=True),
        sa.Column("order_seq", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("bar_index", sa.Integer(), nullable=True),
        sa.Column("fill_time", sa.String(length=64), nullable=True),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("side", sa.String(length=16), nullable=True),
        sa.Column("qty", sa.Text(), nullable=True),
        sa.Column("price", sa.Text(), nullable=True),
        sa.Column("notional", sa.Text(), nullable=True),
        sa.Column("commission", sa.Text(), nullable=True),
        sa.Column("realized_pnl", sa.Text(), nullable=True),
        sa.Column("cash_after", sa.Text(), nullable=True),
        sa.Column("equity_after", sa.Text(), nullable=True),
        _jsonb("payload_json"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "fill_seq", name=op.f("pk_backtest_fill")),
        schema="research",
    )
    op.create_table(
        "backtest_account_point",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("point_seq", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("bar_index", sa.Integer(), nullable=True),
        sa.Column("point_time", sa.String(length=64), nullable=True),
        sa.Column("mark_price", sa.Text(), nullable=True),
        sa.Column("cash", sa.Text(), nullable=True),
        sa.Column("equity", sa.Text(), nullable=True),
        sa.Column("position_qty", sa.Text(), nullable=True),
        sa.Column("average_entry", sa.Text(), nullable=True),
        sa.Column("opened_bar_index", sa.Integer(), nullable=True),
        sa.Column("realized_pnl", sa.Text(), nullable=True),
        sa.Column("unrealized_pnl", sa.Text(), nullable=True),
        _jsonb("payload_json"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "point_seq", name=op.f("pk_backtest_account_point")),
        schema="research",
    )


def downgrade() -> None:
    for table in (
        "backtest_account_point",
        "backtest_fill",
        "backtest_order",
        "backtest_trade",
        "backtest_equity",
        "backtest_run",
    ):
        op.drop_table(table, schema="research")
