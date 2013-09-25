"""
This is so to say the brain module for lab ISO processing. Here it is decided
what sort of plates and layout we need.

AAB
"""
from thelma.automation.tools.base import BaseAutomationTool
from thelma.automation.tools.iso.jobcreator import IsoProvider
from thelma.automation.tools.iso.lab.base import DILUENT_INFO
from thelma.automation.tools.iso.lab.base import FinalLabIsoLayout
from thelma.automation.tools.iso.lab.base import FinalLabIsoPosition
from thelma.automation.tools.iso.lab.base import LABELS
from thelma.automation.tools.iso.lab.base import LabIsoPosition
from thelma.automation.tools.iso.lab.base import LabIsoPrepLayout
from thelma.automation.tools.iso.lab.base import LabIsoPrepPosition
from thelma.automation.tools.iso.lab.base import get_stock_takeout_volume
from thelma.automation.tools.semiconstants \
    import get_rack_specs_from_reservoir_specs
from thelma.automation.tools.semiconstants import PIPETTING_SPECS_NAMES
from thelma.automation.tools.semiconstants import RESERVOIR_SPECS_NAMES
from thelma.automation.tools.semiconstants import get_96_rack_shape
from thelma.automation.tools.semiconstants import get_item_status_future
from thelma.automation.tools.semiconstants import get_max_dilution_factor
from thelma.automation.tools.semiconstants import get_min_transfer_volume
from thelma.automation.tools.semiconstants import get_pipetting_specs
from thelma.automation.tools.semiconstants import get_positions_for_shape
from thelma.automation.tools.semiconstants import get_reservoir_spec
from thelma.automation.tools.stock.tubepicking import TubePicker
from thelma.automation.tools.utils.base import CONCENTRATION_CONVERSION_FACTOR
from thelma.automation.tools.utils.base import VOLUME_CONVERSION_FACTOR
from thelma.automation.tools.utils.base import add_list_map_element
from thelma.automation.tools.utils.base import are_equal_values
from thelma.automation.tools.utils.base import get_trimmed_string
from thelma.automation.tools.utils.base import is_larger_than
from thelma.automation.tools.utils.base import is_smaller_than
from thelma.automation.tools.utils.base import is_valid_number
from thelma.automation.tools.utils.base import round_up
from thelma.automation.tools.utils.iso import IsoRequestAssociationData
from thelma.automation.tools.utils.iso import IsoRequestLayoutConverter
from thelma.automation.tools.utils.iso import IsoRequestPosition
from thelma.automation.tools.utils.layouts import FIXED_POSITION_TYPE
from thelma.automation.tools.utils.layouts import MOCK_POSITION_TYPE
from thelma.automation.tools.utils.layouts import TransferTarget
from thelma.automation.tools.utils.racksector import QuadrantIterator
from thelma.automation.tools.utils.racksector import RackSectorTranslator
from thelma.automation.tools.worklists.base import get_dynamic_dead_volume
from thelma.models.iso import ISO_STATUS
from thelma.models.iso import LabIso
from thelma.models.iso import LabIsoRequest
from thelma.models.liquidtransfer import PlannedRackSampleTransfer
from thelma.models.liquidtransfer import PlannedSampleDilution
from thelma.models.liquidtransfer import PlannedSampleTransfer
from thelma.models.moleculedesign import MoleculeDesignPoolSet

__docformat__ = 'reStructuredText en'

__all__ = ['LabIsoBuilder',
           'LabIsoPlanner',
           'PoolContainer',
           '_LayoutPlanner',
           'SectorPlanner',
           'RackPositionPlanner',
           'JobRackPositionPlanner',
           '_CONTAINER_IDS',
           '_LocationContainer',
           'SectorContainer',
           'RackPositionContainer',
           '_LocationAssigner',
           'SectorLocationAssigner',
           'RackPositionLocationAssigner',
           'JobRackPositionAssigner',
           'get_transfer_volume',
           '_PlateContainer',
           'SectorPlateContainer',
           'RackPositionPlateContainer',
           'LibraryIsoBuilder',
           'LibraryIsoPlanner']


class LabIsoBuilder(object):
    """
    Helper storage class that collects all layout and planned transfer data.
    Once completed it can be used to generate ISOs, worklist series and
    ISO job preparation plates.

    Use :func:`create_isos` or :func:`create_job_preparation_plates` to create
    the entities once completed. The generates of the worklist series requires
    a special tool (:class:`LabIsoWorklistSeriesGenerator`).
    """

    def __init__(self, iso_request, excluded_racks, requested_tubes):
        """
        Constructor

        :param iso_request: The ISO request the ISOs shall belong to.
        :type iso_request: :class:`thelma.models.iso.LabIsoRequest`

        :param excluded_racks: A list of barcodes from stock racks that shall
            not be used for stock sample picking.
        :type excluded_racks: A list of rack barcodes

        :param requested_tubes: A list of barcodes from stock tubes that are
            supposed to be used.
        :type requested_tubes: A list of tube barcodes.
        """
        self.iso_request = iso_request
        #: The ticket ID for the experiment metadata the ISO request belongs to
        #: (is part of worklist and plate labels).
        self.ticket_number = self.iso_request.experiment_metadata.ticket_number
        #: A list of barcodes from stock racks that shall not be used for
        #: stock sample (molecule design pool) picking.
        self.__exluded_racks = excluded_racks
        if len(excluded_racks) < 1: self.__exluded_racks = None
        #: A list of barcodes from stock tubes that are supposed to be used
        #: (for fixed positions).
        self.__requested_tubes = requested_tubes
        if len(requested_tubes) < 1: self.__requested_tubes = None

        kw = dict(shape=iso_request.iso_plate_reservoir_specs.rack_shape)
        #: The layout for the ISO plates (:class:`LabIsoLayout`).
        self.final_iso_layout = FinalLabIsoLayout(**kw)
        #: The layouts for the preparation plates (:class:`LabIsoPrepLayout`)
        #: mapped onto plate markers.
        self.preparation_layouts = dict()
        #: The layouts for ISO job preparation plates
        #: (:class:`LabIsoPrepLayout`) mapped onto plate markers.
        self.job_layouts = dict()

        iso_plate_specs = get_rack_specs_from_reservoir_specs(
                          self.iso_request.iso_plate_reservoir_specs)
        #: The plate specs for the plates mapped onto plate markers (final plate
        #: specs are mapped onto the aliquot role marker of :class:`LABELS`).
        self.plate_specs = {LABELS.ROLE_FINAL : iso_plate_specs}

        #: The planned dilutions for each layout mapped onto plate markers.
        self.planned_dilutions = dict()

        #: The planned transfers (container and rack) that take place within the
        #: same plate. The transfers are sorted by intraplate ancestor count.
        self.intraplate_transfers = dict()
        #: The planned transfers (container and rack) that lead from one plate
        #: to another. The transfers are sorted by source plate (priority 1)
        #: and target plate (priority 2).
        self.interplate_transfers = dict()

        #: The items status for new plates.
        self.__plate_status = get_item_status_future()
        #: The picked candidates for fixed pools mapped onto pools.
        self.__fixed_candidates = None
        #: The floating candidates (in the order of the query result).
        self.__floating_candidates = None
        #: The number of ISOs to be generated - in contrast to the ordered
        #: number this reflects the number that can actually be generated
        #: (taking into account the number of floating positions and
        #: candidates).
        self.__isos_to_generate = None

    def add_final_iso_plate_position(self, plate_pos):
        """
        Convenience method adding a new ISO plate position to the
        :attr:`iso_layout`.
        """
        self.final_iso_layout.add_position(plate_pos)

    def add_preparation_layout(self, plate_marker, prep_layout, plate_specs):
        """
        Adds a :class:`LabIsoPrepLayout` for a particular preparation plate
        and records the :class:`PlateSpecs`.
        """
        self.preparation_layouts[plate_marker] = prep_layout
        self.plate_specs[plate_marker] = plate_specs

    def add_job_preparation_layout(self, plate_marker, prep_layout,
                                   plate_specs):
        """
        Adds a :class:`LabIsoPrepLayout` for a particular ISO job
        preparation plate and records the :class:`PlateSpecs`.
        """
        self.job_layouts[plate_marker] = prep_layout
        self.plate_specs[plate_marker] = plate_specs

    def get_all_layouts(self):
        """
        Convenience functions returning all layouts mapped onto their
        plate markers.
        """
        layouts = {LABELS.ROLE_FINAL : self.final_iso_layout}
        layouts.update(self.preparation_layouts)
        layouts.update(self.job_layouts)
        return layouts

    def add_dilution(self, planned_container_dilution, plate_marker):
        """
        Convenience function storing the planned dilution for the given
        plate marker.
        """
        add_list_map_element(self.planned_dilutions, plate_marker,
                             planned_container_dilution)

    def add_intraplate_transfer(self, planned_transfer, plate_marker,
                                intraplate_ancestor_count):
        """
        Adds the :class:`PlannedTransfer` to the intraplate transfer map.
        The intraplate ancestor count is the len of the ancestor line been
        located in the same plate. This number is important to maintain the
        order of the dilution series.
        """
        if self.intraplate_transfers.has_key(plate_marker):
            transfer_map = self.intraplate_transfers[plate_marker]
        else:
            transfer_map = dict()
            self.intraplate_transfers[plate_marker] = transfer_map

        add_list_map_element(transfer_map, intraplate_ancestor_count,
                             planned_transfer)

    def add_interplate_transfer(self, planned_transfer, source_plate_marker,
                                target_plate_marker):
        """
        Adds the :class:`PlannedTransfer` to the interplate map.
        """
        if self.interplate_transfers.has_key(source_plate_marker):
            transfer_map = self.interplate_transfers[source_plate_marker]
        else:
            transfer_map = dict()
            self.interplate_transfers[source_plate_marker] = transfer_map

        add_list_map_element(transfer_map, target_plate_marker,
                             planned_transfer)

    def _set_immutable_value(self, value, attr_name):
        """
        Sets an immutable attribute to a value or raises an error if the
        attribute has been set before.

        :param value: The value for the attribute.

        :param attr_name: The name of the attribute.
        :type attr_name: :class:`str`

        :raise AttributeError: If the candidates have been set before.
        """
        if not getattr(self, attr_name) is None:
            msg = 'The %s attribute has been set before!' % (attr_name)
            raise AttributeError(msg)

        setattr(self, attr_name, value)

    def set_fixed_candidates(self, fixed_candidates):
        """
        The tube candidates must be mapped onto pools.

        :param fixed_candidates: The fixed candidates mapped onto pools.
        :type fixed_candidates: :class:`dict`

        :raise AttributeError: If the candidates have been set before.
        """
        self._set_immutable_value(fixed_candidates, '__fixed_candidates')

    def set_floating_candidates(self, floating_candidates):
        """
        The tube candidate must be in the same order as returned by the
        optimizing query.

        :param floating_candidates: The tube candidates in query order.
        :type floating_candidates: :class:`list`

        :raise AttributeError: If the candidates have been set before.
        """
        self._set_immutable_value(floating_candidates, '__floating_candidates')

    def set_number_of_isos(self, number_isos):
        """
        Sets the number of ISOs to be generated - in contrast to the ordered
        number this reflects the number that can actually be generated
        (taking into account the number of floating positions and candidates).

        :param number_isos: The number of ISOs to be generated
        :type number_isos: :class:`int`

        :raise AttributeError: If the number have been set before.
        """
        self._set_immutable_value(number_isos, '__isos_to_generate')

    def create_isos(self):
        """
        Creates new ISOs including all plates and layouts. The number of ISOs
        has been determined before during runner set up.
        """
        pool_set_type = None
        if not self.__floating_candidates is None:
            pool_set_type = self.iso_request.molecule_design_pool_set.\
                            molecule_type

        isos = []
        while len(isos) < self.__isos_to_generate:
            iso = self.__create_iso(pool_set_type)
            isos.append(iso)
            if not self.__floating_candidates is None and \
                                    len(self.__floating_candidates) < 1:
                break

        if len(isos) < self.__isos_to_generate:
            msg = 'There are enough floating tubes candidates to fill all ' \
                  'floating positions! This is a programming error. Please ' \
                  'contact the IT department.'
            raise ValueError(msg)

        return isos

    def __create_iso(self, pool_set_type):
        """
        Creates an ISO including ISO plates.
        """
        iso_number = LABELS.get_new_iso_number(self.iso_request)
        iso_label = LABELS.create_iso_label(ticket_number=self.ticket_number,
                                            iso_number=iso_number)

        final_layout = self.__create_layout_without_floatings(
                                                        self.final_iso_layout)
        floating_map = dict()
        pools = set()
        self._fill_final_plate_layout(final_layout, floating_map, pools)
        pool_set = None
        if len(pools) > 0:
            pool_set = MoleculeDesignPoolSet(molecule_type=pool_set_type,
                                             molecule_design_pools=pools)

        prep_layouts = self.__complete_iso_preparation_layouts(floating_map)
        iso_layouts = {LABELS.ROLE_FINAL : final_layout}.update(prep_layouts)
        number_stock_racks = self.distribute_pools_to_stock_racks(iso_layouts,
                                                                  for_job=False)
        iso = LabIso(label=iso_label, iso_request=self.iso_request,
                     number_stock_racks=number_stock_racks,
                     molecule_design_pool_set=pool_set,
                     rack_layout=final_layout.create_rack_layout(),
                     optimizer_excluded_racks=self.__exluded_racks,
                     optimizer_required_racks=self.__requested_tubes)
        self._add_final_iso_plates(iso)
        self.__create_iso_preparation_plates(iso, prep_layouts)
        return iso

    def _fill_final_plate_layout(self, iso_plate_layout, floating_map, pools):
        """
        Add additional positions to the final ISO layout (fixed and mock
        positions are already included).
        """
        for plate_pos in self.final_iso_layout.get_sorted_floating_positions():
            placeholder = plate_pos.molecule_design_pool
            if not floating_map.has_key(placeholder):
                if len(self.__floating_candidates) < 1:
                    candidate = None
                else:
                    candidate = self.__floating_candidates.pop(0)
                    floating_map[placeholder] = candidate
                    pools.add(candidate.pool)
            else:
                candidate = floating_map[placeholder]
            copy_pos = plate_pos.create_completed_copy(candidate)
            iso_plate_layout.add_working_position(copy_pos)

    def __create_layout_without_floatings(self, template_layout):
        """
        Helper function returning a copy of the given layout with fixed
        including stock tube data and mock positions.
        """
        copy_layout = template_layout.__class__(shape=template_layout.shape)
        for plate_pos in template_layout.get_working_positions():
            if plate_pos.is_fixed:
                candidate = self.__fixed_candidates[
                                                plate_pos.molecule_design_pool]
                copy_pos = plate_pos.create_completed_copy(candidate)
            elif plate_pos.is_mock:
                copy_pos = LabIsoPosition.create_mock_position(
                                       rack_position=plate_pos.rack_position,
                                       volume=plate_pos.volume)
            copy_layout.add_working_position(copy_pos)

        return copy_layout

    def _add_final_iso_plates(self, iso):
        """
        Creates or attaches the final ISO plates to the given ISO.
        By default, we create and add :class:`IsoAliquotPlate` objects.
        """
        num_aliquots = self.iso_request.number_aliquots
        plate_specs = self.plate_specs[LABELS.ROLE_FINAL]

        for i in range(num_aliquots):
            plate_number = i + 1
            if num_aliquots == 1:
                plate_number = None
            plate_marker = LABELS.create_rack_marker(LABELS.ROLE_FINAL,
                                                     rack_number=plate_number)
            label = LABELS.create_rack_label(rack_marker=plate_marker,
                                             entity_label=iso.label)
            plate = plate_specs.create_rack(self, label=label,
                                            status=self.__plate_status)
            iso.add_aliquot_plate(plate=plate)

    def __complete_iso_preparation_layouts(self, floating_map):
        """
        Helper function creating a completed copy of an ISO preparation
        layout (with pools for floatings and stock data).
        """
        single_plate = (len(self.preparation_layouts) == 1)
        completed_layouts = dict()
        for plate_marker, prep_layout in self.preparation_layouts.iteritems():
            use_marker = self.__strip_plate_marker(plate_marker, single_plate)
            copy_layout = self.__create_layout_without_floatings(prep_layout)
            self.__add_floating_positions(prep_layout, copy_layout,
                                          floating_map)
            completed_layouts[use_marker] = copy_layout

        return completed_layouts

    def __create_iso_preparation_plates(self, iso, layouts):
        """
        Helper function creation an :class:`IsoPreparationPlate` for each
        layout.
        """
        for plate_marker, layout in layouts.iteritems():
            label = LABELS.create_rack_label(rack_marker=plate_marker,
                                             entity_label=iso.label)
            plate_specs = self.plate_specs[plate_marker]
            plate = plate_specs.create_rack(self, label=label,
                                            status=self.__plate_status)
            iso.add_preparation_plate(plate=plate,
                                  rack_layout=layout.create_rack_layout())

    def __strip_plate_marker(self, plate_marker, is_single_plate):
        """
        Helper method removing the plate number from a plate marker - only used
        if there is only one plate for a type.
        """
        if not is_single_plate: return plate_marker

        value_parts = LABELS.parse_rack_marker(plate_marker)
        if value_parts[LABELS.MARKER_RACK_NUM] == 1:
            role = value_parts[LABELS.MARKER_RACK_ROLE]
            return LABELS.create_rack_marker(rack_role=role)
        else:
            return plate_marker

    def __add_floating_positions(self, template_layout, copy_layout,
                                 floating_map):
        """
        Uses the floating map from the final ISO layout completion. For
        placeholders that are not in the map we do not create positions
        (we assume to have run out of tube candidates).
        """
        for plate_pos in template_layout.get_sorted_floating_positions():
            placeholder = plate_pos.molecule_design_pool
            if not floating_map.has_key(placeholder): continue
            candidate = floating_map[placeholder]
            copy_pos = plate_pos.create_completed_copy(candidate)
            copy_layout.add_working_position(copy_pos)

    def complete_job_preparation_plates(self):
        """
        Returns a completed copy of an ISO job preparation layout (with stock
        data).
        """
        single_plate = (len(self.job_layouts) == 1)
        completed_layouts = dict()
        for plate_marker, layout in self.job_layouts.iteritems():
            use_marker = self.__strip_plate_marker(plate_marker, single_plate)
            copy_layout = self.__create_layout_without_floatings(layout)
            completed_layouts[use_marker] = copy_layout

        return completed_layouts

    def create_job_preparation_plates(self, iso_job, layouts):
        """
        Creates the job preparation plates each layout.
        """
        for plate_marker, layout in layouts.iteritems():
            plate_specs = self.plate_specs[plate_marker]
            label = LABELS.create_rack_label(rack_marker=plate_marker,
                                             entity_label=iso_job.label)
            plate = plate_specs.create_rack(label=label,
                                            status=self.__plate_status)
            iso_job.add_preparation_plate(plate=plate,
                                      rack_layout=layout.create_rack_layout())

    def distribute_pools_to_stock_racks(self, layouts, for_job):
        """
        Attaches stock rack marker to the starting position in the passed
        layouts. Sector positions (= positions transferred via the CyBio)
        gain an own stock rack for each distinct sector since all samples
        have to be transferred together. For all other positions we try
        to exploit the stock rack capacity.
        """
        stock_rack_count = 0
        pos_map = dict()
        for plate_marker, layout in layouts.iteritems():
            is_final_plate = (plate_marker == LABELS.ROLE_FINAL)
            if for_job:
                for plate_pos in layout.working_positions():
                    if not plate_pos.is_fixed: continue
                    add_list_map_element(pos_map, plate_pos.molecule_design_pool,
                                         plate_pos)
            else:
                sector_map = layout.get_sector_map()
                stock_rack_count = self.__assign_sector_stock_racks(
                                        stock_rack_count, sector_map, for_job,
                                        is_final_plate)
                if not sector_map.has_key(layout.NO_SECTOR_MARKER): continue
                remaining_positions = sector_map[layout.NO_SECTOR_MARKER]
                for plate_pos in remaining_positions:
                    if not self.__accept_starting_well(plate_pos, is_final_plate,
                                                       for_job): continue
                    add_list_map_element(pos_map, plate_pos.molecule_design_pool,
                                         plate_pos)

        if len(pos_map) > 0:
            stock_rack_count = self.__assign_position_stock_racks(pos_map,
                                                              stock_rack_count)

        return stock_rack_count

    def __assign_sector_stock_racks(self, rack_count, sector_map, from_job,
                                    is_final_plate):
        """
        Assigns stock rack markers to ISO plate starting positions that are
        transferred by the CyBio (i.e. that have a sector index).
        Since all samples of a stock rack are transferred together there must
        not be other samples in the stock racks and the rack contents cannot
        be condensed.
        If there are floating positions, controls are covered by the job and
        are not regarded.
        """
        # We figure out if a sector is already covered might comparing the
        # pool IDs of the first floating positions or the last position at all.
        sector_hash = dict()
        has_floatings = False

        for sector_index, positions in sector_map.iteritems():
            if sector_index == LabIsoPrepLayout.NO_SECTOR_MARKER: continue
            plate_pos = None
            for plate_pos in positions:
                if plate_pos.is_floating:
                    has_floatings = True
                    break
            if not plate_pos.is_starting_well: continue
            if not self.__accept_starting_well(plate_pos, is_final_plate,
                                               from_job): continue
            pool_id = plate_pos.molecule_design_pool.id
            if sector_hash.has_key(pool_id):
                rack_marker = sector_hash[pool_id]
            else:
                rack_marker = self.__create_stock_rack_marker(rack_count)
                sector_hash[pool_id] = rack_marker
            for plate_pos in positions:
                if has_floatings and plate_pos.is_fixed: continue
                plate_pos.stock_rack_marker = rack_marker

        return rack_count

    def __assign_position_stock_racks(self, pos_map, current_rack_count):
        """
        Assigns stock rack markers to ISO plate starting positions that are
        not transferred by the CyBio but independently for each position.
        We exploit the capacity of the stock racks.
        """
        shape96 = get_96_rack_shape()
        stock_rack_positions = {}
        for pool in sorted(pos_map.keys()):
            used_rack = None
            for rack_marker in sorted(stock_rack_positions.keys()):
                if stock_rack_positions[rack_marker] > 0:
                    used_rack = rack_marker
                    break
            if used_rack is None:
                used_rack = self.__create_stock_rack_marker(
                                                    current_rack_count)
                num_positions = shape96.size
                stock_rack_positions[used_rack] = num_positions
            plate_positions = pos_map[pool]
            for plate_pos in plate_positions:
                plate_pos.stock_rack_marker = used_rack
            stock_rack_positions[used_rack] -= 1

        return current_rack_count

    def __create_stock_rack_marker(self, current_rack_count):
        """
        Helper function incrementing the stock rack count and create a new
        rack marker.
        """
        current_rack_count += 1
        plate_marker = LABELS.create_rack_marker(LABELS.ROLE_STOCK,
                                                 current_rack_count)
        return plate_marker

    def __accept_starting_well(self, plate_pos, from_final_plate, from_job):
        """
        Helper function determinating whether a plate position is accepted as
        starting well. In final plate positions we have to regard whether a
        position originates from job or ISO processing.
        Preparation plates are separated for Job and ISO anyway.
        """
        if not plate_pos.is_starting_well: return False
        if not from_final_plate: return True
        return (plate_pos.from_job == from_job)

    def __str__(self):
        return self.ticket_number

    def __repr__(self):
        str_format = '<%s %s>'
        params = (self.__class__.__name__, self.ticket_number)
        return str_format % params


