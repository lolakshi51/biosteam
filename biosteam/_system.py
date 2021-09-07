# -*- coding: utf-8 -*-
# BioSTEAM: The Biorefinery Simulation and Techno-Economic Analysis Modules
# Copyright (C) 2020-2021, Yoel Cortes-Pena <yoelcortes@gmail.com>
# 
# This module is under the UIUC open-source license. See 
# github.com/BioSTEAMDevelopmentGroup/biosteam/blob/master/LICENSE.txt
# for license details.
"""
"""
import flexsolve as flx
from .digraph import (digraph_from_units_and_streams,
                      digraph_from_system,
                      minimal_digraph,
                      surface_digraph,
                      finalize_digraph)
from thermosteam import functional as fn
from thermosteam import Stream, MultiStream
from thermosteam.utils import registered
from .exceptions import try_method_with_object_stamp
from ._network import Network
from ._facility import Facility
from ._unit import Unit, repr_ins_and_outs
from .utils import repr_items, ignore_docking_warnings
from .report import save_report
from .exceptions import InfeasibleRegion
from .utils import StreamPorts, OutletPort, colors
from .process_tools import utils
from collections.abc import Iterable
from warnings import warn
from inspect import signature
from thermosteam.utils import repr_kwargs
import biosteam as bst
import numpy as np

__all__ = ('System', 'AgileSystem', 'MockSystem', 
           'AgileSystem', 'OperationModeResults',
           'mark_disjunction', 'unmark_disjunction')    

# %% Customization to system creation

disjunctions = []

def mark_disjunction(stream):
    port = OutletPort.from_outlet(stream)
    if port not in disjunctions: 
        disjunctions.append(port)
    
def unmark_disjunction(stream):
    port = OutletPort.from_outlet(stream)
    if port in disjunctions:
        disjunctions.remove(port)


# %% Functions for creating deterministic systems

def facilities_from_units(units):
    isa = isinstance
    return [i for i in units if isa(i, Facility)]
    
def find_blowdown_recycle(facilities):
    isa = isinstance
    for i in facilities:
        if isa(i, bst.BlowdownMixer): return i.outs[0]


# %% Functions for recycle

def check_recycle_feasibility(material: np.ndarray):
    if fn.infeasible(material):
        raise InfeasibleRegion('recycle material flow rate')
    else:
        material[material < 0.] = 0. 


# %% Functions for taking care of numerical specifications within a system path

def converge_system_in_path(system):
    specification = system._specification
    if specification:
        method = specification
    else:
        method = system._converge
    try_method_with_object_stamp(system, method)

def simulate_unit_in_path(unit):
    try_method_with_object_stamp(unit, unit.simulate)


# %% Debugging and exception handling

def raise_recycle_type_error(recycle):
    raise ValueError(
       f"invalid recycle of type '{type(recycle).__name__}' encountered; "
        "recycle must be either a Stream object, a tuple of Stream objects, or None"
    )

def print_exception_in_debugger(self, func, e):
    print(f"{colors.exception(type(e).__name__+ ':')} {e}")
    try: self.show()
    except: pass

def update_locals_with_flowsheet(lcs):
    lcs.update(bst.main_flowsheet.to_dict())
    lcs.update(bst.__dict__)

def _method_debug(self, f):
    """Method decorator for debugging system."""
    def g(*args, **kwargs):
        try:
            f(*args, **kwargs)
        except Exception as e:
            print_exception_in_debugger(self, f, e)
            update_locals_with_flowsheet(locals())
            # All systems, units, streams, and flowsheets are available as 
            # local variables. Although this debugging method is meant
            # for internal development, please feel free to give it a shot.
            breakpoint()
    g.__name__ = f.__name__
    g.__doc__ = f.__doc__
    g._original = f
    return g

def _method_profile(self, f):
    self._total_excecution_time_ = 0.
    t = bst.utils.TicToc()
    def g():
        t.tic()
        f()
        self._total_excecution_time_ += t.elapsed_time
    g.__name__ = f.__name__
    g.__doc__ = f.__doc__
    g._original = f
    return g

# %% Converging recycle systems

class MockSystem:
    """
    Create a MockSystem object with inlets and outlets just like System 
    objects, but without implementing any of the convergence methods nor
    path related attributes.
    
    Parameters
    ----------
    units : Iterable[:class:`~biosteam.Unit`], optional
        Unit operations in mock system.
    
    Notes
    -----
    This object is used to prevent the creation of unneeded systems for less 
    computational effort.
    
    """
    __slots__ = ('units', 
                 'flowsheet',
                 '_ins', 
                 '_outs', 
                 '_irrelevant_units')
    
    def __init__(self, units=()):
        self.units = units or list(units)
        self._load_flowsheet()
    
    def _load_flowsheet(self):
        self.flowsheet = flowsheet_module.main_flowsheet.get_flowsheet()
        
    @property
    def ins(self):
        """StreamPorts[:class:`~InletPort`] All inlets to the system."""
        if hasattr(self, '_ins'):
            ins = self._ins
        else:
            inlets = bst.utils.feeds_from_units(self.units)
            self._ins = ins = StreamPorts.from_inlets(inlets, sort=True)
        return ins
    @property
    def outs(self):
        """StreamPorts[:class:`~OutletPort`] All outlets to the system."""
        if hasattr(self, '_outs'):
            outs = self._outs
        else:
            outlets = bst.utils.products_from_units(self.units)
            self._outs = outs = StreamPorts.from_outlets(outlets, sort=True)
        return outs
    
    def load_inlet_ports(self, inlets, optional=()):
        """Load inlet ports to system."""
        all_inlets = bst.utils.feeds_from_units(self.units)
        inlets = list(inlets)
        for i in inlets: 
            if i not in all_inlets:
                if i in optional:
                    inlets.remove(i)
                else:
                    raise ValueError(f'{i} is not an inlet')
        self._ins = StreamPorts.from_inlets(inlets)
    
    def load_outlet_ports(self, outlets, optional=()):
        """Load outlet ports to system."""
        all_outlets = bst.utils.products_from_units(self.units)
        outlets = list(outlets)
        for i in outlets: 
            if i not in all_outlets:
                if i in optional:
                    outlets.remove(i)
                else:
                    raise ValueError(f'{i} is not an outlet')
        self._outs = StreamPorts.from_outlets(outlets)
    
    def __enter__(self):
        if self.units:
            raise RuntimeError("only empty mock systems can enter `with` statement")
        unit_registry = self.flowsheet.unit
        self._irrelevant_units = set(unit_registry)
        unit_registry._open_dump(self)
        return self
    
    def __exit__(self, type, exception, traceback):
        irrelevant_units = self._irrelevant_units
        del self._irrelevant_units
        if self.units:
            raise RuntimeError('mock system was modified before exiting `with` statement')
        unit_registry = self.flowsheet.unit
        dump = unit_registry._close_dump(self)
        self.units = [i for i in dump if i not in irrelevant_units]
        if exception: raise exception
    
    __sub__ = Unit.__sub__
    __rsub__ = Unit.__rsub__
    __pow__ = __sub__
    __rpow__ = __rsub__
    
    def show(self):
        ins = repr_items('    ins=', self.ins._ports, brackets='[]')
        outs = repr_items('    outs=', self.outs._ports, brackets='[]')
        units = repr_items('    units=', self.units, brackets='[]')
        args = ',\n'.join([ins, outs, units])
        print(f"{type(self).__name__}(\n{args}\n)")
        
    _ipython_display_ = show
        
    def __repr__(self):
        return f"{type(self).__name__}(ins={self.ins}, outs={self.outs})"


