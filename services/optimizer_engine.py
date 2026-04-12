"""
Optimizer Engine - Main orchestrator for cable optimization.

This service coordinates Excel loading, circuit tree building,
and optimization strategy execution.
"""

import pandas as pd
from typing import Dict, List, Tuple, Optional
import time

from domain.models import Circuit, OptimizationContext, OptimizationResult
from data.repository import CableRepository
from core.strategies import IOptimizationStrategy, BFTBStrategy
from core.rules import (
    IntraLevelRule, ParentChildSubgroupRule, LocalSubgroupRule,
    LocalGroupRule, CustomHierarchicalRule,
    calculate_vd_percent, calculate_cumulative_vd_percent
)


class OptimizerEngine:
    """
    Main orchestrator for optimization process.
    
    Coordinates:
    - Excel file loading and validation
    - Circuit hierarchy construction
    - Initial solution generation
    - Strategy execution
    - Result export
    """
    
    def __init__(
        self,
        filepath: str,
        context: OptimizationContext,
        repository: CableRepository,
        strategy: Optional[IOptimizationStrategy] = None
    ):
        """
        Initialize optimizer engine.
        
        Args:
            filepath: Path to Excel input file
            context: Optimization context with rules
            repository: Cable catalog repository
            strategy: Optimization strategy (defaults to BFTBStrategy)
        """
        self.filepath = filepath
        self.context = context
        self.repository = repository
        self.strategy = strategy or BFTBStrategy(repository)
        
        self.circuits: Dict[str, Circuit] = {}
        self.candidate_lists: Dict[int, List[Tuple[float, int]]] = {}
        self.root_nodes: List[Circuit] = []
    
    def load_and_validate(self) -> bool:
        """
        Load Excel file and build circuit hierarchy.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            df = pd.read_excel(self.filepath)
        except Exception as e:
            print(f"[ERROR] Error loading Excel: {e}")
            return False
        
        # Validate required columns
        required = ['Código', 'Longitud', 'Sección Mínima', 'Tipo de Conductor', 'Corriente']
        if not all(col in df.columns for col in required):
            print(f"[ERROR] Missing required columns. Need: {required}")
            return False
            
        # --- Lógica de Método IEC ---
        metodo_col = next((c for c in df.columns if c.lower() in ['metodo_iec', 'metodo iec', 'método iec', 'método_iec']), None)
        if metodo_col:
            df['is_enterrado'] = df[metodo_col].astype(str).str.strip().str.upper().isin(["D1", "D2"])
        else:
            df['is_enterrado'] = False
            
        # Parse circuits from Excel
        temp = {}
        for _, row in df.iterrows():
            try:
                # Parse section (handle "2x25" format)
                raw_sec = str(row['Sección Mínima']).lower().replace(' ', '')
                min_section = 0.0
                init_conductors = 1
                
                if 'x' in raw_sec:
                    parts = raw_sec.split('x')
                    init_conductors = int(parts[0])
                    min_section = float(parts[1])
                else:
                    min_section = float(raw_sec)
                
                # Optional voltage override
                voltage_spec = None
                if 'Voltaje' in df.columns:
                    val = row['Voltaje']
                    if pd.notna(val) and float(val) > 0:
                        voltage_spec = float(val)
                
                # Optional temperature override
                temperature_spec = None
                for temp_col in ['Temperatura', 'T_diseño']:
                    if temp_col in df.columns:
                        val = row[temp_col]
                        if pd.notna(val) and float(val) > 0:
                            temperature_spec = float(val)
                            break
                
                # Optional Derating Factor (K_agrup)
                derating_spec = None
                for der_col in ['Coeficiente Reducción', 'Derating', 'K_agrup', 'Factor Agrupamiento']:
                    if der_col in df.columns:
                        val = row[der_col]
                        if pd.notna(val) and float(val) > 0:
                            derating_spec = float(val)
                            break
                
                circuit = Circuit(
                    code=str(row['Código']),
                    length=float(row['Longitud']),
                    min_section_size=min_section,
                    conductor_type=str(row['Tipo de Conductor']),
                    current=float(row['Corriente']),
                    voltage_specific=voltage_spec,
                    initial_conductors=init_conductors,
                    temperature_specific=temperature_spec,
                    derating_factor=derating_spec,
                    is_enterrado=bool(row.get('is_enterrado', False))
                )
                
                temp[circuit.code] = circuit
                
            except Exception as e:
                print(f"[ERROR] Error parsing row {row.get('Código', '?')}: {e}")
                return False
        
        # Identify missing parent codes
        missing_parents = set()
        for circuit in temp.values():
            parts = circuit.code.split('-')
            for i in range(1, len(parts)):
                parent_code = '-'.join(parts[:i])
                if parent_code not in temp:
                    missing_parents.add(parent_code)
        
        # Create virtual circuits for missing parents
        for parent_code in sorted(missing_parents):
            virtual = Circuit(
                code=parent_code,
                length=0.0,  # Virtual parents have no physical cable
                min_section_size=0.0,
                conductor_type='Cu',
                current=0.0,
                voltage_specific=None,
                initial_conductors=1,
                temperature_specific=None,
                derating_factor=None
            )
            temp[parent_code] = virtual
        
        # Build hierarchy
        for circuit in temp.values():
            parent_code = circuit.code.rpartition('-')[0]
            if parent_code and parent_code in temp:
                temp[parent_code].add_child(circuit)
            elif not parent_code:
                self.root_nodes.append(circuit)
        
        self.circuits = temp
        
        # Build candidate lists
        for level, sections in self.context.level_allowed_sections.items():
            base = sorted(list(set(sections)))
            candidates = [(s, 1) for s in base]
            
            # Add double conductor candidates only for specifically allowed sections
            allowed_doubles = self.context.level_allow_double.get(level, [])
            for s in base:
                if s in allowed_doubles:
                    candidates.append((s, 2))
            
            self.candidate_lists[level] = candidates
        
        print(f"[OK] Loaded {len(self.circuits)} circuits ({len(self.root_nodes)} root nodes)")
        return True
    
    def solve(self) -> Optional[OptimizationResult]:
        """
        Execute optimization strategy.
        
        Returns:
            OptimizationResult or None if failed
        """
        if not self.circuits:
            print("[ERROR] No circuits loaded. Call load_and_validate() first.")
            return None
        
        # Create initial solution
        initial_solution = self._create_initial_solution()
        
        # Execute strategy
        result = self.strategy.execute(
            initial_solution=initial_solution,
            circuits=self.circuits,
            context=self.context,
            repository=self.repository,
            candidate_lists=self.candidate_lists
        )
        
        return result
    
    def _create_initial_solution(self) -> Dict[str, Tuple[float, int]]:
        """
        Create initial solution with minimum allowed sections.
        
        Returns:
            Dict mapping circuit codes to (section, n_conductors)
        """
        solution = {}
        
        for code, circuit in self.circuits.items():
            candidates = self.candidate_lists.get(circuit.level, [])
            
            if not candidates:
                print(f"[WARN]  No candidates for level {circuit.level}")
                solution[code] = (circuit.min_section_size, circuit.initial_conductors)
                continue
            
            # For virtual circuits, use first candidate
            if circuit.length == 0.0:
                solution[code] = candidates[0]
                continue
            
            # Find first valid candidate
            valid = None
            for section, n_cond in candidates:
                is_valid = False
                
                if n_cond > circuit.initial_conductors:
                    is_valid = True
                elif n_cond == circuit.initial_conductors and section >= circuit.min_section_size:
                    is_valid = True
                
                if is_valid:
                    valid = (section, n_cond)
                    break
            
            # Fallback to last candidate if none valid
            solution[code] = valid if valid else candidates[-1]
        
        return solution
    
    def export_to_excel(
        self,
        result: OptimizationResult,
        output_path: str
    ) -> None:
        """
        Export optimization result to Excel.
        
        Args:
            result: Optimization result
            output_path: Path for output Excel file
        """
        rows = []
        
        for code, (section, n_cond) in sorted(result.solution_map.items()):
            circuit = self.circuits.get(code)
            if not circuit:
                continue
            
            # Calculate VD and cost
            try:
                props = self.repository.get_section_properties(section, circuit.conductor_type)
                price = props.price
                cost = circuit.length * price * n_cond
            except:
                price = 0.0
                cost = 0.0
            
            rows.append({
                'Código': code,
                'Nivel': circuit.level,
                'Longitud_m': circuit.length,
                'Corriente_A': circuit.current,
                'Conductor': circuit.conductor_type,
                'Sección_mm2': section,
                'N_Conductores': n_cond,
                'Precio_€_m': price,
                'Coste_Total_€': cost
            })
        
        df = pd.DataFrame(rows)
        
        # Create Excel writer with multiple sheets
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Solución', index=False)
            
            # Summary sheet
            summary_data = {
                'Métrica': ['Coste Total (€)', 'Tiempo Ejecución (s)', 'Circuitos Totales', 'Violaciones'],
                'Valor': [
                    f"{result.total_cost:.2f}",
                    f"{result.execution_time:.2f}",
                    len(result.solution_map),
                    len(result.violations)
                ]
            }
            pd.DataFrame(summary_data).to_excel(writer, sheet_name='Resumen', index=False)
            
            # Violations sheet (if any)
            if result.violations:
                violations_data = {'Violación': result.violations}
                pd.DataFrame(violations_data).to_excel(writer, sheet_name='Violaciones', index=False)
        
        print(f"[OK] Exported to: {output_path}")

    def get_circuit_violations(
        self,
        circuit_code: str,
        solution_map: Dict[str, Tuple[float, int]]
    ) -> List[str]:
        """
        Identify specific rule violations for a single circuit.
        
        Args:
            circuit_code: Circuit code to analyze
            solution_map: Current solution configuration
            
        Returns:
            List of violation descriptions strings
        """
        circuit = self.circuits.get(circuit_code)
        if not circuit:
            return []
        
        violations = []
        
        for rule in self.context.rules:
            # 1. IntraLevelRule
            if isinstance(rule, IntraLevelRule):
                if circuit.level == rule.level:
                    # Calculate my VD
                    cand = solution_map.get(circuit.code)
                    if not cand: continue
                    my_vd = calculate_vd_percent(circuit, cand, self.context, self.repository)
                    
                    if rule.metric == 'max':
                        if my_vd > rule.limit:
                            violations.append(f"Caída Tensión > {rule.limit}% ({my_vd:.2f}%)")
                    elif rule.metric == 'avg':
                         # Check level average (expensive but necessary for correct reporting)
                        level_circuits = [c for c in self.circuits.values() if c.level == rule.level]
                        vds = []
                        for c in level_circuits:
                            cc = solution_map.get(c.code)
                            if cc:
                                vds.append(calculate_vd_percent(c, cc, self.context, self.repository))
                        if vds:
                            avg_vd = sum(vds) / len(vds)
                            if avg_vd > rule.limit:
                                violations.append(f"Promedio Nivel > {rule.limit}% (Nivel {avg_vd:.2f}%)")

            # 2. ParentChildSubgroupRule
            elif isinstance(rule, ParentChildSubgroupRule):
                # If I am the Parent
                if circuit.level == rule.parent_level:
                    val = self._check_parent_child_rule_value(circuit, rule, solution_map)
                    if val > rule.limit:
                        violations.append(f"Grupo Padre-Hijo > {rule.limit}% ({val:.2f}%)")
                
                # If I am a Child
                elif circuit.level == rule.child_level:
                   # Check if my group fails
                   parent = circuit.parent
                   if parent and parent.level == rule.parent_level:
                       val = self._check_parent_child_rule_value(parent, rule, solution_map)
                       if val > rule.limit:
                           # Determine if I am a significant contributor
                           # For MAX metric: only blameworthy if I am the max
                           # For AVG metric: implied blame
                           is_blameworthy = True
                           if rule.child_metric == 'max':
                               # Check if I am the max
                               my_cand = solution_map.get(circuit.code)
                               if my_cand:
                                   my_vd = calculate_vd_percent(circuit, my_cand, self.context, self.repository)
                                   # Get max of siblings
                                   max_sib = 0.0
                                   for sib in parent.children:
                                       if sib.level == rule.child_level:
                                           sc = solution_map.get(sib.code)
                                           if sc:
                                               max_sib = max(max_sib, calculate_vd_percent(sib, sc, self.context, self.repository))
                                   if my_vd < max_sib:
                                       is_blameworthy = False # Not the worst offender

                           if is_blameworthy:
                               violations.append(f"Grupo Padre-Hijo > {rule.limit}%")

            # 3. LocalSubgroupRule
            elif isinstance(rule, LocalSubgroupRule):
                # If I am Parent
                if circuit.level == rule.parent_level:
                    val = self._check_local_subgroup_value(circuit, rule, solution_map)
                    if val > rule.limit:
                        violations.append(f"Subgrupo Local > {rule.limit}% ({val:.2f}%)")
                
                # If I am Child
                elif circuit.level == rule.child_level:
                   parent = circuit.parent
                   if parent and parent.level == rule.parent_level:
                       val = self._check_local_subgroup_value(parent, rule, solution_map)
                       if val > rule.limit:
                           is_blame = True
                           if rule.child_metric == 'max':
                               my_cand = solution_map.get(circuit.code)
                               if my_cand:
                                   my_vd = calculate_vd_percent(circuit, my_cand, self.context, self.repository)
                                   max_sib = 0.0
                                   for sib in parent.children:
                                        if sib.level == rule.child_level:
                                            sc = solution_map.get(sib.code)
                                            if sc:
                                                max_sib = max(max_sib, calculate_vd_percent(sib, sc, self.context, self.repository))
                                   if my_vd < max_sib:
                                       is_blame = False
                           
                           if is_blame:
                               violations.append(f"Subgrupo Local > {rule.limit}%")

        return list(set(violations))

    def _check_parent_child_rule_value(self, parent: Circuit, rule: ParentChildSubgroupRule, sol_map) -> float:
        cand_p = sol_map.get(parent.code)
        if not cand_p: return 0.0
        vd_p = calculate_vd_percent(parent, cand_p, self.context, self.repository)
        
        child_vds = []
        for child in parent.children:
            if child.level == rule.child_level:
                cc = sol_map.get(child.code)
                if cc:
                    child_vds.append(calculate_vd_percent(child, cc, self.context, self.repository))
        
        if not child_vds: return vd_p
        
        metric_val = 0.0
        if isinstance(rule.child_metric, str):
            if rule.child_metric == 'avg': metric_val = sum(child_vds)/len(child_vds)
            elif rule.child_metric == 'max': metric_val = max(child_vds)
        
        return vd_p + metric_val

    def _check_local_subgroup_value(self, parent: Circuit, rule: LocalSubgroupRule, sol_map) -> float:
        child_vds = []
        for child in parent.children:
            if child.level == rule.child_level:
                cc = sol_map.get(child.code)
                if cc:
                    child_vds.append(calculate_vd_percent(child, cc, self.context, self.repository))
        
        if not child_vds: return 0.0
        
        if rule.child_metric == 'avg': return sum(child_vds)/len(child_vds)
        elif rule.child_metric == 'max': return max(child_vds)
        return 0.0
