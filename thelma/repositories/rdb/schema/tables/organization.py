"""
Organization table.
"""
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Table


__docformat__ = "reStructuredText en"
__all__ = ['create_table']


def create_table(metadata):
    "Table factory."
    tbl = Table('organization', metadata,
                Column('organization_id', Integer, primary_key=True),
                Column('name', String, CheckConstraint('length(name)>0'),
                       nullable=False, unique=True),
                )
    return tbl
