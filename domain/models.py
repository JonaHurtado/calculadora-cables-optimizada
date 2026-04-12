"""
Domain Models - Core data structures for cable optimization system.

This module defines immutable data classes following Domain-Driven Design principles.
All models use strict typing and comprehensive docstrings.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
import time

if TYPE_CHECKING:
    from core.rules import BaseRule


@dataclass
class CableProperties:
    """
    Physical and economic properties of a specific cable cross-section.
    
    Attributes:
        r_ohm_km: Electrical resistance at 20°C (Ω/km)
        price: Price per meter (€/m)
        d: Conductor diameter in mm (None for large sections)
        D: Distance between conductors in mm (None for large sections)
        x_ohm_km: Reactance (Ω/km), explicit for DC_MONO catalog, 0.0 otherwise
    """
    r_ohm_km: float
    price: float
    d: Optional[float] = None
    D: Optional[float] = None
    x_ohm_km: float = 0.0

    def __post_init__(self) -> None:
        """Validate that essential properties are positive."""
        if self.r_ohm_km <= 0:
            raise ValueError(f"Resistance must be positive, got {self.r_ohm_km}")
        if self.price < 0:
            raise ValueError(f"Price cannot be negative, got {self.price}")


@dataclass
class CableCatalog:
    """
    Container for unified cable catalogs.
    
    Attributes:
        aluminum: Catalog for aluminum conductors (14 sections: 16-630 mm²)
        copper: Unified catalog for copper conductors (17 sections: 1.5-400 mm²)
                Merges Cu_DC_MONO (small sections) and Cu_default (large sections)
    """
    aluminum: Dict[float, CableProperties]
    copper: Dict[float, CableProperties]

    def get_properties(self, section: float, conductor_type: str) -> CableProperties:
        """
        Retrieve properties for a specific cable section.
        
        Args:
            section: Cross-sectional area in mm²
            conductor_type: 'Al' or 'Cu'
            
        Returns:
            CableProperties object
            
        Raises:
            ValueError: If conductor type is unknown or section not found
        """
        if conductor_type == 'Al':
            catalog = self.aluminum
        elif conductor_type == 'Cu':
            catalog = self.copper
        else:
            raise ValueError(f"Unknown conductor type: {conductor_type}")
        
        if section not in catalog:
            raise ValueError(
                f"Section {section} mm² not found in {conductor_type} catalog. "
                f"Available sections: {sorted(catalog.keys())}"
            )
        
        return catalog[section]

    def get_price(self, section: float, conductor_type: str) -> float:
        """
        Get price per meter for a specific cable section.
        
        Args:
            section: Cross-sectional area in mm²
            conductor_type: 'Al' or 'Cu'
            
        Returns:
            Price in €/m
        """
        return self.get_properties(section, conductor_type).price

    def get_available_sections(self, conductor_type: str) -> List[float]:
        """
        Get sorted list of available sections for a conductor type.
        
        Args:
            conductor_type: 'Al' or 'Cu'
            
        Returns:
            Sorted list of available cross-sections in mm²
        """
        if conductor_type == 'Al':
            return sorted(self.aluminum.keys())
        elif conductor_type == 'Cu':
            return sorted(self.copper.keys())
        else:
            raise ValueError(f"Unknown conductor type: {conductor_type}")


class Circuit:
    """
    Represents an electrical circuit in a hierarchical tree structure.
    
    Each circuit can have parent and children, forming a multi-level hierarchy
    (e.g., Level 1: Mains, Level 2: Feeders, Level 3: Branch circuits).
    
    Attributes:
        code: Unique identifier (e.g., 'A', 'A-1', 'A-1-a')
        length: Cable length in meters
        min_section_size: Minimum allowed cross-section in mm²
        conductor_type: 'Al' or 'Cu'
        current: Operating current in Amperes
        voltage_specific: Circuit-specific voltage override (None to use level default)
        initial_conductors: Number of parallel conductors (1 or more)
        temperature_specific: Circuit-specific temperature override (None to use context default)
        parent: Reference to parent circuit (None for root-level circuits)
        children: List of child circuits
        level: Hierarchy level (1 for root, 2 for children of root, etc.)
    """
    
    def __init__(
        self,
        code: str,
        length: float,
        min_section_size: float,
        conductor_type: str,
        current: float,
        voltage_specific: Optional[float] = None,
        initial_conductors: int = 1,
        temperature_specific: Optional[float] = None,
        derating_factor: Optional[float] = None,
        is_enterrado: bool = False
    ):
        """
        Initialize a Circuit instance.
        
        Args:
            code: Unique circuit identifier
            length: Cable length in meters
            min_section_size: Minimum cross-section in mm²
            conductor_type: 'Al' or 'Cu'
            current: Operating current in A
            voltage_specific: Override voltage (V), None to use level default
            initial_conductors: Number of parallel conductors per phase
            temperature_specific: Override temperature (°C), None to use context default
        """
        self.code = code
        self.length = length
        self.min_section_size = min_section_size
        self.conductor_type = conductor_type
        self.current = current
        self.voltage_specific = voltage_specific
        self.initial_conductors = initial_conductors
        self.temperature_specific = temperature_specific
        self.derating_factor = derating_factor
        self.is_enterrado = is_enterrado
        
        # Hierarchy relationships
        self.parent: Optional[Circuit] = None
        self.children: List[Circuit] = []
        
        # Calculate level from code structure (e.g., 'A-1-a' has 3 levels)
        self.level = code.count('-') + 1

    def add_child(self, child: 'Circuit') -> None:
        """
        Add a child circuit and establish bidirectional relationship.
        
        Args:
            child: Circuit to add as child
        """
        if child not in self.children:
            self.children.append(child)
            child.parent = self

    def get_ancestor_chain(self) -> List['Circuit']:
        """
        Get ordered list of ancestors from root to this circuit.
        
        Returns:
            List of circuits from root (level 1) to current circuit
        """
        chain: List[Circuit] = []
        current: Optional[Circuit] = self
        
        while current is not None:
            chain.insert(0, current)
            current = current.parent
        
        return chain

    def is_at_level(self, level: int) -> bool:
        """
        Check if circuit belongs to a specific hierarchy level.
        
        Args:
            level: Level number to check (1, 2, 3, ...)
            
        Returns:
            True if circuit is at the specified level
        """
        return self.level == level

    def get_children_at_level(self, target_level: int) -> List['Circuit']:
        """
        Recursively get all descendant circuits at a specific level.
        
        Args:
            target_level: Target hierarchy level
            
        Returns:
            List of descendant circuits at target_level
        """
        if target_level < self.level:
            return []
        
        if target_level == self.level:
            return [self]
        
        descendants: List[Circuit] = []
        for child in self.children:
            descendants.extend(child.get_children_at_level(target_level))
        
        return descendants

    def __repr__(self) -> str:
        return f"<Circuit {self.code} (Level {self.level}, {self.conductor_type}, {self.current}A)>"


@dataclass
class OptimizationContext:
    """
    Global configuration for the optimization project.
    
    Contains all system-wide parameters including voltage levels, electrical
    system types, power factors, and constraints for each hierarchy level.
    
    Attributes:
        temperature: Global conductor operating temperature in °C
        voltage_levels: Voltage for each hierarchy level (V)
        system_types: Electrical system type per level ('AC_TRI', 'AC_MONO', 'DC', 'DC_MONO')
        power_factors: Power factor (cos φ) per level
        frecuencia: Frequency in Hz per level (for AC systems)
        disposicion: Cable layout per level ('Tresbolillo', 'Plana')
        level_allowed_sections: Allowed cross-sections (mm²) per level
        level_allow_double: Whether double conductor is allowed per level
        rules: List of optimization rules to enforce
        cable_catalog: Unified cable catalog (Al + Cu)
    """
    temperature: float
    voltage_levels: Dict[int, float]
    system_types: Dict[int, str]
    power_factors: Dict[int, float]
    frecuencia: Dict[int, float]
    disposicion: Dict[int, str]
    level_allowed_sections: Dict[int, List[float]]
    level_allow_double: Dict[int, List[float]]
    derating_factor: Optional[float] = None
    level_ampacities: Dict[int, Dict[float, Dict[str, float]]] = field(default_factory=dict)
    level_t_ref_suelo: Dict[int, float] = field(default_factory=dict)
    level_t_ref_aire: Dict[int, float] = field(default_factory=dict)
    level_t_max: Dict[int, float] = field(default_factory=dict)
    rules: List['BaseRule'] = field(default_factory=list)
    cable_catalog: CableCatalog = field(default_factory=lambda: CableCatalog({}, {}))

    def __post_init__(self) -> None:
        """Validate context configuration."""
        if self.temperature <= 0:
            raise ValueError(f"Temperature must be positive, got {self.temperature}")
        
        # Validate that all levels have consistent configuration
        levels = set(self.voltage_levels.keys())
        if not (
            levels == set(self.system_types.keys()) == 
            set(self.power_factors.keys()) ==
            set(self.frecuencia.keys()) ==
            set(self.disposicion.keys()) ==
            set(self.level_allowed_sections.keys()) ==
            set(self.level_allow_double.keys())
        ):
            raise ValueError("All level configuration dictionaries must have the same keys")

    def get_level_voltage(self, circuit: Circuit) -> float:
        """
        Get effective voltage for a circuit (specific or level default).
        
        Args:
            circuit: Circuit to get voltage for
            
        Returns:
            Voltage in V
        """
        if circuit.voltage_specific is not None and circuit.voltage_specific > 0:
            return circuit.voltage_specific
        return self.voltage_levels[circuit.level]

    def get_level_temperature(self, circuit: Circuit) -> float:
        """
        Get effective temperature for a circuit (specific or global default).
        
        Args:
            circuit: Circuit to get temperature for
            
        Returns:
            Temperature in °C
        """
        if circuit.temperature_specific is not None:
            return circuit.temperature_specific
        return self.temperature


@dataclass
class OptimizationResult:
    """
    Standardized output from optimization strategy execution.
    
    Attributes:
        solution_map: Mapping of circuit codes to (section, n_conductors) tuples
        total_cost: Total project cost in € (cable material only)
        violations: List of human-readable rule violations (empty if all pass)
        execution_time: Time taken to compute solution in seconds
        metadata: Additional information (strategy name, iterations, etc.)
    """
    solution_map: Dict[str, Tuple[float, int]]
    total_cost: float
    violations: List[str] = field(default_factory=list)
    execution_time: float = 0.0
    metadata: Dict[str, any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Check if solution has no rule violations."""
        return len(self.violations) == 0

    def add_violation(self, violation: str) -> None:
        """Add a rule violation message."""
        self.violations.append(violation)

    def __repr__(self) -> str:
        status = "VALID" if self.is_valid else f"INVALID ({len(self.violations)} violations)"
        return (
            f"<OptimizationResult {status}, "
            f"cost={self.total_cost:.2f}€, "
            f"time={self.execution_time:.2f}s>"
        )
