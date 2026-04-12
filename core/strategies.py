"""
Core Optimization Strategies - BFTB (Bang-For-The-Buck) Algorithm.
OPTIMIZED VERSION
"""

from typing import Dict, List, Tuple, Optional, Any
from abc import ABC, abstractmethod
from collections import defaultdict
import time

from domain.models import Circuit, OptimizationContext, OptimizationResult
from domain.physics import calculate_circuit_voltage_drop, calculate_conductor_temperature, get_effective_conductor_temperature
from data.repository import CableRepository
from core.rules import (
    BaseRule, IntraLevelRule, LocalSubgroupRule,
    ParentChildSubgroupRule, LocalGroupRule, CustomHierarchicalRule
)


class IOptimizationStrategy(ABC):
    @abstractmethod
    def execute(
        self,
        initial_solution: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository,
        candidate_lists: Dict[int, List[Tuple[float, int]]]
    ) -> OptimizationResult:
        pass


class BFTBStrategy(IOptimizationStrategy):
    """
    Bang-For-The-Buck optimization strategy (v3) - Dynamic Evaluation.
    
    Optimizations:
    - Pre-calculated VD matrix (O(1) lookups vs O(N) physics calcs).
    - Pre-calculated Cost matrix.
    - Structural indexing of circuits by level.
    - Full candidate evaluation (all superior sections, not just next).
    - Dynamic delta calculation (always relative to current section).
    """
    
    def __init__(self, repository: CableRepository):
        self.repository = repository
        # Cache structures
        self._vd_matrix: Dict[str, Dict[Tuple[float, int], float]] = {}
        self._cost_matrix: Dict[str, Dict[Tuple[float, int], float]] = {}
        self._circuits_by_level: Dict[int, List[Circuit]] = defaultdict(list)
        
    def execute(
        self,
        initial_solution: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository,
        candidate_lists: Dict[int, List[Tuple[float, int]]]
    ) -> OptimizationResult:
        """Execute BFTB optimization algorithm with pre-computation."""
        start_time = time.time()
        
        self.circuits = circuits
        self.context = context
        self.candidate_lists = candidate_lists
        
        print("=" * 60)
        print("INICIANDO BFTB v3 (Dynamic Evaluation)")
        print("=" * 60)
        
        # --- STEP 0: PRE-COMPUTATION (The Speedup Secret) ---
        self._precompute_matrices()
        
        sol = initial_solution.copy()
        
        # PHASE A: Critical Rules (MAX constraints)
        sol = self._phase_a_critical_rules(sol)
        
        # Snapshot after Phase A (used for child reset heuristic)
        snapshot_fase_a = sol.copy()
        
        # PHASE B: Efficiency Rules (AVG constraints)
        # Deltas are now calculated on-the-fly from matrices (always correct)
        sol = self._phase_b_efficiency_rules(sol, snapshot_fase_a)
        
        # PHASE C: Advanced Rules (LocalGroup, Custom)
        sol = self._phase_c_advanced_rules(sol)
        
        # Calculate final results
        total_cost = self._calculate_total_cost(sol)
        violations = self._check_all_rules(sol)
        
        execution_time = time.time() - start_time
        
        print("=" * 60)
        print(f"BFTB v3 completado en {execution_time:.2f}s")
        print(f"Coste total: {total_cost:.2f}€")
        print(f"Violaciones: {len(violations)}")
        print("=" * 60)
        
        return OptimizationResult(
            solution_map=sol,
            total_cost=total_cost,
            violations=violations,
            execution_time=execution_time,
            metadata={'strategy': 'BFTB_v3_Dynamic', 'phases': 3}
        )

    def _precompute_matrices(self):
        """
        Builds lookup tables for VD% and Cost for ALL possible candidates.
        This eliminates repeated calls to physics engine.
        """
        print("  >> Pre-computing physics matrices...")
        
        # 1. Index circuits by level
        self._circuits_by_level.clear()
        for c in self.circuits.values():
            self._circuits_by_level[c.level].append(c)
            
        # 2. Build Matrices
        for level, c_list in self._circuits_by_level.items():
            candidates = self.candidate_lists.get(level, [])
            if not candidates:
                continue
            
            # Fetch level-constant context params once per level
            try:
                sys_type = self.context.system_types[level]
                voltage = self.context.voltage_levels[level]
                cos_phi = self.context.power_factors.get(level, 1.0)
                freq = self.context.frecuencia.get(level, 50.0)
                layout = self.context.disposicion.get(level, 'Plana')
            except KeyError:
                continue # Skip if config missing
                
            for circuit in c_list:
                self._vd_matrix[circuit.code] = {}
                self._cost_matrix[circuit.code] = {}
                
                for cand in candidates:
                    section, n_cond = cand
                    
                    # Determine temperature for this candidate using updated logic
                    temp = get_effective_conductor_temperature(circuit, section, n_cond, self.context)
                    
                    # --- Cost Calculation ---
                    try:
                        price = self.repository.get_section_price(section, circuit.conductor_type)
                        cost = circuit.length * price * n_cond
                    except:
                        cost = 999999999.0
                    self._cost_matrix[circuit.code][cand] = cost
                    
                    # --- VD Calculation ---
                    if circuit.length == 0.0:
                        self._vd_matrix[circuit.code][cand] = 0.0
                        continue

                    try:
                        props = self.repository.get_section_properties(section, circuit.conductor_type)
                        _, vd_pct = calculate_circuit_voltage_drop(
                            current=circuit.current,
                            length_m=circuit.length,
                            r_20=props.r_ohm_km,
                            conductor_type=circuit.conductor_type,
                            temperature=temp,
                            system_type=sys_type,
                            nominal_voltage=voltage,
                            n_conductors=n_cond,
                            cos_phi=cos_phi,
                            x_ohm_km=props.x_ohm_km,
                            d=props.d,
                            D=props.D,
                            frequency=freq,
                            layout=layout
                        )
                        self._vd_matrix[circuit.code][cand] = vd_pct
                    except ValueError:
                        self._vd_matrix[circuit.code][cand] = 0.0

    def _calc_vd(self, circuit: Circuit, candidate: Tuple[float, int]) -> float:
        """Fast O(1) Lookup."""
        return self._vd_matrix.get(circuit.code, {}).get(candidate, 0.0)

    def _get_circuit_cost(self, circuit: Circuit, cand: Tuple[float, int]) -> float:
        """Fast O(1) Lookup."""
        return self._cost_matrix.get(circuit.code, {}).get(cand, 999999999.0)

    def _phase_a_critical_rules(self, sol: Dict[str, Tuple[float, int]]) -> Dict[str, Tuple[float, int]]:
        print("\nPHASE A: Critical Rules (MAX)")
        
        critical_rules = [
            r for r in self.context.rules 
            if (isinstance(r, IntraLevelRule) and r.metric == 'max') or
               (isinstance(r, LocalSubgroupRule) and r.child_metric == 'max') or
               (isinstance(r, ParentChildSubgroupRule) and r.child_metric == 'max')
        ]
        
        if not critical_rules:
            return sol
        
        changed = True
        iteration = 0
        while changed:
            iteration += 1
            changed = False
            
            for rule in critical_rules:
                # Optimized culprit finding
                culprits = self._find_culprits_for_rule(sol, rule)
                
                for circuit in culprits:
                    current_cand = sol[circuit.code]
                    if not self._is_at_max_section(circuit, current_cand):
                        new_cand = self._bump_section(current_cand, circuit.level)
                        if new_cand:
                            sol[circuit.code] = new_cand
                            changed = True
                            # Optional: Reduce print spam in fast mode
                            # print(f"  Upgraded {circuit.code} to {new_cand}")
        
        print(f"Phase A completed in {iteration} iterations.")
        return sol
    
    def _phase_b_efficiency_rules(
        self,
        sol: Dict[str, Tuple[float, int]],
        snapshot_fase_a: Dict[str, Tuple[float, int]]
    ) -> Dict[str, Tuple[float, int]]:
        print("\nPHASE B: Efficiency Rules (AVG)")
        
        ls_rules = [r for r in self.context.rules if isinstance(r, LocalSubgroupRule) and r.child_metric == 'avg']
        pc_rules = [r for r in self.context.rules if isinstance(r, ParentChildSubgroupRule) and r.child_metric == 'avg']
        il_rules = [r for r in self.context.rules if isinstance(r, IntraLevelRule) and r.metric == 'avg']
        
        ordered_rules = ls_rules + pc_rules + il_rules
        
        for idx, rule in enumerate(ordered_rules, 1):
            sol = self._resolve_with_utility_capping(sol, rule, snapshot_fase_a)
        
        return sol
    
    def _phase_c_advanced_rules(
        self,
        sol: Dict[str, Tuple[float, int]]
    ) -> Dict[str, Tuple[float, int]]:
        print("\nPHASE C: Advanced Rules")
        
        local_group_rules = [r for r in self.context.rules if isinstance(r, LocalGroupRule)]
        other_advanced = [r for r in self.context.rules if isinstance(r, CustomHierarchicalRule)]
        
        for rule in local_group_rules:
            sol = self._resolve_local_group_rule(sol, rule)
        
        for rule in other_advanced:
            sol = self._resolve_with_utility_capping(sol, rule, sol.copy())
        
        return sol
    
    def _resolve_with_utility_capping(
        self,
        sol: Dict[str, Tuple[float, int]],
        rule: BaseRule,
        snapshot_inicial: Optional[Dict[str, Tuple[float, int]]] = None
    ) -> Dict[str, Tuple[float, int]]:
        
        max_iterations = 20000
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            if rule.check(sol, self.circuits, self.context, self.repository):
                break
            
            failing_info = self._get_failing_info(sol, rule)
            if not failing_info:
                break
            
            best_circuit_overall = None
            best_cand_overall = None
            best_ratio_overall = -1.0
            best_parent_overall = None
            best_group_overall = None
            
            for failing_item in failing_info:
                parent_circuit, gap = failing_item
                if gap <= 0: continue
                
                group_circuits = self._get_group_members(parent_circuit, rule)
                
                for circuit in group_circuits:
                    curr_cand = sol[circuit.code]
                    
                    # Get candidates list and find current position
                    cands = self.candidate_lists.get(circuit.level, [])
                    try:
                        curr_idx = cands.index(curr_cand)
                    except ValueError:
                        continue
                    
                    # Skip if already at max section
                    if curr_idx >= len(cands) - 1:
                        continue
                    
                    # Dynamic delta calculation from matrices (always correct)
                    curr_vd = self._calc_vd(circuit, curr_cand)
                    curr_cost = self._get_circuit_cost(circuit, curr_cand)
                    
                    # Evaluate ALL superior candidates (full search space)
                    for i in range(curr_idx + 1, len(cands)):
                        candidate = cands[i]
                        
                        delta_vd = curr_vd - self._calc_vd(circuit, candidate)
                        if delta_vd <= 0: continue
                        
                        delta_cost = self._get_circuit_cost(circuit, candidate) - curr_cost
                        is_double = candidate[1] > curr_cand[1]
                        
                        local_improvement = delta_vd
                        actual_group_improvement = self._calculate_group_improvement(
                            circuit, local_improvement, rule, parent_circuit, group_circuits
                        )
                        
                        effective_improvement = min(actual_group_improvement, gap)
                        safe_delta_cost = max(delta_cost, 0.001)
                        
                        ratio = effective_improvement / safe_delta_cost
                        if is_double:
                            ratio *= 0.10
                        
                        if ratio > best_ratio_overall:
                            best_ratio_overall = ratio
                            best_circuit_overall = circuit
                            best_cand_overall = candidate
                            best_parent_overall = parent_circuit
                            best_group_overall = group_circuits
            
            if best_circuit_overall and best_cand_overall:
                sol[best_circuit_overall.code] = best_cand_overall
                if isinstance(rule, ParentChildSubgroupRule) and best_circuit_overall == best_parent_overall and snapshot_inicial:
                    children = [c for c in best_group_overall if c != best_parent_overall]
                    for child in children:
                        if child.code in snapshot_inicial:
                            sol[child.code] = snapshot_inicial[child.code]
            else:
                break
        
        return sol
    
    def _resolve_local_group_rule(
        self,
        sol: Dict[str, Tuple[float, int]],
        rule: LocalGroupRule
    ) -> Dict[str, Tuple[float, int]]:
        
        # Pass 'self' to cache so it uses our lookup tables
        cache = LocalGroupCache(rule, self.circuits, self.context, self.repository, strategy=self)
        parent_codes = sorted(cache.groups.keys())
        
        for parent_code in parent_codes:
            group_value = cache.calculate_group_value(parent_code, sol)
            if group_value is None or group_value <= rule.limit:
                continue
            
            group_circuits = cache.groups.get(parent_code, [])
            
            # Identify circuits that can be upgraded
            candidates_to_evaluate = []
            if isinstance(rule.metric, ParentChildSubgroupRule):
                for circuit_n2 in group_circuits:
                    candidates_to_evaluate.append(circuit_n2)
                    for child in circuit_n2.children:
                        if child.level == rule.metric.child_level:
                            candidates_to_evaluate.append(child)
            else:
                candidates_to_evaluate = group_circuits
            
            for _ in range(500): # max_iter_per_group
                current_value = cache.calculate_group_value(parent_code, sol)
                if current_value is None or current_value <= rule.limit:
                    break
                
                best_circuit = None
                best_cand = None
                best_ratio = -1.0
                
                for circuit in candidates_to_evaluate:
                    curr_cand = sol[circuit.code]
                    
                    # Get candidates and find current position
                    cands = self.candidate_lists.get(circuit.level, [])
                    try:
                        curr_idx = cands.index(curr_cand)
                    except ValueError:
                        continue
                    
                    if curr_idx >= len(cands) - 1:
                        continue
                    
                    # Dynamic cost from matrices (always correct)
                    curr_cost = self._get_circuit_cost(circuit, curr_cand)
                    
                    # Evaluate ALL superior candidates
                    for i in range(curr_idx + 1, len(cands)):
                        candidate = cands[i]
                        is_double = candidate[1] > curr_cand[1]
                        
                        # Accurate group improvement via test simulation
                        test_sol = sol.copy()
                        test_sol[circuit.code] = candidate
                        new_value = cache.calculate_group_value(parent_code, test_sol)
                        if new_value is None: continue
                        
                        improvement = current_value - new_value
                        if improvement <= 0: continue
                        
                        delta_cost = self._get_circuit_cost(circuit, candidate) - curr_cost
                        safe_delta_cost = max(delta_cost, 0.001)
                        
                        ratio = improvement / safe_delta_cost
                        if is_double: ratio *= 0.10
                        
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_circuit = circuit
                            best_cand = candidate
                
                if best_circuit:
                    sol[best_circuit.code] = best_cand
                else:
                    break
        return sol

    # --- Optimized Helpers ---

    def _is_at_max_section(self, circuit: Circuit, candidate: Tuple[float, int]) -> bool:
        cands = self.candidate_lists.get(circuit.level, [])
        return candidate == cands[-1] if cands else True
    
    def _bump_section(self, current: Tuple[float, int], level: int) -> Optional[Tuple[float, int]]:
        cands = self.candidate_lists.get(level, [])
        try:
            # Optim: Binary search or just index since list is short
            idx = cands.index(current)
            return cands[idx + 1] if idx + 1 < len(cands) else None
        except ValueError:
            return None
            
    def _get_next_candidate(self, circuit: Circuit, current: Tuple[float, int]) -> Optional[Tuple[float, int]]:
        return self._bump_section(current, circuit.level)
    
    def _find_culprits_for_rule(self, sol: Dict[str, Tuple[float, int]], rule: BaseRule) -> List[Circuit]:
        culprits = []
        if isinstance(rule, IntraLevelRule):
            # Optim: Use pre-grouped list
            level_circuits = self._circuits_by_level.get(rule.level, [])
            vds = []
            for c in level_circuits:
                if c.code in sol:
                    vds.append((c, self._calc_vd(c, sol[c.code])))
            
            if rule.metric == 'max' and vds:
                max_vd = max((vd for _, vd in vds), default=0)
                if max_vd > rule.limit:
                    culprits = [c for c, vd in vds if vd == max_vd]
        return culprits
    
    def _get_failing_info(self, sol: Dict[str, Tuple[float, int]], rule: BaseRule) -> List[Tuple[Circuit, float]]:
        failing = []
        if isinstance(rule, (LocalSubgroupRule, ParentChildSubgroupRule)):
            # Optim: Use pre-grouped list
            parents = self._circuits_by_level.get(rule.parent_level, [])
            for parent in parents:
                group_value = self._calculate_group_value(parent, sol, rule)
                gap = group_value - rule.limit
                if gap > 0:
                    failing.append((parent, gap))
        return failing
    
    def _get_group_members(self, parent: Circuit, rule: BaseRule) -> List[Circuit]:
        if isinstance(rule, ParentChildSubgroupRule):
            return [parent] + [c for c in parent.children if c.level == rule.child_level]
        elif isinstance(rule, LocalSubgroupRule):
            return [c for c in parent.children if c.level == rule.child_level]
        return [parent]
    
    def _calculate_group_value(self, parent: Circuit, sol: Dict[str, Tuple[float, int]], rule: BaseRule) -> float:
        # Uses _calc_vd (now fast lookup)
        if isinstance(rule, ParentChildSubgroupRule):
            parent_vd = self._calc_vd(parent, sol[parent.code])
            # children already have parent link, no need to search all circuits
            child_vds = [self._calc_vd(c, sol[c.code]) for c in parent.children if c.level == rule.child_level and c.code in sol]
            
            if not child_vds: child_metric = 0.0
            elif rule.child_metric == 'avg': child_metric = sum(child_vds) / len(child_vds)
            elif rule.child_metric == 'max': child_metric = max(child_vds)
            else: child_metric = 0.0
            
            return parent_vd + child_metric
        
        elif isinstance(rule, LocalSubgroupRule):
            child_vds = [self._calc_vd(c, sol[c.code]) for c in parent.children if c.level == rule.child_level and c.code in sol]
            if not child_vds: return 0.0
            elif rule.child_metric == 'avg': return sum(child_vds) / len(child_vds)
            elif rule.child_metric == 'max': return max(child_vds)
        
        return 0.0
    
    def _calculate_group_improvement(self, circuit, local_improvement, rule, parent, group_circuits):
        if isinstance(rule, (ParentChildSubgroupRule, LocalSubgroupRule)):
            if circuit != parent:
                num_children = len([c for c in group_circuits if c != parent]) or 1
                return local_improvement / num_children
            else:
                return local_improvement
        elif isinstance(rule, IntraLevelRule):
             # Optim: Use pre-calculated length
            total_n = len(self._circuits_by_level.get(rule.level, []))
            return local_improvement / (total_n or 1)
        return local_improvement
    
    def _calculate_total_cost(self, sol: Dict[str, Tuple[float, int]]) -> float:
        total = 0.0
        for code, candidate in sol.items():
            circuit = self.circuits.get(code)
            if circuit:
                total += self._get_circuit_cost(circuit, candidate)
        return total
    
    def _check_all_rules(self, sol: Dict[str, Tuple[float, int]]) -> List[str]:
        violations = []
        for rule in self.context.rules:
            # Note: rule.check uses external rules.py logic which is slow.
            # Ideally we refactor rules.py too, but for strict output correctness
            # we keep the original verification call.
            if not rule.check(sol, self.circuits, self.context, self.repository):
                violations.append(f"Rule violated: {rule}")
        return violations


