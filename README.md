# VoltX - Optimizador de Cables

VoltX es una aplicación avanzada desarrollada en Python con Streamlit diseñada para la optimización técnica y económica del dimensionamiento de cables en instalaciones eléctricas (especialmente fotovoltaicas).

## Características Principales

- **Algoritmo BFTB (Bang-For-The-Buck):** Optimiza la selección de secciones de cable maximizando la mejora en la caída de tensión por cada euro invertido.
- **Multinivel:** Gestiona jerarquías complejas de circuitos (desde media tensión hasta strings de paneles).
- **Cálculo Térmico Dinámico:** Estimación precisa de la temperatura del conductor según la carga y condiciones de instalación (IEC 60287).
- **Gestión de Reglas:** Validación de caídas de tensión acumuladas, locales e intra-nivel.
- **Asignación de MPPTs:** Lógica para la distribución óptima de circuitos en las entradas de los inversores.
- **Interfaz Intuitiva:** Interfaz web interactiva que permite cargar datos desde Excel y exportar informes detallados.

## Estructura del Proyecto

- `app.py`: Punto de entrada de la aplicación Streamlit.
- `core/`: Motor de optimización y lógica de reglas.
- `domain/`: Modelos de datos y física eléctrica.
- `services/`: Motores de ejecución y lógica de negocio.
- `data/`: Repositorio y persistencia de catálogos de cables.
- `.streamlit/`: Configuración estética y funcional de la interfaz.

## Instalación

1. Clona este repositorio.
2. Instala las dependencias necesarias:
   ```bash
   pip install -r requirements.txt
   ```
3. Ejecuta la aplicación:
   ```bash
   streamlit run app.py
   ```

## Desarrollador

**Jonathan Hurtado Moreira**
- [LinkedIn](https://www.linkedin.com/in/jonaa-hurtado)
- [Email](mailto:hurtadomoreirajonathan@gmail.com)
