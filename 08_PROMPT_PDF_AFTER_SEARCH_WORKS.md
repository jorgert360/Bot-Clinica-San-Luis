# PROMPT 3 — Apertura y descarga PDF

Usar solamente cuando:

`poc_invoice_search.py`

ya encuentre correctamente una factura.

---

La búsqueda de factura ya funciona correctamente.

Ahora realiza únicamente F0.6 y F0.7.

Objetivo:

1. abrir el documento origen correspondiente al resultado encontrado
2. validar que el documento correcto está visible
3. activar el flujo de imprimir / guardar como PDF
4. guardar el archivo en:

`runtime/downloads/poc/`

Nombre:

`poc_<numero_factura>_<timestamp>.pdf`

5. verificar:
   - existe
   - tamaño > 0
   - terminó de escribirse

6. registrar duración de:
   - búsqueda
   - apertura
   - impresión
   - guardado

7. crear:

`runtime/logs/poc_report.json`

con:

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

Si alguna propiedad no es verdadera, registrar el valor correcto.

No falsear el resultado.

No usar coordenadas de pantalla como primera alternativa.

Si el diálogo de impresión pertenece a Windows, automatizarlo mediante UI Automation.

Si aparece una restricción de seguridad o automatización, detenerse y registrar:

`SECURITY_OR_AUTHORIZATION_BLOCK`

Al finalizar la prueba, mostrarme:

- archivos creados
- resultado
- tiempos
- selectores utilizados
- fallbacks utilizados
- riesgos detectados

Después:

DETENTE.

Todavía no desarrolles el procesamiento masivo.