class LocalGroupCache:
    """Optimized Cache using Strategy Lookups."""
    def __init__(self, rule, circuits, context, repository, strategy=None):
        self.rule = rule
        self.circuits = circuits
        self.groups = defaultdict(list)
        self.strategy = strategy # Reference to main strategy for fast lookups
        self._build_groups()
    
    def _build_groups(self):
        # Optim: Use strategy's grouped list if available, else fallback
        if self.strategy:
            level_circuits = self.strategy._circuits_by_level.get(self.rule.level, [])
        else:
            level_circuits = [c for c in self.circuits.values() if c.level == self.rule.level]
            
        for circuit in level_circuits:
            parent_code = circuit.parent.code if circuit.parent else "ROOT"
            self.groups[parent_code].append(circuit)
            
    def calculate_group_value(self, parent_code, sol):
        group = self.groups.get(parent_code, [])
        if not group: return None
        
        values = []
        for circuit in group:
            candidate = sol.get(circuit.code)
            if not candidate: continue
            
            # Use Fast Lookup
            if self.strategy:
                val_func = lambda c, cand: self.strategy._calc_vd(c, cand)
            else:
                val_func = self._slow_calc_vd
                
            if isinstance(self.rule.metric, ParentChildSubgroupRule):
                circuit_vd = val_func(circuit, candidate)
                child_vds = []
                for child in circuit.children:
                    if child.level == self.rule.metric.child_level:
                        cc = sol.get(child.code)
                        if cc: child_vds.append(val_func(child, cc))
                
                child_metric = 0.0
                if child_vds:
                    if self.rule.metric.child_metric == 'avg': child_metric = sum(child_vds)/len(child_vds)
                    elif self.rule.metric.child_metric == 'max': child_metric = max(child_vds)
                
                values.append(circuit_vd + child_metric)
            else:
                values.append(val_func(circuit, candidate))
        
        if not values: return None
        
        if self.rule.aggregation == 'avg': return sum(values)/len(values)
        elif self.rule.aggregation == 'max': return max(values)
        return 0.0

    def _slow_calc_vd(self, circuit, candidate):
        # Fallback only if strategy not provided
        return 0.0


