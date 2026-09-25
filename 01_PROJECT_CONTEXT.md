# Contexto del proyecto

## Cliente

Clínica ubicada en Tuluá, Colombia.

## Problema actual

El área de facturación/radicación procesa diariamente un listado de facturas. Para cada registro debe ingresar al sistema GO/Índigo, buscar la factura o ingreso correspondiente, descargar documentos, historias clínicas y soportes, organizarlos, renombrarlos y en algunos casos unificarlos en PDF.

El proceso actualmente puede alcanzar aproximadamente 160-165 facturas por día, dependiendo del volumen de facturación.

## Insumos

El proceso utiliza al menos dos insumos principales.

### 1. Listado diario

Ejemplo:

`LISTADO ENVIADO 14-09-2026.xlsx`

Contiene datos como:

- número de factura
- número de ingreso
- documento del paciente
- entidad
- NIT
- unidad funcional
- fecha
- otros campos relacionados con la factura

Este archivo será el disparador principal del robot.

### 2. Tabla de tipificación

Ejemplo:

`TIPIFICACION DE ENTIDADES (1) 1(TIPIFICACIÓN).csv`

Contiene reglas diferentes según entidad, EPS, SOAT y tipo de atención.

La tipificación define:

- nombre de carpeta
- nombre de factura
- nombre de historia clínica
- nombre de constancia
- nombre de orden médica
- nombre de resultados
- documentos que deben unificarse
- documentos que deben quedar separados

El motor final NO debe tener estas reglas quemadas en código.

Las reglas deben provenir de una estructura configurable.

## Sistema principal

Aplicación:

GO / Índigo

Es una aplicación de escritorio Windows instalada localmente.

Se han identificado módulos similares a:

- Vie Finance
- Vie Clinical
- Vie RCM / Revenue Cycle Management
- Facturación Salud
- Liquidación
- Trazabilidad de factura
- Consulta de historias

## Ruta candidata más simple

Para facturas se considera preferible iniciar por:

Vie Finance → Trazabilidad de factura

porque permite buscar directamente por número de factura.

El número de factura ya existe en el Excel diario.

## Flujo funcional futuro

El producto final debería:

1. Leer el Excel.
2. Validar registros.
3. Identificar entidad y unidad funcional.
4. Consultar las reglas de tipificación.
5. Buscar cada factura en GO.
6. Descargar la factura.
7. Descargar los soportes correspondientes.
8. Descargar historia clínica cuando aplique.
9. Descargar resultados/laboratorios cuando aplique.
10. Clasificar documentos.
11. Unificar PDFs según reglas.
12. Renombrar archivos.
13. Crear estructura de carpetas.
14. Registrar éxito o error.
15. Continuar con la siguiente factura.
16. Permitir reanudar procesos interrumpidos.

## Primera fase

NO desarrollar todavía todo el flujo.

Primero debemos demostrar que Python puede controlar GO/Índigo.

La primera prueba debe procesar solamente una factura autorizada de prueba.

## Seguridad y privacidad

El proyecto trabaja con información clínica.

Principios:

- procesamiento local
- no enviar historias clínicas a servicios externos
- no utilizar APIs de IA en nube para procesar contenido de pacientes
- no almacenar contraseñas en texto plano
- no incluir credenciales en Git
- no subir PDFs clínicos al repositorio
- logs sin información clínica sensible innecesaria
- utilizar datos de prueba cuando sea posible

## Modelo comercial

Presupuesto objetivo del proyecto completo:

COP $10.000.000

El desarrollo debe priorizar:

- MVP funcional
- estabilidad
- mantenimiento sencillo
- reutilización
- mínimo costo recurrente
- sin licencias obligatorias de RPA por equipo, salvo que la prueba técnica demuestre que son necesarias
