"""
Domain Physics - Pure electrical engineering calculation functions.

This module contains validated physics formulas for voltage drop calculations
in electrical distribution systems. All functions are pure (no side effects)
and use strict typing.

CRITICAL: These formulas are engineering-validated. DO NOT MODIFY numerical
constants or formula structure without electrical engineering verification.
"""

import math
from typing import Tuple, Optional


def calculate_resistance_at_temperature(
    r_20: float,
    conductor_type: str,
    temperature: float
) -> float:
    """
    Calculate conductor resistance at operating temperature.
    
    Uses temperature coefficient correction formula:
    R(T) = R(20°C) × [1 + α × (T - 20)]
    
    Args:
        r_20: Resistance at 20°C in Ω/km
        conductor_type: 'Cu' or 'Al'
        temperature: Operating temperature in °C
        
    Returns:
        Resistance at operating temperature in Ω/km
        
    Raises:
        ValueError: If conductor type is unknown
        
    Note:
        Temperature coefficients (α) are standard values:
        - Copper (Cu): 0.00393 /°C
        - Aluminum (Al): 0.00407 /°C
    """
    # Standard temperature coefficients
    ALPHA_COPPER = 0.00393  # Temperature coefficient for Cu (/°C)
    ALPHA_ALUMINUM = 0.00407  # Temperature coefficient for Al (/°C)
    
    if conductor_type == 'Cu':
        alpha = ALPHA_COPPER
    elif conductor_type == 'Al':
        alpha = ALPHA_ALUMINUM
    else:
        raise ValueError(f"Unknown conductor type: {conductor_type}. Use 'Cu' or 'Al'")
    
    return r_20 * (1 + alpha * (temperature - 20))


def calculate_conductor_temperature(
    i_design: float,
    iz_base: float,
    derating_factor: float,
    t_ref: float = 20.0,
    t_max: float = 90.0
) -> float:
    """
    Calculate dynamic conductor operating temperature based on load.
    
    Formula (IEC 60287 approximation):
    T_cond = T_ref + (T_max - T_ref) * (I_design / (Iz_base * Derating))^2
    
    Args:
        i_design: Design current (Ib) in Amperes
        iz_base: Base ampacity for the section in Amperes
        derating_factor: Derating factor (default 1.0)
        t_ref: Reference/Ground temperature in °C
        t_max: Maximum insulation temperature in °C
        
    Returns:
        Calculated conductor temperature in °C
    """
    if iz_base <= 0 or derating_factor <= 0:
        return t_max  # Fallback to max temp if invalid data
        
    iz_effective = iz_base * derating_factor
    temp_rise_ratio = (i_design / iz_effective) ** 2
    
    # Cap ratio to avoid unrealistic temperatures if overloaded
    # (Though optimization should avoid overloads, physics should be robust)
    if temp_rise_ratio > 2.0: 
        temp_rise_ratio = 2.0
        
    t_cond = t_ref + (t_max - t_ref) * temp_rise_ratio
    
    # Clamp minimum to t_ref (cannot cool below ambient by itself)
    return max(t_cond, t_ref)


def calculate_reactance_ac(
    d: float,
    D: float,
    frequency: float,
    layout: str,
    n_conductors: int
) -> float:
    """
    Calculate inductive reactance for AC systems.
    
    Formula for AC reactance:
    X = (2πf × (k + 4.6×log₁₀(D/r))) / 10000 / n_conductors
    
    where:
    - k = layout factor (0.5 for 'Tresbolillo', 0.96 for 'Plana')
    - r = conductor radius (d/2)
    - Result divided by n_conductors for parallel configuration
    
    Args:
        d: Conductor diameter in mm
        D: Distance between conductors in mm
        frequency: Electrical frequency in Hz (typically 50 or 60)
        layout: Cable layout ('Tresbolillo' or 'Plana')
        n_conductors: Number of parallel conductors per phase
        
    Returns:
        Reactance in Ω/km (0.0 if d or D is None/invalid)
        
    Note:
        Returns 0.0 if geometric parameters are missing (large sections
        without specified dimensions).
    """
    # Handle missing dimensions (common for large cable sections)
    if d is None or D is None or d <= 0 or D <= 0:
        return 0.0
    
    # Layout-dependent constant
    K_TRESBOLILLO = 0.5
    K_PLANA = 0.96
    
    if layout == 'Tresbolillo':
        k_layout = K_TRESBOLILLO
    else:  # Default to 'Plana' for any other value
        k_layout = K_PLANA
    
    # Calculate reactance
    r = d / 2.0  # Radius from diameter
    calc_log = 4.6 * math.log10(D / r)
    base_comun = 2 * math.pi * frequency
    base_X = (base_comun * (k_layout + calc_log)) / 10000
    
    # Divide by number of parallel conductors
    return base_X / n_conductors


