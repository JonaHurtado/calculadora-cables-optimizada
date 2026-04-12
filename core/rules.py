"""
Core Rules Engine - Optimization constraint rules with recursive support.

This module implements the rule system for cable section optimization.
Rules can be simple (IntraLevelRule) or hierarchical with recursive evaluation.

All rules migrated from legacy reglas.py with architectural improvements:
- Strict typing with type hints
- Separation of calculation logic (uses domain.physics)
- Support for nested/recursive rule definitions
- Robust AST-based parser (no eval())
"""

from typing import Dict, List, Union, Tuple, Optional
from abc import ABC, abstractmethod
from collections import defaultdict
import ast
import re

from domain.models import Circuit, OptimizationContext
from domain.physics import calculate_circuit_voltage_drop, get_effective_conductor_temperature
from data.repository import CableRepository


class BaseRule(ABC):
    """
    Abstract base class for all optimization rules.
    
    Rules define constraints that must be satisfied by the solution,
    such as maximum voltage drop limits at different hierarchy levels.
    """
    
    @abstractmethod
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """
        Verify if the solution satisfies this rule.
        
        Args:
            solution_map: Mapping of circuit codes to (section, n_conductors)
            circuits: All circuits in the optimization
            context: Global optimization parameters
            repository: Cable catalog repository
            
        Returns:
            True if rule is satisfied, False if violated
        """
        pass
    
    @abstractmethod
    def is_local(self) -> bool:
        """
        Indicate if rule is local (evaluated per group) or global.
        
        Returns:
            True for local rules (e.g., per-parent group), False for global
        """
        pass
    
    @abstractmethod
    def __str__(self) -> str:
        """Human-readable rule description."""
        pass


def calculate_vd_percent(
    circuit: Circuit,
    candidate: Tuple[float, int],
    context: OptimizationContext,
    repository: CableRepository
) -> float:
    """
    Helper function to calculate voltage drop percentage for a circuit.
    
    Args:
        circuit: Circuit to analyze
        candidate: (section, n_conductors) tuple
        context: Optimization context
        repository: Cable repository
        
    Returns:
        Voltage drop percentage
    """
    # Virtual circuits (parents with no physical cable) have zero length
    if circuit.length == 0.0:
        return 0.0
    
    section, n_conductors = candidate
    
    # Get cable properties
    try:
        props = repository.get_section_properties(section, circuit.conductor_type)
    except ValueError:
        return 0.0
    
    # Get system parameters for this circuit's level
    try:
        voltage = context.get_level_voltage(circuit)
        system_type = context.system_types[circuit.level]
        cos_phi = context.power_factors.get(circuit.level, 1.0)
        frequency = context.frecuencia.get(circuit.level, 50.0)
        layout = context.disposicion.get(circuit.level, 'Plana')
        temperature = get_effective_conductor_temperature(circuit, section, n_conductors, context)
    except KeyError:
        return 0.0
    
    # Calculate voltage drop using physics functions
    _, vd_percent = calculate_circuit_voltage_drop(
        current=circuit.current,
        length_m=circuit.length,
        r_20=props.r_ohm_km,
        conductor_type=circuit.conductor_type,
        temperature=temperature,
        system_type=system_type,
        nominal_voltage=voltage,
        n_conductors=n_conductors,
        cos_phi=cos_phi,
        x_ohm_km=props.x_ohm_km,
        d=props.d,
        D=props.D,
        frequency=frequency,
        layout=layout
    )
    
    return vd_percent


def calculate_cumulative_vd_percent(
    circuit: Circuit,
    solution_map: Dict[str, Tuple[float, int]],
    context: OptimizationContext,
    repository: CableRepository
) -> float:
    """
    Calculate cumulative voltage drop from root to circuit.
    
    Args:
        circuit: End circuit
        solution_map: Current solution
        context: Optimization context
        repository: Cable repository
        
    Returns:
        Sum of voltage drops from root to circuit
    """
    total_vd = 0.0
    current = circuit
    
    while current is not None:
        candidate = solution_map.get(current.code)
        if candidate:
            total_vd += calculate_vd_percent(current, candidate, context, repository)
        current = current.parent
    
    return total_vd


