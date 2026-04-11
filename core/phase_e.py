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

    def allocate(self, level: int, inputs_per_mppt: List[int]) -> pd.DataFrame:
        """
        Execute the MPPT allocation process.
        
        Args:
            level: The circuit hierarchy level to optimize (e.g., 3 for strings).
            inputs_per_mppt: List containing maximum capacity for each MPPT. Length is number of MPPTs.
            
        Returns:
            DataFrame with assignment results including nomenclature and voltage data.
        """
        # Configuration
        mppts_per_inverter = len(inputs_per_mppt)
        total_inputs_per_inverter = sum(inputs_per_mppt)
        
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
            # We determine how many circuits each MPPT should get by simulating
            # a round-robin distribution to balance the load, respecting individual limits.
            mppt_buckets: Dict[int, list] = {i + 1: [] for i in range(mppts_per_inverter)}
            bucket_sizes = [0] * mppts_per_inverter
            remaining_circuits = actual_count
            
            while remaining_circuits > 0:
                assigned_in_round = False
                for i in range(mppts_per_inverter):
                    if bucket_sizes[i] < inputs_per_mppt[i] and remaining_circuits > 0:
                        bucket_sizes[i] += 1
                        remaining_circuits -= 1
                        assigned_in_round = True
                if not assigned_in_round:
                    break
            
            cursor = 0
            for mppt_num in range(1, mppts_per_inverter + 1):
                for _ in range(bucket_sizes[mppt_num - 1]):
                    if cursor < actual_count:
                        mppt_buckets[mppt_num].append(group[cursor])
                        cursor += 1
            
            # 3.4 Generate Output Rows with Nomenclature
            for mppt_num in range(1, mppts_per_inverter + 1):
                items = mppt_buckets[mppt_num]
                
                prev_capacities = sum(inputs_per_mppt[:mppt_num-1])
                for slot_idx, item in enumerate(items): # slot_idx 0-based
                    # Global input index (1-based)
                    global_pv_num = prev_capacities + (slot_idx + 1)
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
                    
            # 3.5 Handle unassigned circuits (exceeding capacity)
            while cursor < actual_count:
                item = group[cursor]
                row = {
                    'Inversor (Padre)': parent_id,
                    'MPPT': None,
                    'Input_PV': 'UNASSIGNED',
                    'Circuito Original': item['Circuit_Code'],
                    'Nuevo Código': f"{item['Circuit_Code']}-UNASSIGNED",
                    'Voltage Base (V)': item['Voltage_Base'],
                    'Voltage Drop (V)': round(item['Voltage_Drop'], 4),
                    'Voltage Inverter (V)': round(item['Voltage_Inverter'], 4),
                    'Error': error_msg
                }
                final_rows.append(row)
                cursor += 1

        return pd.DataFrame(final_rows)


