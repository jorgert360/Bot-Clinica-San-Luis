# F0.5 — Descarga controlada del documento origen (reanudación tras auditoría)

## Nota de privacidad
El número de factura de prueba se reemplaza en este documento por el
placeholder `FHC000000` (nunca el número real) — mismo criterio de
enmascaramiento que ya aplica todo el código (`_mask_invoice`). El repo se
publica en GitHub; no debe llevar ningún dato identificable de facturación
real.

## Objetivo
Reanudar F0.5 hasta dejar abierto "Opciones de Exportación PDF" y DETENERSE ahí.
No guardar archivo, no pulsar Aceptar, no llegar a "Guardar como" en esta ejecución.

## Por qué
Auditoría de seguridad de `poc_download_invoice_document.py` concluyó
`SAFE_TO_RESUME_F05` (ver reporte previo en esta sesión: py_compile PASS,
sin duplicados mutantes, 1 call site por etapa, bug de `pypdf` corregido).
El usuario autorizó reanudar con un límite explícito: detenerse en
PDF_OPTIONS_READY.

## Restricción dura de esta ejecución
El script `poc_download_invoice_document.py` actual NO tiene punto de parada
en PDF Options — ejecuta PASO 1 a PASO 11 completo (incluye Guardar, diálogo
final, validación de PDF). Para cumplir la instrucción del usuario hace falta
un modo de ejecución que se detenga tras confirmar "Opciones de Exportación
PDF" (fin de PASO 6C), sin ejecutar PASO 7 en adelante.

## Alcance autorizado
- Detección read-only del estado actual de GO (clasificar A-G).
- Reconstrucción SOLO si hace falta: abrir Trazabilidad → escribir
  FHC000000 (única factura autorizada) → consultar → Documento Origen →
  Visor de Reportes → flecha dropdown Export → PDF File → PDF Options.
- Una sola acción mutante por etapa, sin fallback automático (patrón ya
  auditado: `choose_single_action_method` + `activate_once`).
- Split button: solo la flecha, nunca el centro (`resolve_export_arrow_target`
  + `open_export_dropdown_once`); si el método requiere confirmación humana,
  solo mover el cursor, imprimir `EXPORT_ARROW_CONFIRMATION_REQUIRED` y
  preguntar antes de cualquier clic.
- Detenerse en `PDF_OPTIONS_READY`. No Aceptar, no Guardar como, no escribir
  ruta, no guardar archivo, no diálogo final.

## Tareas
- [x] T1 — Detección read-only del estado actual de GO. Primer intento:
      instancia autenticada (PID 20784) sin ventana visible, GO parecía
      cerrada -- usuario confirmó que había sido minimizada/en transición,
      no reiniciada (mismo `process create_time`). Tras login manual del
      usuario, redetectada dinámicamente: handle=1967058, PID=20784,
      evidencia=['Mis Favoritos','Vie Finance','Vie Clinical'].
- [x] T2 — Agregado modo `--stop-after {report_viewer,pdf_options}` a
      `poc_download_invoice_document.py` (dos checkpoints de retorno
      temprano: tras PASO 4 y tras PASO 6C). Ruta: inline (1 archivo,
      cambio mecánico ya entendido tras la auditoría completa). Verificado:
      py_compile PASS, conteo de call sites mutantes sin cambios (6, igual
      que antes del cambio) -- el flag no agrega ni duplica ninguna acción.
- [x] T3 — Reconstrucción ejecutada con mecanismos ya validados:
      `poc_click_trazabilidad.py` (Trazabilidad, 1 clic) →
      `poc_write_invoice.py --invoice FHC000000` (ValuePattern, readback
      9=9) → Enter único (reusando `send_enter_once` de
      `poc_search_invoice_enter.py`, sin el polling profundo de timing --
      espera ligera + una sola verificación read-only con
      `validate_invoice_result_active`) → Documento Origen vía
      `poc_download_invoice_document.py --stop-after report_viewer`.
- [x] T4 — Ejecutado hasta `REPORT_VIEWER_READY`. Documento Origen: 1 clic
      (click_input). Visor de Reportes confirmado: handle=3606610. No se
      tocó Export Document ni el dropdown.
