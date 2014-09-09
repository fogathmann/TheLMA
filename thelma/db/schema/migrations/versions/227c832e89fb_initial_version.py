"""initial version

This loads a raw SQL file that was generated by exporting just the schema
of the unidb DB at db_version 19.1 .

Revision ID: 227c832e89fb
Revises: None
Create Date: 2014-09-10 16:45:20.922367

"""
from thelma.db.schema.migrations.util import load_upgrade_sql_file

# revision identifiers, used by Alembic.
revision = '227c832e89fb'
down_revision = None


def upgrade():
    load_upgrade_sql_file(revision)


def downgrade():
    pass