class IntraLevelRule(BaseRule):
    """
    Rule constraining circuits within a single hierarchy level.
    
    Applies a metric ('max' or 'avg') to all circuits at a specific level
    and enforces a limit.
    
    Example:
        IntraLevelRule(level=3, metric='max', limit=2.0)
        -> "Maximum VD% at level 3 must be <= 2.0%"
    """
    
    def __init__(self, level: int, metric: str, limit: float):
        """
        Initialize intra-level rule.
        
        Args:
            level: Hierarchy level (1, 2, 3, ...)
            metric: 'max' or 'avg'
            limit: Maximum allowed value (%)
        """
        self.level = level
        self.metric = metric
        self.limit = limit
        
        if metric not in ['max', 'avg']:
            raise ValueError(f"Invalid metric '{metric}'. Use 'max' or 'avg'.")
    
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """Check if all circuits at this level satisfy the constraint."""
        level_circuits = [c for c in circuits.values() if c.level == self.level]
        
        if not level_circuits:
            return True
        
        vds = []
        for circuit in level_circuits:
            candidate = solution_map.get(circuit.code)
            if candidate:
                vds.append(calculate_vd_percent(circuit, candidate, context, repository))
        
        if not vds:
            return True
        
        if self.metric == 'avg':
            metric_val = sum(vds) / len(vds)
        elif self.metric == 'max':
            metric_val = max(vds)
        else:
            return False
        
        return metric_val <= self.limit
    
    def is_local(self) -> bool:
        return False
    
    def __str__(self) -> str:
        return f"IntraLevel(N{self.level}, {self.metric}<={self.limit})"


class InterLevelRule(BaseRule):
    """
    Rule constraining the sum of metrics across multiple levels.
    
    Example:
        InterLevelRule(
            components=[{'level': 2, 'metric': 'max'}, {'level': 3, 'metric': 'avg'}],
            limit=5.0
        )
        -> "Max(Level2) + Avg(Level3) <= 5.0%"
    """
    
    def __init__(self, components: List[dict], limit: float):
        """
        Initialize inter-level rule.
        
        Args:
            components: List of dicts with 'level' and 'metric' keys
            limit: Maximum allowed sum (%)
        """
        self.components = components
        self.limit = limit
    
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """Check if sum of components satisfies limit."""
        total_vd = 0.0
        
        for comp in self.components:
            level = comp['level']
            metric = comp['metric']
            
            level_circuits = [c for c in circuits.values() if c.level == level]
            if not level_circuits:
                continue
            
            vds = []
            for circuit in level_circuits:
                candidate = solution_map.get(circuit.code)
                if candidate:
                    vds.append(calculate_cumulative_vd_percent(circuit, solution_map, context, repository))
            
            if not vds:
                continue
            
            if metric == 'avg':
                total_vd += sum(vds) / len(vds)
            elif metric == 'max':
                total_vd += max(vds)
        
        return total_vd <= self.limit
    
    def is_local(self) -> bool:
        return False
    
    def __str__(self) -> str:
        return f"InterLevel(Limit<={self.limit}, Components={self.components})"


