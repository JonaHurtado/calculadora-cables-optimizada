"""
VoltX - Cable Optimization Application

Streamlit UI for cable section optimization using BFTB algorithm.
"""

import streamlit as st
import pandas as pd
from pathlib import Path
import tempfile
import os
import io
import time
import logging

from domain.models import OptimizationContext
from domain.physics import get_detailed_electrical_params
from data.repository import CableRepository
from core.rules import IntraLevelRule, ParentChildSubgroupRule, LocalSubgroupRule, RuleParser
from core.strategies import BFTBStrategy
from core.phase_e import MPPTAllocator, StandaloneMPPTAllocator
from services.optimizer_engine import OptimizerEngine


# --- 1. HELPER FUNCTIONS ---

def calculate_real_project_cost(solution_map, circuits_dict, context, repository):
    """
    Calculate total project cost considering number of wires per system and
    number of conductors per phase (double vein).
    
    Args:
        solution_map: Dict mapping code -> (section, n_conductors_per_phase)
        circuits_dict: Dict of Circuit objects
        context: OptimizationContext
        repository: CableRepository
        
    Returns:
        Total cost in EUR
    """
    # System wire multipliers
    level_multipliers = {}
    for level, sys_type in context.system_types.items():
        if sys_type == 'AC_TRI':
            level_multipliers[level] = 3
        elif sys_type in ['AC_MONO', 'DC_MONO', 'DC']:
            level_multipliers[level] = 2
        else:
            level_multipliers[level] = 1
    
    total_cost = 0.0
    for code, candidate in solution_map.items():
        circuit = circuits_dict.get(code)
        if not circuit:
            continue
        
        section, n_cond_per_phase = candidate
        sys_wires = level_multipliers.get(circuit.level, 3)
        
        try:
            price_unit = context.cable_catalog.get_price(section, circuit.conductor_type)
        except (KeyError, ValueError) as e:
            logging.warning(f"Precio no encontrado para sección {section} mm² ({circuit.conductor_type}): {e}")
            price_unit = 0.0
        
        # Cost = Length * UnitPrice * SystemWires * ConductorsPerPhase
        total_cost += (circuit.length * price_unit * sys_wires * n_cond_per_phase)
    
    return total_cost


def generate_materials_summary(circuits, solution_map, context):
    """
    Generate list of DataFrames with material summary by level.
    
    Args:
        circuits: Dict of Circuit objects
        solution_map: Dict mapping code -> (section, n_conductors)
        context: OptimizationContext
        
    Returns:
        List of tuples (title, DataFrame)
    """
    # Get multipliers mapping
    level_multipliers = {}
    for level, sys_type in context.system_types.items():
        if sys_type == 'AC_TRI':
            level_multipliers[level] = 3
        elif sys_type in ['AC_MONO', 'DC_MONO', 'DC']:
            level_multipliers[level] = 2
        else:
            level_multipliers[level] = 1
    
    system_labels = {}
    for level, sys_type in context.system_types.items():
        if sys_type == 'AC_TRI':
            system_labels[level] = "AC Trifásica (3x)"
        elif sys_type == 'AC_MONO':
            system_labels[level] = "AC Monofásica (2x)"
        elif sys_type in ['DC_MONO', 'DC']:
            system_labels[level] = "DC (2 Hilos)"
        else:
            system_labels[level] = sys_type
    
    summary_dfs = []
    levels_in_sol = sorted(list(set(c.level for c in circuits.values())))
    
    for lvl in levels_in_sol:
        circuits_lvl = [c for c in circuits.values() if c.level == lvl]
        if not circuits_lvl:
            continue
        
        stats = {}
        total_lvl_len = 0.0
        
        for c in circuits_lvl:
            if c.code not in solution_map:
                continue
            
            sec, n_cond = solution_map[c.code]
            sys_wires = level_multipliers.get(lvl, 1)
            
            # Physical cable length to buy = trench length * system wires * wires per phase
            # Calculation: Length * N_Phase_Wires * N_Parallel_Conductors
            real_len = c.length * sys_wires * n_cond
            
            # Group ONLY by section (mm²), merging singles and doubles
            key = sec  # Key is float
            
            if key not in stats:
                stats[key] = {'len': 0.0, 'count': 0}
            stats[key]['len'] += real_len
            stats[key]['count'] += 1
            total_lvl_len += real_len
        
        data_dict = {}
        # Sort strictly by section size (numeric)
        for k in sorted(stats.keys()):
            l = stats[k]['len']
            c = stats[k]['count']
            pct = (l / total_lvl_len * 100) if total_lvl_len > 0 else 0
            # Display format: "240.0 mm²"
            data_dict[f"{k} mm²"] = [f"{l:,.2f}", int(c), f"{pct:.2f}%"]
        
        sys_label = system_labels.get(lvl, f"Nivel {lvl}")
        df_summary = pd.DataFrame(data_dict, index=["Longitud Total (m)", "Nº Tramos", "% sobre Total"])
        
        # Transpose so Section is the index/row
        df_summary = df_summary.T 
        # Columns now: Index (Section), Longitud, Counts, %
        
        summary_dfs.append((f"Resumen Nivel {lvl} ({sys_label})", df_summary))
    
    return summary_dfs


def format_time_ms(seconds):
    """Format time in minutes and seconds."""
    if seconds < 60:
        return f"{seconds:.2f}s"
    else:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.2f}s"


