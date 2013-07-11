"""
ISO request mapper.
"""
from everest.repositories.rdb.utils import mapper
from sqlalchemy.orm import column_property
from sqlalchemy.orm import relationship
from thelma.db.mappers.utils import CaseInsensitiveComparator
from thelma.models.iso import ISO_TYPES
from thelma.models.iso import Iso
from thelma.models.iso import IsoRequest
from thelma.models.liquidtransfer import WorklistSeries
from thelma.models.racklayout import RackLayout
from thelma.models.moleculedesign import MoleculeDesignPoolSet

__docformat__ = "reStructuredText en"
__all__ = ['create_mapper']


#FIXME: no name on DB level #pylint: disable=W0511
def create_mapper(iso_request_tbl,
                  iso_request_rack_layout_tbl,
                  worklist_series_iso_request_tbl,
                  iso_request_pool_set_tbl):
    "Mapper factory."

    irrl = iso_request_rack_layout_tbl
    wsir = worklist_series_iso_request_tbl

    m = mapper(IsoRequest, iso_request_tbl,
               id_attribute='iso_request_id',
               properties=
                    dict(rack_layout=relationship(RackLayout, uselist=False,
                                secondary=irrl,
                                cascade='all,delete,delete-orphan',
                                single_parent=True),
                         owner=column_property(iso_request_tbl.c.owner,
                                comparator_factory=CaseInsensitiveComparator
                                ),
                         isos=relationship(Iso, back_populates='iso_request'),
                         worklist_series=relationship(WorklistSeries,
                                uselist=False, secondary=wsir,
                                cascade='all,delete,delete-orphan',
                                single_parent=True),
                         molecule_design_pool_set=relationship(
                                MoleculeDesignPoolSet,
                                secondary=iso_request_pool_set_tbl,
                                uselist=False, single_parent=True,
                                cascade='all,delete,delete-orphan')
                    ),
               polymorphic_on=iso_request_tbl.c.iso_type,
               polymorphic_identity=ISO_TYPES.BASE,
               )
    return m