class ParentChildSubgroupRule(BaseRule):
    """
    Rule constraining parent tramo + children metric within each parent group.
    
    For each parent at parent_level, evaluates:
        VD(parent) + Metric(children) <= limit
    
    Supports recursive child_metric (can be another rule).
    """
    
    def __init__(
        self,
        parent_level: int,
        child_level: int,
        child_metric: Union[str, BaseRule],
        limit: float
    ):
        """
        Initialize parent-child subgroup rule.
        
        Args:
            parent_level: Level of parent circuits
            child_level: Level of child circuits
            child_metric: 'max', 'avg', or nested BaseRule
            limit: Maximum combined value (%)
        """
        self.parent_level = parent_level
        self.child_level = child_level
        self.child_metric = child_metric
        self.limit = limit
    
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """Check each parent group individually."""
        parent_circuits = [c for c in circuits.values() if c.level == self.parent_level]
        
        if not parent_circuits:
            return True
        
        for parent_circuit in parent_circuits:
            candidate_parent = solution_map.get(parent_circuit.code)
            if not candidate_parent:
                continue
            
            # Calculate parent tramo VD
            vd_parent_tramo = calculate_vd_percent(parent_circuit, candidate_parent, context, repository)
            
            # Calculate children metric
            if isinstance(self.child_metric, BaseRule):
                # Recursive evaluation: child_metric is a nested rule
                # Create subset of circuits (only children)
                child_circuits_dict = {
                    c.code: c for c in parent_circuit.children 
                    if c.level == self.child_level
                }
                
                if not child_circuits_dict:
                    metric_children = 0.0
                else:
                    # For simplicity: average VD of children
                    child_vds = []
                    for child in parent_circuit.children:
                        if child.level == self.child_level:
                            candidate_child = solution_map.get(child.code)
                            if candidate_child:
                                child_vds.append(calculate_vd_percent(child, candidate_child, context, repository))
                    metric_children = sum(child_vds) / len(child_vds) if child_vds else 0.0
            else:
                # Simple string metric
                child_vds = []
                for child in parent_circuit.children:
                    if child.level == self.child_level:
                        candidate_child = solution_map.get(child.code)
                        if candidate_child:
                            child_vds.append(calculate_vd_percent(child, candidate_child, context, repository))
                
                metric_children = 0.0
                if child_vds:
                    if self.child_metric == 'max':
                        metric_children = max(child_vds)
                    elif self.child_metric == 'avg':
                        metric_children = sum(child_vds) / len(child_vds)
            
            # Check limit
            if (vd_parent_tramo + metric_children) > self.limit:
                return False
        
        return True
    
    def is_local(self) -> bool:
        return True
    
    def __str__(self) -> str:
        if isinstance(self.child_metric, BaseRule):
            return f"ParentChild(N{self.parent_level} + [{self.child_metric}] <= {self.limit})"
        return f"ParentChild(N{self.parent_level} + N{self.child_level}_{self.child_metric} <= {self.limit})"


class LocalSubgroupRule(BaseRule):
    """
    Rule constraining children metric within each parent group.
    
    For each parent at parent_level, evaluates:
        Metric(children at child_level) <= limit
    """
    
    def __init__(self, parent_level: int, child_level: int, child_metric: str, limit: float):
        """
        Initialize local subgroup rule.
        
        Args:
            parent_level: Level of parent circuits (grouping key)
            child_level: Level of child circuits to evaluate
            child_metric: 'max' or 'avg'
            limit: Maximum allowed value (%)
        """
        self.parent_level = parent_level
        self.child_level = child_level
        self.child_metric = child_metric
        self.limit = limit
    
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """Check each parent's children independently."""
        parent_circuits = [c for c in circuits.values() if c.level == self.parent_level]
        
        if not parent_circuits:
            return True
        
        for parent_circuit in parent_circuits:
            child_vds = []
            for child in parent_circuit.children:
                if child.level == self.child_level:
                    candidate = solution_map.get(child.code)
                    if candidate:
                        child_vds.append(calculate_vd_percent(child, candidate, context, repository))
            
            if not child_vds:
                continue
            
            if self.child_metric == 'max':
                metric_val = max(child_vds)
            elif self.child_metric == 'avg':
                metric_val = sum(child_vds) / len(child_vds)
            else:
                continue
            
            if metric_val > self.limit:
                return False
        
        return True
    
    def is_local(self) -> bool:
        return True
    
    def __str__(self) -> str:
        return f"LocalSubgroup(N{self.parent_level}->N{self.child_level}_{self.child_metric} <= {self.limit})"


