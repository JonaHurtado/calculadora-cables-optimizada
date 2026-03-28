import pandas as pd
import math
from typing import List, Dict, Tuple, Optional
from domain.models import Circuit, OptimizationContext, OptimizationResult
from domain.physics import get_detailed_electrical_params

class MPPTAllocator:
    """
    Phase E: Inverter MPPT Assignment logic.
    Assigns circuits to inverter MPPT inputs based on voltage levels.
    """
    
    def __init__(self, engine, result: OptimizationResult, context: OptimizationContext):
        self.engine = engine
        self.result = result
        self.context = context

    def allocate(self, level: int, mppts_per_inverter: int, inputs_per_mppt: int) -> pd.DataFrame:
        """
        Execute the MPPT allocation process.
        
        Args:
            level: The circuit hierarchy level to optimize (e.g., 3 for strings).
            mppts_per_inverter: Number of MPPTs available per physical inverter.
            inputs_per_mppt: Number of inputs per MPPT.
            
        Returns:
            DataFrame with assignment results including nomenclature and voltage data.
        """
        # Configuration
        total_inputs_per_inverter = mppts_per_inverter * inputs_per_mppt
        
        # 1. Filter circuits by level and build working dataset
        target_circuits = []
        for code, circuit in self.engine.circuits.items():
            if circuit.level == level and code in self.result.solution_map:
                target_circuits.append(circuit)
                
        if not target_circuits:
            return pd.DataFrame()

        # 2. Group by Parent (Inverter)
        grouped_circuits: Dict[str, List[Dict]] = {}
        
        for circuit in target_circuits:
            # Determine Parent ID
            parent_id = circuit.parent.code if circuit.parent else "ROOT"
            
            # Calculate Electrical Params using the optimization result
            candidate = self.result.solution_map[circuit.code]
            params = get_detailed_electrical_params(circuit, candidate, self.context, self.context.cable_catalog)
            
            voltage_base = self.context.get_level_voltage(circuit)
            voltage_drop = params['VD_volts']
            voltage_inverter = voltage_base - voltage_drop
            
            item = {
                'Circuit_Code': circuit.code,
                'Parent_ID': parent_id,
                'Voltage_Base': voltage_base,
                'Voltage_Drop': voltage_drop,
                'Voltage_Inverter': voltage_inverter,
                'Original_Circuit': circuit
            }
            
            if parent_id not in grouped_circuits:
                grouped_circuits[parent_id] = []
            grouped_circuits[parent_id].append(item)
            
        # 3. Process each group
        final_rows = []
        
        for parent_id, group in grouped_circuits.items():
            # 3.1 Validation
            actual_count = len(group)
            if actual_count > total_inputs_per_inverter:
                error_msg = f"Excede Capacidad (Def:{total_inputs_per_inverter} vs Real:{actual_count})"
            else:
                error_msg = "OK"
                
            # 3.2 Sort by Voltage_Inverter Descending
            group.sort(key=lambda x: x['Voltage_Inverter'], reverse=True)
            
            # 3.3 Contiguous Block Assignment (Minimizes Mismatch)
            # Circuits are already sorted by Voltage_Inverter descending.
            # Assign consecutive blocks to each MPPT so that strings with
            # similar final voltages share the same tracker.
            #   Example: 29 circuits / 6 MPPTs → 5 MPPTs×5 + 1 MPPT×4
            mppt_buckets = {i+1: [] for i in range(mppts_per_inverter)}
            
            base_size = actual_count // mppts_per_inverter
            remainder = actual_count % mppts_per_inverter
            
            cursor = 0
            for mppt_num in range(1, mppts_per_inverter + 1):
                # First 'remainder' MPPTs get one extra circuit
                bucket_size = base_size + (1 if mppt_num <= remainder else 0)
                # Clamp to inputs_per_mppt capacity
                bucket_size = min(bucket_size, inputs_per_mppt)
                
                for i in range(bucket_size):
                    if cursor < actual_count:
                        mppt_buckets[mppt_num].append(group[cursor])
                        cursor += 1
            
            # 3.4 Generate Output Rows with Nomenclature
            for mppt_num in range(1, mppts_per_inverter + 1):
                items = mppt_buckets[mppt_num]
                
                for slot_idx, item in enumerate(items): # slot_idx 0-based
                    # Global input index (1-based)
                    # "Respetar los huecos... en la numeración" -> Assume Fixed Slots.
                    # PV01 is MPPT1-Input1. PV05 is MPPT1-Input5. PV06 is MPPT2-Input1.
                    
                    global_pv_num = (mppt_num - 1) * inputs_per_mppt + (slot_idx + 1)
                    pv_name = f"PV{global_pv_num:02d}"
                    new_code = f"{item['Circuit_Code']}-{pv_name}"
                    
                    row = {
                        'Inversor (Padre)': parent_id,
                        'MPPT': mppt_num,
                        'Input_PV': pv_name,
                        'Circuito Original': item['Circuit_Code'],
                        'Nuevo Código': new_code,
                        'Voltage Base (V)': item['Voltage_Base'],
                        'Voltage Drop (V)': round(item['Voltage_Drop'], 4),
                        'Voltage Inverter (V)': round(item['Voltage_Inverter'], 4),
                        'Error': error_msg
                    }
                    final_rows.append(row)

        return pd.DataFrame(final_rows)
