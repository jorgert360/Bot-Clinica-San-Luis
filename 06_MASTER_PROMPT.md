# PROMPT MAESTRO PARA CLAUDE CODE

Quiero que actúes como arquitecto principal y desarrollador senior de automatización RPA en Python para Windows.

Estás trabajando en un proyecto real para automatizar un proceso de facturación de una clínica.

Antes de escribir código, lee COMPLETAMENTE estos archivos:

- README_START_HERE.md
- 01_PROJECT_CONTEXT.md
- 02_ARCHITECTURE.md
- 03_CLAUDE_RULES.md
- 04_PHASE_0_POC.md
- 05_ACCEPTANCE_CRITERIA.md

Debes obedecer especialmente `03_CLAUDE_RULES.md`.

# OBJETIVO ACTUAL

NO vamos a construir todavía todo el robot.

La misión actual es realizar únicamente la FASE 0:

PROBAR SI EL SISTEMA GO/ÍNDIGO INSTALADO EN ESTE PC PUEDE SER AUTOMATIZADO DE MANERA ESTABLE CON PYTHON Y WINDOWS UI AUTOMATION.

Necesitamos demostrar este recorrido mínimo:

GO/Índigo
→ localizar ventana
→ navegar a Trazabilidad de factura
→ introducir número de factura
→ buscar
→ abrir documento origen
→ imprimir/guardar
→ obtener PDF local
→ validar PDF
→ generar reporte PASS/FAIL

# MUY IMPORTANTE

No inventes selectores.

No supongas nombres de botones.

No uses coordenadas de pantalla como primera opción.

No implementes todo el proceso.

No hagas cambios masivos.

No intentes saltarte CAPTCHAs, MFA, permisos, licencias, controles anti-automatización ni mecanismos de seguridad.

Si aparece una restricción de este tipo:

DETENTE.

Registra:

SECURITY_OR_AUTHORIZATION_BLOCK

y explícame qué ocurrió.

# TECNOLOGÍA

Utiliza inicialmente:

- Python 3.12+
- pywinauto
- backend UIA
- pywin32
- psutil
- loguru
- pydantic
- python-dotenv
- pytest

No instales librerías innecesarias.

Antes de instalar cada dependencia explica por qué es necesaria.

# PRIMERA TAREA

Quiero que hagas SOLAMENTE F0.1 y F0.2.

## F0.1

Crear un proyecto Python limpio con la estructura indicada en `02_ARCHITECTURE.md`.

Configurar:

- pyproject.toml
- .gitignore
- .env.example
- src layout
- runtime directories
- logging inicial

Crear:

`scripts/list_windows.py`

El script debe mostrar las ventanas de escritorio relevantes con:

- título
- handle
- PID
- nombre del proceso

Debe ayudar a encontrar GO/Índigo sin asumir su nombre exacto.

## F0.2

Crear:

`scripts/inspect_go.py`

El script debe permitir seleccionar la ventana encontrada y analizar su árbol UI Automation.

Debe mostrar:

- control_type
- name
- automation_id
- class_name
- rectangle

Debe permitir:

```bash
python scripts/inspect_go.py
```

y opcionalmente:

```bash
python scripts/inspect_go.py --contains factura
```

Guardar el árbol encontrado en:

`runtime/logs/go_ui_tree.txt`

No recorras infinitamente el árbol.

Incluye:

- max_depth
- manejo de errores
- timeout razonable

# FLUJO DE TRABAJO

Antes de editar:

1. dime qué vas a crear
2. explícame brevemente la estrategia
3. crea los archivos
4. ejecuta pruebas estáticas
5. ejecuta los scripts que no requieran interacción destructiva
6. muéstrame el resultado

Después:

DETENTE.

No avances a F0.3 hasta que yo ejecute los scripts con GO abierto y te copie la salida.

# CRITERIO DE CALIDAD

El código debe:

- tener type hints
- tener logs
- ser modular
- manejar excepciones
- no tener credenciales
- funcionar en Windows
- ser fácil de empaquetar más adelante
- permitir añadir GUI posteriormente
- permitir reemplazar selectores sin tocar la lógica

# RESULTADO QUE ESPERO DE ESTA PRIMERA EJECUCIÓN

Quiero terminar con instrucciones exactas como:

1. abre GO/Índigo
2. inicia sesión manualmente
3. deja visible la pantalla principal
4. ejecuta:

   python scripts/list_windows.py

5. copia la salida
6. después ejecuta:

   python scripts/inspect_go.py --contains factura

7. copia la salida

No continúes automáticamente con clics reales hasta que yo valide contigo qué controles detectó el sistema.

Empieza ahora únicamente con F0.1 y F0.2.
