# Datos sintéticos

Ningún dato de esta demostración proviene de una organización real. La empresa, sus productos, proveedores,
clientes, personas, documentos, cifras y series fueron inventados para el escenario y se generan con un programa a
partir de una semilla. No se cargó ni se transformó información de ninguna empresa.

## Cómo se genera el escenario

`manage.py reiniciar_feria` (o `REINICIAR_FERIA.ps1`) borra la base local y construye el escenario desde cero.

- Semilla y fecha de referencia. La semilla por defecto es 4711 y la fecha de referencia es el día en que se
  ejecuta; `--semilla` y `--fecha AAAA-MM-DD` las fijan. Con la misma semilla y la misma fecha se obtienen los
  mismos datos, incluidos los identificadores.
- Simulación con los servicios reales de la plataforma. El generador recorre día a día unos 24 meses hasta la fecha
  de referencia con el reloj controlado, y cada operación (pedir, registrar y validar una factura, recibir por
  lotes, vender, emitir en el simulador contable, importar un extracto, aplicar un ingreso, pagar, dar de baja) se
  hace llamando a los mismos servicios que usan las pantallas. No se escriben saldos a mano. Por eso documentos,
  movimientos, saldos, auditoría y gráficos cuadran entre sí.
- Datos maestros. Solo lo que no tiene servicio propio (equipo, catálogo, listas de precios, cuenta bancaria) se
  crea directamente.
- Agentes. En los últimos cuatro meses simulados los agentes corren en su turno: el pronóstico semanal queda
  guardado el lunes, antes de conocer la semana, y los recordatorios de cobro se registran como envíos simulados y
  se evalúan al cumplirse su ventana.
- Claves. Las claves de las cuentas de demostración se generan al final, fuera de la simulación, y se escriben en
  `datos/ACCESOS_DEMO.md`. Cambian en cada reinicio.

## Qué contiene

Catálogo: 32 referencias en cuatro categorías (mantenimiento, adhesivos, sellantes y recubrimientos, 8 cada una),
con códigos internos `MNT-`, `ADH-`, `SEL-` y `REC-`, unidad y empaque de compra, vida útil al recibir, código de
barras ficticio y, en 12 de ellas, almacenamiento controlado. Las descripciones son genéricas, sin marcas
comerciales.

Patrones de salida semanal, uno por referencia, para que los pronósticos enfrenten casos distintos:

| Patrón | Referencias |
|---|---|
| Estable (alto, medio o bajo) | 15 |
| Estacional (dos temporadas de lluvias o fin de año) | 4 |
| Con periodos sin existencias por entregas retrasadas | 3 |
| Intermitente (muchas semanas en cero y pedidos por obra) | 3 |
| Con pedidos extraordinarios | 2 |
| Tendencia creciente | 1 |
| Tendencia decreciente | 1 |
| Sin rotación reciente | 1 |
| Lanzada hace pocas semanas | 1 |
| Recién creada, sin movimientos | 1 |

Actores:

- 6 proveedores con plazos de 30, 45 o 60 días, tramos de pronto pago distintos y tiempos de entrega de 4 a 14
  días.
- 19 clientes, entre empresas y personas, con comportamientos de pago distintos (puntual, con retraso, con mucho
  retraso, de contado y nuevo) y tres listas de precios (mostrador, empresa y contratista).
- 8 cuentas de demostración: Dirección, Administración, Inventario, Recepción, Contabilidad, Facturación y dos
  solicitantes de áreas internas.

Convenciones para que nada se confunda con datos reales: NIT en el rango 900.555.0xx, cédulas en 1.000.555.xxx,
correos en dominios `.example`, teléfonos 300 555 01xx, banco «Banco Ejemplo» y códigos de barras con prefijo de
uso interno.

Volumen del escenario generado con fecha 18/11/2026 y semilla 4711 (cambia con otra fecha o semilla):

| Elemento | Cantidad |
|---|---|
| Filas en total | 69.364 |
| Ventas emitidas | 1.158 |
| Pedidos a proveedores | 439 |
| Facturas de proveedor | 430 |
| Recepciones | 457 |
| Lotes | 672 |
| Movimientos de inventario | 8.909 |
| Pagos a proveedores | 396 |
| Extractos importados | 39 |
| Movimientos bancarios | 1.312 |
| Turnos de agentes | 520 |
| Registros de auditoría | 11.039 |

## Casos preparados

En la fecha de referencia quedan listos los casos que usa el recorrido. Nacen de operaciones normales hechas el día
adecuado de la simulación; ningún estado se fuerza. La plataforma guarda dónde está cada uno para que el recorrido
los abra directamente.