def calculate_reactance_dc(
    x_base: float,
    n_conductors: int
) -> float:
    """
    Calculate reactance for DC systems.
    
    For DC_MONO systems, reactance is explicitly provided in catalog
    and divided by number of parallel conductors.
    
    Args:
        x_base: Base reactance from catalog in Ω/km
        n_conductors: Number of parallel conductors per phase
        
    Returns:
        Effective reactance in Ω/km
    """
    return x_base / n_conductors


def calculate_voltage_drop_dc(
    current: float,
    resistance: float,
    length_km: float
) -> float:
    """
    Calculate voltage drop for DC systems.
    
    Formula: VD = I × R × L × 2
    
    The factor of 2 accounts for the round-trip current path
    (positive and negative conductors).
    
    Args:
        current: Load current in A
        resistance: Conductor resistance at operating temp in Ω/km
        length_km: Cable length in km
        
    Returns:
        Voltage drop in V
    """
    return current * resistance * length_km * 2


def calculate_voltage_drop_ac_mono(
    current: float,
    length_km: float,
    resistance: float,
    reactance: float,
    cos_phi: float
) -> float:
    """
    Calculate voltage drop for single-phase AC systems.
    
    Formula: VD = 2 × I × L × (R×cosφ + X×sinφ)
    
    where:
    - Factor of 2 for round-trip path (phase and neutral)
    - Z_factor = R×cosφ + X×sinφ (impedance projection)
    
    Args:
        current: Load current in A
        length_km: Cable length in km
        resistance: Conductor resistance at operating temp in Ω/km
        reactance: Conductor reactance in Ω/km
        cos_phi: Power factor (typically 0.8 to 1.0)
        
    Returns:
        Voltage drop in V
    """
    sin_phi = math.sqrt(1 - (cos_phi ** 2))
    z_factor = (resistance * cos_phi) + (reactance * sin_phi)
    return 2 * current * length_km * z_factor


def calculate_voltage_drop_ac_tri(
    current: float,
    length_km: float,
    resistance: float,
    reactance: float,
    cos_phi: float
) -> float:
    """
    Calculate voltage drop for three-phase AC systems.
    
    Formula: VD = √3 × I × L × (R×cosφ + X×sinφ)
    
    where:
    - √3 factor accounts for three-phase geometry
    - Z_factor = R×cosφ + X×sinφ (impedance projection)
    
    Args:
        current: Line current in A
        length_km: Cable length in km
        resistance: Conductor resistance at operating temp in Ω/km
        reactance: Conductor reactance in Ω/km
        cos_phi: Power factor (typically 0.8 to 1.0)
        
    Returns:
        Line-to-line voltage drop in V
    """
    sin_phi = math.sqrt(1 - (cos_phi ** 2))
    z_factor = (resistance * cos_phi) + (reactance * sin_phi)
    return math.sqrt(3) * current * length_km * z_factor


def calculate_voltage_drop_percent(
    voltage_drop_volts: float,
    nominal_voltage: float
) -> float:
    """
    Convert absolute voltage drop to percentage.
    
    Args:
        voltage_drop_volts: Voltage drop in V
        nominal_voltage: Nominal system voltage in V
        
    Returns:
        Voltage drop as percentage (e.g., 2.5 for 2.5%)
        
    Note:
        Returns 0.0 if nominal voltage is zero (to avoid division by zero)
    """
    if nominal_voltage == 0:
        return 0.0
    return (voltage_drop_volts / nominal_voltage) * 100