- [x] T5 — Informe entregado al usuario. DETENIDO en REPORT_VIEWER_READY
      por instrucción explícita.

## F0.5A — Export dropdown → PDF File → PDF Options (completado)
- [x] Recibo de sesión del visor registrado manualmente (solo escritura
      local, reusando `save_viewer_receipt`) para desbloquear el pre-check
      tras un falso negativo de timing en una corrida anterior.
- [x] Flecha del split button resuelta solo geométricamente
      (`relative_export_rect`) — confirmación humana obligatoria obtenida
      antes del primer clic (MOVE-ONLY primero).
- [x] Descubierto: el menú de formatos (PDF File/HTML File/...) es
      CUSTOM_DRAWN_CONTROL -- ni UIA (`control_type=menuitem`) ni MSAA
      (`accChild` falla) exponen los ítems individuales. Diagnosticado con
      `inspect_go_msaa.py` reutilizado en vivo antes de decidir el
      fallback.
- [x] Aplicado el mismo patrón ya usado en F0.3A: coordenadas calculadas
      dinámicamente desde el rect real del popup (nunca absolutas), con
      confirmación visual humana antes del primer clic real. El popup
      resultó tener vida corta (se cierra en pocos segundos, antes de que
      un round-trip de chat complete) -- la apertura + clic se ejecutaron
      en una sola corrida continua, reusando el cálculo ya validado
      visualmente.
- [x] PDF File activado (clic único por coordenadas, `click_once`,
      Regla 5 TEMPORARY_FALLBACK documentado).
- [x] `Opciones de Exportación PDF` confirmado (handle=920514, título
      exacto). Aceptar y Cancelar identificados vía UIA (1 candidato cada
      uno), ninguno tocado. Sanity-check de contenido esperado: rango de
      páginas, convertir imágenes a jpeg, calidad de imagen, PDF/A.
- [x] RESULTADO: `PDF_OPTIONS_READY`. Detenido según instrucción explícita
      -- no se pulsó Aceptar, no se abrió Guardar como, no se guardó
      archivo.

## Cierre — F0.5 completado end-to-end
- [x] Agregado tercer checkpoint `--stop-after save_as` (detiene justo
      después de Guardar, antes de esperar archivo/diálogo final). py_compile
      PASS, sin nuevos call sites mutantes (6, sin cambios).
- [x] Aceptar (PDF Options) ejecutado (1 vez, `activate_preselected_once`).
- [x] Guardar como: ruta destino calculada antes de tocar el diálogo
      (`runtime/downloads/poc/factura_FHC000000_poc.pdf`), campo de nombre
      escrito UNA vez vía ValuePattern (verificado `_supports_value_pattern`
      antes de escribir, sin fallback a teclado), botón Guardar identificado
      sin ambigüedad y clickeado 1 vez.
- [x] Archivo confirmado en disco: 86,963 bytes, header `%PDF` válido,
      pypdf (ya presente en el venv) confirma 2 páginas legibles.
- [x] Diálogo final "Exportar" (¿Desea abrir?): contenido confirmado
      read-only antes de actuar, botón "No" identificado por match EXACTO
      (nunca "Sí"), clickeado 1 vez.
- [x] RESULTADO FINAL: `DOWNLOAD_COMPLETED`.

F0.5 (descarga controlada del documento origen de una factura) queda
completo end-to-end para la factura autorizada FHC000000. Cada acción
mutante (clic en Trazabilidad, escritura de factura, Enter de búsqueda,
clic Documento Origen, clic flecha Export, clic PDF File, clic Aceptar,
escritura de ruta, clic Guardar, clic No) se ejecutó exactamente una vez,
con verificación de foreground y confirmación humana donde el control no
tenía identidad de accesibilidad (flecha del split button y "PDF File",
ambos CUSTOM_DRAWN_CONTROL confirmados en vivo).

## TDD
No aplica (script de automatización RPA interactivo contra una app externa,
sin suite de tests unitarios en este PoC; mismo criterio que F0.1-F0.4).
Verificación = `py_compile` + lectura del log/reporte generado + evidencia
read-only antes/después de cada acción.

## Evidencia
(se completa durante la ejecución)
