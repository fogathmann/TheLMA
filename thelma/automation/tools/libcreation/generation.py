"""
The classes in this module serve the creation of an ISO request for a
library creation process.

The ISO request requires a base layout (provided by the user), a number
of molecule design pools to be included. The worklists for the samples
transfers a created here as well.

AAB
"""
from everest.entities.utils import get_root_aggregate
from math import ceil
from thelma.automation.handlers.libbaselayout \
    import LibraryBaseLayoutParserHandler
from thelma.automation.handlers.libmembers import LibraryMemberParserHandler
from thelma.automation.tools.base import BaseAutomationTool
from thelma.automation.tools.libcreation.base \
    import get_source_plate_transfer_volume
from thelma.automation.tools.libcreation.base \
    import get_stock_pool_buffer_volume
from thelma.automation.tools.libcreation.base import ALIQUOT_PLATE_CONCENTRATION
from thelma.automation.tools.libcreation.base import ALIQUOT_PLATE_VOLUME
from thelma.automation.tools.libcreation.base import LibraryBaseLayout
from thelma.automation.tools.libcreation.base import MOLECULE_TYPE
from thelma.automation.tools.libcreation.base import NUMBER_MOLECULE_DESIGNS
from thelma.automation.tools.libcreation.base import NUMBER_SECTORS
from thelma.automation.tools.libcreation.base import PREPARATION_PLATE_VOLUME
from thelma.automation.tools.libcreation.base import STARTING_NUMBER_ALIQUOTS
from thelma.automation.tools.semiconstants import get_96_rack_shape
from thelma.automation.tools.stock.base import STOCKMANAGEMENT_USER
from thelma.automation.tools.stock.base import get_default_stock_concentration
from thelma.automation.tools.utils.base import CONCENTRATION_CONVERSION_FACTOR
from thelma.automation.tools.utils.base import VOLUME_CONVERSION_FACTOR
from thelma.automation.tools.utils.base import is_valid_number
from thelma.automation.tools.utils.racksector import QuadrantIterator
from thelma.automation.tools.utils.racksector import RackSectorTranslator
from thelma.interfaces import IMoleculeType
from thelma.models.iso import ISO_TYPES
from thelma.models.iso import IsoRequest
from thelma.models.library import MoleculeDesignLibrary
from thelma.models.liquidtransfer import PlannedContainerDilution
from thelma.models.liquidtransfer import PlannedRackTransfer
from thelma.models.liquidtransfer import PlannedWorklist
from thelma.models.liquidtransfer import WorklistSeries
from thelma.models.user import User

__docformat__ = 'reStructuredText en'

__all__ = ['LibraryGenerator',
           'LibraryCreationWorklistGenerator']


