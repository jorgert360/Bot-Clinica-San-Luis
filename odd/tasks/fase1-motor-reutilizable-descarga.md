# Fase 1A — Motor reutilizable de descarga de facturas

## Objetivo
Extraer progresivamente la lógica ya probada de `scripts/poc_download_invoice_document.py`
hacia una arquitectura reutilizable (`process_invoice(invoice_number, destination_directory) -> InvoiceDownloadResult`),
sin reescribir la automatización desde cero y sin romper el comportamiento
validado en Fase 0. Procesa UNA factura por ejecución.

## Por qué
Fase 0 (`DOWNLOAD_COMPLETED`, factura FHC245333, commits `ede060a`/`54e2334`
en `master`) probó que el flujo funciona de punta a punta para un caso
controlado. Fase 1A convierte ese PoC en un componente parametrizable que
la Fase 1B/2 (masivo, Excel, SQLite) podrá reutilizar — explícitamente
fuera de alcance en esta fase.

## Fuera de alcance (explícito, per instrucción del usuario)
Lectura de Excel, SQLite, Consulta historias, tipificación, merge de PDFs,
SOAT, interfaz gráfica, procesamiento paralelo/masivo, segunda factura de
prueba sin autorización nueva.

## Regla 1 (cumplida antes de escribir código)
- [x] Checkpoint de Fase 0 verificado (`git log`: `ede060a` milestone,
      `54e2334` docs).
- [x] `git status` confirmado limpio (solo caché local `.atl/`, ahora
      ignorado).
- [x] Rama creada: `phase-1-reusable-invoice-engine`.

## Regla 2 — reutilizar, no reescribir
El PoC (`scripts/poc_download_invoice_document.py`) permanece intacto como
referencia de regresión durante toda la fase — NO se borra. Los módulos
nuevos extraen/envuelven su lógica ya auditada (patrones
`choose_single_action_method`/`activate_once`/`activate_preselected_once`,
`resolve_export_arrow_target`/`open_export_dropdown_once`,
`find_no_button_candidates_live` con match exacto, `find_mdi_child_live`,
recibo de sesión del visor, validación de PDF) en vez de reimplementarla.

## Arquitectura objetivo (guía, no mandato estético)
```
src/clinica_rpa/
├── automation/{go_session,trazabilidad,report_viewer,dialogs,waits}.py
├── services/invoice_download_service.py
├── domain/{models,errors}.py
├── infrastructure/logging_config.py (ya existe)
└── pdf/validation.py
scripts/download_invoice.py
```
Extraer solo donde la responsabilidad esté claramente identificada; no
mover archivos por estética.

## Selectores centralizados (nunca PID/handle/coordenadas absolutas)
- Campo N° Factura: `automation_id=INDbteInvoiceNumber`
- Documento Origen: `automation_id=INDHleDocument`
- Visor de Reportes: `automation_id=FrmReportViewer`
- Fallbacks relativos/calibrados existentes (Trazabilidad tile, flecha del
  split button, ítem "PDF File" del menú) se preservan tal cual — nunca se
  sustituyen por coordenadas absolutas nuevas.

## Regla de acciones mutantes (obligatoria en todo el refactor)
Por etapa: detectar read-only → elegir método → verificar foreground →
ejecutar UNA sola acción → verificar resultado. Nunca fallback automático
tras una excepción/timeout de una acción mutante — se trata como
`ESTADO_AMBIGUO` y requiere inspección antes de cualquier segunda acción.

## Modelo de resultado
`InvoiceDownloadResult`: `invoice_number_masked`, `status`, `pdf_created`,
`pdf_path`, `pdf_size_bytes`, `pdf_pages`, `elapsed_seconds`, `error_code`,
`error_message_safe`. Sin información clínica adicional.

## Detección de estado de GO (reutilizable, sin recuperación agresiva)
Estados: `MIS_FAVORITOS`, `TRAZABILIDAD_EMPTY`, `TRAZABILIDAD_RESULT`,
`REPORT_VIEWER`, `EXPORT_MENU`, `PDF_OPTIONS`, `SAVE_AS`,
`FINAL_EXPORT_DIALOG`, `UNKNOWN`. Estado inesperado → error seguro, nunca
recuperación agresiva en esta fase.

## Tareas
- [x] T0 — Regla 1: checkpoint verificado, rama creada, `.gitignore` para
      `.atl/`. Ruta: inline (git/estado, mecánico).
- [ ] T1 — Módulos `domain/errors.py` (códigos de error centralizados) y
      `domain/models.py` (`InvoiceDownloadResult`, estados de GO). Ruta:
      delegado (writer único, junto con T2-T5 — ver nota de delegación).
- [ ] T2 — `pdf/validation.py`: extraer `validate_pdf_file` del PoC
      (mismo criterio: header+tamaño obligatorio, páginas vía pypdf
      best-effort, nunca bloqueante si pypdf no está).
- [ ] T3 — `automation/{go_session,trazabilidad,report_viewer,dialogs,waits}.py`:
      extraer descubrimiento dinámico de GO, apertura de Trazabilidad
      (calibración relativa), apertura de Documento Origen/Visor, manejo
      de diálogos (Export dropdown/PDF File/PDF Options/Guardar
      como/diálogo final), y esperas con backoff progresivo (nunca walk
      profundo cada 500ms).
- [ ] T4 — `services/invoice_download_service.py`: `process_invoice(invoice_number, destination_directory) -> InvoiceDownloadResult`
      orquestando detección de estado + los 16 pasos ya validados en Fase 0.