class LabIsoPlanner(IsoProvider):
    """
    Creates an :class:`LabIsoBuilder` that can be used to generate lab ISOs.
    Also tube picking is done here.

    **Return Value:** :class:`LabIsoBuilder`
    """

    NAME = 'Lab ISO Planner'

    #: The class of the builder  generated by this planner.
    _BUILDER_CLS = LabIsoBuilder

    #: The CyBio is only used if it would transfer at the least the given
    #: number of pools.
    __MIN_CYBIO_TRANSFER_NUMBER = 20

    def __init__(self, log, iso_request, number_isos,
                       excluded_racks=None, requested_tubes=None):
        """
        Constructor:

        :param log: The log to record events.
        :type log: :class:`thelma.ThelmaLog`

        :param iso_request: The ISO request containing the ISO layout for the
            ISO (and experiment metadata with the molecule design pools).
        :type iso_request: :class:`thelma.models.iso.IsoRequest`

        :param number_isos: The number of ISOs ordered.
        :type number_isos: :class:`int`

        :param excluded_racks: A list of barcodes from stock racks that shall
            not be used for stock sample picking.
        :type excluded_racks: A list of rack barcodes

        :param requested_tubes: A list of barcodes from stock tubes that are
            supposed to be used.
        :type requested_tubes: A list of tube barcodes.
        """
        IsoProvider.__init__(self, log=log, iso_request=self.iso_request,
                             number_isos=number_isos,
                             excluded_racks=excluded_racks,
                             requested_tubes=requested_tubes)

        #: Contains the data about the ordered plate layout - the information
        #: are not specific to a particular ISO.
        self._iso_request_layout = None
        #: This builder collects all data and is then used to generate
        #: layouts and worklists.
        self._builder = None

        #: The :class:`PoolContainer` objects for mock, floating and fixed
        #: positions.
        self.__pool_containers = None
        #: The number of ISOs we can acutally create taking into account the
        #: the number of available floating pool candidates (if there are any).
        self._real_number_isos = None

        #: The pool container contains the mock positions.
        self.__mock_container = None

        # == Floating position values ==

        #: Do we have floating positions?
        self._has_floatings = None
        #: The number of floating positions in the ISO request layout.
        self.__number_floatings = None
        #: The floating pools for which to pick tubes.
        self._queued_pools = None
        #: The pool containers for all floating pools.
        self.__floating_pool_containers = None
        #: The stock concentration for floating pools *in nM*.
        self.__floating_stock_conc = None

        # == Data related to rack sectors ==

        #: Contains the data for the different rack sectors - we only get
        #: this if it is possible to use the CyBio.
        self.__association_data = None
        #: Applies only if we can work with rack sectors
        #: (see :attr:`__association_data`). Are controls part of the sector
        #: handling (*True*) or do they have to handled completely separate
        #: (*False*)?
        self.__controls_in_quadrants = None

        # == Data related to ISO jobs ==

        #: The pool containers for all fixed positions covered by the job
        #: (happens if we have flaoting positions). These pool containers
        #: contain both final and preparation positions. If controls are
        #: also part of the sector preparation they are only starting wells.
        self.__job_pool_containers = None

    def reset(self):
        IsoProvider.reset(self)
        self._iso_request_layout = None
        self._builder = None
        self.__pool_containers = []
        self._real_number_isos = None
        self.__mock_container = None
        self._has_floatings = None
        self.__number_floatings = 0
        self._queued_pools = None
        self.__floating_pool_containers = dict()
        self.__floating_stock_conc = None
        self.__association_data = None
        self.__controls_in_quadrants = None
        self.__job_pool_containers = dict()
        _CONTAINER_IDS.start()

    def _collect_iso_data(self):
        builder_kw = dict(iso_request=self.iso_request,
                          excluded_racks=self.excluded_racks,
                          requested_tubes=self.requested_tubes)
        self._builder = self._BUILDER_CLS(**builder_kw)
        if not self.has_errors(): self.__get_iso_request_layout()
        if not self.has_errors(): self._analyse_iso_request()
        if not self.has_errors(): self._assign_sectors()
        if not self.has_errors(): self._assign_iso_specific_rack_positions()

        if self._has_floatings and not self.has_errors():
            self.__find_floating_candidates()
        if not self.has_errors():
            self._builder.set_number_of_isos(self._real_number_isos)
        if not self.has_errors() and not self.has_errors():
            self.__assign_job_positions()

        if not self.has_errors(): self.__assign_mock_positions()
        if not self.has_errors(): self.__find_fixed_candidates()

        if not self.has_errors():
            _CONTAINER_IDS.shut_down()
            self.return_value = self._builder
            self.add_info('ISO builder completed.')

    def __get_iso_request_layout(self):
        """
        Converts the rack layout of the ISO request into a
        :class:`IsoRequestLayout`.
        """
        self.add_debug('Convert ISO request layout ...')

        converter = IsoRequestLayoutConverter(log=self.log,
                                    rack_layout=self.iso_request.rack_layout)
        self._iso_request_layout = converter.get_result()

        if self._iso_request_layout is None:
            msg = 'Error when trying to convert ISO request layout.'
            self.add_error(msg)

    def _analyse_iso_request(self):
        """
        Creates some attribute short cuts references for convenience,
        creates pool containers for all pools and determines some basic data
        concerning floating positions.
        """
        self.add_debug('Analyse ISO request ...')

        self.__collect_pools()
        self._has_floatings = (self.__number_floatings > 0)
        if self._has_floatings:
            pool_set = self.iso_request.molecule_design_pool_set
            if pool_set is not None or len(pool_set) < 1:
                msg = 'There are no molecule design pools in the molecule ' \
                      'design pool set although there are floating positions!'
                self.add_error(msg)
            else:
                self.__find_queued_pools()
                # We cannot use the molecule type, because the stock
                # concentration also depends on the number of designs.
                # The stock concentration must be equal for all members of the
                # set therefore we can pick an arbitrary member.
                for pool in pool_set:
                    self.__floating_stock_conc = round(pool.\
                                        default_stock_concentration \
                                        * CONCENTRATION_CONVERSION_FACTOR, 1)
                    break

        else:
            if self.number_isos > 1:
                msg = 'You have requested %i ISOs. The system will only ' \
                      'generate 1 ISO though, because there are no floating ' \
                      'positions for this ISO request.' % (self.number_isos)
                self.add_warning(msg)
            self._real_number_isos = 1

    def __collect_pools(self):
        """
        Generates a :class:`PoolContainer` for mock, floating and fixed pools
        in the ISO request layout. The container is a convenience storage class.
        We also count the number of floating positions.
        """
        container_map = dict()
        for ir_pos in self._iso_request_layout.get_sorted_working_positions():
            if ir_pos.is_library: continue
            pool = ir_pos.molecule_design_pool
            if container_map.has_key(pool):
                pool_container = container_map[pool]
                pool_container.add_target_working_position(ir_pos)
            else:
                stock_conc = ir_pos.stock_concentration
                if ir_pos.is_floating:
                    stock_conc = self.__floating_stock_conc
                    self.__number_floatings += 1
                pool_container = PoolContainer(pool=pool,
                                           position_type=ir_pos.position_type,
                                           stock_concentration=stock_conc)
                container_map[pool] = pool_container
                self.__pool_containers.append(pool_container)
                if ir_pos.is_floating:
                    self.__floating_pool_containers[pool] = pool_container
                elif ir_pos.is_mock:
                    self.__mock_container = pool_container

        conc_too_high = []
        for pool_container in self.__pool_containers:
            stock_conc = pool_container.stock_concentration
            for ir_pos in pool_container.target_working_positions:
                iso_conc = ir_pos.iso_concentration
                if is_larger_than(iso_conc, stock_conc):
                    info = '%s (ISO: %s nM, stock: %s nM)' \
                            % (ir_pos.rack_position,
                               get_trimmed_string(iso_conc),
                               get_trimmed_string(stock_conc))
                    conc_too_high.append(info)
        if len(conc_too_high) > 0:
            msg = 'The ISO concentration for some positions is larger than ' \
                  'the stock concentration for the pool: %s.' \
                   % (', '.join(conc_too_high))
            self.add_error(msg)

    def __find_queued_pools(self):
        """
        Finds the pools to be created that are still in the queue.
        All molecule design pools from the ISO request that are not part
        of an ISO yet, are used. Cancelled ISOs are ignored.
        """
        used_pools = set()
        for iso in self.iso_request.isos:
            if iso.status == ISO_STATUS.CANCELLED:
                continue
            if iso.molecule_design_pool_set is None:
                continue
            used_pools.update(
                        iso.molecule_design_pool_set.molecule_design_pools)

        pool_set = self.iso_request.molecule_design_pool_set
        self._queued_pools = pool_set.molecule_design_pools.difference(
                                                                    used_pools)

        if len(self._queued_pools) < 1:
            msg = 'There are no unused molecule design pools left!'
            self.add_error(msg)
            return None

    def _assign_sectors(self):
        """
        Floating positions (and fixed ones if possible) are sorted into sectors.
        The sector association check both ISO volumes and ISO concentrations
        for compatibility.
        After that we determine preparation routes (using the CyBio).
        In some cases (only one position per pool, uniform ISO concentrations
        and ISO volumes) we can also use the CyBio to prepare 96-well plates.
        """
        self.add_debug('Assign ISO sectors ...')

        shape_size = self.iso_request.iso_plate_reservoir_specs.rack_shape.size
        if shape_size == 384:
            if self._has_floatings: self.__try_sorting_into_sectors_384()
        else:
            self.__try_sorting_into_sectors_96()

        if not self.has_errors() and not self.__association_data is None:
            sector_map = self.__get_sector_positions()
            planner = SectorPlanner(log=self.log,
                             iso_request=self.iso_request,
                             builder=self._builder,
                             association_data=self.__association_data,
                             sector_positions=sector_map,
                             stock_concentration=self.__floating_stock_conc)
            self._builder = planner.get_result()
            if self._builder is None:
                msg = 'Error when trying to plan sector routes.'
                self.add_error(msg)

    def __try_sorting_into_sectors_384(self):
        """
        Tries to sort the floating positions (and fixed ones if possible)
        into sectors. If there are floating positions in the ISO request
        layout, sorting has to be possible.
        """
        self.__find_association()
        if self.__association_data is None and self._has_floatings:
            msg = 'The values for the floating positions (ISO volume ' \
                  'and ISO concentration) for the floating positions ' \
                  'in the ISO request layout do not comply to the rack ' \
                  'sectors! In the current layout samples would be ' \
                  'treated differently!'
            self.add_error(msg)

    def __try_sorting_into_sectors_96(self):
        """
        If each pools occurs only once and if all ISO volumes, concentrations
        and stock concentrations are the same, we can also use a CyBio.
        """
        compatible = True
        stock_concentration = None
        for pool_container in self.__pool_containers:
            if pool_container.position_type == MOCK_POSITION_TYPE: continue
            if len(pool_container) > 1:
                compatible = False
                break
            stock_conc = pool_container.stock_concentration
            if stock_concentration is None:
                stock_concentration = round(stock_conc, 1)
            elif not are_equal_values(stock_conc, stock_concentration):
                compatible = False
                break

        if compatible:
            if self.__floating_stock_conc is None:
                self.__floating_stock_conc = stock_concentration
            self.__find_association()

    def __find_association(self):
        """
        Sets :attr:`__association_data` and :attr:`__controls_in_quadrants`
        if association is possible.
        Setting the :attr:`__association_data` activates the use of the
        CyBio. The activation only takes place though, if the number of
        pools to be transferred by it is at least equal to
        :attr:`__MIN_CYBIO_TRANSFER_NUMBER`.
        """
        association_data, regard_controls = IsoRequestAssociationData.\
                find(log=self.log, iso_request_layout=self._iso_request_layout)

        if not association_data is None:
            if regard_controls:
                modifier = 0
                if not self.__mock_container is None: modifier = -1
                num_pools = len(self.__pool_containers) - modifier
                type_str = 'fixed and floating'
            else:
                num_pools = len(self.__floating_pool_containers)
                type_str = 'floating'
            if num_pools < self.__MIN_CYBIO_TRANSFER_NUMBER:
                msg = 'It would be possible to use the CyBio to transfer %s ' \
                      'positions, but since there are only %i pools to be ' \
                      'transferred the use of the CyBio is disabled ' \
                      '(current limit: %i pool).' \
                      % (type_str, num_pools, self.__MIN_CYBIO_TRANSFER_NUMBER)
                self.add_warning(msg)
            else:
                self.__association_data = association_data
                self.__controls_in_quadrants = regard_controls
                if not self.iso_request.process_job_first == \
                                            self.__controls_in_quadrants:
                    msg = 'The order of job and ISO processing has been ' \
                          'changed (from %s to %s).' \
                          % (self.iso_request.process_job_first,
                             self.__controls_in_quadrants)
                    self.add_warning(msg)
                    self.iso_request.process_job_first = \
                                                self.__controls_in_quadrants

    def __get_sector_positions(self):
        """
        Sorts the positions that are covered by the sector preparations
        into sectors.
        """
        number_sectors = self.__association_data.number_sectors
        quadrant_irs = QuadrantIterator.sort_into_sectors(
                                  self._iso_request_layout, number_sectors)
        sector_map = dict()
        for sector_index, ir_positions in quadrant_irs.iteritems():
            positions = []
            for ir_pos in ir_positions:
                if ir_pos.is_floating or \
                            (ir_pos.is_fixed and self.__controls_in_quadrants):
                    positions.append(ir_pos)
            if len(positions) < 1: continue
            sector_map[sector_index] = positions

        return sector_map

    def _assign_iso_specific_rack_positions(self):
        """
        Determines ISO-specific positions that are covered by neither the
        sector preparation nor the ISO job preparation. This can be floating
        positions for 96-well plates or controls (fixed) for positions in
        layouts without floating positions.
        If there such positions we determine a preparation route for them.
        """
        self.add_debug('Assign ISO rack positions ...')

        regard_controls = True
        if self.__controls_in_quadrants: regard_controls = False
        if self._has_floatings: regard_controls = False

        if regard_controls:
            iso_rack_pos_containers = []
            for pool_container in self.__pool_containers:
                pos_type = pool_container.position_type
                if not pos_type == FIXED_POSITION_TYPE: continue
                iso_rack_pos_containers.append(pool_container)

            if len(iso_rack_pos_containers) > 0:
                planner = RackPositionPlanner(log=self.log,
                                    iso_request=self.iso_request,
                                    builder=self._builder,
                                    pool_containers=self.__pool_containers)
                self._builder = planner.get_result()
                if self._builder is None:
                    msg = 'Error when trying to plan rack position routes.'
                    self.add_error(msg)

    def __find_floating_candidates(self):
        """
        Runs the tube picker for the floating pools. The tube candidates are
        stored in query return order, but we use the pool-based lookup
        provided by the tube picker to determine the number of ISOs we will
        create.
        """
        self.add_debug('Find candidates for floatings ...')

        floating_take_out_vol = self.__determine_floating_take_out_volume()
        if not self.has_errors():
            tube_picker = TubePicker(log=self.log,
                 molecule_design_pools=self._queued_pools,
                 stock_concentration=self.__floating_stock_conc,
                 take_out_volume=floating_take_out_vol,
                 excluded_racks=self.excluded_racks,
                 requested_tubes=self.requested_tubes)
            sorted_candidates = tube_picker.get_result()
            if sorted_candidates is None:
                msg = 'Error when trying to find floating tube candidates!'
                self.add_error(msg)
            else:
                all_floating_candidates = tube_picker.get_unsorted_candidates()
                num_isos = float(len(sorted_candidates)) \
                           / self.__number_floatings
                self._real_number_isos = round_up(num_isos, 0)
                if not self._real_number_isos == self.number_isos:
                    msg = 'You have requested %i ISOs. The system will only ' \
                          'generate %i ISO though, because there are no more ' \
                          'floating positions for this ISO request.' \
                          % (self.number_isos, self._real_number_isos)
                    self.add_warning(msg)
                self._builder.set_floating_candidates(all_floating_candidates)

    def __determine_floating_take_out_volume(self):
        """
        This is the volume that has to be taken from the stock (stock
        dead volume is *not* included). If there is sector data available
        all floating data is included in these containers. Otherwise, we
        have to search the rack position containers.
        Before starting the calculation we sure that the volume of the
        first source container for each pool (= starting container) are equal
        for all floating positions.
        """
        layouts = self._builder.get_all_layouts()
        pool_volumes = dict()

        for layout in layouts.values():
            for plate_pos in layout.get_working_positions():
                if not plate_pos.is_floating: continue
                if not plate_pos.is_starting_well: continue
                volume = plate_pos.volume
                take_out_vol = get_stock_takeout_volume(
                            stock_concentration=self.__floating_stock_conc,
                            final_volume=volume,
                            concentration=plate_pos.concentration)
                pool = plate_pos.molecule_design_pool
                if not pool_volumes.has_key(pool):
                    pool_volumes[pool] = 0
                pool_volumes[pool] += take_out_vol

        volumes = set(pool_volumes.values())
        if len(volumes) > 1:
            msg = 'There are different volumes to be taken from the stock ' \
                  'for the floating positions. This is an programming error. ' \
                  'Please talk to Anna.'
            self.add_error(msg)
        else:
            return pool_volumes.values()[0]

    def __assign_job_positions(self):
        """
        Determines the preparation routes for fixed positions (controls) in
        an case with floating positions. These samples are prepared via the
        ISO job and are shared by all ISOs of this job.
        """
        self.add_debug('Assign ISO Job rack positions ...')

        if self.__controls_in_quadrants:
            # All controls are covered.
            self.__search_layouts_for_fixed_positions()
        else:
            # No controls has been covered.
            for pool_container in self.__pool_containers:
                if pool_container.position_type == FIXED_POSITION_TYPE:
                    self.__job_pool_containers[pool_container.pool] = \
                                                            pool_container

        copy_number = self.__get_copy_number_for_job_position()
        assigner = JobRackPositionPlanner(log=self.log,
                      iso_request=self.iso_request, builder=self._builder,
                      pool_containers=self.__job_pool_containers,
                      number_copies=copy_number)
        self._builder = assigner.get_result()
        if self._builder is None:
            msg = 'Error when trying to plan rack position routes for ISO ' \
                  'job preparation. If the problem is caused by too low ' \
                  'volume capacities, try reducing the number of ISOs for ' \
                  'the job.'
            self.add_error(msg)

    def __search_layouts_for_fixed_positions(self):
        """
        There are only controls in the ISO plate layouts, if the controls
        have been covered by the sector preparation. We are only interested
        in starting wells here, because the remaining preparation route
        has already been covered.
        """
        layouts = self._builder.get_all_layouts()
        for layout in layouts.values():
            for plate_pos in layout.get_working_positions():
                if not plate_pos.is_fixed: continue
                if not plate_pos.is_starting_well: continue
                pool = plate_pos.molecule_design_pool
                if self.__job_pool_containers.has_key(pool):
                    pool_container = self.__job_pool_containers[pool]
                else:
                    pool_container = PoolContainer(pool=pool,
                             position_type=plate_pos.position_type,
                             stock_concentration=plate_pos.stock_concentration)
                    self.__job_pool_containers[pool] = pool_container
                pool_container.target_working_positions.append(plate_pos)

    def __get_copy_number_for_job_position(self):
        """
        The copy number for job rack position depends on the number of ISOs
        to generate and - if fix positions have not prepared at all yet -
        on the number of aliquots.
        """
        if self.__controls_in_quadrants:
            return self._real_number_isos
        else:
            return self._real_number_isos * self.iso_request.number_aliquots

    def __assign_mock_positions(self):
        """
        Adds the mock positions to the final ISO layout in the builder.
        """
        if self.__mock_container is not None:
            for ir_pos in self.__mock_container.target_working_positions:
                plate_pos = FinalLabIsoPosition.create_mock_position(
                                            rack_position=ir_pos.rack_position,
                                            volume=ir_pos.volume)
                self._builder.add_final_iso_plate_position(plate_pos)

    def __find_fixed_candidates(self):
        """
        Finds tube candidates for the fixed (control) positions. The take out
        volumes are determined via the ISO plate positions.
        """
        self.add_debug('Find candidates for fixed pools ...')

        layouts = self._builder.get_all_layouts()
        conc_map = dict()
        vol_map = dict()
        for layout in layouts.values():
            for plate_pos in layout.get_working_positions():
                if not plate_pos.is_fixed: continue
                if not plate_pos.is_starting_well: continue
                pool = plate_pos.molecule_design_pool
                if not vol_map.has_key(pool):
                    vol_map[pool] = plate_pos.volume
                    add_list_map_element(conc_map,
                                         plate_pos.stock_concentration, pool)
                else:
                    vol_map[pool] += plate_pos.volume

        fixed_candidates = dict()
        for stock_conc, pools in conc_map.iteritems():
            tube_picker = TubePicker(log=self.log, molecule_design_pools=pools,
                                     stock_concentration=stock_conc,
                                     excluded_racks=self.excluded_racks,
                                     requested_tubes=self.requested_tubes)
            sorted_candidates = tube_picker.get_result()
            if sorted_candidates is None:
                msg = 'Error when trying to find tube candidates for fixed ' \
                      'pools.'
                self.add_error(msg)
                break
            else:
                unsorted_candidates = tube_picker.get_unsorted_candidates()
                for pool, candidates in sorted_candidates.iteritems():
                    picked_candidate = self.__pick_fixed_candidate(
                            vol_map[pool], candidates, unsorted_candidates)
                    if picked_candidate is None: continue
                    fixed_candidates[pool] = picked_candidate

        missing_pools = []
        for pool, required_volume in vol_map.iteritems():
            if fixed_candidates.has_key(pool): continue
            info = '%s (%s ul)' % (pool, get_trimmed_string(required_volume))
            missing_pools.append(info)
        if len(missing_pools) > 0:
            msg = 'Could not find stock tubes for the following fixed ' \
                  '(control pools): %s.' % (', '.join(missing_pools))
            self.add_error(msg)
        else:
            self._builder.set_fixed_candidates(fixed_candidates)

    def __pick_fixed_candidate(self, required_volume, pool_candidates,
                               unsorted_candidates):
        """
        We take the one with the lowest volume. If there are several tubes
        with the same volume we take the one with the lowest index in the
        unsorted candidates list (= the one that is best for rack number
        minimisation).
        """
        suitable = dict()
        for candidate in pool_candidates:
            if is_smaller_than(candidate.volume, required_volume): continue
            add_list_map_element(suitable, candidate.volume, candidate)
        if len(suitable) < 1: return None

        min_vol = min(suitable.keys())
        suitable_candidates = suitable[min_vol]
        if len(suitable_candidates) == 1: return suitable_candidates[0]

        index_map = dict()
        for candidate in suitable_candidates:
            index_map[unsorted_candidates.index(candidate)] = candidate
        return index_map[min(index_map.keys())]