| Caso | Qué queda listo |
|---|---|
| Faltante próximo | Una referencia de alta rotación bajo su punto de pedido y sin pedidos en camino, con propuesta del agente de reposición para aprobar. |
| Vencimientos | Un lote que vence en 30 días o menos con unidades en riesgo, y un lote vencido con saldo y propuesta de baja. |
| Recepción | Una factura validada por recibir, con códigos de prueba para sus cajas, y otra con una línea recibida a medias. |
| Pronto pago | Una factura cuyo descuento vence en 2 a 4 días, con propuesta del agente. |
| Banco | Un ingreso con sugerencia clara (menciona la factura), uno ambiguo (el valor coincide con el saldo de dos clientes y no trae referencia) y uno sin coincidencias. |
| Cartera vencida | Tres clientes en tramos de mora de unos 15, 45 y más de 90 días, con propuestas de cobranza. |
| Sin historial | Una referencia lanzada hace unas cuatro semanas y otra recién creada. |
| Sin rotación | Una referencia con existencias y sin salidas recientes. |
| Solicitudes | Una pendiente con propuesta de reserva, una sin existencias suficientes y una reserva aprobada por entregar. |
| Descuentos | Una solicitud de descuento en espera de Dirección, con el cupo mensual de Administración agotado. |
| Ventas | Un borrador listo para emitir y una venta que factura una entrega previa sin duplicar la salida. |
| Pedidos | Uno enviado y en tránsito, y otro ya facturado y parcialmente recibido. |

Los casos se preparan para la fecha de referencia. Si la demostración se abre otro día, los plazos se corren (un
descuento que vencía en tres días puede estar vencido) y `verificar_escenario` lo advierte. Por eso conviene
reiniciar el mismo día de la exposición.

Aprobar una propuesta o confirmar una recepción consume ese caso. Para volver al punto de partida el mismo día se
restaura la copia inicial, que tarda segundos (ver «Copia inicial y restauración rápida»); reiniciar solo hace falta
al cambiar de día.

## Documentos y códigos de prueba

Cada reinicio deja en `documentos_demo/` (también disponibles en la aplicación, en la página Documentos de
demostración, que se abre desde el paso 4 del recorrido):

| Archivo | Para qué sirve | Dónde se carga |
|---|---|---|
| Dos facturas de proveedor en PDF y dos en XML | Corresponden a pedidos abiertos del escenario; al registrarlas, el pedido se cierra solo. | Compras → Facturas de proveedores → Cargar factura |
| Un extracto bancario en CSV | Cinco movimientos que aún no están importados. | Finanzas → Banco → Importar extracto |
| `codigos_recepcion.html` | Hoja imprimible con los códigos de las cajas de la factura por recibir, en barras y en texto. | Operaciones → Recibir mercancía |
| `codigos_recepcion.txt` | Los mismos códigos, uno por línea, para pegar o teclear sin cámara. | Operaciones → Recibir mercancía |

Las facturas usan un formato propio, la «Factura de demostración», que es el único que la plataforma sabe leer en
PDF; el XML es un documento de factura mínimo con la misma información. Todas llevan la leyenda «Documento de
demostración sin validez fiscal». La lectura no registra nada por sí sola: la persona revisa los campos y confirma.

Los nombres de los archivos incluyen el número de la factura y la fecha, así que cambian con cada escenario.

## Reinicio seguro

El reinicio solo puede borrar la base de la demostración:

- Solo actúa sobre SQLite, sobre el archivo `feria.sqlite3` que está directamente en la carpeta de datos, y solo si
  esa carpeta es `datos/` del proyecto o la indicada en `FERIA_PRIVATE_DIR`. Si la configuración apunta a otra cosa,
  no hace nada y explica por qué.
- Borra la base, `datos/adjuntos/` y la copia inicial del escenario anterior, vuelve a crear las tablas, genera el
  escenario y guarda la copia inicial nueva. No toca el código, el entorno ni otros archivos.
- Pide confirmación escrita salvo que se use `--si`.
- Si el servidor está encendido, la base está en uso: el reinicio lo avisa y no cambia nada.

Para ensayar sin tocar la base de la demostración se puede generar un escenario aparte en una carpeta temporal:

```
$env:FERIA_PRIVATE_DIR = "$env:TEMP\ensayo_feria"
.venv\Scripts\python.exe manage.py reiniciar_feria --si --meses 5
.venv\Scripts\python.exe manage.py verificar_escenario
Remove-Item Env:FERIA_PRIVATE_DIR
```

`--meses` acepta entre 5 y 36; con 5 la generación tarda cerca de un minuto. En ese modo los documentos de prueba
quedan dentro de la misma carpeta temporal.