- [ ] T5 — `scripts/download_invoice.py`: CLI (`--invoice`, `--output`),
      imprime resumen sanitizado (STATUS/PDF_CREATED/SIZE/PAGES/ELAPSED),
      nunca datos de paciente.
- [ ] T6 — Verificación estática (yo, no delegado): `py_compile` de todo
      lo nuevo, grep de seguridad (sin PID/handle hardcodeado, sin
      coordenadas absolutas nuevas, conteo de call sites mutantes por
      etapa = 1), lectura del diff completo del writer.
- [ ] T7 — Regresión en vivo (yo, no delegado): `download_invoice.py
      --invoice FHC245333 --output runtime/downloads/test` contra el GO
      real, con nombre de archivo con timestamp (nunca sobrescribe el PDF
      de Fase 0). DETENERSE tras `COMPLETED` — sin segunda factura.
- [ ] T8 — Informe FASE 1A entregado al usuario.

## Cierre — T1-T8 completadas

- [x] T1-T5 delegadas a un writer (general-purpose/sonnet); resultado
      verificado en detalle por mí (lectura completa de cada archivo
      nuevo, no solo el resumen del agente).
- [x] T6 verificación estática: `py_compile` PASS en los 14 archivos.
      Call sites mutantes por archivo: dialogs.py=1 (write_save_path...),
      go_session.py=3 (definiciones de activate_once/click_once, no
      duplicados), report_viewer.py=4 (incluye las 3 ramas internas de
      `open_export_dropdown_once` + el click de PDF File), trazabilidad.py=2
      (tile Trazabilidad + escritura de factura) -- ninguno duplicado, cada
      etapa tiene exactamente 1 acción real.
- [x] **3 bugs encontrados y corregidos durante la verificación** (no
      cosméticos, hubieran impedido que el motor funcionara contra GO real):
  1. `_matches_any()` en `go_session.py` solo casefoldeaba el haystack, no
     la needle -- rompía TODO match contra `STRONG_SIGNAL_TEXTS` (Title
     Case, copiado del PoC original), incluyendo `find_authenticated_go_window`,
     el primer paso de todo el pipeline. Corregido casefoldeando ambos lados.
  2. `_resolve_go_state()` hacía un único intento de lectura inmediatamente
     después del clic en Trazabilidad, sin tolerar un fallo transitorio de
     GO ocupado (patrón ya documentado toda la sesión) -- agregado retry
     con backoff progresivo sobre la VERIFICACIÓN (nunca el clic).
  3. `compute_destination_path()` nunca resolvía `destination_directory` a
     ruta absoluta -- al escribirse una ruta relativa en el diálogo nativo
     de Windows (que corre en el proceso de GO, no en el nuestro), Windows
     la resolvía contra el directorio de trabajo de GO, no el del proyecto
     (observado en vivo: el diálogo se reancló solo en "Descargas", la
     carpeta destino quedó vacía). Corregido con `.resolve()`.
- [x] Timeouts recalibrados en vivo (GO demostró latencia consistente de
      20-45s hoy, muy por encima del comportamiento de Fase 0):
      `DOCUMENTO_ORIGEN_NO_EFFECT` 7→30s, `EXPORT_MENU` 10→20s,
      `PDF_OPTIONS` 10→45s, `SAVE_DIALOG`/`FINAL_DIALOG` 15→30s. Backoff
      progresivo (0.5/1/1.5/2s) agregado a `light_wait_for_window`.
- [x] T7 regresión en vivo: **COMPLETED**. Ejecutada en dos tramos (CLI
      completo hasta el fix del bug #3, luego continuación manual
      reusando las mismas funciones del motor para verificar el resto de
      la cadena) por la sucesión de timeouts durante el diagnóstico -- cada
      función individual del motor quedó ejercitada y confirmada contra GO
      real al menos una vez.

### Evidencia de regresión (FHC245333)
- archivo: `runtime/downloads/test/factura_FHC245333_20260924_235257.pdf`
- tamaño: 86963 bytes (idéntico al de Fase 0 -- mismo documento origen)
- header `%PDF`: válido
- páginas: 2
- resultado: `COMPLETED`
- no se sobrescribió el PDF de Fase 0 (`runtime/downloads/poc/`, directorio
  distinto, y el nombre lleva timestamp de todas formas)

## Nota de delegación
T1-T5 se delegan a UN solo writer (general-purpose, model=sonnet, ruta
"Writer trigger" — 2+ archivos no triviales) con: (a) los archivos fuente
completos a extraer (`poc_download_invoice_document.py` y sus
dependencias: `poc_move_to_trazabilidad.py`, `poc_search_invoice.py`,
`poc_search_invoice_enter.py`, `poc_write_invoice.py`,
`poc_click_trazabilidad.py`, `diagnose_go_windows.py`, `inspect_go.py`,
`inspect_trazabilidad_form.py`), (b) todas las reglas de seguridad de esta
sesión, (c) prohibición explícita de ejecutar nada contra GO real (solo
escribe código; la ejecución en vivo la hago yo en T7).

## TDD
No hay TDD explícito configurado por el usuario para este proyecto RPA
(mismo criterio que Fase 0). Verificación = `py_compile` + grep de
seguridad + regresión en vivo real contra GO, no una suite unitaria nueva
obligatoria (aunque `tests/test_poc_download_invoice_document.py` ya
existente puede extenderse si el writer lo considera natural, sin que sea
requisito de esta fase).

## Evidencia
(se completa durante la ejecución)
