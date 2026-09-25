# RPA Clínica Tuluá — Inicio del proyecto

## Objetivo inmediato

Construir un MVP local en Python que permita comprobar, antes de cerrar el proyecto comercial con la clínica, que es técnicamente viable automatizar el sistema GO/Índigo desde Windows.

El MVP NO debe intentar automatizar todavía todo el proceso clínico. Su objetivo es probar de forma controlada:

1. Detectar la ventana principal de GO/Índigo.
2. Identificar controles accesibles mediante Windows UI Automation.
3. Navegar hasta el módulo de trazabilidad o facturación.
4. Escribir un número de factura de prueba.
5. Ejecutar la búsqueda.
6. Abrir el documento origen.
7. Accionar la opción de impresión/guardado.
8. Guardar un PDF en una carpeta temporal.
9. Confirmar que el archivo fue creado.
10. Registrar en log cada paso.

## Decisión tecnológica

Tecnología principal:

- Python 3.12+
- pywinauto
- uiautomation o pywinauto backend="uia"
- pywin32
- psutil
- pandas
- openpyxl
- pydantic
- loguru
- python-dotenv
- pypdf
- PySide6 o CustomTkinter para la interfaz, después del PoC
- pytest

No usar PyAutoGUI como mecanismo principal. Puede utilizarse únicamente como fallback temporal y documentado para pruebas, nunca como base del producto.

## Restricción crítica

La automatización debe trabajar únicamente con funciones y permisos que el usuario ya tenga autorizados en GO/Índigo.

Si aparece:

- CAPTCHA
- bloqueo anti-automatización
- restricción explícita de seguridad
- control de acceso no autorizado
- MFA que requiera intervención humana
- mensaje que indique que el uso automatizado no está permitido

el robot debe detenerse, registrar la situación y solicitar intervención humana.

No implementar mecanismos para evadir o saltarse controles de seguridad.

## Resultado esperado de la fase 0

Debemos terminar esta fase con una de estas conclusiones:

### A. VIABLE CON PYTHON
GO/Índigo expone suficientes controles mediante UI Automation y se puede automatizar con estabilidad.

### B. VIABLE HÍBRIDO
Algunas partes se pueden automatizar con Python y otras requerirían un motor RPA como UiPath.

### C. NO VIABLE SIN API O SOPORTE DEL PROVEEDOR
La aplicación no expone controles adecuados o bloquea legítimamente la automatización.

## Orden de trabajo

Claude debe leer, en este orden:

1. `01_PROJECT_CONTEXT.md`
2. `02_ARCHITECTURE.md`
3. `03_CLAUDE_RULES.md`
4. `04_PHASE_0_POC.md`
5. `05_ACCEPTANCE_CRITERIA.md`
6. `06_MASTER_PROMPT.md`

Luego debe iniciar el desarrollo siguiendo `06_MASTER_PROMPT.md`.