def build_table_data(engine_inst, result_inst, ctx, phase_e_df=None):
    """
    Build data for results table and Excel export with exact specified structure.

    Args:
        engine_inst: OptimizerEngine instance with circuits
        result_inst: OptimizationResult with solution_map
        ctx: OptimizationContext
        phase_e_df: Optional Phase E DataFrame

    Returns:
        List of dicts (one per circuit row)
    """
    # System multipliers
    level_multipliers = {}
    for level, sys_type in ctx.system_types.items():
        if sys_type == 'AC_TRI':
            level_multipliers[level] = 3
        elif sys_type in ['AC_MONO', 'DC_MONO', 'DC']:
            level_multipliers[level] = 2
        else:
            level_multipliers[level] = 1

    # Create Phase E Map for fast lookup if available
    pe_map = {}
    if phase_e_df is not None and not phase_e_df.empty:
        for _, row in phase_e_df.iterrows():
            pe_map[row['Circuito Original']] = {
                'MPPT': row['MPPT'],
                'Input_PV': row['Input_PV'],
                'Nuevo Código': row['Nuevo Código']
            }

    data = []
    for code in sorted(result_inst.solution_map.keys()):
        c = engine_inst.circuits[code]
        cand = result_inst.solution_map[code]
        sec, n_cond = cand

        # Get detailed electrical parameters
        params = get_detailed_electrical_params(c, cand, ctx, ctx.cable_catalog)

        # Get unit price for ONE unipolar conductor
        pr = ctx.cable_catalog.get_price(sec, c.conductor_type)

        # Calculate total cost = Length × Price × System_Wires × Conductors_Per_Phase
        l_cost = c.length * pr * level_multipliers[c.level] * n_cond

        # Get rule violations
        violations = engine_inst.get_circuit_violations(code, result_inst.solution_map)
        status = ", ".join(violations) if violations else "Cumple"

        # Phase E Check
        display_code = code
        input_pv_val = ""
        mppt_val = ""
        if code in pe_map:
            display_code = pe_map[code]['Nuevo Código']
            input_pv_val = pe_map[code]['Input_PV']
            mppt_val = pe_map[code]['MPPT']

        # Calculate initial and final voltages
        v_nominal = ctx.get_level_voltage(c)
        v_final = v_nominal - params['VD_volts']

        # Build row — columns ordered for logical reading:
        # Identification → Cable specs → Cost → Electrical calcs → Voltage → Status
        data.append({
            "Nivel": c.level,
            "Código": display_code,
            "MPPT": mppt_val,
            "Input_PV": input_pv_val,
            "Conductor": c.conductor_type,
            "Sección (mm²)": round(sec, 6),
            "Cond. por Fase": n_cond,
            "Longitud (m)": round(c.length, 6),
            "Precio Unit. (€/m)": round(pr, 6),
            "Coste Total (€)": round(l_cost, 6),
            "R(20ºC)": round(params['R_20'], 6),
            "Tcond (ºC)": round(params['T_cond'], 6),
            "R(Tcond)": round(params['R_Tcond'], 6),
            "X": round(params['X'], 6),
            "V Nominal (V)": round(v_nominal, 4),
            "VD (V)": round(params['VD_volts'], 6),
            "VD%": round(params['VD_percent'], 6),
            "V Final (V)": round(v_final, 4),
            "Estado": status
        })
    return data


# --- 2. PAGE CONFIGURATION ---
st.set_page_config(page_title="VoltX - Optimizador de Cables", page_icon="⚡", layout="wide")

st.title("VoltX - Optimizador de Cables")
st.markdown("""
Optimizador de dimensionamiento de cables en instalaciones eléctricas mediante el algoritmo **BFTB (Bang-For-The-Buck)**.
Maximiza la eficiencia económica seleccionando el cable con la mejor relación entre mejora de caída de tensión
y coste adicional (V/€). El objetivo es obtener instalaciones que cumplan todos los criterios de caída de tensión con el mínimo
coste total posible, optimizando la inversión en materiales.
""")

# --- 3. SIDEBAR ---
with st.sidebar:
    st.header("Configuración Global")
    temp_global = st.number_input("Temperatura del Conductor MAX (°C)", min_value=0.0, value=90.0, step=5.0)
    
    st.markdown("---")
    num_levels = st.number_input("¿Cuántos niveles de profundidad tiene el proyecto?", min_value=1, max_value=5, value=3)
    
    st.info("ℹ️ El cálculo de costes es real: Precio Unipolar × Nº Hilos.")
    
    # --- AUTHOR SECTION ---
    st.markdown("---")
    st.markdown("### Desarrollado por")
    st.markdown("**Jonathan Hurtado Moreira**")
    st.markdown("""
    <div style="display: flex; flex-direction: column; gap: 5px;">
        <a href="https://www.linkedin.com/in/jonaa-hurtado" target="_blank" style="text-decoration: none;">
            LinkedIn
        </a>
        <a href="mailto:hurtadomoreirajonathan@gmail.com" style="text-decoration: none;">
            Email
        </a>
    </div>
    """, unsafe_allow_html=True)

# --- 4. MAIN SECTION: FILE UPLOAD ---

st.header("1. Carga de Datos")

# Helper function to generate template
def generate_excel_template():
    df_template = pd.DataFrame({
        "Código": ["C1", "C1-1", "C1-1-1"],
        "Longitud": [150.0, 30.0, 10.0],
        "Sección Mínima": ["2x240", "150", 16.0],
        "Tipo de Conductor": ["Al", "Al", "Cu"],
        "Corriente": [630.0, 400.0, 63.0],
        "Voltaje": [None, None, 1100.0],
        "Temperatura": [None, 90.0, None],
        "K_agrup": [None, 0.8, None],
        "metodo_iec": ["D1", "E", "D2"]
    })
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_template.to_excel(writer, index=False, sheet_name='Plantilla_VoltX')
    return output.getvalue()


def generate_phase_e_template() -> bytes:
    """
    Genera la plantilla Excel para el modo 'Solo Fase E'.

    Columnas requeridas:
        - Codigo_Circuito : Identificador del circuito/string.
          El nivel jerárquico se infiere automáticamente del código:
          'INV01' = Nivel 1, 'INV01-S01' = Nivel 2, 'INV01-S01-X' = Nivel 3.
        - V_final  : Tensión FINAL en bornes del inversor en Voltios (V).
          Es la tensión ya calculada = V_nominal - Caída_de_tensión.

    Returns:
        Bytes del archivo Excel generado en memoria.
    """
    df_tpl = pd.DataFrame({
        "Codigo_Circuito": [
            # Nivel 1 (1 segmento)
            "INV01",
            "INV02",
            # Nivel 2 (2 segmentos) ← strings típicos de un parque FV
            "INV01-S01", "INV01-S02", "INV01-S03",
            "INV01-S04", "INV01-S05",
            "INV02-S01", "INV02-S02", "INV02-S03",
        ],
        "V_final": [
            # Nivel 1
            795.00, 794.50,
            # Nivel 2
            787.50, 788.20, 786.90,
            789.10, 787.80,
            788.40, 786.60, 788.00,
        ],
    })
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_tpl.to_excel(writer, index=False, sheet_name='PlantillaFaseE')
    return output.getvalue()