class LibraryGenerator(BaseAutomationTool):
    """
    This tools creates a ISO request for a library creation procedure.
    The input stream contains the base layout for the library and the
    molecule design for the pools to be created.
    The worklists for the samples transfers a created here as well.

    **Return Value:** :class:`thelma.models.library.MoleculeDesignLibrary`
    """
    NAME = 'Library Generation'

    def __init__(self, library_name, stream, requester,
                 logging_level=None, add_default_handlers=False):
        """
        Constructor:

        :param library_name: The name of the library to be created.
        :type library_name: :class:`str`

        :param stream: Excel file stream containing one sheet with the
            base layout and one with the molecule design data.

        :param requester: This user will be owner and reporter of the ISO
            trac tickets.
        :type requester: :class:`thelma.models.user.User`

        :param logging_level: the desired minimum log level
        :type log_level: :class:`int` (or logging_level as
                         imported from :mod:`logging`)
        :default logging_level: None

        :param add_default_handlers: If *True* the log will automatically add
            the default handler upon instantiation.
        :type add_default_handlers: :class:`boolean`
        :default add_default_handlers: *False*
        """
        BaseAutomationTool.__init__(self, logging_level=logging_level,
                                    add_default_handlers=add_default_handlers,
                                    depending=False)

        #: The name of the library to be created.
        self.library_name = library_name
        #: Excel file stream containing one sheet with the base layout and one
        #: with the molecule design data.
        self.stream = stream
        #: This user will be owner and reporter of the ISO trac tickets.
        self.requester = requester

        #: The base layout (384-well) defining which position might contain
        #: libary samples.
        self.__base_layout = None
        #: The pool set containing the stock sample pools for the library.
        self.__pool_set = None
        #: The worklist series (generated by the
        #: :class:`LibraryCreationWorklistGenerator`).
        self.__worklist_series = None

        #: The stock concentration for the single molecule design pools.
        self.__stock_concentration = None
        #: The number of plates (ISOs) depends on the number of positions in the
        #: base layouts and the number of pools in the molecule design set.
        self.__number_plates = None

    def reset(self):
        BaseAutomationTool.reset(self)
        self.__base_layout = None
        self.__pool_set = None
        self.__worklist_series = None
        self.__stock_concentration = None
        self.__number_plates = None

    def run(self):
        """
        Creates the ISO request.
        """
        self.reset()
        self.add_info('Start ISO request creation ...')

        self.__check_input()
        if not self.has_errors(): self.__parse_base_layout()
        if not self.has_errors(): self.__get_pool_set()
        if not self.has_errors(): self.__create_worklist_series()
        if not self.has_errors(): self.__determine_number_of_plates()
        if not self.has_errors():
            self.return_value = self.__create_library()
            self.add_info('ISO request generation completed.')

    def __check_input(self):
        """
        Checks the initialisation values.
        """
        self._check_input_class('library name', self.library_name, basestring)
        self._check_input_class('requester', self.requester, User)

    def __parse_base_layout(self):
        """
        The layout contains the positions for the library samples.
        """
        self.add_debug('Obtain base layout ...')

        handler = LibraryBaseLayoutParserHandler(log=self.log,
                                                 stream=self.stream)
        self.__base_layout = handler.get_result()

        if self.__base_layout is None:
            msg = 'Error when trying to obtain library base layout.'
            self.add_error(msg)

    def __get_pool_set(self):
        """
        Also set the stock concentration.
        """
        self.add_debug('Obtain pool set ...')

        agg = get_root_aggregate(IMoleculeType)
        md_type = agg.get_by_id(MOLECULE_TYPE)
        self.__stock_concentration = get_default_stock_concentration(md_type)

        handler = LibraryMemberParserHandler(log=self.log,
                            stream=self.stream,
                            number_molecule_designs=NUMBER_MOLECULE_DESIGNS,
                            molecule_type=md_type)
        self.__pool_set = handler.get_result()

        if self.__pool_set is None:
            msg = 'Unable to parse library pool set!'
            self.add_error(msg)

    def __create_worklist_series(self):
        """
        Generates all required liquid transfer worklists except for the
        ones for the transfer from 1-molecule-design stock rack to pool stock
        rack. These worklists will be stored at the ISO sample stock racks
        to enable quadrant tracking.
        """
        self.add_debug('Create worklist series ...')

        generator = LibraryCreationWorklistGenerator(log=self.log,
                                base_layout=self.__base_layout,
                                stock_concentration=self.__stock_concentration,
                                library_name=self.library_name)
        self.__worklist_series = generator.get_result()

        if self.__worklist_series is None:
            msg = 'Error when trying to generate worklist series.'
            self.add_error(msg)

    def __determine_number_of_plates(self):
        """
        The number of plates depends on the number of molecule design set
        member and the number of available positions in the library layout.
        """
        number_members = len(self.__pool_set)
        number_positions = len(self.__base_layout)
        number_plates = ceil(float(number_members) / number_positions)
        self.__number_plates = int(number_plates)

    def __create_library(self):
        """
        The actual ISO request is created here.
        """
        self.add_debug('Create ISO request ...')

        iso_request = IsoRequest(
                    iso_layout=self.__base_layout.create_rack_layout(),
                    requester=self.requester,
                    owner=STOCKMANAGEMENT_USER,
                    number_plates=self.__number_plates,
                    number_aliquots=STARTING_NUMBER_ALIQUOTS,
                    plate_set_label=self.library_name,
                    worklist_series=self.__worklist_series,
                    iso_type=ISO_TYPES.LIBRARY_CREATION)

        library = MoleculeDesignLibrary(
                label=self.library_name, iso_request=iso_request,
                molecule_design_pool_set=self.__pool_set,
                final_volume=ALIQUOT_PLATE_VOLUME / VOLUME_CONVERSION_FACTOR,
                final_concentration=ALIQUOT_PLATE_CONCENTRATION \
                                    / CONCENTRATION_CONVERSION_FACTOR)

        return library


