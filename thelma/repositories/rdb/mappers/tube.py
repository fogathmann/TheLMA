"""
Tube mapper.
"""
from sqlalchemy import String
from sqlalchemy.orm import mapper as sa_mapper
from sqlalchemy.orm import relationship
from sqlalchemy.orm.deprecated_interfaces import MapperExtension
from sqlalchemy.sql import case
from sqlalchemy.sql import cast
from sqlalchemy.sql import literal
from sqlalchemy.sql.expression import delete
from sqlalchemy.sql.expression import insert

from everest.repositories.rdb.utils import mapper
from thelma.entities.container import CONTAINER_TYPES
from thelma.entities.container import Tube


__docformat__ = 'reStructuredText en'
__all__ = ['create_mapper']


class TubeMapperExtension(MapperExtension):
    def __init__(self, container_barcode_tbl):
        MapperExtension.__init__(self)
        self.__container_barcode_tbl = container_barcode_tbl

    def after_insert(self, cnt_mapper, connection, instance): # pylint:disable=W0613
        value_map = dict(container_id=instance.container_id,
                         barcode=instance.barcode)
        connection.execute(insert(self.__container_barcode_tbl,
                                  values=value_map))

    def before_delete(self, cnt_mapper, connection, instance): # pylint:disable=W0613
        connection.execute(delete(self.__container_barcode_tbl.c.container_id
                                        == instance.container_id))

class TubeBarcode(object):
    pass


def create_mapper(container_mapper, container_barcode_tbl):
    "Mapper factory."
    sa_mapper(TubeBarcode, container_barcode_tbl)
    m = mapper(Tube,
               slug_expression=lambda cls: case([(cls.barcode == None,
                                                  literal('no-barcode-') +
                                                  cast(cls.id, String))],
                                                else_=cls.barcode),
#               extension=TubeMapperExtension(container_barcode_tbl),
               inherits=container_mapper,
               properties=
                dict(_tube_barcode=relationship(TubeBarcode, lazy='subquery',
                                                uselist=False, viewonly=True),
                     ),
               polymorphic_identity=CONTAINER_TYPES.TUBE)
    return m