@registered('SYS')
class System:
    """
    Create a System object that can iteratively run each element in a path
    of BioSTREAM objects until the recycle stream is converged. A path can
    have function, Unit and/or System objects. When the path contains an
    inner System object, it converges/solves it in each loop/iteration.

    Parameters
    ----------
    ID : str
         A unique identification. If ID is None, instance will not be
         registered in flowsheet.
    path : tuple[:class:`~biosteam.Unit`, function and/or :class:`~biosteam.System`], optional
        A path that is run element by element until the recycle converges.
    recycle=None : :class:`~thermosteam.Stream` or tuple[:class:`~thermosteam.Stream`], optional
        A tear stream for the recycle loop.
    facilities=() : tuple[:class:`~biosteam.Unit`, function, and/or :class:`~biosteam.System`], optional
        Offsite facilities that are simulated only after
        completing the path simulation.
    facility_recycle : :class:`~thermosteam.Stream`, optional
        Recycle stream between facilities and system path.
    N_runs : int, optional
        Number of iterations to run system. This parameter is applicable 
        only to systems with no recycle loop.
    operating_hours : float, optional
        Number of operating hours in a year. This parameter is used to
        compute convinience properties such as utility cost and material cost
        on a per year basis. 
    lang_factor : float, optional
        Lang factor for getting fixed capital investment from 
        total purchase cost. If no lang factor, installed equipment costs are 
        estimated using bare module factors.

    """
    __slots__ = (
        '_ID',
        '_path',
        '_facilities',
        '_facility_loop',
        '_recycle',
        '_N_runs',
        '_specification',
        '_mol_error',
        '_T_error',
        '_rmol_error',
        '_rT_error',
        '_iter',
        '_ins',
        '_outs',
        'maxiter',
        'molar_tolerance',
        'relative_molar_tolerance',
        'temperature_tolerance',
        'relative_temperature_tolerance',
        'operating_hours',
        'flowsheet',
        'lang_factor',
        '_stabilized',
        '_connections',
        '_irrelevant_units',
        '_converge_method',
        '_TEA',
    )
    
    ### Class attributes ###
    
    #: [int] Default maximum number of iterations
    default_maxiter = 200
    
    #: [float] Default molar tolerance for each component (kmol/hr)
    default_molar_tolerance = 1.

    #: [float] Default relative molar tolerance for each component 
    default_relative_molar_tolerance = 0.01
    
    #: [float] Default temperature tolerance (K)
    default_temperature_tolerance = 0.10

    #: [float] Default relative temperature tolerance
    default_relative_temperature_tolerance = 0.001
    
    #: [str] Default convergence method.
    default_converge_method = 'Aitken'

    # [bool] Whether to use stabilized convergence algorithm.
    default_stabilized_convergence = False

    #: [bool] Whether to raise a RuntimeError when system doesn't converge
    strict_convergence = True

    @classmethod
    def from_feedstock(cls, ID, feedstock, feeds=None, facilities=(), 
                       ends=None, facility_recycle=None, operating_hours=None,
                       lang_factor=None):
        """
        Create a System object from a feedstock.
        
        Parameters
        ----------
        ID : str
            Name of system.
        feedstock : :class:`~thermosteam.Stream`
            Main feedstock of the process.
        feeds : Iterable[:class:`~thermosteam.Stream`]
            Additional feeds to the process.
        facilities : Iterable[Facility]
            Offsite facilities that are simulated only after 
            completing the path simulation.
        ends : Iterable[:class:`~thermosteam.Stream`]
            Streams that not products, but are ultimately specified through
            process requirements and not by its unit source.
        facility_recycle : [:class:`~thermosteam.Stream`], optional
            Recycle stream between facilities and system path.
        operating_hours : float, optional
            Number of operating hours in a year. This parameter is used to
            compute convinience properties such as utility cost and material cost
            on a per year basis. 
        lang_factor : float, optional
            Lang factor for getting fixed capital investment from 
            total purchase cost. If no lang factor, installed equipment costs are 
            estimated using bare module factors.
        
        """
        network = Network.from_feedstock(feedstock, feeds, ends)
        return cls.from_network(ID, network, facilities, 
                                facility_recycle, operating_hours,
                                lang_factor)

    @classmethod
    def from_units(cls, ID="", units=None, feeds=None, ends=None,
                   facility_recycle=None, operating_hours=None,
                   lang_factor=None):
        """
        Create a System object from all units and streams defined in the flowsheet.
        
        Parameters
        ----------
        ID : str, optional
            Name of system.
        units : Iterable[:class:`biosteam.Unit`], optional
            Unit operations to be included. 
        feeds : Iterable[:class:`~thermosteam.Stream`], optional
            All feeds to the system. Specify this argument if only a section 
            of the complete system is wanted as it may disregard some units.
        ends : Iterable[:class:`~thermosteam.Stream`], optional
            End streams of the system which are not products. Specify this
            argument if only a section of the complete system is wanted, or if 
            recycle streams should be ignored.
        facility_recycle : :class:`~thermosteam.Stream`, optional
            Recycle stream between facilities and system path. This argument
            defaults to the outlet of a BlowdownMixer facility (if any).
        operating_hours : float, optional
            Number of operating hours in a year. This parameter is used to
            compute convinience properties such as utility cost and material cost
            on a per year basis. 
        lang_factor : float, optional
            Lang factor for getting fixed capital investment from 
            total purchase cost. If no lang factor, installed equipment costs are 
            estimated using bare module factors.
        
        """
        if units is None: 
            units = ()
        elif feeds is None:
            isa = isinstance
            Facility = bst.Facility
            feeds = bst.utils.feeds_from_units([i for i in units if not isa(i, Facility)])
            bst.utils.sort_feeds_big_to_small(feeds)
        if feeds:
            feedstock, *feeds = feeds
            facilities = facilities_from_units(units) if units else ()
            if not ends:
                ends = bst.utils.products_from_units(units) + [i.get_stream() for i in disjunctions]
            system = cls.from_feedstock(
                ID, feedstock, feeds, facilities, ends,
                facility_recycle or find_blowdown_recycle(facilities),
                operating_hours=operating_hours, lang_factor=lang_factor,
            )
        else:
            system = cls(ID, (), operating_hours=operating_hours)
        return system

    @classmethod
    def from_network(cls, ID, network, facilities=(), facility_recycle=None,
                     operating_hours=None, lang_factor=None):
        """
        Create a System object from a network.
        
        Parameters
        ----------
        ID : str
            Name of system.
        network : Network
            Network that defines the simulation path.
        facilities : Iterable[Facility]
            Offsite facilities that are simulated only after 
            completing the path simulation.
        facility_recycle : [:class:`~thermosteam.Stream`], optional
            Recycle stream between facilities and system path.
        operating_hours : float, optional
            Number of operating hours in a year. This parameter is used to
            compute convinience properties such as utility cost and material cost
            on a per year basis. 
        lang_factor : float, optional
            Lang factor for getting fixed capital investment from 
            total purchase cost. If no lang factor, installed equipment costs are 
            estimated using bare module factors.
        
        """
        facilities = Facility.ordered_facilities(facilities)
        isa = isinstance 
        path = [(cls.from_network('', i) if isa(i, Network) else i)
                for i in network.path]
        self = cls.__new__(cls)
        self.recycle = network.recycle
        self._set_path(path)
        self._specification = None
        self._load_flowsheet()
        self._reset_errors()
        self._set_facilities(facilities)
        self._set_facility_recycle(facility_recycle)
        self._register(ID)
        self._load_defaults()
        self._save_configuration()
        self._load_stream_links()
        self.operating_hours = operating_hours
        self.lang_factor = lang_factor
        return self
        
    def __init__(self, ID, path=(), recycle=None, facilities=(), 
                 facility_recycle=None, N_runs=None, operating_hours=None,
                 lang_factor=None):
        self.recycle = recycle
        self.N_runs = N_runs
        self._set_path(path)
        self._specification = None
        self._load_flowsheet()
        self._reset_errors()
        self._set_facilities(facilities)
        self._set_facility_recycle(facility_recycle)
        self._register(ID)
        self._load_defaults()
        self._save_configuration()
        self._load_stream_links()
        self.operating_hours = operating_hours
        self.lang_factor = lang_factor
    
    def __enter__(self):
        if self._path or self._recycle or self._facilities:
            raise RuntimeError("only empty systems can enter `with` statement")
        unit_registry = self.flowsheet.unit
        self._irrelevant_units = set(unit_registry)
        unit_registry._open_dump(self)
        return self
    
    def __exit__(self, type, exception, traceback):
        irrelevant_units = self._irrelevant_units
        ID = self._ID
        del self._irrelevant_units
        unit_registry = self.flowsheet.unit
        dump = unit_registry._close_dump(self)
        if exception: raise exception
        if self._path or self._recycle or self._facilities:
            raise RuntimeError('system cannot be modified before exiting `with` statement')
        else:
            units = [i for i in dump if i not in irrelevant_units]
            system = self.from_units(None, units)
            self.ID = ID
            self.copy_like(system)
    
    def _save_configuration(self):
        self._connections = [i.get_connection() for i in bst.utils.streams_from_units(self.unit_path)]
    
    @ignore_docking_warnings
    def _load_configuration(self):
        for i in self._connections:
            if i.source:
                i.source.outs[i.source_index] = i.stream
            if i.sink:
                i.sink.ins[i.sink_index] = i.stream
    
    @ignore_docking_warnings
    def _interface_property_packages(self):
        path = self._path
        Stream = bst.Stream
        Interface = (bst.Junction, bst.Mixer, bst.MixTank)
        isa = isinstance
        new_path = []
        for obj in path:
            new_path.append(obj)
            outs = obj.outs
            for s in outs:
                source = s._source
                sink = s._sink
                if not sink or isa(sink, Interface): continue
                if sink.chemicals is not source.chemicals:
                    chemicals = s.chemicals
                    source_index = source._outs.index(s)
                    sink_index = sink._ins.index(s)
                    if chemicals is sink.chemicals:
                        s_sink = s
                        s_source = Stream(thermo=source.thermo)
                        s_source.copy_like(s)
                    else:
                        s_sink = Stream(thermo=sink.thermo)
                        s_sink.copy_like(s)
                        if chemicals is source.chemicals:
                            s_source = s
                        else:
                            s_source = Stream(thermo=source.thermo)
                            s_source.copy_like(s)
                    junction = bst.Junction(upstream=s_source, downstream=s_sink)
                    new_path.append(junction)
                    source._outs[source_index] = s_source 
                    sink._ins[sink_index] = s_sink 
        for obj in path:
            if isa(obj, System): obj._interface_property_packages()
        self._path = tuple(new_path)
        self._save_configuration()
             
    def _reduced_thermo_data(self, required_chemicals, unit_thermo, mixer_thermo, thermo_cache):
        isa = isinstance
        mixers = [i for i in self.units if isa(i, (bst.Mixer, bst.MixTank))]
        past_upstream_units = set()
        for mixer in mixers:
            if mixer in past_upstream_units: continue
            upstream_units = mixer.get_upstream_units()
            upstream_units.difference_update(past_upstream_units)
            available_chemicals = set(required_chemicals)
            for unit in upstream_units: 
                if isa(unit, bst.Junction): continue
                available_chemicals.update(unit.get_available_chemicals())
            for unit in upstream_units: 
                if isa(unit, bst.Junction): continue
                chemicals = [i for i in unit.chemicals if i in available_chemicals]
                if unit in unit_thermo:
                    other_thermo = unit_thermo[unit]
                    for i in other_thermo.chemicals:
                        if i not in chemicals: chemicals.append(i)
                IDs = tuple([i.ID for i in chemicals])
                if IDs in thermo_cache:
                    unit_thermo[unit] = thermo_cache[IDs]
                else:
                    unit_thermo[unit] = thermo_cache[IDs] = unit.thermo.subset(chemicals)
            past_upstream_units.update(upstream_units)
        for mixer in mixers: 
            outlet = mixer.outs[0]
            sink = outlet.sink
            if sink:
                chemicals = sink.thermo.chemicals 
            else:
                chemicals = outlet.available_chemicals
            if mixer in mixer_thermo:
                other_thermo = mixer_thermo[mixer]
                new_chemicals = []
                for i in other_thermo.chemicals:
                    if i not in chemicals: new_chemicals.append(i)
                if new_chemicals:
                    chemicals = list(chemicals) + new_chemicals
            IDs = tuple([i.ID for i in chemicals])
            if IDs in thermo_cache:
                mixer_thermo[mixer] = thermo_cache[IDs]
            else:
                mixer_thermo[mixer] = thermo_cache[IDs] = unit.thermo.subset(chemicals)
        
    def reduce_chemicals(self, required_chemicals=()):
        unit_thermo = {}
        mixer_thermo = {}
        thermo_cache = {}
        self._reduced_thermo_data(required_chemicals, unit_thermo, mixer_thermo, thermo_cache)
        for unit, thermo in unit_thermo.items(): unit._reset_thermo(thermo)
        for mixer, thermo in mixer_thermo.items(): 
            for i in mixer._ins:
                if i._source: i._reset_thermo(unit_thermo[i._source])
            thermo = mixer_thermo[mixer]
            mixer._load_thermo(thermo)
            mixer._outs[0]._reset_thermo(thermo)
        self._interface_property_packages()
    
    def copy(self, ID=None):
        """Copy system.""" 
        new = System(ID)
        new.copy_like(self)
        return new
    
    def copy_like(self, other):
        """Copy path, facilities and recycle from other system.""" 
        self._path = other._path
        self._facilities = other._facilities
        self._facility_loop = other._facility_loop
        self._recycle = other._recycle
        self._connections = other._connections
    
    def set_tolerance(self, mol=None, rmol=None, T=None, rT=None, subsystems=False, maxiter=None):
        """
        Set the convergence tolerance of the system.

        Parameters
        ----------
        mol : float, optional
            Molar tolerance.
        rmol : float, optional
            Relative molar tolerance.
        T : float, optional
            Temperature tolerance.
        rT : float, optional
            Relative temperature tolerance.
        subsystems : bool, optional
            Whether to also set tolerance of subsystems as well. 
        maxiter : int, optional
            Maximum number if iterations.

        """
        if mol: self.molar_tolerance = float(mol)
        if rmol: self.relative_molar_tolerance = float(rmol)
        if T: self.temperature_tolerance = float(T)
        if rT: self.temperature_tolerance = float(rT)
        if maxiter: self.maxiter = int(maxiter)
        if subsystems: 
            for i in self.subsystems: i.set_tolerance(mol, rmol, T, rT, subsystems, maxiter)
    
    ins = MockSystem.ins
    outs = MockSystem.outs
    load_inlet_ports = MockSystem.load_inlet_ports
    load_outlet_ports = MockSystem.load_outlet_ports
    _load_flowsheet  = MockSystem._load_flowsheet
    
    def _load_stream_links(self):
        for u in self.units: u._load_stream_links()
    
    def _load_defaults(self):
        #: [int] Maximum number of iterations.
        self.maxiter = self.default_maxiter
        
        #: [float] Molar tolerance (kmol/hr)
        self.molar_tolerance = self.default_molar_tolerance
        
        #: [float] Relative molar tolerance
        self.relative_molar_tolerance = self.default_relative_molar_tolerance
        
        #: [float] Temperature tolerance (K)
        self.temperature_tolerance = self.default_temperature_tolerance
        
        #: [float] Relative temperature tolerance
        self.relative_temperature_tolerance = self.default_relative_temperature_tolerance
        
        #: [str] Converge method
        self.converge_method = self.default_converge_method
        
        self.use_stabilized_convergence_algorithm = self.default_stabilized_convergence
    
    @property
    def TEA(self):
        """TEA object linked to the system."""
        return getattr(self, '_TEA', None)
    
    @property
    def specification(self):
        """Process specification."""
        return self._specification
    @specification.setter
    def specification(self, specification):
        if specification:
            if callable(specification):
                self._specification = specification
            else:
                raise AttributeError(
                    "specification must be callable or None; "
                   f"not a '{type(specification).__name__}'"
                )
        else:
            self._specification = None
    
    @property
    def use_stabilized_convergence_algorithm(self):
        """[bool] Whether stablized convergence by adding an inner loop that uses 
        mass and energy balance approximations when applicable."""
        return self._stabilized
    @use_stabilized_convergence_algorithm.setter
    def use_stabilized_convergence_algorithm(self, stabilized):
        if stabilized and not self._recycle:
            for i in self.subsystems: i.use_stabilized_convergence_algorithm = True
        else:
            for i in self.subsystems: i.use_stabilized_convergence_algorithm = False
        self._stabilized = stabilized
    
    save_report = save_report
    
    def _extend_recycles(self, recycles):
        isa = isinstance
        recycle = self._recycle
        if recycle:
            if isa(recycle, Stream):
                recycles.append(recycle)
            elif isa(recycle, Iterable):
                recycles.extend(recycle)
            else:
                raise_recycle_type_error(recycle)
        for i in self._path:
            if isa(i, System): i._extend_recycles(recycles)
    
    def get_all_recycles(self):
        recycles = []
        self._extend_recycles(recycles)
        return recycles
    
    def _extend_flattend_path_and_recycles(self, path, recycles, stacklevel):
        isa = isinstance
        recycle = self._recycle
        stacklevel += 1
        if recycle:
            if isa(recycle, Stream):
                recycles.append(recycle)
            elif isa(recycle, Iterable):
                recycles.extend(recycle)
            else:
                raise_recycle_type_error(recycle)
        for i in self._path:
            if isa(i, System):
                if i.facilities:
                    warning = RuntimeWarning('subsystem with facilities could not be flattened')
                    warn(warning, stacklevel=stacklevel)
                    path.append(i)
                elif i.specification:
                    warning = RuntimeWarning('subsystem with specification could not be flattened')
                    warn(warning, stacklevel=stacklevel)
                    path.append(i)
                else:
                    i._extend_flattend_path_and_recycles(path, recycles, stacklevel)
            else:
                path.append(i)
    
    def prioritize_unit(self, unit):
        """
        Prioritize unit operation to run first within it's recycle system,
        if there is one.

        Parameters
        ----------
        unit : Unit
            Unit operation to prioritize.

        Raises
        ------
        ValueError
            When unit is not in the system.
        RuntimeError
            When prioritization algorithm fails. This should never happen.

        Examples
        --------
        Create a simple recycle loop and prioritize a different unit operation:
        
        >>> from biosteam import main_flowsheet as f, Stream, settings, Mixer, Splitter
        >>> f.set_flowsheet('simple_recycle_loop')
        >>> settings.set_thermo(['Water'], cache=True)
        >>> feedstock = Stream('feedstock', Water=1000)
        >>> water = Stream('water', Water=10)
        >>> recycle = Stream('recycle')
        >>> product = Stream('product')
        >>> M1 = Mixer('M1', [feedstock, water, recycle])
        >>> S1 = Splitter('S1', M1-0, [product, recycle], split=0.5)
        >>> recycle_loop_sys = f.create_system('recycle_loop_sys')
        >>> recycle_loop_sys.print()
        System('recycle_loop_sys',
            [M1,
             S1],
            recycle=S1-1)
        >>> recycle_loop_sys.prioritize_unit(S1)
        >>> recycle_loop_sys.print()
        System('recycle_loop_sys',
            [S1,
             M1],
            recycle=S1-1)

        """
        isa = isinstance
        if unit not in self.unit_path: 
            raise ValueError(f'unit {repr(unit)} not in system')
        path = self._path
        if (self._recycle or self.N_runs):
            for index, other in enumerate(path):
                if unit is other:
                    self._path = path[index:] + path[:index]
                    return
                elif isa(other, System) and unit in other.unit_path:
                    other.prioritize_unit(unit)
                    return 
            raise RuntimeError('problem in system algorithm')
                            
    
    def split(self, stream, ID_upstream=None, ID_downstream=None):
        """
        Split system in two; upstream and downstream.
        
        Parameters
        ----------    
        stream : Iterable[:class:~thermosteam.Stream], optional
            Stream where unit group will be split.
        ID_upstream : str, optional
            ID of upstream system.
        ID_downstream : str, optional
            ID of downstream system.
        
        Examples
        --------
        >>> from biorefineries.cornstover import cornstover_sys, M201
        >>> from biosteam import default
        >>> upstream_sys, downstream_sys = cornstover_sys.split(M201-0)
        >>> upstream_group = upstream_sys.to_unit_group()
        >>> upstream_group.show()
        UnitGroup: Unnamed
         units: U101, H2SO4_storage, T201, M201
        >>> downstream_group = downstream_sys.to_unit_group()
        >>> for i in upstream_group: assert i not in downstream_group.units
        >>> assert set(upstream_group.units + downstream_group.units) == set(cornstover_sys.units)
        >>> default() # Reset to biosteam defaults
        
        """
        if self._recycle: raise RuntimeError('cannot split system with recycle')
        path = self._path
        streams = self.streams
        surface_units = {i for i in path if isinstance(i, Unit)}
        if stream.source in surface_units:
            index = path.index(stream.source) + 1
        elif stream.sink in surface_units:
            index = path.index(stream.sink)
        elif stream not in streams:
            raise ValueError('stream not in system')
        else:
            raise ValueError('stream cannot reside within a subsystem')
        return (System(ID_upstream, path[:index], None),
                System(ID_downstream, path[index:], None, self._facilities))
    
    def flatten(self):
        """Flatten system by removing subsystems."""
        recycles = []
        path = []
        self._extend_flattend_path_and_recycles(path, recycles, stacklevel=2)
        self._path = tuple(path)
        self._recycle = tuple(recycles)
        N_recycles = len(recycles)
        self.molar_tolerance *= N_recycles
        self.temperature_tolerance *= N_recycles
    
    def to_unit_group(self, name=None):
        """Return a UnitGroup object of all units within the system."""
        return bst.UnitGroup(name, self.units)
    
    def _set_path(self, path):
        #: tuple[Unit, function and/or System] A path that is run element
        #: by element until the recycle converges.
        self._path = path = tuple(path)
        
    def _set_facilities(self, facilities):
        #: tuple[Unit, function, and/or System] Offsite facilities that are simulated only after completing the path simulation.
        self._facilities = tuple(facilities)
        self._load_facilities()
        
    def _load_facilities(self):
        isa = isinstance
        units = self.units.copy()
        for i in self._facilities:
            if isa(i, Facility):
                if i._system: continue
                i._system = self
                i._other_units = other_units = units.copy()
                other_units.remove(i)
            
            
    def _set_facility_recycle(self, recycle):
        if recycle:
            sys = self._downstream_system(recycle.sink)
            sys.recycle = recycle
            sys.__class__ = FacilityLoop
            #: [FacilityLoop] Recycle loop for converging facilities
            self._facility_loop = sys
        else:
            self._facility_loop = None
        
    # Forward pipping
    __sub__ = Unit.__sub__
    __rsub__ = Unit.__rsub__

    # Backwards pipping
    __pow__ = __sub__
    __rpow__ = __rsub__
    
    @property
    def subsystems(self):
        """list[System] All subsystems in the system."""
        return [i for i in self._path if isinstance(i, System)]
    
    @property
    def units(self):
        """[list] All unit operations as ordered in the path without repetitions."""
        units = []
        past_units = set()
        isa = isinstance
        for i in self._path + self._facilities:
            if isa(i, Unit):
                if i in past_units: continue
                units.append(i)
                past_units.add(i)
            elif isa(i, System):
                sys_units = i.units
                units.extend([i for i in sys_units if i not in past_units])
                past_units.update(sys_units)
        return units 
    
    @property
    def unit_path(self):
        """[list] Unit operations as ordered in the path (some units may be repeated)."""
        units = []
        isa = isinstance
        for i in self._path + self._facilities:
            if isa(i, Unit):
                units.append(i)
            elif isa(i, System):
                units.extend(i.unit_path)
        return units 
    
    @property
    def cost_units(self):
        """[set] All unit operations with costs."""
        units = set()
        isa = isinstance
        for i in self._path + self._facilities:
            if isa(i, Unit) and (i._design or i._cost):
                units.add(i)
            elif isa(i, System):
                units.update(i.cost_units)
        return units
    
    @property
    def streams(self):
        """set[:class:`~thermosteam.Stream`] All streams within the system."""
        streams = bst.utils.streams_from_units(self.unit_path)
        bst.utils.filter_out_missing_streams(streams)
        return streams
    @property
    def feeds(self):
        """set[:class:`~thermosteam.Stream`] All feeds to the system."""
        return bst.utils.feeds(self.streams)
    @property
    def products(self):
        """set[:class:`~thermosteam.Stream`] All products of the system."""
        return bst.utils.products(self.streams)
    
    @property
    def facilities(self):
        """tuple[Facility] All system facilities."""
        return self._facilities
    
    @property
    def recycle(self):
        """
        :class:`~thermosteam.Stream` or Iterable[:class:`~thermosteam.Stream`]
        A tear stream for the recycle loop.
        """
        return self._recycle
    @recycle.setter
    def recycle(self, recycle):
        isa = isinstance
        self._N_runs = None
        if recycle is None:
            self._recycle = recycle
        elif isa(recycle, Stream):
            self._recycle = recycle
        elif isa(recycle, Iterable):
            recycle = set(recycle)
            for i in recycle:
                if not isa(i, Stream):
                    raise ValueError("recycle streams must be Stream objects; "
                                     f"not {type(i).__name__}")                
            self._recycle = recycle
        else:
            raise_recycle_type_error(recycle)

    @property
    def N_runs(self):
        """Number of times to run the path."""
        return self._N_runs
    @N_runs.setter
    def N_runs(self, N_runs):
        if N_runs: self._recycle = None
        self._N_runs = N_runs

    @property
    def path(self):
        """
        tuple[Unit, function and/or System] A path that is run element by 
        element until the recycle(s) converges (if any).
        
        """
        return self._path

    @property
    def converge_method(self):
        """Iterative convergence method ('wegstein', 'aitken', or 'fixedpoint')."""
        return self._converge_method.__name__[1:]
    @converge_method.setter
    def converge_method(self, method):
        method = method.lower().replace('-', '').replace(' ', '')
        try:
            self._converge_method = getattr(self, '_' + method)
        except:
            raise ValueError("only 'wegstein', 'aitken', and 'fixedpoint' "
                            f"methods are valid, not '{method}'")

    def _downstream_path(self, unit):
        """Return a list composed of the `unit` and everything downstream."""
        if unit not in self.unit_path: return []
        elif self._recycle: return self._path
        isa = isinstance
        for index, i in enumerate(self._path):
            if unit is i:
                return self._path[index:]
            elif (isa(i, System) and unit in i.unit_path): 
                return i._downstream_path(unit) + self._path[index+1:]
        return []
    
    def _downstream_facilities(self, unit):
        """Return a list of facilities composed of the `unit` and 
        everything downstream."""
        isa = isinstance
        for index, i in enumerate(self._facilities):
            if unit is i or (isa(i, System) and unit in i.unit_path):
                return self._facilities[index:]
        return []
    
    def _downstream_system(self, unit):
        """Return a system with a path composed of the `unit` and
        everything downstream (facilities included)."""
        if self._recycle or unit is self._path[0]: return self
        path = self._downstream_path(unit)
        if path:
            facilities = self._facilities            
        else:
            facilities = self._downstream_facilities(unit)
            if not facilities:
                raise RuntimeError(f'{unit} not found in system')
        system = System(None, path,
                        facilities=facilities)
        system._ID = f'{type(unit).__name__}-{unit} and downstream'
        return system
    
    def _minimal_digraph(self, graph_attrs):
        """Return digraph of the path as a box."""
        return minimal_digraph(self.ID, self.units, self.streams, **graph_attrs)

    def _surface_digraph(self, graph_attrs):
        return surface_digraph(self._path, **graph_attrs)

    def _thorough_digraph(self, graph_attrs):
        return digraph_from_units_and_streams(self.unit_path, 
                                              self.streams,
                                              **graph_attrs)
        
    def _cluster_digraph(self, graph_attrs):
        return digraph_from_system(self, **graph_attrs)
        
    def diagram(self, kind=None, file=None, format=None, display=True, 
                number=None, profile=None, label=None, **graph_attrs):
        """
        Display a `Graphviz <https://pypi.org/project/graphviz/>`__ diagram of 
        the system.
        
        Parameters
        ----------
        kind : int or string, optional
            * 0 or 'cluster': Display all units clustered by system.
            * 1 or 'thorough': Display every unit within the path.
            * 2 or 'surface': Display only elements listed in the path.
            * 3 or 'minimal': Display a single box representing all units.
        file=None : str, display in console by default
            File name to save diagram.
        format='png' : str
            File format (e.g. "png", "svg").
        display : bool, optional
            Whether to display diagram in console or to return the graphviz 
            object.
        number : bool, optional
            Whether to number unit operations according to their 
            order in the system path.
        profile : bool, optional
            Whether to clock the simulation time of unit operations.
        label : bool, optional
            Whether to label the ID of streams with sources and sinks.
            
        """
        self._load_configuration()
        if not kind: kind = 0
        graph_attrs['format'] = format or 'png'
        original = (bst.LABEL_PATH_NUMBER_IN_DIAGRAMS,
                    bst.LABEL_PROCESS_STREAMS_IN_DIAGRAMS,
                    bst.PROFILE_UNITS_IN_DIAGRAMS)
        if number is not None: bst.LABEL_PATH_NUMBER_IN_DIAGRAMS = number
        if label is not None: bst.LABEL_PROCESS_STREAMS_IN_DIAGRAMS = label
        if profile is not None: bst.PROFILE_UNITS_IN_DIAGRAMS = profile
        try:
            if kind == 0 or kind == 'cluster':
                f = self._cluster_digraph(graph_attrs)
            elif kind == 1 or kind == 'thorough':
                f = self._thorough_digraph(graph_attrs)
            elif kind == 2 or kind == 'surface':
                f = self._surface_digraph(graph_attrs)
            elif kind == 3 or kind == 'minimal':
                f = self._minimal_digraph(graph_attrs)
            else:
                raise ValueError("kind must be one of the following: "
                                 "0 or 'cluster', 1 or 'thorough', 2 or 'surface', "
                                 "3 or 'minimal'")
            if display or file: 
                finalize_digraph(f, file, format)
            else:
                return f
        finally:
            (bst.LABEL_PATH_NUMBER_IN_DIAGRAMS, 
             bst.LABEL_PROCESS_STREAMS_IN_DIAGRAMS,
             bst.PROFILE_UNITS_IN_DIAGRAMS) = original
            
    # Methods for running one iteration of a loop
    def _iter_run(self, mol):
        """
        Run the system at specified recycle molar flow rate.
        
        Parameters
        ----------
        mol : numpy.ndarray
              Recycle molar flow rates.
            
        Returns
        -------
        mol_new : numpy.ndarray
            New recycle molar flow rates.
        not_converged : bool
            True if recycle has not converged.
            
        """
        check_recycle_feasibility(mol)
        self._set_recycle_data(mol)
        T = self._get_recycle_temperatures()
        self._run()
        mol_new = self._get_recycle_data()
        T_new = self._get_recycle_temperatures()
        mol_errors = np.abs(mol - mol_new)
        positive_index = mol_errors > 1e-16
        mol_errors = mol_errors[positive_index]
        if mol_errors.size == 0:
            self._mol_error = mol_error = 0.
            self._rmol_error = rmol_error = 0.
        else:
            self._mol_error = mol_error = mol_errors.max()
            if mol_error > 1e-12:
                self._rmol_error = rmol_error = (mol_errors / np.maximum.reduce([np.abs(mol[positive_index]), np.abs(mol_new[positive_index])])).max()
            else:
                self._rmol_error = rmol_error = 0.
        T_errors = np.abs(T - T_new)
        self._T_error = T_error = T_errors.max()
        self._rT_error = rT_error = (T_errors / T).max()
        self._iter += 1
        not_converged = not (
            (mol_error < self.molar_tolerance
             or rmol_error < self.relative_molar_tolerance)
            and
            (T_error < self.temperature_tolerance
             or rT_error < self.relative_temperature_tolerance)
        )
        if not_converged and self._iter >= self.maxiter:
            if self.strict_convergence: raise RuntimeError(f'{repr(self)} could not converge' + self._error_info())
            else: not_converged = False
        return mol_new, not_converged
            
    def _get_recycle_data(self):
        recycle = self._recycle
        if isinstance(recycle, Stream):
            return recycle.imol.data.copy()
        elif isinstance(recycle, Iterable):
            return np.vstack([i.imol.data for i in recycle])
        else:
            raise RuntimeError('no recycle available')
    
    def _set_recycle_data(self, data):
        recycle = self._recycle
        isa = isinstance
        if isa(recycle, Stream):
            try:
                recycle._imol._data[:] = data
            except:
                raise IndexError(f'expected 1 row; got {data.shape[0]} rows instead')
        elif isa(recycle, Iterable):
            length = len
            N_rows = data.shape[0]
            M_rows = sum([length(i) if isa(i, MultiStream) else 1 for i in recycle])
            if M_rows != N_rows: 
                raise IndexError(f'expected {M_rows} rows; got {N_rows} rows instead')
            index = 0
            for i in recycle:
                if isa(i, MultiStream):
                    next_index = index + length(i)
                    i._imol._data[:] = data[index:next_index, :]
                    index = next_index
                else:
                    i._imol._data[:] = data[index, :]
                    index += 1
        else:
            raise RuntimeError('no recycle available')
            
    def _get_recycle_temperatures(self):
        recycle = self._recycle
        if isinstance(recycle, Stream):
            T = self._recycle.T
        elif isinstance(recycle, Iterable):
            T = [i.T for i in recycle]
        else:
            raise RuntimeError('no recycle available')
        return np.array(T, float)
    
    def _setup(self):
        """Setup each element of the system."""
        self._load_facilities()
        self._load_configuration()
        for i in self.units: i._setup()
        
    def _run(self):
        """Rigorously run each element in the path."""
        isa = isinstance
        converge = converge_system_in_path
        run = try_method_with_object_stamp
        for i in self._path:
            if isa(i, Unit): run(i, i.run)
            elif isa(i, System): converge(i)
            else: i() # Assume it's a function
    
    # Methods for convering the recycle stream    
    def _fixedpoint(self):
        """Converge system recycle iteratively using fixed-point iteration."""
        self._solve(flx.conditional_fixed_point)
        
    def _wegstein(self):
        """Converge the system recycle iteratively using wegstein's method."""
        self._solve(flx.conditional_wegstein)
    
    def _aitken(self):
        """Converge the system recycle iteratively using Aitken's method."""
        self._solve(flx.conditional_aitken)
        
    def _solve(self, solver):
        """Solve the system recycle iteratively using given solver."""
        self._reset_iter()
        f = iter_run = self._iter_run
        if self._stabilized:
            special_units = [i for i in self.units if hasattr(i, '_steady_run')]
            if special_units:
                def f(mol):
                    self._set_recycle_data(mol)
                    for unit in special_units: unit._run = unit._steady_run
                    try:
                        solver(iter_run, self._get_recycle_data())
                    finally:
                        for unit in special_units: del unit._run
                    return iter_run(self._get_recycle_data())
        try:
            solver(f, self._get_recycle_data())
        except IndexError as error:
            try:
                solver(f, self._get_recycle_data())
            except:
                raise error
    
    def _converge(self):
        if self._N_runs:
            for i in range(self.N_runs): self._run()
        elif self._recycle:
            self._converge_method()
        else:
            self._run()
        
    def _summary(self):
        simulated_units = set()
        isa = isinstance
        Unit = bst.Unit
        for i in self._path:
            if isa(i, Unit):
                if i in simulated_units: continue
                simulated_units.add(i)
            try_method_with_object_stamp(i, i._summary)
        simulate_unit = simulate_unit_in_path
        for i in self._facilities:
            if isa(i, Unit): simulate_unit(i)
            elif isa(i, System): 
                converge_system_in_path(i)
                i._summary()
            else: i() # Assume it is a function
        for i in self._facilities:
            if isa(i, (bst.BoilerTurbogenerator, bst.Boiler)): simulate_unit(i)

    def _reset_iter(self):
        self._iter = 0
        for system in self.subsystems: system._reset_iter()
    
    def _reset_errors(self):
        #: Molar flow rate error (kmol/hr)
        self._mol_error = 0
        
        #: Relative molar flow rate error
        self._rmol_error = 0
        
        #: Temperature error (K)
        self._T_error = 0
        
        #: Relative temperature error
        self._rT_error = 0
        
        #: Number of iterations
        self._iter = 0
    
    def empty_outlet_streams(self):
        """Reset all outlet streams to zero flow."""
        self._reset_errors()
        units = self.units
        streams = bst.utils.streams_from_units(units)
        bst.utils.filter_out_missing_streams(streams)
        streams_by_data = {}
        for i in streams:
            data = i.imol.data
            data_id = id(data)
            if data_id in streams_by_data:
                streams_by_data[data_id].append(i)
            else:
                streams_by_data[data_id] = [i]
        for streams in streams_by_data.values():
            if all([i.source in units for i in streams]):
                streams[0].empty()

    def empty_recycles(self):
        """Reset all recycle streams to zero flow."""
        self._reset_errors()        
        recycle = self._recycle
        if recycle:
            if isinstance(recycle, Stream):
                recycle.empty()
            elif isinstance(recycle, Iterable):
                for i in recycle: i.empty()
            else:
                raise_recycle_type_error(recycle)
        for system in self.subsystems:
            system.empty_recycles()

    def reset_cache(self):
        """Reset cache of all unit operations."""
        for unit in self.units: unit.reset_cache()

    def simulate(self):
        """Converge the path and simulate all units."""
        self._setup()
        self._converge()
        self._summary()
        if self._facility_loop: self._facility_loop._converge()
    
    # Convinience methods
    
    @property
    def heat_utilities(self):
        """[tuple] All HeatUtility objects."""
        return utils.get_heat_utilities(self.cost_units)
    
    @property
    def power_utilities(self):
        """[tuple] All PowerUtility objects."""
        return tuple(utils.get_power_utilities(self.cost_units))
    
    def get_inlet_flow(self, units, key=None):
        """
        Return total flow across all inlets per year.
        
        Parameters
        ----------
        units : str
            Material units of measure (e.g., 'kg', 'gal', 'kmol').
        key : tuple[str] or str, optional
            Chemical identifiers. If none given, the sum of all chemicals returned
            
        Examples
        --------
        >>> from biosteam import Stream, Mixer, Splitter, settings, main_flowsheet
        >>> settings.set_thermo(['Water', 'Ethanol'])
        >>> main_flowsheet.clear()
        >>> S1 = Splitter('S1', Stream(Ethanol=10, units='ton/hr'), split=0.1)
        >>> M1 = Mixer('M1', ins=[Stream(Water=10, units='ton/hr'), S1-0])
        >>> sys = main_flowsheet.create_system(operating_hours=330*24)
        >>> sys.get_inlet_flow('Mton') # Sum of all chemicals
        0.1584
        >>> sys.get_inlet_flow('Mton', 'Water') # Just water
        0.0792
        
        """
        units += '/hr'
        if key:
            return self.operating_hours * sum([i.get_flow(units, key) for i in bst.utils.feeds_from_units(self.units)])
        else:
            return self.operating_hours * sum([i.get_total_flow(units) for i in bst.utils.feeds_from_units(self.units)])
    
    def get_outlet_flow(self, units, key=None):
        """
        Return total flow across all outlets per year.
        
        Parameters
        ----------
        units : str
            Material units of measure (e.g., 'kg', 'gal', 'kmol').
        key : tuple[str] or str, optional
            Chemical identifiers. If none given, the sum of all chemicals returned
            
        Examples
        --------
        >>> from biosteam import Stream, Mixer, Splitter, settings, main_flowsheet
        >>> settings.set_thermo(['Water', 'Ethanol'])
        >>> main_flowsheet.clear()
        >>> S1 = Splitter('S1', Stream(Ethanol=10, units='ton/hr'), split=0.1)
        >>> M1 = Mixer('M1', ins=[Stream(Water=10, units='ton/hr'), S1-0])
        >>> sys = main_flowsheet.create_system(operating_hours=330*24)
        >>> sys.simulate()
        >>> sys.get_outlet_flow('Mton') # Sum of all chemicals
        0.1584
        >>> sys.get_outlet_flow('Mton', 'Water') # Just water
        0.0792
        
        """
        units += '/hr'
        if key:
            return self.operating_hours * sum([i.get_flow(units, key) for i in bst.utils.products_from_units(self.units)])
        else:
            return self.operating_hours * sum([i.get_total_flow(units) for i in bst.utils.products_from_units(self.units)])
    
    def market_value(self, stream):
        """Return the market value of a stream [USD/yr]."""
        return stream.cost * self.operating_hours
    
    def _price2cost(self, stream):
        """Get factor to convert stream price to cost."""
        F_mass = stream.F_mass
        if not F_mass: warn(RuntimeWarning(f"stream '{stream}' is empty"))
        price2cost = F_mass * self.operating_hours
        if stream.sink and not stream.source:
            return - price2cost 
        elif stream.source:
            return price2cost
        else:
            raise ValueError("stream must be either a feed or a product")
    
    @property
    def sales(self):
        """Annual sales revenue."""
        return sum([s.cost for s in self.products if s.price]) * self.operating_hours
    @property
    def material_cost(self):
        """Annual material cost."""
        return sum([s.cost for s in self.feeds if s.price]) * self.operating_hours
    @property
    def utility_cost(self):
        """Total utility cost (USD/yr)."""
        return sum([u.utility_cost for u in self.cost_units]) * self.operating_hours
    @property
    def purchase_cost(self):
        """Total purchase cost (USD)."""
        return sum([u.purchase_cost for u in self.cost_units])
    @property
    def installed_equipment_cost(self):
        """Total installed cost (USD)."""
        lang_factor = self.lang_factor
        if lang_factor:
            return sum([u.purchase_cost * lang_factor for u in self.cost_units])
        else:
            return sum([u.installed_cost for u in self.cost_units])
    
    def get_electricity_consumption(self):
        """Return the total electricity consumption in MW."""
        return self.operating_hours * utils.get_electricity_consumption(self.power_utilities)

    def get_electricity_production(self):
        """Return the total electricity production in MW."""
        return self.operating_hours * utils.get_electricity_production(self.power_utilities)
    
    def get_utility_duty(self, agent):
        """Return the total utility duty for given agent in GJ/hr"""
        return self.operating_hours * utils.get_utility_duty(self.heat_utilities, agent)
    
    def get_utility_flow(self, agent):
        """Return the total utility flow for given agent in MT/hr"""
        return self.operating_hours * utils.get_utility_flow(self.heat_utilities, agent)
    
    def get_cooling_duty(self):
        """Return the total cooling duty in GJ/yr."""
        return self.operating_hours * utils.get_cooling_duty(self.heat_utilities)
    
    def get_heating_duty(self):
        """Return the total heating duty in GJ/yr."""
        return self.operating_hours * utils.get_heating_duty(self.heat_utilities)
    
    def get_purchase_cost(self):
        """Return the total equipment purchase cost in million USD."""
        return utils.get_purchase_cost(self.cost_units)
    
    def get_installed_equipment_cost(self):
        """Return the total installed equipment cost in million USD."""
        return utils.get_installed_cost(self.cost_units)
    
    # Other
    def to_network(self):
        """Return network that defines the system path."""
        isa = isinstance
        path = [(i.to_network() if isa(i, System) else i) for i in self._path]
        network = Network.__new__(Network)    
        network.path = path
        network.recycle = self._recycle
        network.units = set(self.unit_path)
        return network
        
    # Debugging
    def _turn_on(self, mode):
        """Turn on special simulation modes like `profile` or `debug`."""
        if not isinstance(mode, str):
            raise TypeError(f"mode must be a string; not a {type(mode).__name__} object")
        mode = mode.lower()
        if mode == 'debug':
            _wrap_method = _method_debug
        elif mode == 'profile': 
            _wrap_method = _method_profile
        else:
            raise ValueError(f"mode must be either 'debug' or 'profile'; not '{mode}'")
        for u in self.units:
            if u._specification:
                u._specification = [_wrap_method(u, i) for i in u.specification]
            else:
                u.run = _wrap_method(u, u.run)
            u._design = _wrap_method(u, u._design)
            u._cost = _wrap_method(u, u._cost)

    def _turn_off(self):
        """Turn off special simulation modes like `profile` or `debug`."""
        for u in self.units:
            if u.specification:
                u.specification = u.specification._original
            else:
                u.run = u.run._original
            u._design = u._design._original
            u._cost = u._cost._original
    
    def debug(self):
        """Simulate in debug mode. If an exception is raised, it will 
        automatically enter in a breakpoint"""
        self._turn_on('debug')
        try: self.simulate()
        finally: self._turn_off()
            
    def profile(self):
        """
        Simulate system in profile mode and return a DataFrame object of unit 
        operation simulation times.
        
        """
        import pandas as pd
        self._turn_on('profile')
        try: self.simulate()
        finally: self._turn_off()
        units = self.units
        units.sort(key=(lambda u: u._total_excecution_time_), reverse=True)
        data = [(u.line, 1000. * u._total_excecution_time_) for u in units]
        for u in units: del u._total_excecution_time_
        return pd.DataFrame(data, index=[u.ID for u in units],
                            columns=('Unit Operation', 'Time (ms)'))
            
    # Representation
    def print(self, spaces=''): # pragma: no cover
        """
        Print in a format that you can use recreate the system.
        """
        print(self._stacked_info())
    
    def _stacked_info(self, spaces=''): # pragma: no cover
        """
        Return info with inner layers of path and facilities stacked.
        """
        info = f"{type(self).__name__}({repr(self.ID)}"
        spaces += 4 * " "
        dlim = ',\n' + spaces
        update_info = lambda new_info: dlim.join([info, new_info])
        def get_path_info(path):
            isa = isinstance
            path_info = []
            for i in path:
                if isa(i, Unit):
                    path_info.append(str(i))
                elif isa(i, System):
                    path_info.append(i._stacked_info(spaces))
                else:
                    path_info.append(str(i))
            return '[' + (dlim + " ").join(path_info) + ']'
        path_info = get_path_info(self._path)
        info = update_info(path_info)
        facilities = self._facilities
        if facilities:
            facilities_info = get_path_info(facilities)
            facilities_info = f'facilities={facilities_info}'
            info = update_info(facilities_info)
        recycle = self._recycle
        if recycle:
            recycle = self._get_recycle_info()
            info = update_info(f"recycle={recycle}")
        if self.N_runs:
            info = update_info(f"N_runs={self.N_runs}")
        info += ')'
        return info
    
    def _get_recycle_info(self):
        recycle = self._recycle
        if isinstance(recycle, Stream):
            recycle = recycle._source_info()
        else:
            recycle = ", ".join([i._source_info() for i in recycle])
            recycle = '{' + recycle + '}'
        return recycle
    
    def _ipython_display_(self):
        if bst.ALWAYS_DISPLAY_DIAGRAMS: self.diagram('minimal')
        self.show()

    def _error_info(self):
        """Return information on convergence."""
        recycle = self._recycle
        if recycle:
            s = '' if isinstance(recycle, Stream) else 's'
            return (f"\nHighest convergence error among components in recycle"
                    f"\nstream{s} {self._get_recycle_info()} after {self._iter} loops:"
                    f"\n- flow rate   {self._mol_error:.2e} kmol/hr ({self._rmol_error*100.:.2g}%)"
                    f"\n- temperature {self._T_error:.2e} K ({self._rT_error*100.:.2g}%)")
        else:
            return ""

    def __str__(self):
        if self.ID: return self.ID
        else: return type(self).__name__ 
    
    def __repr__(self):
        if self.ID: return f'<{type(self).__name__}: {self.ID}>'
        else: return f'<{type(self).__name__}>'

    def show(self, layout=None, T=None, P=None, flow=None, composition=None, N=None, 
             IDs=None, data=True):
        """Prints information on system."""
        print(self._info(layout, T, P, flow, composition, N, IDs, data))

    def _info(self, layout, T, P, flow, composition, N, IDs, data):
        """Return string with all specifications."""
        error = self._error_info()
        ins_and_outs = repr_ins_and_outs(layout, self.ins, self.outs, 
                                         T, P, flow, composition, N, IDs, data)
        return (f"System: {self.ID}"
                + error + '\n'
                + ins_and_outs)
       