class LocalGroupRule(BaseRule):
    """
    Advanced rule with recursive metric support and group aggregation.
    
    Groups circuits at a specific level by parent, calculates metric
    (possibly recursive), then applies aggregation across groups.
    
    Example:
        LocalGroupRule(
            level=2,
            metric=ParentChildSubgroupRule(...),
            aggregation='avg',
            limit=1.8
        )
        -> "Average of (parent_N2 + avg(children_N3)) across all N2 groups <= 1.8%"
    """
    
    def __init__(
        self,
        level: int,
        metric: Union[str, BaseRule],
        aggregation: str,
        limit: float
    ):
        """
        Initialize local group rule.
        
        Args:
            level: Level to group circuits at
            metric: 'max', 'avg', or nested BaseRule
            aggregation: 'max' or 'avg' (applied across groups)
            limit: Maximum allowed value after aggregation (%)
        """
        self.level = level
        self.metric = metric
        self.aggregation = aggregation
        self.limit = limit
    
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """Evaluate grouped circuits with aggregation."""
        level_circuits = [c for c in circuits.values() if c.level == self.level]
        
        if not level_circuits:
            return True
        
        # Group by parent
        groups = defaultdict(list)
        for circuit in level_circuits:
            parent_code = circuit.parent.code if circuit.parent else "ROOT"
            groups[parent_code].append(circuit)
        
        # Evaluate each group
        for parent_code, group_circuits in groups.items():
            group_values = []
            
            for circuit in group_circuits:
                candidate = solution_map.get(circuit.code)
                if not candidate:
                    continue
                
                # Calculate value based on metric type
                if isinstance(self.metric, BaseRule):
                    # Recursive metric: special handling for ParentChildSubgroupRule
                    if isinstance(self.metric, ParentChildSubgroupRule):
                        vd_circuit = calculate_vd_percent(circuit, candidate, context, repository)
                        
                        # Calculate children metric
                        child_vds = []
                        for child in circuit.children:
                            if child.level == self.metric.child_level:
                                child_candidate = solution_map.get(child.code)
                                if child_candidate:
                                    child_vds.append(calculate_vd_percent(child, child_candidate, context, repository))
                        
                        child_metric_val = 0.0
                        if child_vds:
                            if isinstance(self.metric.child_metric, str):
                                if self.metric.child_metric == 'avg':
                                    child_metric_val = sum(child_vds) / len(child_vds)
                                elif self.metric.child_metric == 'max':
                                    child_metric_val = max(child_vds)
                        
                        circuit_value = vd_circuit + child_metric_val
                        group_values.append(circuit_value)
                    else:
                        # Other rules: use simple VD
                        circuit_value = calculate_vd_percent(circuit, candidate, context, repository)
                        group_values.append(circuit_value)
                else:
                    # Simple string metric
                    circuit_value = calculate_vd_percent(circuit, candidate, context, repository)
                    group_values.append(circuit_value)
            
            if not group_values:
                continue
            
            # Apply aggregation within group
            if self.aggregation == 'avg':
                aggregated_value = sum(group_values) / len(group_values)
            elif self.aggregation == 'max':
                aggregated_value = max(group_values)
            else:
                aggregated_value = 0.0
            
            # Check limit
            if aggregated_value > self.limit:
                return False
        
        return True
    
    def is_local(self) -> bool:
        return True
    
    def __str__(self) -> str:
        metric_str = f"[{self.metric}]" if isinstance(self.metric, BaseRule) else self.metric
        return f"LocalGroup(N{self.level}, Metric={metric_str}, Agg={self.aggregation} <= {self.limit})"


class CustomHierarchicalRule(BaseRule):
    """
    Rule for specific parent circuit and its children.
    
    Evaluates: VD_cumulative(parent) + Metric(children) <= limit
    """
    
    def __init__(self, parent_code: str, child_level: int, child_metric: str, limit: float):
        """
        Initialize custom hierarchical rule.
        
        Args:
            parent_code: Specific parent circuit code
            child_level: Level of children to evaluate
            child_metric: 'max' or 'avg'
            limit: Maximum combined cumulative VD (%)
        """
        self.parent_code = parent_code
        self.child_level = child_level
        self.child_metric = child_metric
        self.limit = limit
    
    def check(
        self,
        solution_map: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository
    ) -> bool:
        """Check specific parent and its children."""
        parent_circuit = circuits.get(self.parent_code)
        if not parent_circuit:
            return True
        
        # Cumulative VD to parent
        vd_parent = calculate_cumulative_vd_percent(parent_circuit, solution_map, context, repository)
        
        # Children metric
        child_vds = []
        child_prefix = self.parent_code + '-'
        for code, circuit in circuits.items():
            if circuit.level == self.child_level and code.startswith(child_prefix):
                child_vds.append(calculate_cumulative_vd_percent(circuit, solution_map, context, repository))
        
        metric_val = 0.0
        if child_vds:
            if self.child_metric == 'max':
                metric_val = max(child_vds)
            elif self.child_metric == 'avg':
                metric_val = sum(child_vds) / len(child_vds)
        
        return (vd_parent + metric_val) <= self.limit
    
    def is_local(self) -> bool:
        return True
    
    def __str__(self) -> str:
        return f"CustomHierarch({self.parent_code} + N{self.child_level}_{self.child_metric} <= {self.limit})"