# Format guide
with st.expander("📂 Ver formato de archivo Excel requerido", expanded=False):
    st.markdown("""
    Para que el optimizador procese correctamente los datos, el archivo Excel debe seguir esta estructura:

    | Columna | Requerido | Descripción / Opciones |
    | :--- | :---: | :--- |
    | **Código** | ✅ Sí | ID único. Usa guiones para jerarquía (ej: `C1` → `C1-1` → `C1-1-1`). |
    | **Longitud** | ✅ Sí | Longitud física del tramo en metros (m). |
    | **Sección Mínima** | ✅ Sí | mm² mínimos. Admite `240` o `2x240` (doble conductor/fase). |
    | **Tipo de Conductor** | ✅ Sí | Solo se permite `Al` (Aluminio) o `Cu` (Cobre). |
    | **Corriente** | ✅ Sí | Corriente máxima de diseño en Amperios (A). |
    | **Voltaje** | ℹ️ Opt | Voltaje específico del circuito (V). Sobrescribe el valor del nivel. |
    | **Temperatura** | ℹ️ Opt | Temperatura de diseño (°C). Sobrescribe la global. |
    | **K_agrup** | ℹ️ Opt | Coeficiente de reducción (Derating factor). *Solo si se usa temp. dinámica.* |
    | **metodo_iec** | ℹ️ Opt | Método de instalación (ej: `D1`, `D2`, `E`, `F`). Define temp. de referencia. |

    > [!TIP]
    > **Jerarquía por Código:** El optimizador asume que `C1-1` es "hijo" de `C1`. Si un padre no existe físicamente, se creará un nodo virtual automáticamente.
    """)
    
    col_t1, col_t2 = st.columns([2, 1])
    with col_t1:
        st.info("Descarga la plantilla base para asegurar la compatibilidad:")
    with col_t2:
        template_bytes = generate_excel_template()
        st.download_button(
            label="📄 Descargar Plantilla",
            data=template_bytes,
            file_name="plantilla_voltx_optimizador.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    
    st.dataframe(pd.DataFrame({
        "Código": ["C1", "C1-1"], "Longitud": [150.0, 30.0],
        "Sección Mínima": ["2x240", 16.0], "Tipo de Conductor": ["Al", "Cu"], "Corriente": [630.0, 63.0], "metodo_iec": ["D1", "E"]
    }), hide_index=True, use_container_width=True)

uploaded_file = st.file_uploader("Sube tu archivo Excel", type=["xlsx"])

# Check for Derating Column
has_derating_col = False
if uploaded_file is not None:
    try:
        # Read header only to check columns
        df_header = pd.read_excel(uploaded_file, nrows=0)
        der_cols = ['Coeficiente Reducción', 'Derating', 'K_agrup', 'Factor Agrupamiento']
        if any(col in df_header.columns for col in der_cols):
            has_derating_col = True
    except Exception as e:
        st.warning(f"⚠️ No se pudo leer la cabecera del archivo para detectar columnas de derating: {e}")
    finally:
        # Reset file position so subsequent reads (getvalue) work correctly
        uploaded_file.seek(0)

# --- 5. LEVEL CONFIGURATION ---
st.header("2. Definición de Niveles Eléctricos")

st.info("""
**¿Qué son los Niveles?** Definen la jerarquía de tu instalación eléctrica desde la red hasta el string, por ejemplo:
* **Nivel 1:** Suele ser los circuitos de MV.
* **Nivel 2:** El tramo de la PST al inversor string.
* **Nivel 3:** Circuitos strings.
""")

tabs = st.tabs([f"Nivel {i}" for i in range(1, num_levels + 1)])

# Extended configuration structure with 'allow_double' and dynamic temp
config_niveles = {
    "voltages": {}, "systems_ui": {}, "factors": {},
    "frequencies": {}, "disposiciones": {}, "allowed_sections": {},
    "allow_double": {},
    "level_t_ref_suelo": {},
    "level_t_ref_aire": {},
    "level_t_max": {},
    "level_ampacities": {}
}

secciones_std = [1.5, 2.5, 4.0, 6.0, 10.0, 16.0, 25.0, 35.0, 50.0, 70.0, 95.0, 120.0, 150.0, 185.0, 240.0, 300.0, 400.0,
                 500.0, 630.0]
OPCIONES_SISTEMA = ["AC Trifásica (3x)", "AC Monofásica (2x)", "DC (2 Hilos)"]

for i, tab in enumerate(tabs):
    lvl = i + 1
    with tab:
        c1, c2, c3 = st.columns(3)
        
        # Column 1: Voltage and System
        with c1:
            config_niveles["voltages"][lvl] = st.number_input(f"Voltaje por Defecto (V) - N{lvl}", min_value=0.0,
                                                              value=400.0 if lvl < 3 else 800.0)
            sel_sys = st.selectbox(f"Sistema - N{lvl}", OPCIONES_SISTEMA, index=0)
            config_niveles["systems_ui"][lvl] = sel_sys
        
        # Conditional logic for DC (hide AC params)
        is_dc = "DC" in sel_sys
        
        # Column 2: Factor and Hz
        with c2:
            if not is_dc:
                config_niveles["factors"][lvl] = st.number_input(f"Factor de Potencia - N{lvl}", min_value=0.0,
                                                                 max_value=1.0, value=0.9)
                config_niveles["frequencies"][lvl] = st.number_input(f"Frecuencia (Hz) - N{lvl}", min_value=0.0,
                                                                     value=50.0)
            else:
                # Invisible defaults for DC
                config_niveles["factors"][lvl] = 1.0
                config_niveles["frequencies"][lvl] = 0.0
                st.caption("Sistema DC seleccionado")
                st.markdown("*Factor de potencia y frecuencia no aplican en corriente continua.*")
        
        # Column 3: Layout and Sections
        with c3:
            if not is_dc:
                config_niveles["disposiciones"][lvl] = st.selectbox(f"Disposición - N{lvl}", ["Tresbolillo", "Plana"],
                                                                    index=0)
            else:
                config_niveles["disposiciones"][lvl] = "Plana"
            
            default_s = [240.0, 300.0, 400.0] if lvl < 3 else [4.0, 6.0, 10.0, 16.0]
            defaults_validos = [s for s in default_s if s in secciones_std]
            config_niveles["allowed_sections"][lvl] = st.multiselect(f"Secciones permitidas - N{lvl}", secciones_std,
                                                                     default=defaults_validos)
            
            # --- NEW CHECKBOX: DOUBLE VEIN ---
            st.markdown("---")
            # Replace Checkbox with Multiselect
            # Filter sections that are already selected in 'allowed_sections'
            current_allowed = config_niveles["allowed_sections"][lvl]
            
            allow_double = st.multiselect(
                f"¿En qué secciones permites Doble Vena (2x) - Nivel {lvl}?",
                options=current_allowed,
                default=[],
                help="Selecciona las secciones donde el algoritmo puede probar 2 conductores por fase."
            )
            config_niveles["allow_double"][lvl] = allow_double

            # --- DYNAMIC TEMPERATURE CONFIG ---
            st.markdown("---")
            use_dyn_temp = st.checkbox(
                f"Calcular Temperatura por Tabla (IEC/Ampacidad) - Nivel {lvl}",
                disabled=not has_derating_col,
                help="Requiere columna 'Coeficiente Reducción' en el Excel. Calcula temperatura exacta según carga y ampacidad." if not has_derating_col else "Activa el cálculo dinámico de temperatura según carga."
            )
            
            if use_dyn_temp:
                st.info('**Configuración de Métodos IEC:** El sistema detecta automáticamente el tipo de instalación. Los métodos **D1 y D2** se calculan como "Enterrados" usando su temperatura de referencia (Suelo). Cualquier otro método introducido en el Excel será tratado como "Aéreo".')
                c_t1, c_t2, c_t3 = st.columns(3)
                with c_t1:
                    t_ref_suelo_val = st.number_input(f"Temp. Ref./Suelo (ºC) - N{lvl}", value=20.0, step=1.0)
                    config_niveles["level_t_ref_suelo"][lvl] = t_ref_suelo_val
                with c_t2:
                    t_ref_aire_val = st.number_input(f"Temp. Ref./Aire (ºC) - N{lvl}", value=40.0, step=1.0)
                    config_niveles["level_t_ref_aire"][lvl] = t_ref_aire_val
                with c_t3:
                    t_max_val = st.number_input(f"Temp. Máx. Aislamiento (ºC) - N{lvl}", value=90.0, step=1.0)
                    config_niveles["level_t_max"][lvl] = t_max_val
                
                st.caption("Tabla de Ampacidades (Iz Base) para Secciones Permitidas:")
                
                # Default Ampacities Split by Installation Method (Requirement 2)
                # Hardcoded defaults:
                # 4 mm² → Aéreo: 45 A | Enterrado: 43 A
                # 6 mm² → Aéreo: 58 A | Enterrado: 53 A
                # 10 mm² → Aéreo: 80 A | Enterrado: 71 A
                # 240 mm² → Aéreo: 530 A | Enterrado: 343 A
                # 300 mm² → Aéreo: 613 A | Enterrado: 386 A
                # 400 mm² → Aéreo: 740 A | Enterrado: 441 A
                
                defaults_split = {
                    4.0: {"Aéreo": 45.0, "Enterrado": 43.0},
                    6.0: {"Aéreo": 58.0, "Enterrado": 53.0},
                    10.0: {"Aéreo": 80.0, "Enterrado": 71.0},
                    240.0: {"Aéreo": 530.0, "Enterrado": 343.0},
                    300.0: {"Aéreo": 613.0, "Enterrado": 386.0},
                    400.0: {"Aéreo": 740.0, "Enterrado": 441.0},
                }
                
                # Filter for allowed sections only
                amp_data = []
                for s in sorted(current_allowed):
                    d = defaults_split.get(s, {"Aéreo": 0.0, "Enterrado": 0.0})
                    amp_data.append({
                        "Sección": s, 
                        "Ampacidad Aéreo [A]": d["Aéreo"],
                        "Ampacidad Enterrado [A]": d["Enterrado"]
                    })
                
                edited_amps = st.data_editor(
                    pd.DataFrame(amp_data),
                    hide_index=True,
                    use_container_width=True,
                    key=f"editor_amps_n{lvl}"
                )
                
                # Convert back to dict with nested structure: {section: {'aereo': X, 'enterrado': Y}}
                lvl_amp_dict = {}
                for idx, row in edited_amps.iterrows():
                    lvl_amp_dict[row["Sección"]] = {
                        "aereo": row["Ampacidad Aéreo [A]"],
                        "enterrado": row["Ampacidad Enterrado [A]"]
                    }
                config_niveles["level_ampacities"][lvl] = lvl_amp_dict

# --- 6. RULES MANAGER ---
st.header("3. Reglas de Validación")
if 'rules_list' not in st.session_state:
    st.session_state['rules_list'] = []

# Rule descriptions dictionary
rule_descriptions = {
    "Intra-Nivel": "Establece un límite máximo para la caída de tensión de un solo cable en un nivel específico (ej. ningún cable del Nivel 3 puede superar el 1%).",
    "Padre-Hijo (Subgrupo)": "Controla la caída acumulada de un tramo completo. Suma la caída del cable 'Padre' (Nivel Superior) más la de sus 'Hijos' (Nivel Inferior).",
    "Local (Subgrupo)": "Restringe la caída de tensión de un grupo de circuitos hijos que comparten un mismo padre, sin contar al padre."
}

with st.expander("➕ Añadir nueva regla", expanded=False):
    c_type, c_params = st.columns([1, 3])
    with c_type:
        rule_type = st.selectbox("Tipo de Regla", ["Intra-Nivel", "Padre-Hijo (Subgrupo)", "Local (Subgrupo)"])
        st.caption(rule_descriptions.get(rule_type, ""))
    
    new_rule_data = {}
    with c_params:
        if rule_type == "Intra-Nivel":
            l = st.number_input("Nivel Objetivo", 1, num_levels, num_levels)
            m = st.selectbox("Métrica", ["max", "avg"])
            lim = st.number_input("Límite (%)", min_value=0.0, value=2.0, step=0.1)
            new_rule_data = {"type": "IntraLevelRule", "level": int(l), "metric": m, "limit": float(lim)}
        
        elif rule_type == "Padre-Hijo (Subgrupo)":
            pl = st.number_input("Nivel Padre", 1, num_levels - 1, 1)
            cl = st.number_input("Nivel Hijo", pl + 1, num_levels, pl + 1)
            m = st.selectbox("Métrica Hijos", ["avg", "max"])
            lim = st.number_input("Límite Acumulado Total %", min_value=0.0, value=2.5, step=0.1)
            
            new_rule_data = {
                "type": "ParentChildSubgroupRule",
                "parent_level": int(pl),
                "child_level": int(cl),
                "child_metric": m,
                "limit": float(lim)
            }
        
        elif rule_type == "Local (Subgrupo)":
            pl = st.number_input("Nivel Padre", 1, num_levels - 1, 1)
            cl = st.number_input("Nivel Hijo", pl + 1, num_levels, pl + 1)
            m = st.selectbox("Métrica Hijos", ["avg", "max"])
            lim = st.number_input("Límite Local %", min_value=0.0, value=1.0, step=0.1)
            
            new_rule_data = {
                "type": "LocalSubgroupRule",
                "parent_level": int(pl),
                "child_level": int(cl),
                "child_metric": m,
                "limit": float(lim)
            }
    
    if st.button("Agregar Regla"):
        st.session_state['rules_list'].append(new_rule_data)
        st.success("Regla añadida.")

if st.session_state['rules_list']:
    for i, rule in enumerate(st.session_state['rules_list']):
        col_rule, col_del = st.columns([9, 1])
        with col_rule:
            rule_text = f"**{rule['type']}** | "
            if 'level' in rule:
                rule_text += f"Nivel: {rule['level']} | "
            if 'parent_level' in rule:
                rule_text += f"Padre: N{rule['parent_level']} → Hijo: N{rule['child_level']} | "
            if 'child_metric' in rule:
                rule_text += f"Métrica: {rule['child_metric']} | "
            elif 'metric' in rule:
                rule_text += f"Métrica: {rule['metric']} | "
            rule_text += f"Límite: {rule['limit']}%"
            st.markdown(rule_text)
        with col_del:
            if st.button("❌", key=f"del_rule_{i}", help=f"Eliminar regla {i+1}"):
                st.session_state['rules_list'].pop(i)
                st.rerun()
    
    if st.button("🗑️ Borrar todas las reglas"):
        st.session_state['rules_list'] = []
        st.rerun()

# --- 6.5. ADVANCED RULES (PYTHON SYNTAX) ---
st.header("4. Reglas Avanzadas (Sintaxis Python)")

with st.expander("📚 Guía de Sintaxis y Ejemplos", expanded=False):
    st.markdown("""
    ### Sintaxis de Reglas Compuestas
    
    Puedes definir reglas que usan **otras reglas como métricas**, permitiendo
    restricciones multi-nivel complejas.
    
    ---
    
    #### Ejemplo Pedagógico Completo
    
    > **Objetivo:** En un sistema de 3 niveles, queremos agrupar por cada cuadro del Nivel 1.
    > Para cada circuito de Nivel 2 dentro de ese cuadro, calculamos su caída de tensión
    > SUMADA al promedio de sus hijos (N3). Finalmente, verificamos que el PROMEDIO de
    > todos esos valores (N2 + Hijos) en el cuadro no supere el 1.8%.
    >
    > **Sintaxis:**
    > ```
    > LocalGroup(Level=2, Metric=ParentChild(Level=2, ChildLevel=3, ChildMetric='avg'), Aggregation='avg') < 1.8
    > ```
    >
    > **Cómo se calcula internamente:**
    > 1. **Nivel Interno (ParentChild):** Para cada circuito N2 (ej: A-01), calcula:
    >    `VD(A-01) + Promedio(VD Hijos A-01...)`. Resultado: **1.3%**.
    > 2. **Iteración:** Repite para A-02 (1.5%) y A-03 (1.1%).
    > 3. **Nivel Externo (LocalGroup):** Agrupa por padre común (Cuadro A):
    >    `(1.3% + 1.5% + 1.1%) / 3 = 1.3%`.
    > 4. **Validación:** Comprueba si `1.3% < 1.8%`.
    
    ---
    
    ### Reglas Disponibles
    
    - **ParentChild(Level=X, ChildLevel=Y, ChildMetric='avg'|'max'):** 
      Suma VD del padre + métrica de hijos
    - **LocalGroup(Level=X, Metric=..., Aggregation='avg'|'max'):**
      Agrupa por padre común y aplica agregación sobre la métrica
    
    ### Ejemplos Adicionales
    
    **Regla Simple (sin anidación):**
    ```
    ParentChild(Level=2, ChildLevel=3, ChildMetric='max') < 2.5
    ```
    
    **Regla Anidada:**
    ```
    LocalGroup(Level=1, Metric=ParentChild(Level=1, ChildLevel=2, ChildMetric='avg'), Aggregation='max') < 3.0
    ```
    """)

# Template examples for quick insertion
ADVANCED_RULE_TEMPLATES = {
    "Padre-Hijo (avg)": "ParentChild(Level=2, ChildLevel=3, ChildMetric='avg') < 2.5",
    "Padre-Hijo (max)": "ParentChild(Level=2, ChildLevel=3, ChildMetric='max') < 2.5",
    "Grupo Local anidado": "LocalGroup(Level=2, Metric=ParentChild(Level=2, ChildLevel=3, ChildMetric='avg'), Aggregation='avg') < 1.8",
}

col_templates = st.columns(len(ADVANCED_RULE_TEMPLATES))
for idx, (name, template) in enumerate(ADVANCED_RULE_TEMPLATES.items()):
    with col_templates[idx]:
        if st.button(f"Insertar: {name}", key=f"tpl_{idx}"):
            st.session_state['_adv_rule_template'] = template
            st.rerun()

# Use template if inserted, otherwise keep existing text
default_adv_text = st.session_state.pop('_adv_rule_template', '')

advanced_rules_text = st.text_area(
    "Escribe tus reglas avanzadas (una por línea):",
    value=default_adv_text,
    height=150,
    help="Ejemplo: LocalGroup(Level=2, Metric=ParentChild(Level=2, ChildLevel=3, ChildMetric='avg'), Aggregation='avg') < 1.8"
)

if 'parsed_advanced_rules' not in st.session_state:
    st.session_state['parsed_advanced_rules'] = []

col_parse, col_clear = st.columns([3, 1])

with col_parse:
    if st.button("🔍 Validar y Parsear Reglas Avanzadas", type="primary"):
        st.session_state['parsed_advanced_rules'] = []
        if advanced_rules_text.strip():
            lines = [l.strip() for l in advanced_rules_text.split('\n') if l.strip()]
            for i, line in enumerate(lines):
                try:
                    parsed_rule = RuleParser.parse(line)
                    st.session_state['parsed_advanced_rules'].append(parsed_rule)
                    st.success(f"✅ Regla {i+1} parseada: {parsed_rule}")
                except Exception as e:
                    st.error(f"❌ Error en regla {i+1}: {e}")
        else:
            st.info("No hay reglas avanzadas para parsear.")

with col_clear:
    if st.button("🗑️ Limpiar Reglas Avanzadas"):
        st.session_state['parsed_advanced_rules'] = []
        st.success("Reglas avanzadas eliminadas.")

if st.session_state['parsed_advanced_rules']:
    st.info(f"✅ {len(st.session_state['parsed_advanced_rules'])} regla(s) avanzada(s) activa(s):")
    for idx, rule in enumerate(st.session_state['parsed_advanced_rules']):
        st.markdown(f"  {idx+1}. `{rule}`")

# --- 7. PRICES EDITOR ---
st.header("5. Precios de Cables (€/m - Conductor Unipolar)")

# Initialize repository to get catalog (cached to avoid recreating on every rerun)
@st.cache_resource
def get_repository():
    return CableRepository()

repository = get_repository()
raw_catalog = repository.get_catalog()

# Get all available sections from catalog
all_sections = sorted(list(set(
    list(raw_catalog.aluminum.keys()) + list(raw_catalog.copper.keys())
)))

data_prices = []
for s in all_sections:
    price_cu = raw_catalog.copper.get(s, type('obj', (object,), {'price': 0.0})).price
    price_al = raw_catalog.aluminum.get(s, type('obj', (object,), {'price': 0.0})).price
    data_prices.append({"Sección (mm²)": s, "Cobre (€/m)": price_cu, "Aluminio (€/m)": price_al})

with st.expander("💶 Editar precios por metro de conductor unipolar", expanded=False):
    edited_prices_df = st.data_editor(pd.DataFrame(data_prices), hide_index=True, use_container_width=True,
                                      num_rows="fixed")

# --- 7.5. PHASE E CONFIG (Pre-Simulation) ---
st.header("6. Configuración Fase E (MPPTs)")
st.info("Configura la asignación automática de MPPTs que se ejecutará tras la optimización.")

col_e1, col_e2 = st.columns(2)
with col_e1:
    pe_target_level = st.number_input("Nivel a Optimizar (Strings)", min_value=1, max_value=5, value=num_levels)
with col_e2:
    pe_n_mppts = st.number_input("Nº MPPTs por Inversor", min_value=1, value=6, key="pe_n_mppts_ui")

# Configurar número de entradas por cada MPPT
if 'pe_mppt_capacities' not in st.session_state or len(st.session_state['pe_mppt_capacities']) != pe_n_mppts:
    st.session_state['pe_mppt_capacities'] = [{"MPPT": i+1, "Entradas Máximas": 6} for i in range(pe_n_mppts)]

st.write("Capacidad por MPPT (Editable):")
pe_edited_caps = st.data_editor(
    pd.DataFrame(st.session_state['pe_mppt_capacities']),
    hide_index=True,
    use_container_width=True,
    key="pe_mppt_editor"
)
st.session_state['pe_mppt_capacities'] = pe_edited_caps.to_dict('records')
pe_inputs_list = [int(row["Entradas Máximas"]) for row in st.session_state['pe_mppt_capacities']]

# ─────────────────────────────────────────────────────────────────────────────
# --- 8. MODO DE EJECUCIÓN: Navegación Persistente ---
# Usamos st.radio horizontal en lugar de st.tabs para evitar el reset al interactuar.
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("⚙️ Seleccionar Modo de Resultados / Ejecución")
selected_mode = st.radio(
    "Modo de trabajo:",
    ["⚡ Flujo Completo (Pasos 1-6)", "🔌 Solo Fase E (MPPTs)"],
    horizontal=True,
    key="active_navigation_mode",
    label_visibility="collapsed"
)

# ══════════════════════════════════════════════════════════════════════════════
# OPCIÓN 1 — FLUJO COMPLETO
# ══════════════════════════════════════════════════════════════════════════════
if selected_mode == "⚡ Flujo Completo (Pasos 1-6)":
    st.header("7. Resultados")

    if st.button("🚀 CALCULAR OPTIMIZACIÓN", type="primary", key="btn_full_run"):
        # Check for rules in both standard (Step 3) and advanced (Step 4) sections
        has_rules = len(st.session_state.get('rules_list', [])) > 0 or len(st.session_state.get('parsed_advanced_rules', [])) > 0
        
        if uploaded_file is None or not has_rules:
            st.error("⚠️ Faltan datos: Por favor carga el Excel y define al menos una regla (estándar o avanzada).")
        else:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
                tmp_file.write(uploaded_file.getvalue())
                tmp_path = tmp_file.name
            
            try:
                # A. Prepare Context
                final_systems = {}
                for lvl, ui_val in config_niveles["systems_ui"].items():
                    if "AC Trifásica" in ui_val:
                        final_systems[lvl] = "AC_TRI"
                    elif "AC Monofásica" in ui_val:
                        final_systems[lvl] = "AC_MONO"
                    elif "DC" in ui_val:
                        final_systems[lvl] = "DC_MONO"
                
                final_rules = []
                for r in st.session_state['rules_list']:
                    if r['type'] == 'IntraLevelRule':
                        final_rules.append(IntraLevelRule(int(r['level']), r['metric'], float(r['limit'])))
                    elif r['type'] == 'ParentChildSubgroupRule':
                        final_rules.append(
                            ParentChildSubgroupRule(int(r['parent_level']), int(r['child_level']), r['child_metric'],
                                                    float(r['limit'])))
                    elif r['type'] == 'LocalSubgroupRule':
                        final_rules.append(
                            LocalSubgroupRule(int(r['parent_level']), int(r['child_level']), r['child_metric'],
                                              float(r['limit'])))
                
                # Update catalog with edited prices
                for index, row in edited_prices_df.iterrows():
                    s = row["Sección (mm²)"]
                    p_cu = row["Cobre (€/m)"]
                    p_al = row["Aluminio (€/m)"]
                    if s in raw_catalog.copper:
                        raw_catalog.copper[s].price = p_cu
                    if s in raw_catalog.aluminum:
                        raw_catalog.aluminum[s].price = p_al
                
                ctx = OptimizationContext(
                    temp_global,
                    config_niveles["voltages"],
                    final_systems,
                    config_niveles["factors"],
                    config_niveles["frequencies"],
                    config_niveles["disposiciones"],
                    config_niveles["allowed_sections"],
                    config_niveles["allow_double"],
                    None, # derating_factor (unused in context, kept for compatibility if needed)
                    config_niveles["level_ampacities"],
                    config_niveles["level_t_ref_suelo"],
                    config_niveles["level_t_ref_aire"],
                    config_niveles["level_t_max"],
                    final_rules,
                    raw_catalog
                )
                
                if st.session_state.get('parsed_advanced_rules'):
                    # HAL-R04: Validate that advanced rules reference valid levels
                    for adv_rule in st.session_state['parsed_advanced_rules']:
                        rule_levels = []
                        if hasattr(adv_rule, 'level'):
                            rule_levels.append(adv_rule.level)
                        if hasattr(adv_rule, 'parent_level'):
                            rule_levels.append(adv_rule.parent_level)
                        if hasattr(adv_rule, 'child_level'):
                            rule_levels.append(adv_rule.child_level)
                        for rl in rule_levels:
                            if rl > num_levels:
                                st.warning(f"⚠️ Regla avanzada referencia Nivel {rl}, pero solo hay {num_levels} niveles definidos.")
                    ctx.rules.extend(st.session_state['parsed_advanced_rules'])
                
                # B. Execution with st.status for clear user feedback
                results = {}
                
                with st.status("🔄 Ejecutando optimización...", expanded=True) as status:
                    status.update(label="📂 Cargando y validando datos del Excel...")
                    st.write("Construyendo árbol de circuitos...")
                    
                    strategy = BFTBStrategy(repository)
                    engine = OptimizerEngine(
                        filepath=tmp_path,
                        context=ctx,
                        repository=repository,
                        strategy=strategy
                    )
                    
                    if not engine.load_and_validate():
                        status.update(label="❌ Error al cargar datos", state="error", expanded=True)
                        st.error("No se pudieron cargar o validar los datos del Excel.")
                        results["v2"] = (None, None, 0.0)
                    else:
                        # Requirement 4: Validation of Ampacities before simulation
                        invalid_circuits = []
                        for code, circuit in engine.circuits.items():
                            if circuit.level in ctx.level_ampacities:
                                # For this circuit's level, check if it has the required ampacity for its method
                                # We check the allowed sections because the optimizer will try them
                                amp_map = ctx.level_ampacities[circuit.level]
                                method_key = "enterrado" if circuit.is_enterrado else "aereo"
                                method_label = "Enterrado" if circuit.is_enterrado else "Aéreo"
                                
                                # Check the minimum section and all other allowed sections
                                # because the optimizer will iterate over candidates
                                for section, n_cond in engine.candidate_lists.get(circuit.level, []):
                                    iz_vals = amp_map.get(section, {})
                                    val = iz_vals.get(method_key, 0.0)
                                    if val <= 0:
                                        invalid_circuits.append(
                                            f"Circuito {code} (N{circuit.level}): Falta ampacidad {method_label} para sección {section} mm²"
                                        )
                        
                        if invalid_circuits:
                            status.update(label="❌ Validación fallida", state="error", expanded=True)
                            st.error("### ⚠️ Faltan Ampacidades Base\n" + "\n".join([f"- {m}" for m in list(set(invalid_circuits))[:10]]))
                            if len(invalid_circuits) > 10:
                                st.write(f"... y {len(invalid_circuits)-10} errores más.")
                            results["v2"] = (None, None, 0.0)
                        else:
                            status.update(label="⚡ Ejecutando algoritmo BFTB...")
                            st.write(f"Optimizando {len(engine.circuits)} circuitos...")
                        
                        start_time = time.time()
                        result = engine.solve()
                        elapsed_time = time.time() - start_time
                        
                        results["v2"] = (engine, result, elapsed_time)
                        status.update(label="✅ Optimización completada", state="complete", expanded=False)
                
                # C. Process Results
                if results["v2"][1]:
                    engine_v2, result_v2, time_v2 = results["v2"]
                    cost_v2 = calculate_real_project_cost(result_v2.solution_map, engine_v2.circuits, ctx, repository)
                    
                    # SAVE TO SESSION STATE (persists across reruns)
                    # NOTE (HAL-S01): We store full engine/result objects for Phase E and table generation.
                    # These are non-serializable and can be large in multi-user deployments.
                    # Acceptable for single-user/local use; consider extracting only needed data if scaling.
                    st.session_state['last_engine'] = engine_v2
                    st.session_state['last_result'] = result_v2
                    st.session_state['last_context'] = ctx
                    st.session_state['last_cost'] = cost_v2
                    st.session_state['last_time'] = time_v2
                    
                    # --- AUTOMATIC PHASE E EXECUTION ---
                    try:
                        allocator = MPPTAllocator(engine_v2, result_v2, ctx)
                        df_phase_e = allocator.allocate(int(pe_target_level), pe_inputs_list)
                        st.session_state['phase_e_data'] = df_phase_e
                    except Exception as e_ph:
                        st.session_state['phase_e_data'] = None
                        st.error(f"Error en Fase E Automática: {e_ph}")
                    
                    # Build table data and persist in session_state
                    phase_e_data = st.session_state.get('phase_e_data', None)
                    data_v2 = build_table_data(engine_v2, result_v2, ctx, phase_e_data)
                    st.session_state['last_table_data'] = data_v2
                    
                    # Build materials summary and persist
                    m_v2 = generate_materials_summary(engine_v2.circuits, result_v2.solution_map, ctx)
                    st.session_state['last_materials_summary'] = m_v2
                    
                    # Generate Excel in memory (BytesIO) and persist
                    excel_buffer = io.BytesIO()
                    out_name = f"{os.path.splitext(uploaded_file.name)[0]}_Optimizado.xlsx"
                    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                        pd.DataFrame(data_v2).to_excel(writer, sheet_name="Detalle", index=False)
                    excel_buffer.seek(0)
                    st.session_state['last_excel_bytes'] = excel_buffer.getvalue()
                    st.session_state['last_excel_name'] = out_name
                    
                    st.session_state['optimization_done'] = True
                else:
                    st.session_state['optimization_done'] = False
                    st.error("❌ No se encontró solución.")
            except Exception as e:
                st.error(f"Error: {e}")
                import traceback
                st.code(traceback.format_exc())
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    # --- 9. PERSISTENT RESULTS DISPLAY ---
    # Results are rendered OUTSIDE the button block so they survive reruns
    if st.session_state.get('optimization_done', False):
        st.success("✅ ¡Optimización Finalizada!")
        
        # KPI Metrics
        c1, c2 = st.columns(2)
        c1.metric("💰 Coste Total Optimizado", f"{st.session_state['last_cost']:,.2f} €")
        c2.metric("⏱️ Tiempo de Ejecución", format_time_ms(st.session_state['last_time']))
        
        # Results table
        if 'last_table_data' in st.session_state:
            st.subheader("Detalle de Resultados")
            st.dataframe(pd.DataFrame(st.session_state['last_table_data']), use_container_width=True)
            
            st.subheader("📦 Resumen Materiales")
            if 'last_materials_summary' in st.session_state:
                for title, df in st.session_state['last_materials_summary']:
                    st.markdown(f"**{title}**")
                    st.dataframe(df, use_container_width=True)
        
        # Excel download (from memory buffer)
        if 'last_excel_bytes' in st.session_state:
            st.download_button(
                "📥 Descargar Reporte Excel",
                data=st.session_state['last_excel_bytes'],
                file_name=st.session_state.get('last_excel_name', 'VoltX_Optimizado.xlsx'),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    # --- 10. PHASE E: RESULTS DISPLAY (inside Tab 1) ---
    # Automatically display results if they exist in session state
    if 'phase_e_data' in st.session_state and st.session_state['phase_e_data'] is not None:
        st.markdown("---")
        st.header("8. Resultados Fase E: Asignación de MPPTs")
        
        df_pe = st.session_state['phase_e_data']
        
        if not df_pe.empty:
            # Check for errors (with column existence check)
            if 'Error' in df_pe.columns:
                errors = df_pe[df_pe['Error'] != 'OK']
                if not errors.empty:
                    st.error(f"⚠️ Se encontraron {len(errors)} circuitos con errores de capacidad.")
                    st.dataframe(errors, use_container_width=True)
                else:
                    st.success("✅ Asignación MPPT correcta (Sin errores de capacidad).")
            else:
                st.success("✅ Asignación MPPT completada.")
            
        else:
            st.warning(f"⚠️ La Fase E se ejecutó pero no encontró circuitos para el nivel {pe_target_level}.")


# ══════════════════════════════════════════════════════════════════════════════
# OPCIÓN 2 — SOLO FASE E
# ══════════════════════════════════════════════════════════════════════════════
else:
    st.header("Ejecución Aislada: Solo Fase E (MPPTs)")
    st.info(
        "Este modo permite asignar MPPTs **sin ejecutar la optimización completa**. "
        "Solo debes proporcionar un Excel con los circuitos y su tensión final (V_final)."
    )

    # ── Guía del formato + plantilla descargable ──────────────────────────────
    with st.expander("📋 Ver formato del Excel de entrada requerido", expanded=True):
        st.markdown("""
        El archivo Excel debe contener **exactamente** las siguientes columnas:

        | Columna | Tipo | Requerido | Descripción |
        | :--- | :---: | :---: | :--- |
        | **`Codigo_Circuito`** | `str` | ✅ Sí | ID único del circuito/string. El **nivel se infiere automáticamente** contando los segmentos separados por guión: `INV01` = Nivel 1, `INV01-S01` = Nivel 2, `INV01-S01-X` = Nivel 3. |
        | **`V_final`** | `float` | ✅ Sí | **Tensión final en bornes del inversor/MPPT**, en Voltios (V). |

        > **Nota sobre `V_final`:** No es la caída de tensión, sino la tensión resultante.
        > Ej: `V_nominal = 800 V`, caída = `12.5 V` → `V_final = 787.5 V`.
        """)

        col_g1, col_g2 = st.columns([2, 1])
        with col_g1:
            st.info("Descarga la plantilla de ejemplo para comenzar rápidamente:")
        with col_g2:
            st.download_button(
                label="📄 Descargar Plantilla Fase E",
                data=generate_phase_e_template(),
                file_name="plantilla_solo_fase_e.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="btn_download_fase_e_template"
            )

        st.dataframe(
            pd.DataFrame({
                "Codigo_Circuito": ["INV01", "INV01-S01", "INV01-S02", "INV02-S01"],
                "V_final":         [795.00,  787.50,       788.20,       786.90],
            }),
            hide_index=True,
            use_container_width=True,
        )

    st.markdown("---")

    # ── Carga del Excel de Fase E ─────────────────────────────────────────────
    st.subheader("1. Carga del Excel")
    uploaded_fase_e = st.file_uploader(
        "Sube tu archivo Excel con `Codigo_Circuito` y `V_final`",
        type=["xlsx"],
        key="uploader_fase_e",
        help="El archivo debe contener las columnas 'Codigo_Circuito' y 'V_final'. El nivel se infiere del código."
    )

    # ── Configuración de Fase E Standalone ───────────────────────────────────
    st.subheader("2. Configuración MPPT")
    col_se1, col_se2 = st.columns(2)
    with col_se1:
        se_nivel = st.number_input(
            "Nivel de circuitos a procesar",
            min_value=1,
            max_value=5,
            value=2,
            step=1,
            key="se_nivel",
            help="Selecciona el nivel jerárquico de los circuitos que quieres asignar a MPPTs."
        )
    with col_se2:
        se_n_mppts = st.number_input("Nº MPPTs por Inversor", min_value=1, value=6, key="se_n_mppts")

    if 'se_mppt_capacities' not in st.session_state or len(st.session_state['se_mppt_capacities']) != se_n_mppts:
        st.session_state['se_mppt_capacities'] = [{"MPPT": i+1, "Entradas Máximas": 6} for i in range(se_n_mppts)]

    st.write("Capacidad por MPPT (Editable):")
    se_edited_caps = st.data_editor(
        pd.DataFrame(st.session_state['se_mppt_capacities']),
        hide_index=True,
        use_container_width=True,
        key="se_mppt_editor"
    )
    st.session_state['se_mppt_capacities'] = se_edited_caps.to_dict('records')
    se_inputs_list = [int(row["Entradas Máximas"]) for row in st.session_state['se_mppt_capacities']]

    st.markdown("---")

    # ── Botón de Ejecución ────────────────────────────────────────────────────
    st.subheader("3. Ejecutar")

    if st.button("🔌 EJECUTAR SOLO FASE E", type="primary", key="btn_solo_fase_e"):
        if uploaded_fase_e is None:
            st.error("⚠️ Debes cargar un archivo Excel antes de ejecutar.")
        else:
            try:
                # 1. Leer el Excel
                df_input_fase_e = pd.read_excel(uploaded_fase_e)

                # 2. Instanciar el allocator
                allocator_standalone = StandaloneMPPTAllocator(df_input=df_input_fase_e)

                # 3. Ejecutar la asignación
                with st.spinner("⚙️ Calculando asignación de MPPTs..."):
                    df_resultado_se = allocator_standalone.allocate(
                        nivel=int(se_nivel),
                        inputs_per_mppt=se_inputs_list
                    )

                # 4. Construir el Excel de salida enriquecido
                df_enriquecido = df_input_fase_e.copy()
                mppt_map = df_resultado_se.set_index('Circuito Original')[['MPPT', 'Input_PV']].to_dict('index')

                df_enriquecido['MPPT'] = df_enriquecido['Codigo_Circuito'].map(
                    lambda c: mppt_map.get(c, {}).get('MPPT', None)
                )
                df_enriquecido['Input_PV'] = df_enriquecido['Codigo_Circuito'].map(
                    lambda c: mppt_map.get(c, {}).get('Input_PV', None)
                )

                # 5. Persistir en session_state
                st.session_state['solo_fase_e_result']      = df_resultado_se
                st.session_state['solo_fase_e_enriquecido'] = df_enriquecido
                st.session_state['solo_fase_e_done']        = True
                st.session_state['solo_fase_e_filename']    = uploaded_fase_e.name

            except Exception as exc:
                st.session_state['solo_fase_e_done'] = False
                st.error(f"❌ Error: {exc}")

    # ── Resultados Fase E Standalone ──────────────────────────────────────────
    if st.session_state.get('solo_fase_e_done', False):
        df_se          = st.session_state['solo_fase_e_result']
        df_enriquecido = st.session_state.get('solo_fase_e_enriquecido', df_se)

        st.markdown("---")
        st.subheader("4. Resultados: Asignación de MPPTs")
        
        # LEDs de estado
        if 'Error' in df_se.columns:
            errors_se = df_se[df_se['Error'] != 'OK']
            if not errors_se.empty:
                st.error(f"⚠️ {len(errors_se)} circuito(s) exceden capacidad.")
                with st.expander("Ver detalles de errores"):
                    st.dataframe(errors_se, use_container_width=True)
            else:
                st.success("✅ Asignación MPPT correcta.")

        # KPIs
        n_inversores = df_se['Inversor (Padre)'].nunique()
        n_circuitos  = df_se['Circuito Original'].nunique()
        c1, c2, c3 = st.columns(3)
        c1.metric("Inversores", n_inversores)
        c2.metric("Circuitos", n_circuitos)
        c3.metric("MPPTs", int(se_n_mppts))

        # Tabla y descarga
        st.caption("👇 Vista previa del Excel de salida (enriquecido)")
        st.dataframe(df_enriquecido, use_container_width=True, hide_index=True)

        nombre_base = os.path.splitext(st.session_state['solo_fase_e_filename'])[0]
        out_se_name = f"{nombre_base}_MPPT.xlsx"
        
        excel_se_buf = io.BytesIO()
        with pd.ExcelWriter(excel_se_buf, engine='openpyxl') as writer:
            df_enriquecido.to_excel(writer, sheet_name="Datos_MPPT", index=False)
        
        st.download_button(
            label="📥 Descargar Excel con MPPTs asignados",
            data=excel_se_buf.getvalue(),
            file_name=out_se_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