def calculate_parallel_resistance(
    r_single: float,
    n_conductors: int
) -> float:
    """
    Calculate equivalent resistance for parallel conductors.
    
    For n identical conductors in parallel:
    R_equivalent = R_single / n
    
    Args:
        r_single: Resistance of a single conductor in Ω/km
        n_conductors: Number of parallel conductors
        
    Returns:
        Equivalent resistance in Ω/km
    """
    if n_conductors <= 0:
        raise ValueError(f"Number of conductors must be positive, got {n_conductors}")
    return r_single / n_conductors


# High-level calculation function that integrates all physics
def calculate_circuit_voltage_drop(
    current: float,
    length_m: float,
    r_20: float,
    conductor_type: str,
    temperature: float,
    system_type: str,
    nominal_voltage: float,
    n_conductors: int,
    cos_phi: float = 1.0,
    x_ohm_km: float = 0.0,
    d: Optional[float] = None,
    D: Optional[float] = None,
    frequency: float = 50.0,
    layout: str = 'Plana'
) -> Tuple[float, float]:
    """
    Calculate voltage drop for a circuit considering all parameters.
    
    This is the main calculation function that orchestrates all physics formulas.
    
    Args:
        current: Load current in A
        length_m: Cable length in meters
        r_20: Base resistance at 20°C in Ω/km
        conductor_type: 'Cu' or 'Al'
        temperature: Operating temperature in °C
        system_type: 'DC', 'DC_MONO', 'AC_MONO', or 'AC_TRI'
        nominal_voltage: System nominal voltage in V
        n_conductors: Number of parallel conductors per phase
        cos_phi: Power factor (for AC systems)
        x_ohm_km: Explicit reactance from catalog (for DC_MONO)
        d: Conductor diameter in mm (for AC reactance calculation)
        D: Distance between conductors in mm (for AC reactance)
        frequency: Electrical frequency in Hz (for AC)
        layout: Cable layout 'Tresbolillo' or 'Plana' (for AC)
        
    Returns:
        Tuple of (voltage_drop_volts, voltage_drop_percent)
        
    Note:
        Virtual circuits (length=0) return (0.0, 0.0)
    """
    # Virtual circuits have no voltage drop
    if length_m == 0.0:
        return (0.0, 0.0)
    
    length_km = length_m / 1000.0
    
    # Calculate resistance at operating temperature
    r_temp = calculate_resistance_at_temperature(r_20, conductor_type, temperature)
    
    # Apply parallel conductor effect
    r_effective = calculate_parallel_resistance(r_temp, n_conductors)
    
    # Calculate reactance based on system type
    if system_type in ['AC_TRI', 'AC_MONO']:
        x_effective = calculate_reactance_ac(d, D, frequency, layout, n_conductors)
    elif system_type == 'DC_MONO':
        x_effective = calculate_reactance_dc(x_ohm_km, n_conductors)
    else:  # Pure DC
        x_effective = 0.0
    
    # Calculate voltage drop based on system type
    if system_type in ['DC', 'DC_MONO']:
        vd_volts = calculate_voltage_drop_dc(current, r_effective, length_km)
    elif system_type == 'AC_MONO':
        vd_volts = calculate_voltage_drop_ac_mono(
            current, length_km, r_effective, x_effective, cos_phi
        )
    elif system_type == 'AC_TRI':
        vd_volts = calculate_voltage_drop_ac_tri(
            current, length_km, r_effective, x_effective, cos_phi
        )
    else:
        raise ValueError(f"Unknown system type: {system_type}")
    
    vd_percent = calculate_voltage_drop_percent(vd_volts, nominal_voltage)
    
    return (vd_volts, vd_percent)


