# Arquitectura propuesta

## Visión general

```text
┌─────────────────────────────┐
│ Excel diario                │
│ listado de facturas         │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ Input Loader                │
│ pandas / openpyxl           │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ Orquestador RPA             │
│ Python                      │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ GO / Índigo                 │
│ Windows UI Automation       │
│ pywinauto / UIA             │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ Descargas temporales        │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ Motor de documentos         │
│ clasificación / PDF         │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ Carpetas tipificadas        │
│ + reporte de ejecución      │
└─────────────────────────────┘
```

## Arquitectura por capas

### app/

Interfaz para el usuario.

En el MVP puede ser CLI.

Más adelante será GUI.

### automation/

Responsable exclusivamente de interacción con GO/Índigo.

Ejemplo:

```text
automation/
    go_client.py
    window_finder.py
    selectors.py
    dialogs.py
    waits.py
```

### domain/

Reglas del proceso.

No debe depender de pywinauto.

```text
domain/
    models.py
    statuses.py
    exceptions.py
```

### services/

Casos de uso.

```text
services/
    invoice_processor.py
    download_service.py
    resume_service.py
```

### infrastructure/

Persistencia, archivos, configuración, logs.

```text
infrastructure/
    excel_reader.py
    state_repository.py
    file_manager.py
    logging_config.py
```

### pdf/

Posteriormente:

```text
pdf/
    merger.py
    validator.py
    renamer.py
```

## Estado del procesamiento

Usar SQLite desde una fase temprana.

Ejemplo:

```text
job
invoice
event_log
download
```

Estados posibles:

```text
PENDING
RUNNING
COMPLETED
COMPLETED_WITH_WARNINGS
FAILED
SKIPPED
```

Esto permitirá reanudar procesos.

## Selectores

Nunca distribuir coordenadas de pantalla como solución principal.

Preferir:

- title
- control_type
- automation_id
- name
- class_name

Ejemplo conceptual:

```python
window.child_window(
    title="Trazabilidad de factura",
    control_type="Edit"
)
```

Si un selector no es estable, encapsularlo en `selectors.py`.

No repartir selectores por todo el código.

## Esperas

No usar:

```python
time.sleep(10)
```

como mecanismo normal de sincronización.

Implementar:

- wait_visible
- wait_enabled
- wait_exists
- wait_window
- wait_file_created
- timeout configurable
- retry con backoff

## Carpetas del proyecto

```text
clinica-rpa/
│
├── README.md
├── pyproject.toml
├── .gitignore
├── .env.example
│
├── docs/
│
├── src/
│   └── clinica_rpa/
│       ├── app/
│       ├── automation/
│       ├── domain/
│       ├── services/
│       ├── infrastructure/
│       ├── pdf/
│       └── config/
│
├── scripts/
│   ├── inspect_go.py
│   ├── list_windows.py
│   └── poc_invoice_search.py
│
├── tests/
│
├── runtime/
│   ├── downloads/
│   ├── screenshots/
│   ├── logs/
│   └── state/
│
└── samples/
    └── README.md
```

No guardar datos reales de pacientes dentro de `samples/`.