class PoolContainer(object):
    """
    A helper class storing base data (ISO request positions, position types
    and stock concentration for a pool).
    """
    def __init__(self, pool, position_type, stock_concentration):
        """
        Constructor

        :param pool: The pool these position deal with.
        :type pool: :class:`thelma.models.moleculedesign.MoleculeDesignPool`

        :param position_type: The types of the positions in the layouts
            (must be the same for all positions and layouts).
        :type position_type: see :class:`MoleculeDesignPoolParameters`

        :param stock_concentration: The stock concentration for the pool
            *in nM*.
        :type stock_concentration: positive number
        """
        self.pool = pool
        self.position_type = position_type
        self.target_working_positions = []
        self.stock_concentration = stock_concentration

    def add_target_working_position(self, pool_pos):
        """
        Adds a molecule design pool (layout) position to the list.
        """
        self.target_working_positions.append(pool_pos)

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.pool == self.pool

    def __len__(self):
        return len(self.target_working_positions)

    def __iter__(self):
        return iter(self.target_working_positions)

    def __hash__(self):
        return hash(self.pool)

    def __str__(self):
        return self.pool

    def __repr__(self):
        str_format = '<%s %s position type: %s>'
        params = (self.__class__.__name__, self.pool, self.position_type)
        return str_format % params





class _CONTAINER_IDS(object):
    """
    Provides (temporary) IDs for :class:`_LocationContainer` objects to make
    sure that objects are distinguished even if their values are equal. The
    IDs are running numbers.

    This is a singleton object.
    """

    __current_container_counter = None

    def __init__(self):
        """
        This class must not be instantiated. Use :func:`start` instead.
        """
        raise NotImplementedError('Class must not be instantiated.')

    @classmethod
    def start(cls):
        """
        Resets the ID counter.
        """
        cls.__current_container_counter = 0

    @classmethod
    def get_container_id(cls):
        """
        Increments the ID counter and returns a new ID.
        """
        cls.__current_container_counter += 1
        return cls.__current_container_counter

    @classmethod
    def shut_down(cls):
        """
        Sets the current :attr:`__current_container_counter` to *None*.
        """
        cls.__current_container_counter = None


