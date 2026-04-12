"""
Cable Repository - Data source for cable catalog information.

This module contains the exact numerical data from the legacy catalogo_cables.py.
All values are preserved exactly as provided - DO NOT MODIFY.

The copper catalog is unified from Cu_DC_MONO (small sections) and Cu_default
(large sections), with Cu_DC_MONO taking priority for overlapping sections.
"""

from typing import Dict
from domain.models import CableProperties, CableCatalog


class CableRepository:
    """
    Repository providing access to cable catalog data.
    
    Implements unified catalog architecture:
    - Aluminum: 14 sections (16-630 mm²)
    - Copper: 17 sections unified from Cu_DC_MONO + Cu_default
    """
    
    def __init__(self):
        """Initialize repository with all catalog data."""
        self._aluminum_catalog = self._load_aluminum()
        self._copper_catalog = self._load_copper_unified()
        
        # Create unified catalog object
        self.catalog = CableCatalog(
            aluminum=self._aluminum_catalog,
            copper=self._copper_catalog
        )
    
    def get_catalog(self) -> CableCatalog:
        """
        Get the complete unified cable catalog.
        
        Returns:
            CableCatalog with aluminum and copper sections
        """
        return self.catalog
    
    def _load_aluminum(self) -> Dict[float, CableProperties]:
        """
        Load aluminum conductor catalog.
        
        CRITICAL: Values extracted from catalogo_cables.py - DO NOT MODIFY.
        
        Returns:
            Dictionary mapping section size (mm²) to cable properties
        """
        return {
            # Precios extrapolados (aprox. 60-70% del Cobre)
            16.0: CableProperties(r_ohm_km=1.91, d=4.65, D=8.3, price=3.0),
            25.0: CableProperties(r_ohm_km=1.2, d=5.85, D=9.9, price=4.0),
            35.0: CableProperties(r_ohm_km=0.868, d=6.75, D=10.8, price=5.2),
            50.0: CableProperties(r_ohm_km=0.641, d=8.0, D=12.5, price=6.5),
            70.0: CableProperties(r_ohm_km=0.443, d=10.0, D=14.5, price=8.5),
            95.0: CableProperties(r_ohm_km=0.32, d=11.2, D=15.8, price=11.0),
            120.0: CableProperties(r_ohm_km=0.253, d=12.6, D=17.4, price=14.0),
            150.0: CableProperties(r_ohm_km=0.206, d=13.85, D=19.3, price=17.5),
            185.0: CableProperties(r_ohm_km=0.164, d=16.0, D=21.4, price=22.0),
            240.0: CableProperties(r_ohm_km=0.125, d=18.0, D=24.2, price=28.0),
            300.0: CableProperties(r_ohm_km=0.1, d=20.0, D=26.7, price=35.0),
            400.0: CableProperties(r_ohm_km=0.0778, d=22.6, D=30.0, price=45.0),
            500.0: CableProperties(r_ohm_km=0.0605, d=None, D=None, price=58.0),
            630.0: CableProperties(r_ohm_km=0.0469, d=None, D=None, price=72.0),
        }
    
    def _load_copper_default(self) -> Dict[float, CableProperties]:
        """
        Load copper default catalog (AC_TRI and general use).
        
        CRITICAL: Values extracted from catalogo_cables.py - DO NOT MODIFY.
        This is used internally to build the unified copper catalog.
        
        Returns:
            Dictionary mapping section size (mm²) to cable properties
        """
        return {
            # Precios extrapolados realistas
            16.0: CableProperties(r_ohm_km=1.21, d=4.51, D=10.6, price=4.50),
            25.0: CableProperties(r_ohm_km=0.78, d=5.64, D=12.3, price=5.80),
            35.0: CableProperties(r_ohm_km=0.55, d=6.68, D=13.8, price=7.50),
            50.0: CableProperties(r_ohm_km=0.38, d=7.98, D=15.4, price=9.50),
            70.0: CableProperties(r_ohm_km=0.27, d=9.44, D=17.3, price=12.50),  # Extrapolado
            95.0: CableProperties(r_ohm_km=0.2, d=11.0, D=19.2, price=16.80),   # Extrapolado
            120.0: CableProperties(r_ohm_km=0.16, d=12.36, D=21.3, price=21.00), # Extrapolado
            150.0: CableProperties(r_ohm_km=0.12, d=13.82, D=26.00, price=26.00), # Precio añadido
            185.0: CableProperties(r_ohm_km=0.1, d=15.35, D=25.6, price=33.00),  # Extrapolado
            240.0: CableProperties(r_ohm_km=0.08, d=17.48, D=28.6, price=42.00), # Extrapolado
            300.0: CableProperties(r_ohm_km=0.06, d=19.54, D=31.3, price=53.00), # Extrapolado
            400.0: CableProperties(r_ohm_km=0.05, d=22.57, D=36.0, price=70.00), # Extrapolado
        }
    
    def _load_copper_dc_mono(self) -> Dict[float, CableProperties]:
        """
        Load copper DC/Mono catalog with explicit reactance values.
        
        CRITICAL: Exact prices provided by user - DO NOT MODIFY.
        This is used internally to build the unified copper catalog.
        
        Returns:
            Dictionary mapping section size (mm²) to cable properties
        """
        return {
            # PRECIOS EXACTOS PROPORCIONADOS POR EL USUARIO
            1.5: CableProperties(r_ohm_km=13.7, d=None, D=None, price=0.85, x_ohm_km=0.0),
            2.5: CableProperties(r_ohm_km=8.21, d=None, D=None, price=0.95, x_ohm_km=0.0),
            4.0: CableProperties(r_ohm_km=5.09, d=None, D=None, price=1.65, x_ohm_km=0.0),
            6.0: CableProperties(r_ohm_km=3.39, d=None, D=None, price=2.25, x_ohm_km=0.0),
            10.0: CableProperties(r_ohm_km=1.95, d=None, D=None, price=4.00, x_ohm_km=0.0),
            16.0: CableProperties(r_ohm_km=1.24, d=None, D=None, price=4.50, x_ohm_km=0.0),
            25.0: CableProperties(r_ohm_km=0.795, d=None, D=None, price=5.80, x_ohm_km=0.0),
            35.0: CableProperties(r_ohm_km=0.565, d=None, D=None, price=7.50, x_ohm_km=0.0),
            50.0: CableProperties(r_ohm_km=0.393, d=None, D=None, price=9.50, x_ohm_km=0.0),
        }
    
    def _load_copper_unified(self) -> Dict[float, CableProperties]:
        """
        Load unified copper catalog merging Cu_DC_MONO and Cu_default.
        
        Strategy:
        1. Load all sections from Cu_default as base
        2. Overwrite with Cu_DC_MONO sections (priority for small sections)
        
        Result: 17 unique sections
        - From Cu_DC_MONO: 1.5, 2.5, 4, 6, 10, 16, 25, 35, 50 mm²
        - From Cu_default: 70, 95, 120, 150, 185, 240, 300, 400 mm²
        
        Note: Sections 16, 25, 35, 50 exist in both catalogs.
              Cu_DC_MONO values take priority (overwrite).
        
        Returns:
            Unified dictionary with all copper sections
        """
        catalog = {}
        
        # Step 1: Load Cu_default (base for large sections)
        for section, props in self._load_copper_default().items():
            catalog[section] = props
        
        # Step 2: Overwrite with Cu_DC_MONO (priority for small sections)
        for section, props in self._load_copper_dc_mono().items():
            catalog[section] = props
        
        return catalog
    
    def get_available_sections(self, conductor_type: str) -> list[float]:
        """
        Get sorted list of available cross-sections for a conductor type.
        
        Args:
            conductor_type: 'Al' or 'Cu'
            
        Returns:
            Sorted list of available sections in mm²
        """
        return self.catalog.get_available_sections(conductor_type)
    
    def get_section_properties(self, section: float, conductor_type: str) -> CableProperties:
        """
        Get properties for a specific cable section.
        
        Args:
            section: Cross-sectional area in mm²
            conductor_type: 'Al' or 'Cu'
            
        Returns:
            CableProperties object
            
        Raises:
            ValueError: If section or conductor type not found
        """
        return self.catalog.get_properties(section, conductor_type)
    
    def get_section_price(self, section: float, conductor_type: str) -> float:
        """
        Get price per meter for a specific cable section.
        
        Args:
            section: Cross-sectional area in mm²
            conductor_type: 'Al' or 'Cu'
            
        Returns:
            Price in €/m
        """
        return self.catalog.get_price(section, conductor_type)
    
    def get_summary(self) -> str:
        """
        Get human-readable summary of catalog contents.
        
        Returns:
            Multi-line string with catalog statistics
        """
        al_sections = self.get_available_sections('Al')
        cu_sections = self.get_available_sections('Cu')
        
        return (
            f"Cable Catalog Summary:\n"
            f"  Aluminum: {len(al_sections)} sections ({min(al_sections)}-{max(al_sections)} mm²)\n"
            f"  Copper:   {len(cu_sections)} sections ({min(cu_sections)}-{max(cu_sections)} mm²)\n"
            f"  Total:    {len(al_sections) + len(cu_sections)} unique sections"
        )
