# Reglas obligatorias para Claude Code

## Regla 1 — No asumir

Nunca inventar:

- nombres de botones
- automation_id
- class_name
- rutas del sistema
- títulos de ventanas
- tiempos de carga
- estructura de controles

Primero inspeccionar el sistema.

## Regla 2 — Fase por fase

No desarrollar todo el robot de una vez.

Trabajar en este orden:

```text
F0.1 detectar proceso/ventana
F0.2 inspeccionar árbol UI
F0.3 conectar con ventana
F0.4 localizar módulo
F0.5 escribir factura
F0.6 ejecutar búsqueda
F0.7 abrir documento
F0.8 activar impresión/guardado
F0.9 verificar PDF
F0.10 generar informe PoC
```

No avanzar a la siguiente fase si la anterior no fue validada.

## Regla 3 — No bypass de seguridad

No intentar evadir:

- CAPTCHA
- MFA
- bloqueo anti-bot
- licenciamiento
- protección del proveedor
- permisos
- controles de acceso

Si aparece alguno, detener el flujo y registrar:

`SECURITY_OR_AUTHORIZATION_BLOCK`

## Regla 4 — Aplicación autorizada solamente

Interactuar exclusivamente con la sesión y permisos proporcionados por la clínica.

No modificar bases de datos internas de GO.

No inspeccionar memoria del proceso.

No hacer ingeniería inversa del binario.

No interceptar tráfico para extraer credenciales o tokens.

## Regla 5 — UI Automation antes que coordenadas

Orden:

1. pywinauto backend UIA
2. pywinauto backend win32
3. UI Automation alternativo
4. interacción por teclado accesible
5. coordenadas solo como diagnóstico temporal

Si se usa coordenada, marcarla:

`TEMPORARY_FALLBACK`

## Regla 6 — Logs obligatorios

Cada acción debe registrar:

- timestamp
- etapa
- ventana
- acción
- resultado
- duración
- error si existe

No registrar:

- contraseñas
- tokens
- contenido clínico
- documentos completos
- información sensible innecesaria

## Regla 7 — Screenshots

En caso de error de UI se puede guardar screenshot local.

No subir automáticamente screenshots a servicios externos.

## Regla 8 — Código mantenible

Requisitos:

- type hints
- docstrings breves
- funciones pequeñas
- selectores centralizados
- excepciones propias
- logging estructurado
- configuración externa
- testabilidad

## Regla 9 — No hardcodear tipificación

Las reglas de EPS/SOAT no deben quedar dispersas en condicionales.

Posteriormente deben venir de un motor configurable.

## Regla 10 — Git

Nunca incluir:

```text
.env
runtime/
downloads/
screenshots/
logs/
*.pdf
*.xlsx reales
*.csv reales
credentials*
```

## Regla 11 — Antes de modificar

Claude debe:

1. explicar qué archivo va a crear o modificar
2. indicar el objetivo
3. realizar el cambio
4. ejecutar las pruebas pertinentes
5. mostrar resultado
6. esperar validación cuando la fase dependa de GO real

## Regla 12 — No abrir procesos arbitrariamente

No cerrar ni matar procesos ajenos al proyecto.

Solo se puede interactuar con GO y los procesos explícitamente relacionados con la prueba.

## Regla 13 — Idempotencia

Una prueba repetida no debe:

- duplicar archivos sin control
- sobrescribir documentos existentes silenciosamente
- dejar ventanas bloqueadas
- dejar procesos zombies

## Regla 14 — Criterio de parada

Ante comportamiento no entendido:

STOP.

Registrar evidencia.

No improvisar clics.