class _LocationContainer(object):
    """
    Storage class whose data is set successively.
    A location container represents a rack position or rack sector (depending
    on the subclass) in an ISO plate (aliquot, library or preparation plate).
    Location containers can be linked to each other to provide preparation
    routes (= groups of containers that are derived from one another).

    In case of preparation cotainers first the sample data is set. The actual
    location is assigned later.
    """

    #: The name of the attribute containing the location data.
    LOCATION_ATTR_NAME = None
    #: The :class:`PipettingSpecs` supported by this class (used for minimum
    #: volume checks).
    _PIPETTING_SPECS_NAME = None
    #: The :class:`PlannedTransfer` class supported by this class.
    _PLANNED_TRANSFER_CLS = None

    def __init__(self, volume, target_concentration, parent_concentration,
                 is_final_container=False):
        """
        Constructor

        :param volume: The volume the container shall have *after all transfers*
            (i.e. without dead volumes and volumes that have been transferred
            to other containers) *in ul*. Use :attr:`full_volume` to access
            the complete (maximum) volume.
        :type volume: positive number, unit ul

        :param target_concentration: The concentration of each sample *after
            all transfers in nM*.
        :type target_concentration: positive number, unit nM

        :param parent_concentration: The concentration of the source (parent
            container or stock) for this sample *after all transfers in nM*.
        :type parent_concentration: positive number, unit nM

        :param is_final_container: Does this container belong to a final ISO
            plate (if so, its location and sample data cannot be altered).
        :type is_final_container: :class:`bool`
        """
        if isinstance(self, _LocationContainer):
            raise NotImplementedError('Abstract class.')

        #: The volume the container shall have *after all transfers in ul*
        self.__volume = volume
        #: The concentration of each sample *after all transfers in nM*.
        self.__target_concentration = target_concentration
        #: The concentration of the source (parent container or stock) for
        #: this sample *after all transfers in nM*.
        self.__parent_concentration = parent_concentration
        #: Does this container belong to a final ISO plate?
        self.__is_final_container = is_final_container

        #: Can sample (volume) data be altered? Is always *False* for final
        #: plate containers.
        self.__allows_modification = not self.__is_final_container
        #: The minimum transfer volume for the supported pipetting specs *in ul*
        self.__min_transfer_volume = get_min_transfer_volume(
                                     self._PIPETTING_SPECS_NAME)

        #: The container this container is derived from.
        self.__parent_container = None
        #: The transfer volume *in ul* for each child container.
        self.targets = dict()

        #: The plate marker marks the plate this container has been placed onto.
        self.plate_marker = None

        #: A temporary ID used to distinguish container also if they have
        #: equal values (might be tthe case if copy number are larger 1).
        self.__id = _CONTAINER_IDS.get_container_id()
        #: The dead volume that must remain in a plate.
        self.__dead_volume = 0
        #: The mininum full volume the container might have (might be relevant
        #: for preparation containers).
        self.__min_full_volume = 0

    @property
    def target_concentration(self):
        """
        The concentration of each sample *after all transfers in nM*.
        """
        return self.__target_concentration

    @property
    def parent_concentration(self):
        """
        The concentration of the source (parent container or stock) for
        this sample *after all transfers in nM*.
        """
        return self.__parent_concentration

    @property
    def parent_container(self):
        """
        The container this container is derived from.
        """
        return self.__parent_container

    @property
    def final_volume(self):
        """
        The final volume of the container after all transfers.
        """
        return self.__volume

    @property
    def full_volume(self):
        """
        The maximum volume of this container including dead volume and volume
        that will be transferred to other containers *in ul*.
        """
        full_volume = self.__volume + self.__dead_volume \
                        + sum(self.targets.values())
        return max(full_volume, self.__min_full_volume)

    @property
    def is_final_container(self):
        """
        Does this container belong to a final ISO plate?
        """
        return self.__is_final_container

    @property
    def allows_modification(self):
        """
        Smaple and location modification is forbidden, if the container is an
        final plate container or if there clones of it (see :func:`clone`).
        """
        return self.__allows_modification

    def disable_modification(self):
        """
        Blocks sample and location modifications for this container.
        Cannot be reversed.
        Use :attr:`allow_sample_modification` to request the current status.

        Is invoked when creating a clone. Do not invoke from outside this class.
        """
        self.__allows_modification = False

    @property
    def location(self):
        """
        The rack positions or sector index for this container.
        """
        return getattr(self, self.LOCATION_ATTR_NAME)

    def set_location(self, location, plate_marker):
        """
        Assigns a location on a plate to this preparation container.

        :raises AttributeError: If the container sample data must not be
            altered.
        """
        if not self.__allows_modification:
            msg = 'The data of this container must not be altered!'
            raise AttributeError(msg)

        setattr(self, self.LOCATION_ATTR_NAME, location)
        self.plate_marker = plate_marker

    @property
    def from_stock(self):
        """
        Is the pool derived from the stock?
        """
        return (self.__parent_container is None)

    @property
    def stock_concentration(self):
        """
        The stock concentration is the :attr:`parent_concentration` of
        the earliest ancestor. The unit is *nM*.
        """
        if self.__parent_concentration is None:
            return self.__parent_concentration
        else:
            return self.__parent_container.stock_concentration

    @property
    def starting_concentration(self):
        """
        The starting concentration is the :attr:`target_concentration` of
        the earliest ancestor. The unit is *nM*.
        """
        if self.__parent_container is None:
            return self.__target_concentration
        else:
            return self.__parent_container.starting_concentration

    @property
    def starting_volume(self):
        """
        The starting volume is the :attr:`full_volume` of the earliest
        ancestor. The unit is *ul*.
        """
        if self.__parent_container is None:
            return round(self.full_volume, 1)
        else:
            return self.__parent_container.starting_volume

    def get_ancestors(self):
        """
        Returns the ancestor line of this container. The parent container is
        the first one in the list, the grandparent the second one, etc.
        """
        if self.__parent_container is None:
            return []
        else:
            return [self.__parent_container] \
                    + self.__parent_container.get_ancestors()

    def get_descendants(self):
        """
        Returns the target containers an all its targets (iterativly) of this
        container.
        """
        if len(self.targets) < 1:
            return []
        else:
            descendants = []
            for child_container in sorted(self.targets.keys()):
                descendants += [child_container]
                descendants.extend(child_container.get_descendants())
            return descendants

    def get_intraplate_ancestor_count(self):
        """
        For containers that originate from a different plate or the stock
        the number is 0. If only the first ancestor comes from the same plate
        the number is 1. If also the ancestor of the ancestors is from the
        same plate the number is 2, etc.
        This number is used to keep the planned transfers (dilution series)
        in order.
        """
        if self.__parent_container is None:
            return 0
        elif not self.__parent_container.plate_marker == self.plate_marker:
            return 0
        else:
            parent_ancestor_count = self.__parent_container.\
                                    get_intraplate_ancestor_count()
            return parent_ancestor_count + 1

    @classmethod
    def create_final_plate_container(cls, location, volume,
                            target_concentration, parent_concentration, **kw):
        """
        Factory method creating an final ISO plate container.
        """
        kw['is_final_container'] = True
        kw['volume'] = volume
        kw['target_concentration'] = target_concentration
        kw['parent_concentration'] = parent_concentration
        container = cls(**kw)
        setattr(container, cls.LOCATION_ATTR_NAME, location)
        return container

    def set_parent_container(self, parent_container):
        """
        Sets a parent container for this container. The both containers are
        linked an the volumes are adjusted if necessary.
        """
        self.adjust_transfer_data(parent_container)
        self.__parent_container = parent_container
        self.__parent_concentration = \
                            self.__parent_container.target_concentration

    def adjust_transfer_data(self, parent_container):
        """
        Determines the volume that is transferred from the given parent
        container to this container using the target concentrations of both
        containers and the :attr:`full_volume` of this container.

        If there is already a transfer for this combination the transfer
        volume might be increased if necessary (iteratively for all parents
        in the line). The volume is not decreased below the minimum transfer
        volume for this container.

        Do not invoke from outside the class.
        """
        transfer_volume = get_transfer_volume(
                            source_conc=parent_container.target_concentration,
                            target_conc=self.__target_concentration,
                            target_vol=self.full_volume)

        transfer_volume = min(transfer_volume, self.__min_transfer_volume)
        parent_container.targets[self] = transfer_volume
        grand_parent_container = parent_container.parent_container
        if grand_parent_container is not None:
            parent_container.set_transfer_volumes(grand_parent_container)

    def increase_min_full_volume(self, new_volume):
        """
        Is only allowed if modification is enabled (non-final containers
        without clones). The volume must not be lower than the current one.

        :param new_volume: The new volume *in ul*.
        :type new_volume: positive number

        :raises ValueError: If the new volume is not larger than the current
            one.
        :raises AttributeError: If modification of this container is not
            allowed.
        """
        if not self.__allows_modification:
            raise AttributeError('Volume adjustments for this container ' \
                                 'are blocked!')

        if not is_larger_than(new_volume, self.__min_full_volume):
            msg = 'The new minimum volume (%s ul) must be larger than the ' \
                  'current one (%s ul).' % (get_trimmed_string(new_volume),
                                get_trimmed_string(self.__min_full_volume))
            raise ValueError(msg)

        self.__volume = new_volume
        if not self.__parent_container is None:
            self.adjust_transfer_data(self.__parent_container)

    def set_dead_volume(self, dead_volume):
        """
        Is only allowed if modification of the container is not blocked. Also
        adjusts the transfer data if there is a parent container registered.

        :param dead_volume: The new dead volume *in ul*.
        :type dead_volume: positive number

        :raises AttributeError: If the container reflects an final position.
        """
        if not self.__allows_modification:
            raise AttributeError('Adjusting the dead volume for this ' \
                                 'container is not allowed!')

        self.__dead_volume = dead_volume
        if not self.__parent_container is None:
            self.adjust_transfer_data(self.__parent_container)

    def _get_subclass_specific_keywords(self):
        """
        Generates a keyword dictionary that is used to create new objects
        of this class. The dictionary shall only contain subclass-specific
        keywords and values (the value are the values of this object).
        The location attribute must *not* be included.
        """
        raise NotImplementedError('Abstract method.')

    def create_prep_copy(self, target_concentration, dead_volume):
        """
        Creates a copy of this container that does not include location data.
        The resulting container is always part of a preparation plate with
        a starting volume of 0 (dead volumes and volumes for transfers are
        recorded separately).

        :param target_concentration: The concentration for the preparation
            container *in nM*.
        :type target_concentration: positive number

        :param dead_volume: The dead volume of the current reservoir specs
            container *in ul*.
        :type dead_volume: positive number
        """
        kw = self._get_subclass_specific_keywords()
        kw['volume'] = 0
        kw['target_concentration'] = target_concentration
        kw['parent_concentration'] = self.__parent_concentration
        kw['is_final_container'] = False
        prep_container = self.__class__(**kw)
        prep_container.set_dead_volume(dead_volume)
        return prep_container

    def get_clones(self, copy_number):
        """
        Used if the copy number for the requested container is bigger than 1.
        Creates as many copies of this container until the number of containers
        is equal to the requested copy number. The locations are the same for
        all containers.
        Child containers (in the :attr:`targets` map) are cloned as well.

        :param copy_number: The number of copies that must be filled
            by the preparation container (larger than 1!).
        :type copy_number: integer bigger than 1

        :raises ValueError: If the number of copies is smaller or equal 1.

        :returns: The list of x container clones where x is the copy number.
        """
        if not copy_number > 1:
            msg = 'The number of copies must be larger than 1!'
            raise ValueError(msg)

        clones = [self]
        while len(clones) < copy_number:
            clone = self.clone()
            clones.append(clone)
        return clones

    def clone(self):
        """
        Helper method returning a clone of this container. Child containers
        (in the :attr:`target_maps` are cloned as well.
        """
        kw = self._get_subclass_specific_keywords()
        kw['volume'] = self.__volume
        kw['target_concentration'] = self.__target_concentration
        kw['parent_concentration'] = self.__parent_concentration
        kw[self.LOCATION_ATTR_NAME] = self.location
        clone = self.__class__(**kw)

        for child_container in self.targets.keys():
            child_clone = child_container.clone()
            child_clone.set_parent_container(clone)

        clone.disable_modification()
        return clone

    def _to_iso_plate_position(self, pos_cls, rack_pos, pool, position_type,
                               transfer_targets, **kw):
        """
        Creates an :class:`FinalLabIsoPosition` or
        :class:`LabIsoPrepPosition` for an final ISO layout.
        """
        base_kw = self.__get_iso_plate_position_base_kw(rack_pos, pool,
                                  position_type, transfer_targets)
        base_kw.update(kw)
        return pos_cls(**kw)

    def __get_iso_plate_position_base_kw(self, rack_pos, pool, position_type,
                                         transfer_targets):
        if self.from_stock:
            stock_tube_barcode = LabIsoPosition.TEMP_STOCK_DATA
            stock_rack_marker = LabIsoPosition.TEMP_STOCK_DATA
        else:
            stock_tube_barcode = None
            stock_rack_marker = None
        return dict(rack_position=rack_pos, molecule_design_pool=pool,
                    position_type=position_type, volume=self.full_volume,
                    concentration=self.__target_concentration,
                    transfer_targets=transfer_targets,
                    stock_tube_barcode=stock_tube_barcode,
                    stock_rack_marker=stock_rack_marker)

    def get_buffer_volume(self):
        """
        Returns the buffer volume that is required to obtain the full volume
        for this container. The buffer volume is the difference from full
        volume and transfer volume.
        """
        if self.__parent_container is None:
            transfer_vol = get_transfer_volume(
                           source_conc=self.__parent_concentration,
                           target_conc=self.__target_concentration,
                           target_vol=self.full_volume)
        else:
            transfer_vol = self.__parent_container.targets[self]

        buffer_volume = self.full_volume - transfer_vol
        return round(buffer_volume, 1)

    def get_planned_transfers(self):
        """
        Converts the data from the :attr:`targets` map into
        :class:`PlannedTransfer` objects.
        The planned transfers are mapped onto the plate markers of the
        child containers.
        """
        transfers = dict()
        for child_container, transfer_vol in self.targets.iteritems():
            kw = self._get_planned_transfer_kw(child_container)
            vol = round(transfer_vol, 1)
            kw['volume'] = vol
            pt = self._PLANNED_TRANSFER_CLS.get_entity(**kw)
            add_list_map_element(transfers, child_container.plate_marker, pt)

        return transfers

    def _get_planned_transfer_kw(self, child_container):
        """
        Returns the keyword dictionary required to initialize an object
        of the supported :attr:
        """
        raise NotImplementedError('Abstract method.')

    def __cmp__(self, other):
        """
        The objects are sorted by :attr:`parent_concentration`. If the
        concentrations are the same they are sorted by location.
        """
        if is_smaller_than(self.__parent_concentration,
                           other.parent_concentration):
            return -1
        elif is_larger_than(self.__parent_concentration,
                            other.parent_concentration):
            return 1
        else:
            self_value = getattr(self, self.LOCATION_ATTR_NAME)
            other_value = getattr(other, self.LOCATION_ATTR_NAME)
            return cmp(self_value, other_value)

    @property
    def temp_id(self):
        """
        A unique temporary ID. This is not persisted but only used to safely
        distinguish different container objects with equal values.
        """
        return self.__id

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__id == other.temp_id

    def __hash__(self):
        return hash(self.__id)

    def __str__(self):
        return self.__id

    def __repr__(self):
        str_format = '<%s volume: %s ul, target concentration: %s nM, ' \
                     'parent concentration: %s nM>'
        params = (self.__class__.__name__, get_trimmed_string(self.__volume),
                  get_trimmed_string(self.__target_concentration),
                  get_trimmed_string(self.__parent_concentration))
        return str_format % params


class SectorContainer(_LocationContainer):
    """
    The locations for these container are rack sectors.
    """

    LOCATION_ATTR_NAME = 'sector_index'
    _PIPETTING_SPECS_NAME = PIPETTING_SPECS_NAMES.CYBIO
    _PLANNED_TRANSFER_CLS = PlannedRackSampleTransfer

    def __init__(self, number_sectors, **kw):
        """
        Constructor

        :param number_sectors: The total number of rack sectors for a final ISO
            plate (usually 1 or 4).
        :type number_sectors: positive integer
        """
        _LocationContainer.__init__(self, **kw)
        self.__number_sectors = number_sectors
        self.sector_index = None

    def _get_subclass_specific_keywords(self):
        return dict(number_sectors=self.__number_sectors)

    def create_aliquot_position(self, iso_request_pos, transfer_targets):
        """
        Creates a particular :class:`FinalLabIsoPosition` for a final ISO
        plate layout using the data of an :class:IsoRequestPosition`.
        """
        from_job = iso_request_pos.is_floating
        return self._to_iso_plate_position(pos_cls=FinalLabIsoPosition,
                            rack_pos=iso_request_pos.rack_position,
                            pool=iso_request_pos.molecule_design_pool,
                            position_type=iso_request_pos.position_type,
                            transfer_targets=transfer_targets,
                            from_job=from_job,
                            sector_index=self.sector_index)

    def create_preparation_position(self, iso_request_pos, preparation_targets,
                                    external_targets, sector_index):
        """
        Creates a particular :class:`LabIsoPrepPosition` for an preparation
        plate layout using the data of an :class:IsoRequestPosition`.
        """
        return self._to_iso_plate_position(pos_cls=LabIsoPrepPosition,
                            rack_pos=iso_request_pos.rack_position,
                            pool=iso_request_pos.molecule_design_pool,
                            position_type=iso_request_pos.position_type,
                            transfer_targets=preparation_targets,
                            external_targets=external_targets,
                            sector_index=sector_index)

    def _get_planned_transfer_kw(self, child_container):
        return dict(source_sector_index=self.sector_index,
                    target_sector_index=child_container.sector_index,
                    sector_number=self.__number_sectors)

    def __repr__(self):
        str_format = '<%s sector index: %s, volume: %s ul, target ' \
                     'concentration: %s nM, parent concentration: %s nM>'
        params = (self.__class__.__name__, self.sector_index,
                  get_trimmed_string(self.__volume),
                  get_trimmed_string(self.__target_concentration),
                  get_trimmed_string(self.__parent_concentration))
        return str_format % params


class RackPositionContainer(_LocationContainer):
    """
    The locations for these container are rack position.

    Since a rack position can only contain one sample, we also store pool and
    position type data.
    """

    LOCATION_ATTR_NAME = 'rack_position'
    _PIPETTING_SPECS_NAME = PIPETTING_SPECS_NAMES.BIOMEK
    _PLANNED_TRANSFER_CLS = PlannedSampleTransfer

    def __init__(self, pool, position_type, **kw):
        """
        Constructor

        :param pool: the molecule design pool for this position
        :type pool: :class:`thelma.models.moleculedesig.MoleculeDesignPool`

        :param position_type: see :class:`MoleculeDesignPoolParameters`
        :type position_type: :class:`str`
        """
        _LocationContainer.__init__(self, **kw)
        self.pool = pool
        self.position_type = position_type
        self.rack_position = None

    def _get_subclass_specific_keywords(self):
        return dict(pool=self.pool,
                    position_type=self.position_type)

    @classmethod
    def from_iso_request_position(cls, iso_request_pos, stock_concentration):
        """
        Factory method creating an ISO plate rack position container from an
        :class:`IsoRequestPosition`.
        """
        kw = dict(pool=iso_request_pos.molecule_design_pool,
                  position_type=iso_request_pos.position_type)
        return cls.create_final_plate_container(
                  location=iso_request_pos.rack_position,
                  volume=iso_request_pos.iso_volume,
                  target_concentration=iso_request_pos.iso_concentration,
                  parent_concentration=stock_concentration, **kw)

    @classmethod
    def from_iso_plate_position(cls, plate_pos, stock_concentration):
        """
        Factory method creating an rack position container with immutable
        volume from an :class:`LabIsoPosition`.
        """
        container = cls(rack_position=plate_pos.rack_position,
                pool=plate_pos.molecule_design_pool,
                position_type=plate_pos.position_type,
                target_concentration=plate_pos.concentration,
                parent_concentration=stock_concentration,
                volume=plate_pos.volume)
        container.disable_modification()
        return container

    def create_final_iso_plate_position(self, transfer_targets, from_job):
        """
        Creates a particular :class:`FinalLabIsoPosition` for a final ISO
        plate layout.
        """
        return self._to_iso_plate_position(pos_cls=FinalLabIsoPosition,
                            rack_pos=self.rack_position,
                            pool=self.pool, position_type=self.position_type,
                            transfer_targets=transfer_targets,
                            from_job=from_job)

    def create_preparation_position(self, preparation_targets, final_targets):
        """
        Creates a particular :class:`LabIsoPrepPosition` for an preparation
        plate layout from a :class:`PoolContainer` object.
        """
        return self._to_iso_plate_position(LabIsoPrepPosition,
                            rack_pos=self.rack_position,
                            pool=self.pool, position_type=self.position_type,
                            transfer_targets=preparation_targets,
                            external_targets=final_targets)

    def _get_planned_transfer_kw(self, child_container):
        return dict(source_position=self.rack_position,
                    target_position=child_container.rack_position)


