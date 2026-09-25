# PROMPT 2 — Continuar después de inspeccionar GO

Usa este prompt SOLO después de ejecutar:

```bash
python scripts/list_windows.py
python scripts/inspect_go.py
```

y confirmar que el árbol UI de GO/Índigo es visible.

---

Ya ejecuté la fase F0.1 y F0.2.

A continuación voy a proporcionarte:

1. salida de `list_windows.py`
2. fragmentos relevantes de `go_ui_tree.txt`
3. nombres / automation_id / control_type detectados
4. screenshot si hace falta

Tu siguiente tarea es realizar ÚNICAMENTE F0.3, F0.4 y F0.5.

Objetivo:

- crear `go_client.py`
- conectarse de forma robusta a GO
- navegar al módulo de Trazabilidad de factura
- identificar el campo donde se escribe la factura
- introducir UNA factura de prueba
- ejecutar búsqueda
- verificar que el resultado aparece
- NO abrir ni descargar documentos todavía

Reglas:

- no inventar selectores
- usar solamente los identificadores encontrados en la inspección
- centralizar selectores
- no usar coordenadas salvo diagnóstico explícito
- no continuar si hay más de un resultado ambiguo
- guardar screenshot local del resultado
- generar logs
- nunca registrar contraseña
- si GO bloquea la acción, detenerse
- no intentar evitar ningún mecanismo de seguridad

Crear:

`scripts/poc_invoice_search.py`

Uso:

```bash
python scripts/poc_invoice_search.py --invoice <FACTURA_AUTORIZADA_DE_PRUEBA>
```

Al terminar:

DETENTE.

No implementes descarga de PDF hasta que yo confirme que la búsqueda funciona correctamente.