## Copia inicial y restauración rápida

Al terminar, cada reinicio guarda una copia íntegra del escenario recién generado:

| Archivo | Contenido |
|---|---|
| `datos/escenario_inicial.sqlite3` | Copia de la base tal como quedó al generar el escenario, con las cuentas y sus claves de ese reinicio. |
| `datos/escenario_inicial.json` | Ficha de la copia: huella de los datos, número de filas, momento en que se guardó, fecha de referencia, semilla y meses simulados. |

`manage.py restaurar_feria` (o `RESTAURAR_FERIA.ps1`) devuelve la base a esa copia sin generar nada de nuevo. En un
ensayo con un escenario de 5 meses y el servidor encendido, la restauración tomó menos de un segundo y el comando
completo, cerca de cuatro.

Qué hace, en orden:

1. Guardas de seguridad. Usa las mismas del reinicio: solo actúa sobre `feria.sqlite3` dentro de la carpeta de
   datos. Si falta la copia, o su ficha no se puede leer o está incompleta, se detiene con un mensaje y no cambia
   nada. Pide confirmación escrita salvo que se use `--si`.
2. Restauración. No reemplaza el archivo de la base: copia sobre ella el contenido de la copia inicial con el
   mecanismo de respaldo de SQLite. Por eso funciona con el servidor encendido, que ve los datos restaurados en su
   siguiente consulta.
3. Limpieza de adjuntos. Borra de `datos/adjuntos/` los archivos creados después de la copia que la base restaurada
   ya no referencia (por ejemplo, el soporte de una factura cargada durante la exposición). No toca los adjuntos
   del escenario ni ninguna otra carpeta.
4. Comprobación de huella. Vuelve a calcular la huella de la base y la compara con la de la ficha. Si no coincide,
   termina con error e indica generar el escenario de nuevo.
5. Aviso de fecha. Si la copia se preparó para una fecha distinta de hoy, lo advierte: los casos dependen de la
   fecha y corresponde reiniciar.

Consecuencias que conviene conocer:

- Se descarta todo lo hecho desde el último reinicio, incluidas las preguntas al copiloto y las decisiones sobre
  propuestas.
- Las sesiones abiertas se cierran, porque viven en la base. Se vuelve a entrar con un clic en el perfil o con las
  claves de `datos/ACCESOS_DEMO.md`, que siguen siendo las del último reinicio.
- Los archivos de `documentos_demo/` no cambian.
- Es una operación de consola: la aplicación no ofrece un botón para restaurar.

`manage.py restaurar_feria --guardar-copia` crea la copia inicial a partir de la base actual. Sirve para una base
generada antes de que existiera la copia, y solo procede si la huella de la base sigue siendo la de su generación,
es decir, si no se ha operado desde entonces. Si la base ya cambió, se niega e indica reiniciar.

## Verificación de coherencia

`manage.py verificar_escenario` revisa la base cargada y termina con código 1 si algo falla. Comprueba, entre otras
cosas, que:

- el físico de cada lote sea la suma de sus movimientos, lo reservado sea la suma de sus reservas activas y ningún
  saldo sea negativo;
- lo recibido de cada línea de factura sea la suma de sus entradas, y los pedidos abiertos y cerrados sean
  coherentes con sus facturas;
- el saldo de cada factura de proveedor sea total menos pagos, descuentos y notas crédito, más o menos ajustes, y
  cada pago conciliado tenga su egreso en el extracto;
- el saldo de cada cuenta por cobrar sea valor menos aplicaciones vigentes y ajustes, y coincida con el documento
  del simulador contable;
- en cada ingreso bancario lo aplicado no supere su valor;
- la simulación haya terminado sin operaciones rechazadas y todos los turnos de agentes hayan terminado bien;
- el catálogo y los patrones de salida sean los descritos, y cada caso preparado exista en el estado esperado.

Sobre el escenario del 18/11/2026 con semilla 4711, las 54 comprobaciones cuadran.

## Reproducibilidad

`manage.py verificar_escenario --reproducibilidad` genera el escenario dos veces en carpetas temporales, con la
misma semilla y fecha, y compara el número de filas, la suma de cada columna numérica de cada tabla y una huella de
todos los valores. Con el número completo de meses compara también la base cargada contra una generación nueva.

Quedan por fuera de la comparación los pocos valores que cambian por diseño entre dos instalaciones: las claves de
las cuentas, las huellas que dependen del secreto local de cada carpeta de datos y los pesos en coma flotante del
modelo de pagos.

Esta comprobación tarda varios minutos con 24 meses. Para una versión corta: `--reproducibilidad --meses 5`.