class _PlateContainer(object):
    """
    This is an abstract helper storage class that reflects a preparation plate
    (for an ISO or ISO job). It stores and helps to distribute
    :class:`_LocationContainer` objects.
    """

    def __init__(self, plate_marker, available_locations):
        """
        Constructor

        :param plate_marker: Contains the role and a plate number. Is generated
            by :func:`LABELS.create_plate_marker`.
        :type plate_marker: :class:`str`

        :param available_locations: All possible locations. Each location can
            take up a container. The nature of the location depends on the
            subclass.
        :type available_locations: sector index or :class:`RackPosition`
            (depending on the subclass).
        """
        if isinstance(self, _PlateContainer):
            raise NotImplementedError('Abstract class.')

        self.plate_marker = plate_marker

        #: The containers for each possible locations. Locations without
        #: containers are also part of the map, there values is *None* then.
        self._location_map = dict()
        self._empty_locations = []

        for location in available_locations:
            self._location_map[location] = None
            self._empty_locations.append(location)

    def has_empty_locations(self):
        """
        Are there still some unoccupied locations left in this plate?
        """
        return (len(self._empty_locations) > 0)

    def is_empty_location(self, location):
        """
        Returns *True* if the given location is still unassigned.
        """
        return (self._location_map[location] is None)

    def get_locations(self):
        """
        Regards all locations for this plate container (regardless of whether
        there is a container assigned to them).
        """
        return self._location_map.keys()

    def get_container_for_location(self, location):
        """
        Returns the container that is stored for the specified location.
        """
        return self._location_map[location]

    def get_containers(self):
        """
        Returns all location containers for this plate container.
        """
        containers = []
        for container in self._location_map.values():
            if container is None: continue
            containers.append(container)
        return containers

    def set_container(self, container, location=None):
        """
        Adds the given container to this plate container. You can specify a
        location for the container, otherwise the plate will find a location
        by itself.

        :param container: The container to add to this plate.
        :type container: :class:_LocationContainer` subclass

        :param location: The location for the container.
        :type location: depends on the subclass
        :default location: *None*

        :raises ValueError: If the specified location is already occupied.
        """
        if location is None:
            self._find_location(container)
        elif not self.is_empty_location(location):
            raise ValueError('Location "%s" is already occupied!' % location)

        self._location_map[location] = container
        self._empty_locations.remove(location)
        container.set_location(location, self.plate_marker)

    def _find_location(self, container):
        """
        Finds an empty location for a container that shall be added.
        """
        raise NotImplementedError('Abstract method.')

    def __hash__(self):
        return hash(self.plate_marker)

    def __eq__(self, other):
        return isinstance(other, self.__class__) and \
               self.plate_marker == other.plate_marker

    def str(self):
        return self.plate_marker

    def __repr__(self):
        str_format = '<%s %s>'
        params = (self.__class__.__name__, self.plate_marker)
        return str_format % params


class SectorPlateContainer(_PlateContainer):
    """
    A plate container dealing with sectors (:class:`SectorContainer` objects).
    """

    def _find_location(self, container):
        """
        For sector locations we simple take the first empty sector.

        :raises AttributeError: If there is no empty location left.
        """
        for location in sorted(self._location_map):
            if self._location_map[location] is None:
                return location

        raise AttributeError('There is no empty sectors left!')


class RackPositionPlateContainer(_PlateContainer):
    """
    A plate container dealing with separate rack positions
    (:class:`RackPositionContainer` objects).

    When searching locations for new containers we try to assign a row
    with other containers having the same pool.
    """

    def __init__(self, plate_marker, available_locations):
        """
        Constructor

        :param plate_marker: Contains the role and a plate number. Is generated
            by :func:`LABELS.create_plate_marker`.
        :type plate_marker: :class:`str`

        :param available_locations: All possible locations. Each location can
            take up a container. The nature of the location depends on the
            subclass.
        :type available_locations: sector index or :class:`RackPosition`
            (depending on the subclass).
        """
        _PlateContainer.__init__(self, plate_marker=plate_marker,
                                 available_locations=available_locations)

        #: Stores the row indices a pool occurs in.
        self.__pool_row_map = dict()
        #: Stores the empty rack positions for a row index.
        self.__row_map = dict()
        for rack_pos in self._empty_locations:
            add_list_map_element(self.__row_map, rack_pos.row_index, rack_pos)

        #: Contains all rows that are completely empty.
        self.__empty_rows = sorted(self.__row_map.keys())

    def _find_location(self, container):
        """
        If possible we try to put containers for the same pool into the
        same row.
        """
        pool = container.pool

        if self.__pool_row_map.has_key(pool):
            row_indices = self.__pool_row_map[pool]
            for row_index in sorted(row_indices):
                rack_pos = self.__get_position(row_index)
                if rack_pos is not None:
                    break
        else:
            row_index = self.__empty_rows[0]
            rack_pos = self.__get_position(row_index)

        self.__pick_position(row_index, rack_pos, pool)
        return rack_pos

    def __get_position(self, row_index):
        """
        Returns an empty position for the given row index.
        """
        if self.__row_map.has_key(row_index):
            positions = self.__row_map[row_index]
            return positions[0]

        return None

    def __pick_position(self, row_index, rack_pos, pool):
        """
        Stores row index and pool in the pool map and removes the rack
        position form the empty position collections.
        """
        if not self.__pool_row_map.has_key(pool):
            self.__pool_row_map[pool] = set()
        self.__pool_row_map[pool].add(row_index)

        positions = self.__row_map[row_index]
        positions.remove(rack_pos)
        if len(positions) < 1: del self.__row_map[row_index]

        if row_index in self.__empty_rows:
            self.__empty_rows.remove(row_index)