class FacilityLoop(System):
    __slots__ = ()
    
    def _run(self):
        obj = super()
        for i in self.units:
            if i._design or i._cost: Unit._setup(i)
        obj._run()
        self._summary()
        
from biosteam import _flowsheet as flowsheet_module
del ignore_docking_warnings


# %% Working with different operation modes

class OperationModeResults:
    
    __slots__ = ('unit_capital_costs', 'utility_cost', 'flow_rates', 
                 'feeds', 'products', 'operating_hours')
    
    def __init__(self, unit_capital_costs, flow_rates, utility_cost, 
                 feeds, products, operating_hours):
        self.unit_capital_costs = unit_capital_costs
        self.flow_rates = flow_rates
        self.utility_cost = utility_cost
        self.feeds = feeds
        self.products = products
        self.operating_hours = operating_hours
    
    @property
    def material_cost(self):
        flow_rates = self.flow_rates
        return sum([flow_rates[i] * i.price  for i in self.feeds])
    
    @property
    def sales(self):
        flow_rates = self.flow_rates
        return sum([flow_rates[i] * i.price for i in self.products])


class OperationMode:
    __slots__ = ('__dict__',)
    def __init__(self, **data):
        self.__dict__ = data
    
    def simulate(self):
        """
        Simulate operation mode and return an OperationModeResults object with 
        data on variable operating costs (i.e. utility and material costs) and sales.
        
        """
        operation_parameters = self.agile_system.operation_parameters
        mode_operation_parameters = self.agile_system.mode_operation_parameters
        for name, value in self.__dict__.items():    
            if name in operation_parameters: operation_parameters[name](value)
            elif name in mode_operation_parameters: mode_operation_parameters[name](value, self)
        system = self.system
        system.simulate()
        feeds = system.feeds
        products = system.products
        cost_units = system.cost_units
        operating_hours = self.operating_hours
        return OperationModeResults(
            {i: i.get_design_and_capital() for i in cost_units},
            {i: i.F_mass * operating_hours for i in feeds + products},
            operating_hours * sum([i.utility_cost for i in cost_units]),
            feeds, products,
            operating_hours,
        )

    def __repr__(self):
        return f"{type(self).__name__}({repr_kwargs(self.__dict__, start='')})"
    
        
