"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-04 06:09:33.306765
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('monthly_limit', sa.Integer(), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('alert_thresholds', sa.String(length=64), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('goals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('target_amount', sa.Integer(), nullable=False),
    sa.Column('target_date', sa.Date(), nullable=True),
    sa.Column('progress_source', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('import_mappings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution', sa.String(length=128), nullable=False),
    sa.Column('column_mapping', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution')
    )
    op.create_table('provider_connections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('institution_name', sa.String(length=128), nullable=True),
    sa.Column('encrypted_credentials', sa.Text(), nullable=False),
    sa.Column('sync_cursor', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('status_detail', sa.Text(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('provider_connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_provider_connections_external_id'), ['external_id'], unique=False)

    op.create_table('accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('institution', sa.String(length=128), nullable=True),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('subtype', sa.String(length=64), nullable=True),
    sa.Column('mask', sa.String(length=8), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('connection_id', sa.Integer(), nullable=True),
    sa.Column('current_balance', sa.Integer(), nullable=True),
    sa.Column('available_balance', sa.Integer(), nullable=True),
    sa.Column('balance_updated_at', sa.DateTime(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('interest_rate', sa.Numeric(precision=7, scale=3), nullable=True),
    sa.Column('min_payment', sa.Integer(), nullable=True),
    sa.Column('next_payment_due', sa.Date(), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['connection_id'], ['provider_connections.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_accounts_external_id'), ['external_id'], unique=False)

    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.Column('month', sa.String(length=7), nullable=False),
    sa.Column('threshold', sa.Integer(), nullable=False),
    sa.Column('spent', sa.Integer(), nullable=False),
    sa.Column('limit', sa.Integer(), nullable=False),
    sa.Column('fired_at', sa.DateTime(), nullable=False),
    sa.Column('dismissed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('category_id', 'month', 'threshold')
    )
    op.create_table('goal_contributions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('goal_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('note', sa.String(length=256), nullable=True),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('goal_contributions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_goal_contributions_goal_id'), ['goal_id'], unique=False)

    op.create_table('payee_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('match_type', sa.String(length=16), nullable=False),
    sa.Column('match_value', sa.String(length=256), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('mark_transfer', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('balance_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('balance', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'date')
    )
    with op.batch_alter_table('balance_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_balance_snapshots_account_id'), ['account_id'], unique=False)

    op.create_table('goal_accounts',
    sa.Column('goal_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('goal_id', 'account_id')
    )
    op.create_table('holdings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('ticker', sa.String(length=32), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=6), nullable=True),
    sa.Column('value', sa.Integer(), nullable=True),
    sa.Column('as_of', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('holdings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_holdings_account_id'), ['account_id'], unique=False)

    op.create_table('import_batches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=256), nullable=True),
    sa.Column('rows_total', sa.Integer(), nullable=False),
    sa.Column('inserted', sa.Integer(), nullable=False),
    sa.Column('duplicates', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('recurring_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('payee_match', sa.String(length=256), nullable=True),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('frequency', sa.String(length=16), nullable=False),
    sa.Column('next_due', sa.Date(), nullable=True),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('last_seen', sa.Date(), nullable=True),
    sa.Column('last_amount', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('payee_raw', sa.String(length=256), nullable=False),
    sa.Column('payee_clean', sa.String(length=256), nullable=False),
    sa.Column('category_id', sa.Integer(), nullable=True),
    sa.Column('category_status', sa.String(length=16), nullable=False),
    sa.Column('suggestion_source', sa.String(length=16), nullable=True),
    sa.Column('provider_category', sa.String(length=128), nullable=True),
    sa.Column('is_transfer', sa.Boolean(), nullable=False),
    sa.Column('transfer_source', sa.String(length=16), nullable=True),
    sa.Column('transfer_pair_id', sa.Integer(), nullable=True),
    sa.Column('is_pending', sa.Boolean(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('import_fingerprint', sa.String(length=512), nullable=True),
    sa.Column('import_batch_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_batch_id'], ['import_batches.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_transactions_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_category_id'), ['category_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_external_id'), ['external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_import_fingerprint'), ['import_fingerprint'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_payee_clean'), ['payee_clean'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_transactions_payee_clean'))
        batch_op.drop_index(batch_op.f('ix_transactions_import_fingerprint'))
        batch_op.drop_index(batch_op.f('ix_transactions_external_id'))
        batch_op.drop_index(batch_op.f('ix_transactions_date'))
        batch_op.drop_index(batch_op.f('ix_transactions_category_id'))
        batch_op.drop_index(batch_op.f('ix_transactions_account_id'))

    op.drop_table('transactions')
    op.drop_table('recurring_items')
    op.drop_table('import_batches')
    with op.batch_alter_table('holdings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_holdings_account_id'))

    op.drop_table('holdings')
    op.drop_table('goal_accounts')
    with op.batch_alter_table('balance_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_balance_snapshots_account_id'))

    op.drop_table('balance_snapshots')
    op.drop_table('payee_rules')
    with op.batch_alter_table('goal_contributions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_goal_contributions_goal_id'))

    op.drop_table('goal_contributions')
    op.drop_table('alerts')
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_accounts_external_id'))

    op.drop_table('accounts')
    with op.batch_alter_table('provider_connections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_provider_connections_external_id'))

    op.drop_table('provider_connections')
    op.drop_table('import_mappings')
    op.drop_table('goals')
    op.drop_table('categories')
    op.drop_table('app_settings')