class LibraryCreationWorklistGenerator(BaseAutomationTool):
    """
    Creates the worklist series for containing the worklists involved in
    library creation.

     1. buffer addition into the pool sample stock racks (1 for each quadrant)
     2. buffer addition into preparation plates (1 for each quadrant)
     3. rack transfers from normal stock racks into pool stock racks (as usually
        the take out worklists are not stored as part of this worklist series
        but at the sample stock racks as container transfer worklist to allow
        for container tracking)
     4. rack transfer from stock rack to preparation plate
     5. rack transfer from preparation plates to aliquot plate

    **Return Value:**  worklist series
        (:class:`thelma.models.liquidtransfer.WorklistSeries`).
    """
    NAME = 'Library Creation Worklist Generator'

    #: Name pattern for the worklists that add annealing buffer to the pool
    #: stock racks. The placeholders will contain the library name and the
    #: quadrant sector.
    LIBRARY_STOCK_BUFFER_WORKLIST_LABEL = '%s_stock_buffer_Q%i'
    #: Name pattern for the worklists that add annealing buffer to the pool
    #: preparation plates. The placeholders will contain the library name and
    #: the quadrant sector.
    LIBRARY_PREP_BUFFER_WORKLIST_LABEL = '%s_prep_buffer_Q%i'
    #: Name pattern for the worklists that transfers the pool from the pool
    #: stock rack to the preparation plate. The placeholder will contain the
    #: library name.
    STOCK_TO_PREP_TRANSFER_WORKLIST_LABEL = '%s_stock_to_prep'
    #: Name pattern for the worklists that transfers the pool from the
    #: preparation plate to the final library aliqut plate. The placeholder will
    #: contain the library name.
    PREP_TO_ALIQUOT_TRANSFER_WORKLIST_LABEL = '%s_prep_to_aliquot'

    #: The dilution info for the dilution worklists.
    DILUTION_INFO = 'annealing buffer'


    def __init__(self, log, base_layout, stock_concentration, library_name):
        """
        Constructor:

        :param log: The ThelmaLog you want to write in. If the
            log is None, the object will create a new log.
        :type log: :class:`thelma.ThelmaLog`

        :param base_layout: The layout defining which positions of the layout
            are allowed to take up library samples.
        :type base_layout: :class:`LibraryBaseLayout`

        :param stock_concentration: The concentration of the single source
            molecule designs in the stock in nM.
        :type stock_concentration: positive number

        :param library_name: The name of the library to be created.
        :type library_name: :class:`str`
        """
        BaseAutomationTool.__init__(self, log=log)

        #: Defines which positions of the layout are allowed to take up
        #: library samples.
        self.base_layout = base_layout
        #: The concentration of the single source molecule designs in the
        #: stock in nM.
        self.stock_concentration = stock_concentration
        #: The name of the library to be created.
        self.library_name = library_name

        #: The worklist series for the ISO request.
        self.__worklist_series = None

        #: The last used worklist index (within the series).
        self.__last_worklist_index = None

        #: The base layout for each quadrant.
        self.__quadrant_layouts = None
        #: The volume transferred from the pool stock rack to the preparation
        #: plate.
        self.__stock_to_prep_vol = None

    def reset(self):
        BaseAutomationTool.reset(self)
        self.__worklist_series = None
        self.__last_worklist_index = -1
        self.__quadrant_layouts = dict()
        self.__stock_to_prep_vol = None

    def run(self):
        """
        Runs the tool.
        """
        self.reset()
        self.add_info('Start worklist generation ...')

        self.__check_input()
        if not self.has_errors(): self.__sort_into_sectors()
        if not self.has_errors():
            self.__worklist_series = WorklistSeries()
            self.__create_stock_rack_buffer_worklists()
            self.__create_source_plate_buffer_worklists()
            self.__create_stock_to_prep_worklists()
            self.__create_prep_to_aliquot_worklist()
        if not self.has_errors():
            self.return_value = self.__worklist_series
            self.add_info('Worklist generation completed.')

    def __check_input(self):
        """
        Checks the initialisation values.
        """
        self.add_debug('Check input ...')

        self._check_input_class('base library layout', self.base_layout,
                                LibraryBaseLayout)
        self._check_input_class('library name', self.library_name, basestring)

        if not is_valid_number(self.stock_concentration):
            msg = 'The stock concentration for the single source molecules ' \
                  'must be a positive number (obtained: %s).' \
                  % (self.stock_concentration)
            self.add_error(msg)

    def __sort_into_sectors(self):
        """
        Create a rack layout for each quadrant.
        """
        self.add_debug('Sort positions into sectors ...')

        quadrant_positions = QuadrantIterator.sort_into_sectors(
                                        self.base_layout, NUMBER_SECTORS)
        rack_shape_96 = get_96_rack_shape()

        for sector_index, positions in quadrant_positions.iteritems():
            if len(positions) < 1: continue
            base_layout = LibraryBaseLayout(shape=rack_shape_96)
            for pos in positions:
                base_layout.add_position(pos)
            if len(base_layout) > 0:
                self.__quadrant_layouts[sector_index] = base_layout

        if len(self.__quadrant_layouts) < NUMBER_SECTORS:
            missing_sectors = []
            for sector_index in range(NUMBER_SECTORS):
                if not self.__quadrant_layouts.has_key(sector_index):
                    missing_sectors.append(str(sector_index + 1))
            msg = 'Some rack sectors are empty. You do not require stock ' \
                  'racks for them: %s!' % (', '.join(missing_sectors))
            self.add_warning(msg)

    def __create_stock_rack_buffer_worklists(self):
        """
        These worklists are responsible for the addition of annealing buffer
        to the pool stock rack. There is 1 worklist for each quadrant.
        """
        self.add_debug('Create stock rack buffer worklists ...')

        buffer_volume = get_stock_pool_buffer_volume()

        for sector_index, base_layout in self.__quadrant_layouts.iteritems():
            label = self.LIBRARY_STOCK_BUFFER_WORKLIST_LABEL % (
                                        self.library_name, (sector_index + 1))
            self.__create_buffer_worklist(base_layout, buffer_volume, label,
                                          sector_index)

    def __create_source_plate_buffer_worklists(self):
        """
        These worklists are responsible for the addition of annealing buffer
        to the pool preparation plate. There is 1 worklist for each quadrant.
        """
        self.add_debug('Create preparation plate buffer worklist ...')

        self.__stock_to_prep_vol = get_source_plate_transfer_volume()
        buffer_volume = PREPARATION_PLATE_VOLUME - self.__stock_to_prep_vol

        for sector_index, base_layout in self.__quadrant_layouts.iteritems():
            label = self.LIBRARY_PREP_BUFFER_WORKLIST_LABEL % (
                                        self.library_name, (sector_index + 1))
            self.__create_buffer_worklist(base_layout, buffer_volume, label,
                                          sector_index)

    def __create_buffer_worklist(self, quadrant_layout, buffer_volume, label,
                                 sector_index):
        """
        Creates buffer dilutions worklist for a particular quadrant
        and adds it to the worklist series.
        """
        volume = buffer_volume / VOLUME_CONVERSION_FACTOR
        planned_transfers = []

        translator = RackSectorTranslator(number_sectors=NUMBER_SECTORS,
                        source_sector_index=sector_index,
                        target_sector_index=0,
                        enforce_type=RackSectorTranslator.ONE_TO_MANY)

        for rack_pos_384 in quadrant_layout.get_positions():
            rack_pos_96 = translator.translate(rack_pos_384)
            planned_transfer = PlannedContainerDilution(volume=volume,
                                                target_position=rack_pos_96,
                                                diluent_info=self.DILUTION_INFO)
            planned_transfers.append(planned_transfer)

        worklist = PlannedWorklist(label=label,
                                   planned_transfers=planned_transfers)
        self.__last_worklist_index += 1
        self.__worklist_series.add_worklist(self.__last_worklist_index,
                                            worklist)

    def __create_stock_to_prep_worklists(self):
        """
        This rack transfer worklist (transfer from pool stock rack to
        preparation plate) is executed once for each quadrant.
        """
        self.add_debug('Add worklist for transfer to preparation plate ...')

        label = self.STOCK_TO_PREP_TRANSFER_WORKLIST_LABEL % (self.library_name)
        volume = self.__stock_to_prep_vol / VOLUME_CONVERSION_FACTOR
        rack_transfer = PlannedRackTransfer.create_one_to_one(volume)
        worklist = PlannedWorklist(label=label,
                                   planned_transfers=[rack_transfer])
        self.__last_worklist_index += 1
        self.__worklist_series.add_worklist(self.__last_worklist_index,
                                            worklist)

    def __create_prep_to_aliquot_worklist(self):
        """
        There is one rack transfer for each sector (many-to-one transfer).
        Each transfer is executed once per aliquot plate.
        """
        self.add_debug('Add worklist for transfer into aliquot plates ...')

        volume = ALIQUOT_PLATE_VOLUME / VOLUME_CONVERSION_FACTOR
        rack_transfers = []
        for sector_index in self.__quadrant_layouts.keys():
            rack_transfer = PlannedRackTransfer(volume=volume,
                                        source_sector_index=0,
                                        target_sector_index=sector_index,
                                        sector_number=NUMBER_SECTORS)
            rack_transfers.append(rack_transfer)
        label = self.PREP_TO_ALIQUOT_TRANSFER_WORKLIST_LABEL % (
                                                            self.library_name)
        worklist = PlannedWorklist(label=label,
                                   planned_transfers=rack_transfers)
        self.__last_worklist_index += 1
        self.__worklist_series.add_worklist(self.__last_worklist_index,
                                            worklist)
