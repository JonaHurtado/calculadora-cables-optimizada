"""
Domain layer - Core business models and physics calculations.
"""

from domain.models import (
    CableProperties,
    CableCatalog,
    Circuit,
    OptimizationContext,
    OptimizationResult
)

__all__ = [
    'CableProperties',
    'CableCatalog',
    'Circuit',
    'OptimizationContext',
    'OptimizationResult'
]