class GreedyMaxStrategy(BFTBStrategy):
    """
    Greedy Max-ΔV Optimization Strategy.

    Variante competitiva de BFTB que reemplaza la selección basada en utilidad
    (bang-for-the-buck) en las Fases B y C por un criterio greedy puro:
    
    En cada iteración, identifica todos los circuitos que incumplen las reglas,
    selecciona el de mayor caída de tensión (ΔV) individual, y sube su sección
    al calibre inmediatamente superior.
    
    - Fase A: Idéntica a BFTB (reglas críticas MAX).
    - Fase B: Greedy max-ΔV para reglas de eficiencia (AVG).
    - Fase C: Greedy max-ΔV para reglas avanzadas (LocalGroup, Custom).
    """

    def execute(
        self,
        initial_solution: Dict[str, Tuple[float, int]],
        circuits: Dict[str, Circuit],
        context: OptimizationContext,
        repository: CableRepository,
        candidate_lists: Dict[int, List[Tuple[float, int]]]
    ) -> OptimizationResult:
        """Execute Greedy Max-ΔV optimization algorithm."""
        start_time = time.time()

        self.circuits = circuits
        self.context = context
        self.candidate_lists = candidate_lists

        print("=" * 60)
        print("INICIANDO GREEDY MAX-dV")
        print("=" * 60)

        # --- STEP 0: PRE-COMPUTATION (reutiliza matrices de BFTB) ---
        self._precompute_matrices()

        sol = initial_solution.copy()

        # PHASE A: Critical Rules (MAX) — idéntica a BFTB
        sol = self._phase_a_critical_rules(sol)

        # Snapshot after Phase A
        snapshot_fase_a = sol.copy()

        # PHASE B: Efficiency Rules (AVG) — GREEDY
        sol = self._phase_b_greedy(sol)

        # PHASE C: Advanced Rules — GREEDY
        sol = self._phase_c_greedy(sol)

        # Calculate final results
        total_cost = self._calculate_total_cost(sol)
        incumplimientos = self._check_all_rules(sol)

        execution_time = time.time() - start_time

        print("=" * 60)
        print(f"GREEDY MAX-dV completado en {execution_time:.2f}s")
        print(f"Coste total: {total_cost:.2f}€")
        print(f"Incumplimientos: {len(incumplimientos)}")
        print("=" * 60)

        return OptimizationResult(
            solution_map=sol,
            total_cost=total_cost,
            violations=incumplimientos,
            execution_time=execution_time,
            metadata={'strategy': 'GreedyMax_DeltaV', 'phases': 3}
        )

    # ------------------------------------------------------------------
    # PHASE B — Greedy Max-ΔV para reglas de eficiencia (AVG)
    # ------------------------------------------------------------------
    def _phase_b_greedy(
        self,
        sol: Dict[str, Tuple[float, int]]
    ) -> Dict[str, Tuple[float, int]]:
        print("\nPHASE B (Greedy): Efficiency Rules (AVG)")

        ls_rules = [r for r in self.context.rules if isinstance(r, LocalSubgroupRule) and r.child_metric == 'avg']
        pc_rules = [r for r in self.context.rules if isinstance(r, ParentChildSubgroupRule) and r.child_metric == 'avg']
        il_rules = [r for r in self.context.rules if isinstance(r, IntraLevelRule) and r.metric == 'avg']

        ordered_rules = ls_rules + pc_rules + il_rules

        for rule in ordered_rules:
            sol = self._greedy_resolve_rule(sol, rule)

        return sol

    # ------------------------------------------------------------------
    # PHASE C — Greedy Max-ΔV para reglas avanzadas
    # ------------------------------------------------------------------
    def _phase_c_greedy(
        self,
        sol: Dict[str, Tuple[float, int]]
    ) -> Dict[str, Tuple[float, int]]:
        print("\nPHASE C (Greedy): Advanced Rules")

        local_group_rules = [r for r in self.context.rules if isinstance(r, LocalGroupRule)]
        other_advanced = [r for r in self.context.rules if isinstance(r, CustomHierarchicalRule)]

        for rule in local_group_rules:
            sol = self._greedy_resolve_rule(sol, rule)

        for rule in other_advanced:
            sol = self._greedy_resolve_rule(sol, rule)

        return sol

    # ------------------------------------------------------------------
    # Core Greedy Resolver — Aplica a cualquier tipo de regla
    # ------------------------------------------------------------------
    def _greedy_resolve_rule(
        self,
        sol: Dict[str, Tuple[float, int]],
        rule: 'BaseRule'
    ) -> Dict[str, Tuple[float, int]]:
        """
        Resuelve una regla mediante criterio greedy max-ΔV.

        En cada iteración:
        1. Comprueba si la regla se cumple globalmente.
        2. Identifica todos los circuitos que participan en grupos que incumplen.
        3. De ese conjunto, selecciona el que tiene mayor ΔV individual.
        4. Le sube la sección al calibre inmediatamente superior.
        5. Repite hasta cumplir o agotar el espacio de mejora.
        """
        max_iterations = 2000
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            # ¿Se cumple la regla?
            if rule.check(sol, self.circuits, self.context, self.repository):
                break

            # Recopilar circuitos que participan en grupos que incumplen
            affected_circuits = self._get_affected_circuits_for_rule(sol, rule)

            if not affected_circuits:
                break

            # De los afectados, seleccionar el de mayor ΔV individual
            worst_circuit = None
            worst_vd = -1.0

            for circuit in affected_circuits:
                cand = sol.get(circuit.code)
                if not cand:
                    continue
                # Verificar que no esté al máximo
                if self._is_at_max_section(circuit, cand):
                    continue

                vd = self._calc_vd(circuit, cand)
                if vd > worst_vd:
                    worst_vd = vd
                    worst_circuit = circuit

            if worst_circuit is None:
                # Todos al máximo o sin margen — salir
                break

            # Subir un calibre
            current_cand = sol[worst_circuit.code]
            new_cand = self._bump_section(current_cand, worst_circuit.level)
            if new_cand is None:
                break  # Seguridad: no debería llegar aquí

            sol[worst_circuit.code] = new_cand

        if iteration >= max_iterations:
            print(f"  [WARN] Greedy alcanzó el máximo de iteraciones para regla: {rule}")
        else:
            print(f"  Regla resuelta en {iteration} iteraciones: {rule}")

        return sol

    # ------------------------------------------------------------------
    # Identificación de circuitos afectados según tipo de regla
    # ------------------------------------------------------------------
    def _get_affected_circuits_for_rule(
        self,
        sol: Dict[str, Tuple[float, int]],
        rule: 'BaseRule'
    ) -> List[Circuit]:
        """
        Devuelve la lista de circuitos que participan en grupos/niveles
        que actualmente incumplen la regla dada.
        """
        affected = []

        if isinstance(rule, IntraLevelRule):
            # Todos los circuitos del nivel que tengan ΔV > 0
            for circuit in self._circuits_by_level.get(rule.level, []):
                if circuit.code in sol:
                    affected.append(circuit)

        elif isinstance(rule, LocalSubgroupRule):
            # Hijos de padres que incumplen
            parents = self._circuits_by_level.get(rule.parent_level, [])
            for parent in parents:
                group_value = self._calculate_group_value(parent, sol, rule)
                if group_value > rule.limit:
                    for child in parent.children:
                        if child.level == rule.child_level and child.code in sol:
                            affected.append(child)

        elif isinstance(rule, ParentChildSubgroupRule):
            # Padre + hijos de grupos que incumplen
            parents = self._circuits_by_level.get(rule.parent_level, [])
            for parent in parents:
                group_value = self._calculate_group_value(parent, sol, rule)
                if group_value > rule.limit:
                    affected.append(parent)
                    for child in parent.children:
                        if child.level == rule.child_level and child.code in sol:
                            affected.append(child)

        elif isinstance(rule, LocalGroupRule):
            # Circuitos del nivel cuyo grupo incumple
            cache = LocalGroupCache(rule, self.circuits, self.context, self.repository, strategy=self)
            for parent_code, group_circuits in cache.groups.items():
                group_value = cache.calculate_group_value(parent_code, sol)
                if group_value is not None and group_value > rule.limit:
                    affected.extend(group_circuits)
                    # Si la métrica es ParentChild, incluir también hijos
                    if isinstance(rule.metric, ParentChildSubgroupRule):
                        for c in group_circuits:
                            for child in c.children:
                                if child.level == rule.metric.child_level and child.code in sol:
                                    affected.append(child)

        elif isinstance(rule, CustomHierarchicalRule):
            # Circuito padre específico + hijos del nivel
            parent = self.circuits.get(rule.parent_code)
            if parent:
                affected.append(parent)
                for child in parent.children:
                    if child.level == rule.child_level and child.code in sol:
                        affected.append(child)

        # Eliminar duplicados conservando orden
        seen = set()
        unique = []
        for c in affected:
            if c.code not in seen:
                seen.add(c.code)
                unique.append(c)
        return unique
