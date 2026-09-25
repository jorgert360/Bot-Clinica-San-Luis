# FASE 0 — Proof of Concept GO/Índigo

## Objetivo

Demostrar que Python puede controlar de forma estable y autorizada GO/Índigo.

## Alcance

Procesar una sola factura de prueba.

No implementar aún:

- historias clínicas completas
- reglas de EPS
- SOAT completo
- consolidación de PDFs
- GUI final
- procesamiento masivo

## Fase F0.1 — Detectar GO

Crear:

`scripts/list_windows.py`

Debe listar ventanas visibles de Windows con:

- título
- handle
- process_id
- executable si se puede obtener
- backend recomendado

Resultado:

identificar inequívocamente GO/Índigo.

## Fase F0.2 — Inspeccionar controles

Crear:

`scripts/inspect_go.py`

Debe:

1. conectar con GO
2. recorrer árbol accesible
3. imprimir:
   - control_type
   - name
   - automation_id
   - class_name
   - rectangle
4. guardar resultado en:

`runtime/logs/go_ui_tree.txt`

Limitar profundidad para evitar resultados gigantes.

Debe permitir filtrar por texto.

Ejemplo:

```bash
python scripts/inspect_go.py --contains factura
```

## Fase F0.3 — Conector

Crear:

`src/clinica_rpa/automation/go_client.py`

Responsabilidades:

- localizar proceso
- conectar a ventana
- comprobar estado
- activar ventana
- recuperar diálogo activo
- devolver errores claros

No incluir todavía lógica de factura.

## Fase F0.4 — Navegación

Crear:

`src/clinica_rpa/automation/navigation.py`

Probar navegación manual asistida hacia:

Vie Finance → Trazabilidad de factura

Primero intentar selectores accesibles.

Si no se puede, registrar qué parte falla.

## Fase F0.5 — Buscar factura

Crear:

`scripts/poc_invoice_search.py`

Argumento:

```bash
python scripts/poc_invoice_search.py --invoice FHCXXXXXX
```

Debe:

1. conectar a GO
2. localizar módulo de trazabilidad
3. localizar campo de factura
4. limpiar campo
5. escribir número
6. ejecutar búsqueda
7. esperar resultado
8. verificar que existe resultado
9. guardar screenshot local del resultado
10. terminar sin descargar nada

## Fase F0.6 — Abrir documento origen

Extender el script.

Debe abrir el documento origen de la factura encontrada.

Validar que aparece la factura.

No avanzar si existen múltiples resultados ambiguos.

## Fase F0.7 — PDF

Probar el flujo autorizado de:

- imprimir
- guardar como PDF

Usar carpeta:

`runtime/downloads/poc/`

Nombre temporal:

`poc_<invoice>_<timestamp>.pdf`

Esperar hasta que:

- el archivo exista
- su tamaño sea mayor a 0
- deje de crecer durante un período razonable

## Fase F0.8 — Resultado

Crear:

`runtime/logs/poc_report.json`

Ejemplo:

```json
{
  "application_detected": true,
  "ui_automation_backend": "uia",
  "invoice_search": true,
  "document_opened": true,
  "pdf_saved": true,
  "manual_fallbacks": [],
  "security_blocks_detected": false,
  "result": "PASS"
}
```

## Diagnóstico si falla

Clasificar fallo:

```text
APP_NOT_FOUND
WINDOW_NOT_ACCESSIBLE
CONTROL_NOT_EXPOSED
CONTROL_AMBIGUOUS
SEARCH_FAILED
DOCUMENT_NOT_FOUND
PRINT_DIALOG_NOT_ACCESSIBLE
FILE_NOT_CREATED
TIMEOUT
SECURITY_OR_AUTHORIZATION_BLOCK
UNKNOWN
```

## Decisión final

### PASS_PYTHON

Se completó búsqueda + apertura + guardado de PDF mediante UI Automation estable.

### PASS_HYBRID

Parte importante requiere automatización visual o un motor RPA adicional.

### FAIL_TECHNICAL

GO no permite una automatización fiable mediante las técnicas autorizadas probadas.

## Evidencia mínima para vender el proyecto

Antes de presentar el MVP a la clínica debemos tener:

- video corto de una factura de prueba
- log de ejecución
- PDF generado
- reporte JSON PASS
- tiempo total de la prueba
- lista de selectores utilizados
- lista de fallbacks utilizados