def get_effective_conductor_temperature(
    circuit: 'Circuit',
    section: float,
    n_cond: int,
    context: 'OptimizationContext'
) -> float:
    """
    Get effective conductor temperature (static or dynamic).
    """
    t_cond = circuit.temperature_specific if circuit.temperature_specific is not None else context.temperature
    
    if circuit.level in context.level_ampacities:
        amp_map = context.level_ampacities[circuit.level]
        if section in amp_map:
            # amp_map[section] is now a dict with 'aereo' and 'enterrado'
            iz_choice = amp_map[section]
            if circuit.is_enterrado:
                iz_base = iz_choice.get('enterrado', 0.0)
                t_ref = context.level_t_ref_suelo.get(circuit.level, 25.0)
            else:
                iz_base = iz_choice.get('aereo', 0.0)
                t_ref = context.level_t_ref_aire.get(circuit.level, 40.0)
            
            t_max = context.level_t_max.get(circuit.level, 90.0)
            
            # Utilizar 1.0 si no hay factor de agrupamiento definido
            derating = circuit.derating_factor if (circuit.derating_factor is not None and circuit.derating_factor > 0) else 1.0
            
            current_per_cond = circuit.current / n_cond
            t_cond_dyn = calculate_conductor_temperature(
                i_design=current_per_cond,
                iz_base=iz_base,
                derating_factor=derating,
                t_ref=t_ref,
                t_max=t_max
            )
            t_cond = t_cond_dyn
            
    return t_cond


def get_detailed_electrical_params(
    circuit: 'Circuit',
    candidate: Tuple[float, int],
    context: 'OptimizationContext',
    cable_catalog: 'CableCatalog'
) -> dict:
    """
    Get detailed electrical parameters for a circuit with specific cable configuration.
    
    This function is useful for detailed Excel exports showing intermediate calculation
    values like R(20°C), R(Tcond), X, VD volts, and VD%.
    
    Args:
        circuit: Circuit object
        candidate: Tuple of (section, n_conductors)
        context: Optimization context with system parameters
        cable_catalog: Cable catalog for properties lookup
        
    Returns:
        Dictionary with keys:
        - R_20: Resistance at 20°C in Ω/km
        - T_cond: Operating temperature in °C (circuit-specific or global)
        - R_Tcond: Resistance at operating temperature in Ω/km
        - X: Reactance in Ω/km
        - VD_volts: Voltage drop in V
        - VD_percent: Voltage drop in %
    """
    from domain.models import Circuit, OptimizationContext, CableCatalog
    
    section, n_cond = candidate
    
    # Get cable properties
    props = cable_catalog.get_properties(section, circuit.conductor_type)
    r_20 = props.r_ohm_km
    
    t_cond = get_effective_conductor_temperature(circuit, section, n_cond, context)
    
    # Calculate resistance at operating temperature
    r_tcond_single = calculate_resistance_at_temperature(r_20, circuit.conductor_type, t_cond)
    r_tcond = calculate_parallel_resistance(r_tcond_single, n_cond)
    
    # Get system type and parameters
    system_type = context.system_types.get(circuit.level, 'AC_TRI')
    cos_phi = context.power_factors.get(circuit.level, 1.0)
    frequency = context.frecuencia.get(circuit.level, 50.0)
    layout = context.disposicion.get(circuit.level, 'Plana')
    
    # Calculate reactance
    if system_type in ['AC_TRI', 'AC_MONO']:
        x_value = calculate_reactance_ac(props.d, props.D, frequency, layout, n_cond)
    elif system_type == 'DC_MONO':
        x_value = calculate_reactance_dc(props.x_ohm_km, n_cond)
    else:
        x_value = 0.0
    
    # Get voltage for circuit
    voltage = context.get_level_voltage(circuit)
    
    # Calculate voltage drop
    vd_volts, vd_percent = calculate_circuit_voltage_drop(
        current=circuit.current,
        length_m=circuit.length,
        r_20=r_20,
        conductor_type=circuit.conductor_type,
        temperature=t_cond,
        system_type=system_type,
        nominal_voltage=voltage,
        n_conductors=n_cond,
        cos_phi=cos_phi,
        x_ohm_km=props.x_ohm_km,
        d=props.d,
        D=props.D,
        frequency=frequency,
        layout=layout
    )
    
    return {
        'R_20': r_20,
        'T_cond': t_cond,
        'R_Tcond': r_tcond,
        'X': x_value,
        'VD_volts': vd_volts,
        'VD_percent': vd_percent
    }