class _LocationAssigner(object):
    """
    Helper object that finds preparation routes for a group of
    :class:`_LocationContainers` assuming particular :class:`ReservoirSpecs`.

    The assignment is done in 2 steps. With :func:`add_containers` you find
    determine volumes, concentrations and relationships of containers.
    :func:`process_preparation_containers` then finds location in preparation
    plates (if preparation containers are required).
    """

    #: Used to get the :class:`PipettingSpecs` for the assigner for all
    #: but stock transfers.
    _PIPETTING_SPECS_NAME = None
    #: Used to get the :class:`PipettingSpecs` for the assigner fro transfers
    #: from the stock.
    _STOCK_PIPETTING_SPECS_NAME = None

    #: The :class:`_PlateContainer` subclass for preparation plates.
    _PLATE_CONTAINER_CLS = _PlateContainer
    #: The role of the preparation plate (ISO or job preparation,
    #: default: ISO preparation).
    _PREP_PLATE_ROLE = LABELS.ROLE_PREPARATION_ISO

    def __init__(self, prep_reservoir_specs, final_plate_dead_vol):
        """
        Constructor

        :param prep_reservoir_specs: The reservoir specs for this run.
        :type prep_reservoir_specs: :class:`ReservoirSpecs`

        :param final_plate_dead_vol: The dead volume of the final plates
            *in ul*.
        :type final_plate_dead_vol: positive number, unit ul
        """
        if isinstance(self, _LocationAssigner):
            raise NotImplementedError('Abstract method.')

        #: The :class:`ReservoirSpecs` for potential preparation plates.
        self._prep_specs = prep_reservoir_specs
        #: The dead volume of the final plates  *in ul*.
        self.__final_plate_dead_vol = final_plate_dead_vol

        #: The target containers that shall be prepared (usually final plate
        #: containers).
        self.__requested_containers = []

        #: The :class:`PipettingSpecs` define the minimum transfer volume,
        #: the maximum dilution factor and whether dead volumes are dynamic.
        #: These specs are used for all but transfers from the stock.
        self.__robot_specs_std = get_pipetting_specs(self._PIPETTING_SPECS_NAME)
        #: The :class:`PipettingSpecs` define the minimum transfer volume,
        #: the maximum dilution factor and whether dead volumes are dynamic.
        #: #: These specs are used for transfers from the stock.
        self.__robot_specs_stock = get_pipetting_specs(
                                            self._STOCK_PIPETTING_SPECS_NAME)
        #: The maximum dilution factor for the pipetting specs.
        self.__max_dilution_factors = dict()
        #: The minimum volume for a transfer *in ul*.
        self.__min_transfer_volumes = dict()
        for robot_specs in (self.__robot_specs_std, self.__robot_specs_stock):
            self.__max_dilution_factors[robot_specs] = get_max_dilution_factor(
                                                       robot_specs)
            self.__min_transfer_volumes[robot_specs] = get_min_transfer_volume(
                                                       robot_specs)
        #: Maps the different batches of requested containers onto an
        #: identifier. Used to record the order of the batches.
        self.__identifier_map = dict()

        #: The minimum dead volume for potential preparation plates *in ul*.
        self.__prep_dead_vol = self._prep_specs.min_dead_volume \
                               * VOLUME_CONVERSION_FACTOR # in ul
        #: The locations available on potential preparation plates.
        self.__available_prep_locations = self._get_locations_for_prep_specs()

        #: The preparation containers mapped onto full volumes.
        self.__volume_map = None

        #: The preparation containers that have been created.
        self._prep_containers = None
        #: The preparation containers sorted by generation (final containers
        #: first).
        self.__sorted_prep_containers = None
        #: The preferred location for each preparation container.
        self.__preferred_prep_locations = None
        #: The :class:`_PlateContainer` for each preparation plate, mapped
        #: onto plate marker.
        self.__prep_plate_containers = None

    @property
    def preparation_reservoir_specs(self):
        """
        The :class:`ReservoirSpecs` for potential preparation plates.
        """
        return self._prep_specs

    def _get_locations_for_prep_specs(self):
        """
        Returns all possible locations for the preparation plate reservoir
        specs - used to assign locations for preparation containers.
        """
        raise NotImplementedError('Abstract method.')

    def add_containers(self, requested_containers, identifier, number_copies=1):
        """
        Multiplies and registers the passed containers and finds preparation
        routes for them. The volume and concentration of the requested
        containers are blocked for modifications.

        :param requested_containers: The containers for which to find
            preparation routes (without copies).
        :type requested_containers: :class:`list` of :class:`_LocationContainer`
            objects

        :param identifier: Used to mark containers belonging together. Will be
            used for sorting when distributing preparation locations.

        :param number_copies: Each requested container will be multiplied until
            the number of copies is reached.
        :type number_copies: :class:`int`
        :default number_copies: 1
        """
        if self._prep_containers is None:
            self._prep_containers = []
            self.__preferred_prep_locations = dict()

        # store containers for this ID mapped onto target conc
        prep_containers = dict()
        final_containers = dict()

        if number_copies > 1:
            starting_containers = []
            for container in requested_containers:
                if not container.parent_container is None: continue
                # child containers are covered by the get_clones method
                clones = container.get_clones(number_copies)
                starting_containers.extend(clones)
                for clone in clones:
                    starting_containers.extend(clone.get_descendants())
            requested_containers = starting_containers

        for container in requested_containers:
            add_list_map_element(final_containers,
                                 container.target_concentration, container)

        not_from_stock = []
        from_stock = []
        for requested_container in requested_containers:
            if requested_container.from_stock:
                stock_list = from_stock
            else:
                stock_list = not_from_stock
            self.__requested_containers.append(requested_container)
            stock_list.append(requested_container)

        # first we check whether smaller concentrations, if possible we derive
        # them from larger concentration among the requested containers
        not_from_stock.sort()
        for container in not_from_stock:
            self.__prepare_container(container, prep_containers,
                                     final_containers)

        # second we check the volumes that are directly derived from the stock
        from_stock.sort(reverse=True)
        for container in from_stock:
            self.__prepare_container(container, prep_containers,
                                     final_containers)

        # store all containers
        all_containers = requested_containers + prep_containers.values()
        self.__identifier_map[identifier] = all_containers

    def __prepare_container(self, container, prep_containers,
                            requested_containers):
        """
        Finds or creates a parent container for the given container (if it
        cannot be derived directly from the stock) and stores them in the
        container map.
        """
        final_container = self.__find_suitable_source_container(container,
                                                     requested_containers)
        has_valid_origin = (final_container is not None)
        if has_valid_origin:
            container.set_parent_container(final_container)
        while not has_valid_origin:
            prep_container = self.__find_suitable_source_container(container,
                                                             prep_containers)
            if prep_container is not None:
                container.set_parent_container(prep_container)
                has_valid_origin = True
            elif not self.__requires_intermediate_position(container):
                has_valid_origin = True
            else:
                prep_container = self.__create_new_prep_container(container,
                                                         container.location)
                self.__store_prep_container(prep_containers, prep_container)
                container = prep_container

    def __store_prep_container(self, container_map, prep_container):
        """
        Stores the preparation container and its parent container in the map
        (mapped onto target concentrations).
        """
        add_list_map_element(container_map, prep_container.target_concentration,
                             prep_container)
        if prep_container.parent_container is not None:
            self.__store_prep_container(container_map,
                                        prep_container.parent_container)

    def __requires_intermediate_position(self, target_container,
                                         source_container=None):
        """
        Checks whether the dilution from parent to target concentration of
        a container can be reached in one dilution step. The dilution factor
        is always checked. If modification of the target container is not
        allowed the check will include also transfer and buffer volume.

        :raises ValueError: if the parent concentration is smaller than the
            target concentration
        """
        target_conc = target_container.target_concentration
        if source_container is None:
            parent_conc = target_container.parent_concentration
        else:
            parent_conc = source_container.target_concentration
        if target_container.from_stock:
            robot_specs = self.__robot_specs_stock
        else:
            robot_specs = self.__robot_specs_std
        min_transfer_volume = self.__min_transfer_volumes[robot_specs]
        max_dilution_factor = self.__max_dilution_factors[robot_specs]

        dil_factor = parent_conc / float(target_conc)
        if is_larger_than(dil_factor, max_dilution_factor):
            return True
        elif is_smaller_than(dil_factor, 1):
            msg = 'The parent concentration (%s nM is smaller than the ISO ' \
                  'concentration (%s)!' % (get_trimmed_string(parent_conc),
                                           get_trimmed_string(target_conc))
            raise ValueError(msg)
        elif are_equal_values(dil_factor, max_dilution_factor):
            return False

        if not target_container.allows_modification:
            if not source_container is None and \
                                    not source_container.allows_modification:
                if is_smaller_than(source_container.final_volume,
                                   self.__final_plate_dead_vol):
                    return True
            full_volume = target_container.full_volume
            transfer_volume = full_volume / dil_factor
            if is_smaller_than(transfer_volume, min_transfer_volume):
                return True
            dilution_vol = full_volume - transfer_volume
            if is_smaller_than(dilution_vol, min_transfer_volume):
                return True

        return False

    def __find_suitable_source_container(self, container,
                                         potential_src_containers):
        """
        Returns a potential source container from the list of potential
        containers. The potential containers are assumed to allow volume
        modifications.
        """
        for src_conc in sorted(potential_src_containers.keys(reverse=True)):
            src_containers = potential_src_containers[src_conc]
            if is_smaller_than(src_conc, container.target_concentration):
                continue
            for src_container in src_containers:
                if not self.__requires_intermediate_position(container,
                                                             src_conc):
                    self.__adjust_volume_of_existing_prep(container,
                                                          src_container)
                    return src_container

        return None

    def __adjust_volume_of_existing_prep(self, target_container, src_conc):
        """
        If we add a new target container to an exisiting preparation container
        we might have to increase the source position volume if the transfer
        volume is not suitable. This might be the case if there are differing
        target concentrations for the source container children and the
        new target concentration is much larger (= transfer_volume is smaller).
        In this case we increase the minimum full volume of the target
        container.
        """
        if target_container.allows_modification:
            min_transfer_vol = self.__min_transfer_volumes[
                                                    self.__robot_specs_std]
            target_conc = target_container.target_concentration
            dil_factor = src_conc / target_conc
            transfer_vol = get_transfer_volume(src_conc, target_conc,
                                    target_container.full_volume, dil_factor)
            if is_smaller_than(transfer_vol, min_transfer_vol):
                corr_factor = min_transfer_vol / transfer_vol
                self.__adjust_volume(corr_factor, transfer_vol, dil_factor,
                                     target_container)

    def __create_new_prep_container(self, target_container, preferred_location):
        """
        The new container serves as parent for the target container. The
        preferred location can be *None*.
        """
        prep_container = self.__determine_preparation_values(target_container)
        target_container.set_parent_container(prep_container)
        self.__preferred_prep_locations[prep_container] = preferred_location
        return prep_container

    def __determine_preparation_values(self, target_container):
        """
        Determines volume and concentration for a new preparation container
        that serves as source for the given container. Dilution factor, transfer
        and buffer volume must all be valid.
        If the target container allows modification its volume might be raised
        to achieve valid volumes for transfers. Otherwise the source container
        dilution is adjusted (this might also include the preparation of
        further ancestor containers).

        It is not checked whether the volumes exceed the maximum transfer
        volumes because this problem can be solved by multiple transfers.
        """
        parent_conc = target_container.parent_concentration
        target_conc = target_container.target_concentration
        target_vol = target_container.full_volume
        dil_factor = parent_conc / target_conc

        min_transfer_vol = self.__min_transfer_volumes[self.__robot_specs_std]
        max_dil_factor = self.__max_dilution_factors[self.__robot_specs_std]
        dil_factor = min(dil_factor, max_dil_factor)
        transfer_vol = get_transfer_volume(parent_conc, target_conc,
                                           target_vol, dil_factor)
        if is_smaller_than(transfer_vol, min_transfer_vol):
            if target_container.allows_modification:
                corr_factor = min_transfer_vol / transfer_vol
                transfer_vol, target_vol = self.__adjust_volume(corr_factor,
                                transfer_vol, dil_factor, target_container)
            else:
                dil_factor = target_vol / min_transfer_vol
                # the new dilution factor is smaller than the old one
                parent_conc = dil_factor / target_conc

        buffer_vol = target_vol - transfer_vol
        if is_smaller_than(buffer_vol, min_transfer_vol):
            corr_factor = min_transfer_vol / buffer_vol
            if target_container.allows_modification:
                self.__adjust_volume(corr_factor, transfer_vol, dil_factor,
                                     target_container)
            else:
                new_parent_conc = parent_conc * corr_factor
                new_dil_factor = new_parent_conc / target_conc
                if is_larger_than(new_dil_factor, max_dil_factor):
                    # in this case we prepare a new preparation position
                    # with exactly the same concentration as the target aliquot
                    parent_conc = target_conc

        new_prep_container = target_container.create_prep_copy(
                                      target_concentration=parent_conc,
                                      dead_volume=self.__prep_dead_vol)
        if are_equal_values(parent_conc, target_conc):
            self.__create_new_prep_container(new_prep_container, None)
        return new_prep_container

    def __adjust_volume(self, corr_factor, transfer_volume,
                        dil_factor, target_container):
        """
        Is used to increase the minimum full volume for a preparation container.
        """
        new_transfer_vol = corr_factor * transfer_volume
        new_target_vol = round_up(new_transfer_vol, 1) * dil_factor
        target_container.increase_min_full_volume(new_target_vol)
        return new_transfer_vol, new_target_vol

    def has_preparation_containers(self):
        """
        Returns *True* if there are preparation containers scheduled in this
        assigner. Assumes you have already invoked :func:`add_container` before.

        :return: boolean
        :raise AttributeError: If you did not invoke :func:`add_container`
            before.
        """
        if self._prep_containers is None:
            msg = 'The layout generation is not completed yet. Please call ' \
                  'add_containers() before.'
            raise AttributeError(msg)

        return len(self._prep_containers) > 0

    def process_preparation_containers(self):
        """
        Assumes you have finished recording target containers (see
        :func:`add_containers`). Invokes :func:`has_preparation_containers`.
        If there are preparation containers scheduled, the method adjusts the
        dead volumes (if the pipetting specs request dynamic dead volumes)
        an determines the maximum volume for all preparation containers.
        """
        if self.has_preparation_containers() and self.__volume_map is None:
            self.__sort_prep_container_by_generation()
            self.__adjust_container_dead_volumes()
            self.__create_volume_map()

    def __sort_prep_container_by_generation(self):
        """
        The generation of preparation container indicates the number of steps
        required to reach an requested container. If at least one target
        container is a requested container, the generation is 1. If no
        child but at least one grandchild is a requested container, the
        generation is 2, etc.
        Smaller generations have smaller indices in the return value list.
        """
        containers_ori_order = dict()
        found_containers = set()
        generation_map = dict()
        for requested_container in self.__requested_containers:
            ancestors = requested_container.get_ancestors()
            for i in range(ancestors):
                container = ancestors[i]
                if not container.allows_modification: continue
                add_list_map_element(generation_map, container, i)
                if not container in found_containers:
                    containers_ori_order[len(found_containers)] = container
                    found_containers.add(container)

        min_generation_map = dict()
        for order_num in sorted(containers_ori_order.keys()):
            container = containers_ori_order[order_num]
            generations = generation_map[container]
            add_list_map_element(min_generation_map, min(generations),
                                 container)

        self.__sorted_prep_containers = []
        for generation in sorted(min_generation_map.keys()):
            self.__sorted_prep_containers.extend(min_generation_map[generation])

    def __adjust_container_dead_volumes(self):
        """
        If the pipetting method requests dynamic dead volumes, this method
        will adjust the dead volume of each preparaion container (containers
        are sorted by generation).
        """
        dynamic_std = self.__robot_specs_std.has_dynamic_dead_volume
        dynamic_stock = self.__robot_specs_stock.has_dynamic_dead_volume
        if dynamic_std or dynamic_stock:
            for container in self.__sorted_prep_containers:
                if container.from_stock and dynamic_stock:
                    self.__adjust_dead_volume_for_container(container)
                elif not container.from_stock and dynamic_std:
                    self.__adjust_dead_volume_for_container(container)

    def __adjust_dead_volume_for_container(self, container):
        """
        For dynamic dead volumes, the dead volume of a container is increased
        depending on the number of transfer targets. The minimum and maximum
        dead volume are defined by the preparation reservoir specs.
        """
        num_targets = len(container.targets)
        new_dead_volume = get_dynamic_dead_volume(num_targets, self._prep_specs)
        if is_larger_than(new_dead_volume, self.__prep_dead_vol):
            add_volume = new_dead_volume - self.__prep_dead_vol
            container.adjust_dead_volume_by(add_volume)

    def __create_volume_map(self):
        """
        Sorts preparation containers by (full) volume.
        """
        self.__volume_map = dict()
        for container in self.__sorted_prep_containers:
            container_vol = container.full_volume
            container_vol = round(container_vol, 1)
            add_list_map_element(self.__volume_map, container_vol,
                                 container)

    def get_max_preparation_volume(self):
        """
        Returns the largest volume for a preparation container (in ul).
        Assumes you have already invoked :func:`add_container` and
        :func:`process_preparation_containers` before.

        :return: boolean
        :raise AttributeError: If you did not invoke :func:`add_container` and
            :func:`process_preparation_containers` before.
        """
        if self.__volume_map is None:
            msg = 'The layout generation is not completed yet. Please call ' \
                  'add_containers() and process_preparation_containers() ' \
                  'before.'
            raise AttributeError(msg)

        return max(self.__volume_map.keys())

    def get_number_preparation_plates(self):
        """
        Returns the number of preparation plates required to provide locations
        for all preparation containers.
        """
        num_prep_containers = len(self._prep_containers)
        return round_up(float(len(self.__available_prep_locations)) \
                        / num_prep_containers, 0)

    def distribute_preparation_containers(self):
        """
        Finds locations for all preparation containers. If possible the
        containers get their preferred locations.

        Assumes you have already invoked :func:`add_container` and
        :func:`process_preparation_containers` before.

        :return: boolean
        :raise AttributeError: If you did not invoke :func:`add_container` and
            :func:`process_preparation_containers` before.
        """
        if self.__volume_map is None:
            msg = 'The layout generation is not completed yet. Please call ' \
                  'add_containers() and process_preparation_containers() ' \
                  'before.'
            raise AttributeError(msg)

        self.__prep_plate_containers = dict()
        self.__create_preparation_plate_containers()
        self.__distribute_prep_containers()

    def __create_preparation_plate_containers(self):
        """
        Helper function.
        Creates :class:`_PlateContainer` objects for each preparation plate.
        """
        num_plates = self.get_number_preparation_plates()
        for i in range(num_plates):
            plate_num = i + 1
            plate_marker = LABELS.create_rack_marker(self._PREP_PLATE_ROLE,
                                                     plate_num)
            self.__create_plate_container(plate_marker)

    def __create_plate_container(self, plate_marker):
        """
        Helper function. Creates and stores a preparation plate container.
        """
        kw = dict(available_locations=self.__available_prep_locations,
                  plate_marker=plate_marker)
        plate_container = self._PLATE_CONTAINER_CLS(**kw)
        self.__prep_plate_containers[plate_marker] = plate_container

    def __distribute_prep_containers(self):
        """
        Helper function. Finds locations for all preparation containers.
        Containers are prioritized before assignment.
        """
        container_identifiers = dict()
        for identifier, containers in self.__identifier_map.iteritems():
            for container in containers:
                container_identifiers[container] = identifier

        priority_scores = self.__sort_by_stock_origin_and_location_preference()

        remaining_plates = []
        for plate_marker in sorted(self.__prep_plate_containers.keys()):
            plate = self.__prep_plate_containers[plate_marker]
            if plate.has_empty_positions():
                remaining_plates.append(plate)

        for score in sorted(priority_scores.keys(), reverse=True):
            containers = priority_scores[score]
            for container in containers:
                pref_location = self.__preferred_prep_locations[container]
                prep_plate = remaining_plates.pop(0)
                location = None
                if pref_location is not None:
                    if prep_plate.is_empty_location(pref_location):
                        location = pref_location
                prep_plate.set_container(container, location)
                if prep_plate.has_empty_locations():
                    remaining_plates.insert(0, prep_plate)

    def __sort_by_stock_origin_and_location_preference(self):
        """
        Helper function creating a score map for the storted preparation
        container. Containers with a high score should get a location first.
        Priority is raised strongly if a container sample originates from
        the stock, and slightly if it has a preferred location.
        """
        priority_scores = dict()

        for identifier in sorted(self.__identifier_map.keys()):
            containers = self.__identifier_map[identifier]
            for container in containers:
                score = 0
                if container.from_stock: score += 2
                if self.__preferred_prep_locations[container] is not None:
                    score += 1
                add_list_map_element(priority_scores, score, container)

        return priority_scores

    def get_preparation_plate_containers(self):
        """
        Returns the preparation plate containers that have been build up.
        Assumes you have already invoked
        :func:`distribute_preparation_containers` before.

        :return: The preparation containers mapped onto plate marker.
        :raise AttributeError: If you did not invoke
        :func:`distribute_preparation_containers` before
        """
        if self.__prep_plate_containers is None:
            msg = 'The layout generation is not completed yet. Please call ' \
                  'distribute_preparation_containers() before.'
            raise AttributeError(msg)

        return self.__prep_plate_containers


class SectorLocationAssigner(_LocationAssigner):
    """
    A location assigner dealing with rack sector. Use the CyBio for pipetting
    specs.
    """

    _PIPETTING_SPECS_NAME = PIPETTING_SPECS_NAMES.CYBIO
    _STOCK_PIPETTING_SPECS_NAME = _PIPETTING_SPECS_NAME
    _PLATE_CONTAINER_CLS = SectorPlateContainer

    def __init__(self, reservoir_specs, number_sectors):
        """
        Constructor

        :param prep_reservoir_specs: The reservoir specs for this run.
        :type prep_reservoir_specs: :class:`ReservoirSpecs`

        :param number_sectors: The number of rack sectors (usually 1 or 4).
        :type number_sectors: :class:`int`
        """
        _LocationAssigner.__init__(prep_reservoir_specs=reservoir_specs)
        self.number_sectors = number_sectors

    def _get_locations_for_prep_specs(self):
        """
        In this case we return a list of sector indices.
        """
        shape_size = self._prep_specs.rack_shape.size
        if shape_size == 384:
            num_sectors = self.number_sectors
        else: # 96
            num_sectors = 1
        return range(num_sectors)


class RackPositionLocationAssigner(_LocationAssigner):
    """
    This assigner deals with rack positions. It uses the Biomek specs as
    pipetting specs.
    """

    _PIPETTING_SPECS_NAME = PIPETTING_SPECS_NAMES.BIOMEK
    _STOCK_PIPETTING_SPECS_NAME = PIPETTING_SPECS_NAMES.BIOMEKSTOCK
    _PLATE_CONTAINER_CLS = RackPositionPlateContainer

    def _get_locations_for_prep_specs(self):
        """
        We do not need to check whether rack positions are already occupied
        because preparation plates are always new ones.
        """
        return get_positions_for_shape(self._prep_specs.rack_shape)


class JobRackPositionAssigner(RackPositionLocationAssigner):
    """
    This assigner deals with rack positions, but unlike the normal
    :class:`RackPositionLocationAssigner` (which deals with routes for single
    ISOs), this assigner creates preparation plates for ISO jobs.
    """

    _PREP_PLATE_ROLE = LABELS.ROLE_PREPARATION_JOB


def get_transfer_volume(source_conc, target_conc, target_vol, dil_factor=None):
    """
    Helper function determine the transfer volume (uncorrected) for a set of
    values.
    """
    if dil_factor is None:
        dil_factor = source_conc / float(target_conc)

    return target_vol / dil_factor