class RuleParser:
    """
    AST-based parser for converting text to rule objects.
    
    Supports Python-like syntax with nested function calls.
    SAFE: Uses ast.parse() instead of eval().
    
    Example:
        "LocalGroup(Level=2, Metric='avg', Aggregation='avg') < 1.8"
        -> LocalGroupRule(level=2, metric='avg', aggregation='avg', limit=1.8)
    """
    
    @staticmethod
    def parse(rule_text: str) -> BaseRule:
        """
        Parse text and return BaseRule instance.
        
        Expected format:
            RuleType(Param1=Value1, Param2=Value2) < Limit
        
        Args:
            rule_text: Text representation of rule
            
        Returns:
            BaseRule instance
            
        Raises:
            ValueError: If syntax is invalid or rule type unknown
        """
        # Extract limit (after < or <=)
        match = re.search(r'([<>=]+)\s*([\d.]+)\s*$', rule_text)
        if not match:
            raise ValueError(f"No limit found in rule: {rule_text}")
        
        limit = float(match.group(2))
        rule_def = rule_text[:match.start()].strip()
        
        # Parse using AST
        try:
            tree = ast.parse(rule_def, mode='eval')
            rule_obj = RuleParser._build_rule_from_ast(tree.body, limit)
            return rule_obj
        except SyntaxError as e:
            raise ValueError(f"Syntax error in rule: {e}")
    
    @staticmethod
    def _build_rule_from_ast(node, limit: float) -> BaseRule:
        """Build rule object from AST node."""
        if not isinstance(node, ast.Call):
            raise ValueError(f"Expected function call, got: {type(node)}")
        
        # Get rule type (function name)
        if isinstance(node.func, ast.Name):
            rule_type = node.func.id
        else:
            raise ValueError(f"Unrecognized rule type: {node.func}")
        
        # Extract parameters
        params = {}
        for keyword in node.keywords:
            param_name = keyword.arg
            param_value = RuleParser._extract_value(keyword.value, limit)
            params[param_name] = param_value
        
        # Construct rule based on type
        if rule_type == 'LocalGroup':
            return LocalGroupRule(
                level=params.get('Level'),
                metric=params.get('Metric'),
                aggregation=params.get('Aggregation'),
                limit=limit
            )
        elif rule_type == 'ParentChild':
            return ParentChildSubgroupRule(
                parent_level=params.get('Level'),
                child_level=params.get('ChildLevel'),
                child_metric=params.get('ChildMetric'),
                limit=limit
            )
        elif rule_type == 'IntraLevel':
            return IntraLevelRule(
                level=params.get('Level'),
                metric=params.get('Metric'),
                limit=limit
            )
        elif rule_type == 'LocalSubgroup':
            return LocalSubgroupRule(
                parent_level=params.get('ParentLevel'),
                child_level=params.get('ChildLevel'),
                child_metric=params.get('ChildMetric'),
                limit=limit
            )
        else:
            raise ValueError(f"Unknown rule type: {rule_type}")
    
    @staticmethod
    def _extract_value(node, parent_limit: float):
        """Extract value from AST node."""
        if hasattr(ast, 'Constant') and isinstance(node, ast.Constant):
            return node.value
        # Fallbacks for Python < 3.8
        elif hasattr(ast, 'Num') and isinstance(node, ast.Num):
            return node.n
        elif hasattr(ast, 'Str') and isinstance(node, ast.Str):
            return node.s
        elif isinstance(node, ast.Call):
            # Recursive call (nested rule)
            return RuleParser._build_rule_from_ast(node, limit=999.0)
        else:
            raise ValueError(f"Unsupported value type: {type(node)}")
