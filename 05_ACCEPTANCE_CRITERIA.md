# Criterios de aceptación

## PoC técnico

El PoC se considera aprobado si se cumplen todos los siguientes criterios:

### Aplicación

- GO/Índigo es detectado automáticamente.
- No se depende de un PID fijo.
- No se depende de una posición fija de pantalla.

### Controles

- Al menos el flujo crítico puede manejarse mediante controles identificables.
- Los selectores se encuentran centralizados.
- Un cambio de resolución de pantalla no rompe el flujo principal.

### Factura

- Se puede introducir un número de factura.
- Se puede ejecutar la búsqueda.
- Se puede identificar un resultado correcto.
- No se selecciona automáticamente un resultado ambiguo.

### Documento

- Se puede abrir el documento origen.
- El robot puede reconocer que el documento está listo.

### PDF

- Se puede iniciar la impresión/guardado.
- Se genera un archivo PDF.
- El archivo se valida.
- El archivo no queda vacío.

### Robustez

- No se utiliza `time.sleep()` como sincronización principal.
- Los timeouts son configurables.
- Existe retry controlado.
- Existe log.
- Los errores tienen código.

### Seguridad

- No existen credenciales en código.
- No existen PDFs reales en Git.
- No se intenta evadir restricciones.
- El proceso se detiene si aparece un bloqueo de seguridad.

## MVP comercial

Después del PoC técnico, el MVP que puede mostrarse a la clínica debe permitir:

1. seleccionar un Excel
2. mostrar registros encontrados
3. seleccionar una sola factura
4. pulsar Ejecutar
5. buscarla en GO
6. descargar factura
7. guardarla localmente
8. mostrar resultado
9. registrar log

Todavía no es obligatorio que el MVP comercial:

- procese 165 facturas
- tipifique todas las EPS
- procese SOAT
- unifique todos los PDFs
- procese todas las unidades funcionales

## Criterio para firmar el proyecto completo

Antes de cerrar el contrato de COP $10.000.000 debe existir evidencia de que:

- GO es automatizable
- la búsqueda funciona
- el guardado de PDF funciona
- no existe un bloqueo técnico evidente
- se conoce cualquier intervención humana requerida
- se conocen los puntos frágiles

Con esto se reduce significativamente el riesgo técnico del proyecto.