class _LayoutPlanner(BaseAutomationTool):
    """
    Abstract base class. The planner finds preparation routes for a bunch
    of :class:`_LocationContainers` (the classes depend on the subclass
    implementation) by means of a :class:`_LocationAssigner`. The resulting data
    is converted into :class:`LabIsoPosition` and :class:`PlannedTransfer`
    objects and stored in the :class:`LabIsoBuilder`.

    The finding of the preparation routes involves complete simulation for
    different reservoir specs (see :attr:`_AVAILABLE_RESERVOIR_SPECS_NAMES`).
    The planner will pick a spec that has capacity for all preparation
    containers. If there are several specs in question, the planner will pick
    the one smallest plate count.
    """

    #: The reservoir specs available for preparation plates in the order of
    #: desirability. The first one is the most desirable one.
    _AVAILABLE_RESERVOIR_SPECS_NAMES = [RESERVOIR_SPECS_NAMES.STANDARD_96,
                                        RESERVOIR_SPECS_NAMES.STANDARD_384,
                                        RESERVOIR_SPECS_NAMES.DEEP_96]

    def __init__(self, log, iso_request, builder):
        """
        Constructor

        :param log: The log to write into.
        :type log: :class:`thelma.ThelmaLog`

        :param iso_request: The ISO request for which to create the ISOs.
        :type iso_request: :class:`thelma.models.iso.LabIsoRequest`

        :param builder: The builder collects the data of the picked assigner.
        :type builder: :class:`LabIsoBuilder`
        """
        BaseAutomationTool.__init__(self, log=log)
        #: The ISO request for which to create the ISOs.
        self.iso_request = iso_request
        #: The builder collects the data of the picked assigner.
        self.builder = builder

        #: The :class:`_LocationContainers` for each plate location (not
        #: regarding copy numbers).
        self._requested_containers = None
        #: The requested containers that can be derived from one another
        #: mapped onto an identifier.
        self._coupled_requested_containers = None
        #: The number of copies ordered for each requested container.
        self._number_copies = None

        #: :class:`ReservoirSpecs` mapped onto their names.
        self.__reservoir_specs_map = dict()
        #: :class:`_LocationAssigner` mapped onto their :class:`ReservoirSpecs`.
        self.__location_assigners = None
        #: The location assigner to be used.
        self._picked_assigner = None

        #: The :class:`_PlateContainer` objects for the preparation plates
        #: mapped onto their plate markers.
        self._prep_plate_containers = None

    def reset(self):
        BaseAutomationTool.reset(self)
        self._number_copies = None
        self._requested_containers = dict()
        self._coupled_requested_containers = dict()
        self.__reservoir_specs_map = dict()
        self.__location_assigners = dict()
        self._picked_assigner = None
        self._prep_plate_containers = None

    def run(self):
        self.reset()
        self.add_info('Start analysis ...')

        self._check_input()

        if not self.has_errors(): self._register_requested_plate_containers()
        if not self.has_errors(): self.__find_preparation_routes()
        if not self.has_errors() and self._picked_assigner is None:
            self.__pick_location_assigner()
        if not self.has_errors(): self.__adjust_layout_builder()
        if not self.has_errors():
            self.return_value = self.builder
            self.add_info('Analysis completed.')

    def _check_input(self):
        if self._check_input_class('ISO request', self.iso_request,
                                   LabIsoRequest):
            if self._number_copies is None:
                self._number_copies = self.iso_request.number_aliquots
        self._check_input_class('ISO layout builder', self.builder,
                                LabIsoBuilder)

    def _register_requested_plate_containers(self):
        """
        Determines the containers for which to find the preparation routes
        and stores them in the :attr:`_requested_containers`.
        """
        raise NotImplementedError('Abstract method!')

    def __find_preparation_routes(self):
        """
        Figures out how to provide the desired volume and concentration for
        each requested container.
        """
        self._find_coupled_containers()

        first_specs = True
        for specs_name in self._AVAILABLE_RESERVOIR_SPECS_NAMES:
            assigner = self.__find_route_for_reservoir_specs(specs_name)
            if first_specs:
                if not assigner.has_preparation_containers():
                    self._picked_assigner = assigner
                    break
                else:
                    first_specs = False

    def __find_route_for_reservoir_specs(self, reservoir_specs_name):
        """
        Finds preparation routes for each requested container assuming a
        particular reservoir spec.
        """
        specs = get_reservoir_spec(reservoir_specs_name)
        assigner = self._init_assigner(specs)
        self.__reservoir_specs_map[reservoir_specs_name] = specs
        self.__location_assigners[specs] = assigner

        for identifier in sorted(self._coupled_requested_containers.keys()):
            requested_containers = self._coupled_requested_containers[
                                                                    identifier]
            containers = []
            for requested_container in requested_containers:
                containers.append(requested_container.clone())
            for assigner in self.__location_assigners.values():
                kw = dict(requested_containers=containers,
                          identifier=identifier,
                          number_copies=self._number_copies)
                self._run_and_record_error(meth=assigner.add_containers,
                            base_msg='Error when trying to add requested ' \
                                     'containers to location assigner: ', **kw)

    def _init_assigner(self, reservoir_specs):
        """
        Initialises the :class:`_LocationAssigner` for the given
        reservoir specs.
        """
        raise NotImplementedError('Abstract method.')

    def _find_coupled_containers(self):
        """
        Finds relationships between requested containers, that is containers
        that share the same pool (combination).
        """
        raise NotImplementedError('Abstract method.')

    def __pick_location_assigner(self):
        """
        We only want location assigners that provide volume capacity for all
        preparation containers. If there are several suitable assigner, we
        take one with the lowest number of preparation plates (priority 1)
        and highest desirability (priority 2).
        """
        full_coverage = dict()
        for specs_name in self._AVAILABLE_RESERVOIR_SPECS_NAMES:
            specs = self.__reservoir_specs_map[specs_name]
            assigner = self.__location_assigners[specs]
            self._run_and_record_error(
                            meth=assigner.process_preparation_containers,
                            base_msg='Error when trying to process ' \
                                     'preparation containers: ')

            specs_max_vol = specs.max_volume * VOLUME_CONVERSION_FACTOR
            max_vol = assigner.get_max_preparation_volume()
            if is_larger_than(max_vol, specs_max_vol): continue
            num_plates = assigner.get_number_preparation_plates()
            full_coverage[specs] = num_plates

        if len(full_coverage) < 1:
            msg = 'The volumes for some preparation containers exceed ' \
                  'the capacity of all available plate types (%s).' \
                      % (', '.join([rs.name for rs in sorted(
                                            self.__location_assigners.keys())]))
            self.add_error(msg)

        elif len(full_coverage) == 1:
            # if there is only one spec covering all containers we pick
            # this one
            specs = full_coverage.keys()[0]
            self.__set_preparation_specs(specs)

        else:
            # if there are several specs covering all containers we pick the
            # one with the lowest number of plates (priority 1) or if there are
            # several ones with an equal number we pick the one which has been
            # defined as more desirable in the
            # :attr:`_AVAILABLE_RESERVOIR_SPECS_NAMES` list.
            min_num_plates = min(full_coverage.values())
            for specs_name in self._AVAILABLE_RESERVOIR_SPECS_NAMES:
                specs = self.__reservoir_specs_map[specs_name]
                if not full_coverage.has_key(specs): continue
                num_plates = full_coverage[specs]
                if num_plates == min_num_plates:
                    self.__set_preparation_specs(specs)
                    break

    def __set_preparation_specs(self, reservoir_specs):
        """
        Also starts the assigns of preparation containers to locations.
        """
        assigner = self.__location_assigners[reservoir_specs]
        self._picked_assigner = assigner
        self._run_and_record_error(
                meth=assigner.distribute_preparation_containers,
                base_msg='Error when trying to distribute preparation ' \
                         'containers: ')
        self._prep_plate_containers = assigner.\
                                      get_preparation_plate_containers()

    def __adjust_layout_builder(self):
        """
        Converts container data to layout positions and planned transfers.
        """
        self._store_final_iso_positions()

        if self._prep_plate_containers is not None:
            plate_specs = get_rack_specs_from_reservoir_specs(
                          self._picked_assigner.preparation_reservoir_specs)
            self._create_preparation_layouts(plate_specs)

        self.__store_planned_transfers()

    def _store_final_iso_positions(self):
        """
        Generates :class:`FinalLabIsoPosition` objects from a final containers
        and adds them to the ISO layout in the :attr:`builder`. We use the
        :attr:`_requested_containers` list because it is free of clones
        and contains each requested location once.
        """
        raise NotImplementedError('Abstract method.')

    def _create_preparation_layouts(self, plate_specs):
        """
        The data for the layouts is taken from the preparation plate containers
        in the picked assigner.
        """
        raise NotImplementedError('Abstract method.')

    def __store_planned_transfers(self):
        """
        Converts the transfers from the container attributes into
        :class:`PlannedTransfer` objects.
        """
        for requested_container in self._requested_containers.values():
            self.__record_transfers_for_container(requested_container)

        if self._prep_plate_containers is not None:
            for prep_plate in self._prep_plate_containers.values():
                for container in prep_plate.get_containers():
                    self.__record_transfers_for_container(container)

    def __record_transfers_for_container(self, container):
        """
        Container dilutions are recorded if the volume is larger than 0.
        Container and rack are only recorded if there are are child containers.
        """
        if container.is_final_container:
            plate_marker = LABELS.ROLE_FINAL
        else:
            plate_marker = container.plate_marker

        # dilution
        buffer_volume = container.get_buffer_volume()
        if not are_equal_values(buffer_volume, 0):
            volume = round(buffer_volume / VOLUME_CONVERSION_FACTOR, 7)
            rack_positions = self._get_rack_positions_for_container(
                                                            container)
            for rack_pos in rack_positions:
                psd = PlannedSampleDilution.get_entity(volume=volume,
                            target_position=rack_pos, diluent_info=DILUENT_INFO)
                self.builder.add_dilution(psd, plate_marker)

        # transfer
        if len(container.targets) > 0:
            transfers = container.get_planned_transfers()
            for child_plate_marker, planned_transfers in transfers.iteritems():
                if (plate_marker == child_plate_marker):
                    intraplate_ancestors = container.\
                                           get_intraplate_ancestor_count()
                    for pt in planned_transfers:
                        self.builder.add_intraplate_transfer(pt, plate_marker,
                                                         intraplate_ancestors)
                else:
                    for pt in planned_transfers:
                        self.builder.add_interplate_transfer(pt,
                                source_plate_marker=plate_marker,
                                target_plate_marker=child_plate_marker)

    def _get_rack_positions_for_container(self, container):
        """
        Returns the rack positions for the given container. Is used to create
        the :class:`PlannedSampleDilution` objects for the passed container.
        """
        raise NotImplementedError('Abstract method.')


class SectorPlanner(_LayoutPlanner):
    """
    A special planner dealing with rack sectors. This planner is called first
    (if rack sectors can be used).
    """

    NAME = 'Sector Planner'

    def __init__(self, log, iso_request, builder, association_data,
                 sector_positions, stock_concentration):
        """
        Constructor

        :param log: The log to write into.
        :type log: :class:`thelma.ThelmaLog`

        :param iso_request: The ISO request for which to create the ISOs.
        :type iso_request: :class:`thelma.models.iso.LabIsoRequest`

        :param builder: The builder collects the data of the picked assigner.
        :type builder: :class:`LabIsoBuilder`

        :param association_data: Stores the determined values and relationship
            for the rack sectors.
        :type association_data: :class:`IsoRequestAssociationData`

        :param sector_positions: The ISO request position included in the
            sector preparation mapped onto sector indices. Sector without
            positions are not included.
        :type sector_positions: :class:`dict`

        :param stock_concentration: The stock concentration for all positions
            (usually the stock concentration for floatings) *in nM*.
        :type stock_concentration: positive number
        """
        _LayoutPlanner.__init__(self, log=log, iso_request=iso_request,
                                builder=builder)
        #: Stores the determined values and relationship for the rack sectors.
        self.association_data = association_data
        #: All ISO request positions to be included mapped onto sector indices.
        self.sector_positions = sector_positions
        #: The stock concentration for all positions *in nM*.
        self.stock_concentration = stock_concentration

        #: The number of rack sectors.
        self._number_sectors = None

        #: The rack positions for each container (used to find rack positions
        #: for the planned dilutions).
        self.__container_position_map = None

    def reset(self):
        _LayoutPlanner.reset(self)
        self._number_sectors = None
        self.__container_position_map = dict()

    def _check_input(self):
        _LayoutPlanner._check_input(self)
        if self._check_input_class('association data', self.association_data,
                                   IsoRequestAssociationData):
            self._number_sectors = self.association_data.number_sectors

        self._check_input_map_classes(self.sector_positions, 'sector map',
                    'sector index', int, 'position list', list)

        if not is_valid_number(self.stock_concentration):
            msg = 'The stock concentration must be a positive number ' \
                  '(obtained: %s).' % (self.stock_concentration)
            self.add_error(msg)

    def _register_requested_plate_containers(self):
        """
        The sectors to be prepared are part of the :attr:`association data`.
        """
        sector_concentrations = self.association_data.sector_concentrations
        parent_sectors = self.association_data.parent_sectors

        for sector_index, parent_sector in parent_sectors.iteritems():
            parent_conc = self.stock_concentration
            from_stock = (parent_sector is None)
            conc = sector_concentrations[sector_index]
            volume = self.association_data.sector_volumes[sector_index]
            if not from_stock:
                parent_conc = sector_concentrations[parent_sector]
            kw = dict(sector_index=sector_index,
                      number_sectors=self._number_sectors)
            container = SectorContainer.create_final_plate_container(
                                    volume=volume, location=sector_index,
                                    parent_concentration=parent_conc,
                                    target_concentration=conc, **kw)
            self._requested_containers[sector_index] = container

    def _init_assigner(self, reservoir_specs):
        return SectorLocationAssigner(reservoir_specs=reservoir_specs,
                                      number_sectors=self._number_sectors)

    def _find_coupled_containers(self):
        """
        The relationships of the sectors have already been determined. They
        are stored in the association data.
        """
        associated_sectors = self.association_data.associated_sectors
        c = 1
        for sectors in associated_sectors:
            c += 1
            coupled_containers = []
            for sector_index in sectors:
                container = self._requested_containers[sector_index]
                coupled_containers.append(container)
            self._coupled_requested_containers[str(c)] = coupled_containers

    def _store_final_iso_positions(self):
        """
        Uses the :attr:`sector_positions` map to determine which position
        of the sectors have to be recorded.
        """
        for container in self._requested_containers.values():
            sector_index = container.sector_index

            translators = dict()
            for child_container in container.targets.values():
                target_sector = child_container.location
                translator = RackSectorTranslator(
                                number_sectors=self._number_sectors,
                                source_sector_index=sector_index,
                                target_sector_index=target_sector,
                                behaviour=RackSectorTranslator.MANY_TO_MANY)
                translators[child_container] = translator

            for ir_pos in self.sector_positions[sector_index]:
                tts = []
                for child_container, transfer_vol in container.targets.\
                                                     iteritems():
                    translator = translators[child_container]
                    trg_pos = translator.translate(ir_pos.rack_position)
                    tt = TransferTarget(trg_pos, transfer_vol,
                                        container.plate_marker)
                    tts.append(tt)
                aliquot_pos = container.create_aliquot_position(ir_pos, tts)
                self.builder.add_final_iso_plate_position(aliquot_pos)
                add_list_map_element(self.__container_position_map, container,
                                     ir_pos.rack_position)

    def _create_preparation_layouts(self, plate_specs):
        """
        In order to determine the rack positions we use the
        :attr:`sector_positions` map and some translators.
        """
        prep_shape = self._picked_assigner.preparation_reservoir_specs.\
                     rack_shape
        aliquot_to_prep_type = 'aliquot_to_prep'
        prep_to_prep_type = 'prep_to_prep'
        prep_to_aliquot_type = 'prep_to_aliquot'
        behaviours = self.__get_behaviour_map(prep_shape, aliquot_to_prep_type,
                     prep_to_aliquot_type, prep_to_prep_type)
        translator_map = {aliquot_to_prep_type : dict(),
                           prep_to_prep_type : dict(),
                           prep_to_aliquot_type : dict()}

        for label, plate_container in self._prep_plate_containers.iteritems():
            layout = LabIsoPrepLayout(shape=prep_shape)
            for container in plate_container.get_containers():
                pos_map = self.__get_preparation_rack_positions(container,
                            translator_map, behaviours, aliquot_to_prep_type)
                prep_sector = container.location
                for src_pos, ir_pos in pos_map.iteritems():
                    final_pos = self.builder.final_iso_layout.\
                                get_working_position(ir_pos.rack_position)
                    ir_sector = final_pos.sector_index
                    prep_tts = []
                    aliquot_tts = []
                    for child, transfer_vol in container.targets.iteritems():
                        if child.is_final_container:
                            transfer_type = prep_to_aliquot_type
                            tts = aliquot_tts
                        else:
                            transfer_type = prep_to_prep_type
                            tts = prep_tts
                        target_pos = self.__get_rack_position(src_pos,
                                     prep_sector, child.location, transfer_type,
                                     behaviours, translator_map)
                        tts.append(TransferTarget(target_pos, transfer_vol,
                                                  child.plate_marker))
                    prep_pos = container.create_preparation_position(ir_pos,
                                         prep_tts, aliquot_tts, ir_sector)
                    layout.add_position(prep_pos)
                    add_list_map_element(self.__container_position_map,
                                         container, prep_pos.rack_position)

                self.builder.add_preparation_layout(label, layout, plate_specs)

    def __get_behaviour_map(self, prep_shape, aliquot_to_prep_type,
                            prep_to_aliquot_type, prep_to_prep_type):
        """
        Helper function determining the rack position translations behaviours
        for all 3 types.
        """
        aliquot_shape = self.iso_request.iso_plate_reservoir_specs.rack_shape
        prep_shape = self._picked_assigner.preparation_reservoir_specs.\
                     rack_shape
        behaviour_ali_to_prep = RackSectorTranslator.get_translation_behaviour(
                number_sectors=self._number_sectors, source_shape=aliquot_shape,
                target_shape=prep_shape)
        behaviour_to_aliquot = RackSectorTranslator.get_translation_behaviour(
                number_sectors=self._number_sectors, source_shape=prep_shape,
                target_shape=aliquot_shape)
        behaviour_to_prep = RackSectorTranslator.get_translation_behaviour(
                number_sectors=self._number_sectors, source_shape=prep_shape,
                target_shape=prep_shape)
        return {aliquot_to_prep_type : behaviour_ali_to_prep,
                prep_to_aliquot_type : behaviour_to_aliquot,
                prep_to_prep_type : behaviour_to_prep}

    def __get_preparation_rack_positions(self, prep_container, translator_map,
                                         behaviours, aliquot_to_prep_type):
        """
        Returns the rack positions and ISO request positions for the given
        preparation container. The positions are derived from the
        :attr:`sector_positions` map using the lowest sector index of the
        final plate container descandants.
        """
        prep_sector = prep_container.location
        aliquot_sectors = set()
        for child_container in prep_container.get_descendants():
            if child_container.is_final_container:
                aliquot_sectors.add(child_container.sector_index)
        aliquot_sector = min(aliquot_sectors)

        pos_map = dict()
        translator = self.__get_translator(aliquot_to_prep_type, translator_map,
                                        behaviours, aliquot_sector, prep_sector)
        for ir_pos in self.sector_positions[aliquot_sector]:
            translated_pos = translator.translate(ir_pos.rack_position)
            pos_map[translated_pos] = ir_pos

        return pos_map

    def __get_rack_position(self, rack_position, source_sector, target_sector,
                            transfer_type, behaviours, translator_map):
        """
        Helper function returning the translated rack position.
        """
        translator = self.__get_translator(transfer_type, translator_map,
                                   behaviours, source_sector, target_sector)
        return translator.translate(rack_position)

    def __get_translator(self, transfer_type, translator_map, behaviours,
                         source_sector, target_sector):
        """
        Helper function returning a translator for the given values. Translators
        are cached.
        """
        type_translators = translator_map[transfer_type]
        if type_translators.has_key(source_sector):
            target_sector_map = type_translators[source_sector]
        else:
            target_sector_map = dict()

        if target_sector_map.has_key(target_sector):
            return target_sector_map[target_sector]
        else:
            behaviour = behaviours[transfer_type]
            translator = RackSectorTranslator(
                                    number_sectors=self._number_sectors,
                                    source_sector_index=source_sector,
                                    target_sector_index=target_sector,
                                    behaviour=behaviour)
            target_sector_map[target_sector] = translator
            return translator

    def _get_rack_positions_for_container(self, container):
        """
        The rack positions for each container are stored in the
        :attr:`__container_position_map` map that has been generated during
        the layout position creation.
        """
        return self.__container_position_map[container]