class AgileSystem:
    """
    Class for creating objects which may serve to retrive
    general results from multiple operation modes in such a way that it 
    represents an agile production process. When simulated, an AgileSystem 
    generates results from system operation modes and compile them to 
    retrieve results later.
    
    Parameters
    ----------
    operation_modes : list[OperationMode], optional
        Defines each mode of operation with time steps and parameter values
    operation_parameters : dict[str: function], optional
        Defines all parameters available for all operation modes.
    lang_factor : float, optional
        Lang factor for getting fixed capital investment from 
        total purchase cost. If no lang factor, installed equipment costs are 
        estimated using bare module factors.
    
    """
    
    __slots__ = ('operation_modes', 'operation_parameters',
                 'mode_operation_parameters', 'unit_capital_costs', 
                 'utility_cost', 'flow_rates', 'feeds', 'products', 
                 'purchase_cost', 'installed_equipment_cost',
                 'lang_factor', '_OperationMode', '_TEA')
    
    TEA = System.TEA
    
    def __init__(self, operation_modes=None, operation_parameters=None, 
                 mode_operation_parameters=None, lang_factor=None):
        self.operation_modes = [] if operation_modes is None else operation_modes 
        self.operation_parameters = {} if operation_parameters  is None else operation_parameters
        self.mode_operation_parameters = {} if mode_operation_parameters is None else mode_operation_parameters
        self.lang_factor = lang_factor
        self._OperationMode = type('OperationMode', (OperationMode,), {'agile_system': self})
        
    def _downstream_system(self, unit):
        return self

    def operation_mode(self, system, operating_hours, **data):
        """
        Define and register an operation mode.
        
        Parameters
        ----------    
        operating_hours : function
            Length of operation in hours.
        **data : str
            Name and value-pairs of operation parameters.
        
        """
        for s in system.streams: s.unlink()
        om = self._OperationMode(system=system, operating_hours=operating_hours, **data)
        self.operation_modes.append(om)
        return om

    def operation_parameter(self, setter=None, name=None, mode_dependent=False):
        """
        Define and register operation parameter.
        
        Parameters
        ----------    
        setter : function
            Should set parameter in the element.
        name : str
            Name of parameter. If None, default to argument name of setter.
        mode_dependent :
            Whether the setter accepts the OperationMode object as a second argument.
        
        """
        if not setter: return lambda setter: self.operation_parameter(setter, name, mode_dependent)
        if not name: name, *_ = signature(setter).parameters.keys()
        if mode_dependent:
            self.mode_operation_parameters[name] = setter
        else:
            self.operation_parameters[name] = setter
        return setter

    def market_value(self, stream):
        """Return the market value of a stream [USD/yr]."""
        return self.flow_rates[stream] * stream.price
    
    def _price2cost(self, stream):
        """Get factor to convert stream price to cost for cash flow in solve_price method."""
        if stream in self.flow_rates:
            F_mass = self.flow_rates[stream] 
        else:
            F_mass = 0.
        if not F_mass: warn(f"stream '{stream}' is empty", category=RuntimeWarning)
        if stream in self.products:
            return F_mass
        elif stream in self.feeds:
            return - F_mass
        else:
            raise ValueError("stream must be either a feed or a product")

    @property
    def material_cost(self):
        flow_rates = self.flow_rates
        return sum([flow_rates[i] * i.price  for i in self.feeds])
    
    @property
    def sales(self):
        flow_rates = self.flow_rates
        return sum([flow_rates[i] * i.price for i in self.products])
    
    @property
    def streams(self):
        streams = []
        stream_set = set()
        for u in self.units:
            for s in u._ins + u._outs:
                if not s or s in stream_set: continue
                streams.append(s)
                stream_set.add(s)
        return streams
    
    @property
    def units(self):
        units = []
        past_units = set() 
        for i in self.operation_modes:
            for i in i.system.unit_path:
                if i in past_units: continue
                units.append(i)
        return units
    
    @property
    def cost_units(self):
        systems = set([i.system for i in self.operation_modes])
        if len(systems) == 1:
            return systems.pop().cost_units
        else:
            units = set()
            for i in systems: units.update(i.cost_units)
            return units

    @property
    def empty_recycles(self):
        return self.system.empty_recycles
    
    @property    
    def reset_cache(self):
        return self.system.reset_cache

    @property
    def operating_hours(self):
        return sum([i.operating_hours for i in self.operation_modes])
    @operating_hours.setter
    def operating_hours(self, operating_hours):
        factor = operating_hours / self.operating_hours
        for i in self.operation_modes: i.operating_hours *= factor
            
    def reduce_chemicals(self, required_chemicals=()):
        for i in self.streams: i.unlink()
        unit_thermo = {}
        mixer_thermo = {}
        thermo_cache = {}
        for mode in self.operation_modes: 
            mode.system._load_configuration()
            mode.system._reduced_thermo_data(required_chemicals, unit_thermo, mixer_thermo, thermo_cache)
        for mode in self.operation_modes:  
            mode.system._load_configuration()
            for unit, thermo in unit_thermo.items(): unit._reset_thermo(thermo)
            for mixer, thermo in mixer_thermo.items(): 
                for i in mixer._ins:
                    if i._source: i._reset_thermo(unit_thermo[i._source])
                thermo = mixer_thermo[mixer]
                mixer._load_thermo(thermo)
                mixer._outs[0]._reset_thermo(thermo)
            mode.system._interface_property_packages()
        
    def simulate(self):
        operation_mode_results = [i.simulate() for i in self.operation_modes]
        units = set(sum([list(i.unit_capital_costs) for i in operation_mode_results], []))
        unit_modes = {i: [] for i in units}
        for results in operation_mode_results:
            for i, j in results.unit_capital_costs.items(): unit_modes[i].append(j)
        self.unit_capital_costs = {i: i.get_agile_design_and_capital(j) for i, j in unit_modes.items()}
        self.utility_cost = sum([i.utility_cost for i in operation_mode_results])
        self.flow_rates = flow_rates = {}
        self.feeds = list(set(sum([i.feeds for i in operation_mode_results], [])))
        self.products = list(set(sum([i.products for i in operation_mode_results], [])))
        self.purchase_cost = sum([u.purchase_cost for u in self.unit_capital_costs])
        lang_factor = self.lang_factor
        if lang_factor:
            self.installed_equipment_cost = sum([u.purchase_cost * lang_factor for u in self.unit_capital_costs.values()])
        else:
            self.installed_equipment_cost = sum([u.installed_cost for u in self.unit_capital_costs.values()])
        for results in operation_mode_results:
            for stream, F_mass in results.flow_rates.items():
                if stream in flow_rates: flow_rates[stream] += F_mass
                else: flow_rates[stream] = F_mass
            
    def __repr__(self):
        return f"{type(self).__name__}(operation_modes={self.operation_modes}, operation_parameters={self.operation_parameters}, lang_factor={self.lang_factor})"