class StandaloneMPPTAllocator:
    """
    Fase E aislada: Asignación de MPPTs sin depender del resultado de la optimización.

    Permite ejecutar el paso de asignación de MPPTs de forma totalmente independiente,
    cargando los datos de entrada desde un archivo Excel proporcionado por el usuario.

    Formato del Excel de entrada requerido
    ---------------------------------------
    El DataFrame de entrada DEBE contener exactamente las siguientes columnas:

        - ``Codigo_Circuito`` (str):
            Identificador único de cada circuito/string.
            El nivel jerárquico se deriva automáticamente del código contando los
            segmentos separados por guión (``-``):

                "INV01"       → Nivel 1  (1 segmento,  sin guión)
                "INV01-S01"   → Nivel 2  (2 segmentos, 1 guión)
                "INV01-S01-X" → Nivel 3  (3 segmentos, 2 guiones)

            El primer segmento se usa como ID del inversor padre para agrupar
            los circuitos (ej. "INV01-S01" → padre = "INV01").

        - ``V_final`` (float):
            Tensión final en bornes del inversor/MPPT, expresada en Voltios (V).
            Es la tensión ya calculada = V_nominal - Caída_de_tensión del circuito.
            Este valor se usa directamente para ordenar los strings y minimizar
            el mismatch entre los que comparten un mismo MPPT.

    Cualquier columna adicional presente en el DataFrame será ignorada.
    El usuario selecciona en la interfaz qué nivel quiere procesar; el allocator
    filtra automáticamente las filas del Excel cuyo código tenga esa profundidad.

    Ejemplo de DataFrame válido (todos los circuitos del proyecto en un solo Excel):
    ┌──────────────────┬─────────┐
    │ Codigo_Circuito  │ V_final  │  ← Nivel inferido del código
    ├──────────────────┼─────────┤
    │ INV01            │  795.00  │  ← Nivel 1 (1 segmento)
    │ INV01-S01        │  787.50  │  ← Nivel 2 (2 segmentos)
    │ INV01-S02        │  788.20  │  ← Nivel 2
    │ INV02-S01        │  786.90  │  ← Nivel 2
    └──────────────────┴─────────┘
    """

    # Nombres exactos de columnas que se requieren en el Excel de entrada.
    REQUIRED_COLUMNS = ["Codigo_Circuito", "V_final"]

    def __init__(self, df_input: pd.DataFrame):
        """
        Args:
            df_input:   DataFrame cargado desde el Excel del usuario.
                        Debe contener las columnas ``Codigo_Circuito`` y ``V_final``.
                        El nivel de cada circuito se deriva automáticamente del código.

        Raises:
            ValueError: Si el DataFrame no contiene las columnas requeridas
                        o si existen valores nulos en columnas críticas.
            TypeError:  Si los valores de ``V_final`` no son numéricos.
        """
        self._validate_input(df_input)
        self.df_input = df_input.copy()

    # ------------------------------------------------------------------
    # Validación de Entrada
    # ------------------------------------------------------------------

    def _validate_input(self, df: pd.DataFrame) -> None:
        """
        Valida que el DataFrame contenga las columnas requeridas y que los
        tipos de datos sean compatibles.

        Args:
            df: DataFrame a validar.

        Raises:
            ValueError: Si faltan columnas obligatorias o hay nulos en columnas clave.
            TypeError:  Si ``V_final`` no es numérica.
        """
        missing = [col for col in self.REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            raise ValueError(
                f"El Excel de entrada no contiene las columnas requeridas: {missing}.\n"
                f"Columnas encontradas: {list(df.columns)}\n"
                f"Columnas requeridas: {self.REQUIRED_COLUMNS}"
            )

        if not pd.api.types.is_numeric_dtype(df["V_final"]):
            raise TypeError(
                "La columna 'V_final' debe contener valores numéricos (float/int). "
                f"Tipo detectado: {df['V_final'].dtype}. "
                "Comprueba que no haya texto o celdas vacías en esa columna."
            )

        if df["Codigo_Circuito"].isnull().any():
            raise ValueError(
                "La columna 'Codigo_Circuito' contiene valores nulos. "
                "Todos los circuitos deben tener un código de identificación."
            )

        if df["V_final"].isnull().any():
            raise ValueError(
                "La columna 'V_final' contiene valores nulos. "
                "Todos los circuitos deben tener una tensión final definida."
            )

    # ------------------------------------------------------------------
    # Lógica de Agrupación
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_level(codigo: str) -> int:
        """
        Deriva el nivel jerárquico del circuito a partir de su código.

        El nivel es igual al número de segmentos separados por guión (``-``):
            "INV01"       → 1  (sin guión → nivel 1)
            "INV01-S01"   → 2  (1 guión   → nivel 2)
            "INV01-S01-X" → 3  (2 guiones → nivel 3)

        Esta convensión es consistente con la utilizada en el resto del
        proyecto VoltX (ej. 'C1' = N1, 'C1-1' = N2, 'C1-1-1' = N3).

        Args:
            codigo: Código del circuito.

        Returns:
            Nivel jerárquico (int, mínimo 1).
        """
        return len(str(codigo).split("-"))

    @staticmethod
    def _extract_parent(codigo: str) -> str:
        """
        Deriva el ID del inversor padre a partir del código del circuito.

        El padre es el código del circuito inmediatamente superior en la jerarquía,
        es decir, TODOS los segmentos separados por guión MENOS el último:

            "INV01-S01"    → "INV01"          (padre directo)
            "H-11-03"      → "H-11"           (inversor correcto)
            "A-01-15"      → "A-01"           (inversor correcto)
            "X"            → "INVERSOR_UNICO" (sin padre → grupo único)

        NOTA: La lógica anterior (solo partes[0]) era incorrecta para niveles
        con más de 2 segmentos, ya que devolvía la raíz ('H', 'A'...)
        en vez del inversor inmediato ('H-11', 'A-01'...).

        Args:
            codigo: Código del circuito.

        Returns:
            ID del padre (str).
        """
        partes = str(codigo).split("-")
        if len(partes) <= 1:
            return "INVERSOR_UNICO"
        return "-".join(partes[:-1])   # todos los segmentos menos el último


    # ------------------------------------------------------------------
    # Ejecución Principal
    # ------------------------------------------------------------------

    def allocate(self, nivel: int, inputs_per_mppt: List[int]) -> pd.DataFrame:
        """
        Ejecuta la asignación de MPPTs sobre los circuitos del nivel indicado.

        Flujo:
          1. Calcula el nivel de cada fila derivando la profundidad jerárquica
             del campo ``Codigo_Circuito`` (número de segmentos separados por ``-``).
          2. Filtra el DataFrame para quedarse solo con las filas de ``nivel``.
          3. Agrupa por inversor (padre del código) y ordena cada grupo por
             ``V_final`` descendente para minimizar el mismatch de tensión.
          4. Asigna en bloques contiguos a cada MPPT, respetando la capacidad particular de cada uno.

        Args:
            nivel:              Nivel jerárquico a procesar (ej. 2 para strings
                                con códigos tipo 'INV01-S01').
            inputs_per_mppt:    Lista con el número máximo de entradas por cada MPPT.

        Returns:
            DataFrame con las siguientes columnas:
                - ``Nivel detectado``    : Nivel inferido del código (debe coincidir con ``nivel``).
                - ``Inversor (Padre)``   : Código del inversor padre.
                - ``MPPT``              : Número de MPPT asignado (1-based).
                - ``Input_PV``          : Etiqueta del input (ej. "PV01").
                - ``Circuito Original`` : Código original del circuito.
                - ``Nuevo Código``      : Código con sufijo MPPT (ej. "INV01-S01-PV01").
                - ``V Final (V)``       : Tensión final en bornes del inversor.
                - ``Error``             : "OK" o descripción del error de capacidad.

        Raises:
            ValueError: Si no hay circuitos para el nivel seleccionado.
        """
        mppts_per_inverter = len(inputs_per_mppt)
        total_inputs_per_inverter = sum(inputs_per_mppt)

        # 1. Calcular el nivel de cada fila a partir del código
        df = self.df_input[self.REQUIRED_COLUMNS].copy()
        df["_nivel"] = df["Codigo_Circuito"].apply(self._extract_level)

        # 2. Filtrar por nivel solicitado
        df_nivel = df[df["_nivel"] == nivel].copy()

        if df_nivel.empty:
            niveles_disponibles = sorted(df["_nivel"].unique().tolist())
            raise ValueError(
                f"No se encontraron circuitos para el Nivel {nivel} "
                f"(códigos con {nivel} segmento{'s' if nivel > 1 else ''} separados por '-').\n"
                f"Niveles detectados en el Excel: {niveles_disponibles}"
            )

        # 2. Derivar el inversor padre desde el código
        df_nivel["Padre"] = df_nivel["Codigo_Circuito"].apply(self._extract_parent)

        # 3. Procesar por grupo (inversor)
        final_rows = []

        for parent_id, group_df in df_nivel.groupby("Padre", sort=True):
            group = group_df.to_dict("records")
            actual_count = len(group)

            # 3.1 Validar capacidad
            error_msg = (
                f"Excede Capacidad (Def:{total_inputs_per_inverter} vs Real:{actual_count})"
                if actual_count > total_inputs_per_inverter
                else "OK"
            )

            # 3.2 Ordenar por V_final descendente (strings con más tensión primero)
            group.sort(key=lambda x: x["V_final"], reverse=True)

            # 3.3 Asignación de bloques contiguos respetando capacidad y balanceando
            mppt_buckets: Dict[int, list] = {i + 1: [] for i in range(mppts_per_inverter)}
            bucket_sizes = [0] * mppts_per_inverter
            remaining_circuits = actual_count

            while remaining_circuits > 0:
                assigned_in_round = False
                for i in range(mppts_per_inverter):
                    if bucket_sizes[i] < inputs_per_mppt[i] and remaining_circuits > 0:
                        bucket_sizes[i] += 1
                        remaining_circuits -= 1
                        assigned_in_round = True
                if not assigned_in_round:
                    break

            cursor = 0
            for mppt_num in range(1, mppts_per_inverter + 1):
                for _ in range(bucket_sizes[mppt_num - 1]):
                    if cursor < actual_count:
                        mppt_buckets[mppt_num].append(group[cursor])
                        cursor += 1

            # 3.4 Generar filas de salida con nomenclatura
            for mppt_num in range(1, mppts_per_inverter + 1):
                prev_capacities = sum(inputs_per_mppt[:mppt_num-1])
                for slot_idx, item in enumerate(mppt_buckets[mppt_num]):
                    global_pv_num = prev_capacities + (slot_idx + 1)
                    pv_name = f"PV{global_pv_num:02d}"
                    nuevo_codigo = f"{item['Codigo_Circuito']}-{pv_name}"

                    final_rows.append({
                        "Inversor (Padre)":  parent_id,
                        "MPPT":              mppt_num,
                        "Input_PV":          pv_name,
                        "Circuito Original": item["Codigo_Circuito"],
                        "Nuevo Código":      nuevo_codigo,
                        "V Final (V)":       round(item["V_final"], 4),
                        "Error":             error_msg,
                    })
            
            # 3.5 Circuitos no asignados (exceso de capacidad)
            while cursor < actual_count:
                item = group[cursor]
                final_rows.append({
                    "Inversor (Padre)":  parent_id,
                    "MPPT":              None,
                    "Input_PV":          "UNASSIGNED",
                    "Circuito Original": item["Codigo_Circuito"],
                    "Nuevo Código":      f"{item['Codigo_Circuito']}-UNASSIGNED",
                    "V Final (V)":       round(item["V_final"], 4),
                    "Error":             error_msg,
                })
                cursor += 1

        return pd.DataFrame(final_rows)