class RackPositionPlanner(_LayoutPlanner):
    """
    A special planner that deals with separate rack positions. The planner
    can either deal if routes specific to ISOs (default) or to an ISO job
    (subclass implementation).
    """

    NAME = 'Rack Position Planner'

    #: The :class:`RackPositionLocationAssigner` class for this planner.
    _LOCATION_ASSIGNER_CLS = RackPositionLocationAssigner
    #: The value for the :attr:`from_job` attribute of final ISO plate
    #: positions.
    FROM_JOB = False

    def __init__(self, log, iso_request, builder, pool_containers):
        """
        Constructor

        :param log: The log to write into.
        :type log: :class:`thelma.ThelmaLog`

        :param iso_request: The ISO request for which to create the ISOs.
        :type iso_request: :class:`thelma.models.iso.LabIsoRequest`

        :param builder: The builder collects the data of the picked assigner.
        :type builder: :class:`LabIsoBuilder`

        :param pool_containers: The :class:`PoolContainer` objects for each
            pool the planner shall regard in order of occurrence.
        :type pool_containers: :class:`list` of :class:`PoolContainer` objects
        """
        _LayoutPlanner.__init__(self, log, iso_request, builder)

        #: The :class:`PoolContainer` objects in order of occurrence.
        self.pool_containers = pool_containers

    def _check_input(self):
        _LayoutPlanner._check_input(self)
        self._check_input_list_classes('pool container', self.pool_containers,
                                       PoolContainer)

    def _register_requested_plate_containers(self):
        """
        The positions to be prepared are defined by the :attr:`pool_container`
        list.
        """
        for pool_container in self.pool_containers:
            for pool_pos in pool_container:
                container = self._create_container(pool_pos, pool_container)
                self._requested_containers[pool_pos.rack_position] = container

    def _create_container(self, pool_pos, pool_container):
        """
        Converts a molecule design pool position into a
        :class:`RackPositionContainer` object.
        """
        return RackPositionContainer.from_iso_request_position(pool_pos,
                                     pool_container.stock_concentration)

    def _init_assigner(self, reservoir_specs):
        return self._LOCATION_ASSIGNER_CLS(prep_reservoir_specs=reservoir_specs)

    def _find_coupled_containers(self):
        """
        All positions with the same pool can in theory be derived from one
        another. The location assigners will figure it out.
        """
        c = 0
        for pool_container in self.pool_containers:
            c += 1
            containers = []
            for pool_pos in pool_container:
                container = self._requested_containers[pool_pos.rack_position]
                containers.append(container)
            self._coupled_requested_containers[c] = containers

    def _store_final_iso_positions(self):
        """
        Except for the targets, everything is already stored in the container.
        """
        for container in self._requested_containers.values():
            tts = []
            for child_container, transfer_vol in container.targets.iteritems():
                tt = TransferTarget(rack_position=child_container.rack_position,
                                transfer_volume=transfer_vol,
                                target_rack_marker=child_container.plate_marker)
                tts.append(tt)

            final_pos = container.create_final_iso_plate_position(tts,
                                                                  self.FROM_JOB)
            self.builder.add_final_iso_plate_position(final_pos)

    def _create_preparation_layouts(self, plate_specs):
        prep_shape = self._picked_assigner.preparation_reservoir_specs.\
                     rack_shape

        for plate_marker, prep_plate in self._prep_plate_containers.iteritems():
            layout = LabIsoPrepLayout(shape=prep_shape)
            for container in prep_plate.get_containers():
                final_tts = []
                prep_tts = []
                for child_container, transfer_vol in container.targets.\
                                                     iteritems():
                    tt = TransferTarget(child_container.rack_position,
                                    transfer_vol, child_container.plate_marker)
                    if child_container.is_final_container:
                        final_tts.append(tt)
                    else:
                        prep_tts.append(tt)
                prep_pos = container.create_preparation_position(prep_tts,
                                                                 final_tts)
                layout.add_position(prep_pos)
            self._store_preparation_layout(plate_marker, layout, plate_specs)

    def _store_preparation_layout(self, plate_marker, prep_layout, plate_specs):
        """
        By default, preparation layouts are stored as normal (= ISO-related
        as opposed to ISO job related) layouts.
        """
        self.builder.add_preparation_layout(plate_marker, prep_layout,
                                            plate_specs)

    def _get_rack_positions_for_container(self, container):
        return [container.rack_position]


class JobRackPositionPlanner(RackPositionPlanner):
    """
    A special :class:`RackPositionPlanner` that deals with fixed (control)
    pools that are shared by all ISOs in an ISO job. Is only used if
    there are floating positions in a layout.
    """

    NAME = 'Job Rack Position Planner'

    _LOCATION_ASSIGNER_CLS = JobRackPositionAssigner
    FROM_JOB = True

    def __init__(self, log, iso_request, builder, pool_containers,
                 number_copies):
        """
        Constructor

        :param log: The log to write into.
        :type log: :class:`thelma.ThelmaLog`

        :param iso_request: The ISO request for which to create the ISOs.
        :type iso_request: :class:`thelma.models.iso.LabIsoRequest`

        :param builder: The builder collects the data of the picked assigner.
        :type builder: :class:`LabIsoBuilder`

        :param pool_containers: The :class:`PoolContainer` objects for each
            pool the planner shall regard in order of occurrence.
        :type pool_containers: :class:`list` of :class:`PoolContainer` objects

        :param number_copies: The copies that shall be created for each
            requested position (depends on number of ISOS, aliquot number and
            the fixed position processing so far).
        :type number_copies: positive integer
        """
        RackPositionPlanner.__init__(self, log, iso_request, builder,
                                     pool_containers)
        self._number_copies = number_copies

        #: Requested containers do only need to be recorded if there have not
        #: been covered by the sector preparation, that is, if the working
        #: positions in the pool containers are :class:`IsoRequestPosition`
        #: objects.
        self.__record_requested_containers = None

    def reset(self):
        RackPositionPlanner.reset(self)
        self.__record_requested_containers = None

    def _check_input(self):
        RackPositionPlanner._check_input(self)
        if self._check_input_class('number ISOs', self._number_copies, int):
            if not self._number_copies > 0:
                msg = 'The number of ISOs must be larger than 0!'
                self.add_error(msg)

    def _create_container(self, pool_pos, pool_container):
        """
        The type of the pool position also determines whether the resulting
        containers need to be added to the final ISO layout.
        """
        if isinstance(pool_pos, IsoRequestPosition):
            self.__set_record_requested_containers(True)
            return RackPositionPlanner._create_container(self, pool_pos,
                                                         pool_container)

        self.__set_record_requested_containers(False)
        return RackPositionContainer.from_iso_plate_position(pool_pos,
                                     pool_container.stock_concentration)

    def __set_record_requested_containers(self, record):
        """
        Helper function that sets the :attr:`__record_requested_containers`
        flag and makes sure the values are consistent.
        """
        if self.__record_requested_containers is None:
            self.__record_requested_containers = record
        elif not self.__record_requested_containers == record:
            self.add_error('The position classes in the pool containers ' \
                           'are inconsistent!')

    def _store_final_iso_positions(self):
        """
        The pool positions in the pool containers may already be part of
        the layout (if the controls are covered by sector preparation).
        If they are there, their stock tube data must be removed.

        """
        if self.__record_requested_containers:
            RackPositionPlanner._store_final_iso_positions(self)
        else:
            for pool_container in self.pool_containers:
                for plate_pos in pool_container:
                    if not isinstance(plate_pos, FinalLabIsoPosition):
                        continue
                    requested_container = self._requested_containers[
                                                    plate_pos.rack_position]
                    if not requested_container.from_stock:
                        plate_pos.stock_tube_barcode = None

    def _store_preparation_layout(self, plate_marker, prep_layout, plate_specs):
        """
        In contrast to the normaler rack position planner, we want to
        store a job preparation layout.
        """
        self.builder.add_job_preparation_layout(plate_marker, prep_layout,
                                                plate_specs)





class LibraryIsoBuilder(LabIsoBuilder):
    """
    A special lab ISO builder for library screenings. Unlike in normal lab
    ISOs there are no ISO preparation or aliquot plates but pre-existing library
    plates.
    """

    def __init__(self, iso_request, excluded_racks, requested_tubes):
        """
        Constructor

        :param iso_request: The ISO request the ISOs shall belong to.
        :type iso_request: :class:`thelma.models.iso.LabIsoRequest`

        :param excluded_racks: A list of barcodes from stock racks that shall
            not be used for stock sample picking.
        :type excluded_racks: A list of rack barcodes

        :param requested_tubes: A list of barcodes from stock tubes that are
            supposed to be used.
        :type requested_tubes: A list of tube barcodes.
        """
        LabIsoBuilder.__init__(self, iso_request=iso_request,
                               excluded_racks=excluded_racks,
                               requested_tubes=requested_tubes)

        #: The ISO request layout is required to transfer the library
        #: positions to the ISO plate layout -
        #: use :func:`set_iso_request_layout` to set.
        self.__iso_request_layout = None

        #: The picked library plates mapped onto layout numbers - use
        #: :func:`set_library_platesset_library_plates` to set.
        self.__library_plates = None

    def set_iso_request_layout(self, iso_request_layout):
        """
        The ISO request layout is required to transfer library positions
        to the ISO plate layouts.

        :param library_plates: The picked library plates mapped onto pools.
        :type library_plates: :class:`dict`

        :raise AttributeError: If the library plates have been set before.
        """
        self._set_immutable_value(iso_request_layout, '__iso_request_layout')

    def set_library_plates(self, library_plates):
        """
        The picked library plates must be mapped onto layout numbers.

        :param library_plates: The picked library plates mapped onto pools.
        :type library_plates: :class:`dict`

        :raise AttributeError: If the library plates have been set before.
        """
        self._set_immutable_value(library_plates, '__library_plates')

    # pylint: disable=W0613
    def _fill_iso_plate_layout(self, iso_plate_layout, floating_map, pools):
        """
        We still need to add the library positions - their data is derived
        from the :attr:`__iso_request_layout` (the actual pool ID for the
        positions is not inserted).
        """
        for ir_pos in self.__iso_request_layout.get_working_positions():
            if not ir_pos.is_library: continue
            lib_pos = FinalLabIsoPosition.create_library_position(
                                rack_position=ir_pos.rack_position,
                                concentration=ir_pos.iso_concentration,
                                volume=ir_pos.iso_volume)
            iso_plate_layout.add_position(lib_pos)
    # pylint: enable=W0613

    def _add_final_iso_plates(self, iso):
        """
        We need to pick a layout number and attach the plate to the ISO. The
        referring plates and layout numbers are removed from the available
        plates map (:attr:`__library_plates`).
        """
        layout_number = min(self.__library_plates.keys())
        library_plates = self.__library_plates[layout_number]
        iso.library_plates = library_plates
        del self.__library_plates[layout_number]


class LibraryIsoPlanner(LabIsoPlanner):
    """
    Ordering a library plate is a special case, because we do not have any
    ISO-specific processing but only job-related preparation routes.
    In addition, we do not generated new aliquot plates for library ISOs but
    assign pre-existing library plates as final ISO plates.

    **Return Value:** :class:`LibraryIsoBuilder`
    """
    NAME = 'Library ISO Planner'

    _BUILDER_CLS = LibraryIsoBuilder

    def __init__(self, log, iso_request, number_isos,
                       excluded_racks=None, requested_tubes=None):
        """
        Constructor:

        :param log: The log to record events.
        :type log: :class:`thelma.ThelmaLog`

        :param iso_request: The ISO request containing the ISO layout for the
            ISO (and experiment metadata with the molecule design pools).
        :type iso_request: :class:`thelma.models.iso.IsoRequest`

        :param number_isos: The number of ISOs ordered.
        :type number_isos: :class:`int`

        :param excluded_racks: A list of barcodes from stock racks that shall
            not be used for stock sample picking.
        :type excluded_racks: A list of rack barcodes

        :param requested_tubes: A list of barcodes from stock tubes that are
            supposed to be used.
        :type requested_tubes: A list of tube barcodes.
        """
        LabIsoPlanner.__init__(self, log=log, iso_request=self.iso_request,
                               number_isos=number_isos,
                               excluded_racks=excluded_racks,
                               requested_tubes=requested_tubes)

        #: The molecule design library used for the screen.
        self.__library = None

        #: The library plates (future aliquot plates) mapped onto layout
        #: numbers.
        self.__library_plates = None

    def reset(self):
        LabIsoPlanner.reset(self)
        self.__library = None
        self.__library_plates = None

    def _analyse_iso_request(self):
        """
        We need to find the library in addition.
        """
        LabIsoPlanner._analyse_iso_request(self)

        self.__library = self.iso_request.molecule_design_library
        if self._has_floatings:
            msg = 'There are both library and floating positions in the ' \
                  'ISO request layout!'
            self.add_error(msg)
        elif self.__library is None:
            msg = 'There is no library for this ISO request!'
            self.add_error(msg)
        else:
            self.__find_library_plates()
            if not self.has_errors():
                self._real_number_isos = len(self.__library_plates)
                # pylint: disable=E1103
                self._builder.set_iso_request_layout(self._iso_request_layout)
                self._builder.set_library_plates(self.__library_plates)
                # pylint: enable=E1103

    def __find_library_plates(self):
        """
        Determines which layout number have not been covered by the ISO
        request yet, picks unused ones (in order of layout numbers) and fetches
        aliquot plates for them.
        """
        number_aliquots = self.iso_request.number_aliquots

        used_layout_numbers = set()
        for iso in self.iso_request.isos:
            if iso.status == ISO_STATUS.CANCELLED: continue
            for lp in iso.library_plates:
                used_layout_numbers.add(lp.layout_number)

        queued_layout_numbers = set()
        for i in range(self.__library.number_layouts):
            ln = i + 1
            if ln in used_layout_numbers: continue
            queued_layout_numbers.add(ln)
        if len(queued_layout_numbers) < 1:
            msg = 'There are no unused library layouts left for this ISO ' \
                  'request!'
            self.add_error(msg)
            return None

        available_plates = dict()
        for lp in self.__library.library_plates:
            if not lp in queued_layout_numbers: continue
            if lp.has_been_used: continue
            add_list_map_element(available_plates, lp.layout_number, lp)
        self.__check_plate_availability(queued_layout_numbers, available_plates,
                                        number_aliquots)
        if self.has_errors(): return None

        for layout_number in sorted(list(queued_layout_numbers)):
            plates = available_plates[layout_number]
            self.__library_plates[layout_number] = []
            for i in range(number_aliquots):
                lp = plates.pop(0)
                self.__library_plates[layout_number].append(lp)
            if len(self.__library_plates) == self.number_isos: break

        if len(queued_layout_numbers) < self.number_isos:
            msg = 'You have requested %i ISOs. The system will only generate ' \
                  '%s ISO though, because there are no more library layouts ' \
                  'left for this ISO request.' \
                  % (self.number_isos, len(self.__library_plates))
            self.add_warning(msg)

    def __check_plate_availability(self, queued_layout_numbers,
                                   available_plates, number_aliquots):
        """
        Checks whether there are enough plates left for the queued layout
        numbers. Records warnings in case of missing plates.
        """
        not_enough_plates = []
        no_plates_left = []
        del_numbers = []
        for layout_number in queued_layout_numbers:
            if not available_plates.has_key(layout_number):
                no_plates_left.append(layout_number)
                del_numbers.append(layout_number)
                continue
            elif len(available_plates[layout_number]) < number_aliquots:
                info = '%i (%i plates)' % (layout_number,
                                           len(available_plates[layout_number]))
                not_enough_plates.append(info)
                del_numbers.append(layout_number)

        if len(no_plates_left) > 0:
            msg = 'There are no unused library plates left for some layout ' \
                  'numbers that are still in the queue: %s.' \
                  % (', '.join(sorted(no_plates_left)))
            self.add_warning(msg)
        if len(not_enough_plates) > 0:
            msg = 'There are not enough unused library plates left for some ' \
                  'layout numbers that are still in the queue: %s.' \
                  % (', '.join(sorted(not_enough_plates)))
            self.add_warning(msg)

        if len(queued_layout_numbers) == len(del_numbers):
            msg = 'Cannot generate ISOs because there is no sufficient ' \
                  'number of library plates left for any layout still in the ' \
                  'queue (%s).' % (', '.join(sorted(del_numbers)))
            self.add_error(msg)
        for layout_number in del_numbers:
            queued_layout_numbers.remove(layout_number)

    def _assign_sectors(self):
        """
        Sectors are not supported for this type of ISO requests.
        """
        pass

    def _assign_iso_specific_rack_positions(self):
        """
        All fixed positions are shared by all ISOs of the job.
        """
        pass
