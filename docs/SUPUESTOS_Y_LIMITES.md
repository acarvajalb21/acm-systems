# Supuestos y límites

Este documento reúne lo que la demostración da por sentado, lo que simula y lo que no se ha verificado. Conviene
leerlo antes de responder preguntas sobre resultados.

## Alcance general

- Es una demostración sobre un caso empresarial con datos sintéticos. No hay datos de ninguna organización real.
- Los resultados de los modelos demuestran funcionamiento y metodología. No prueban eficacia, ahorro ni reducción de
  faltantes en una empresa real.
- La aplicación corre en un solo equipo, con una base SQLite local y por HTTP en la dirección local. No tiene modo
  de despliegue ni se ha usado con varias personas a la vez.
- Las cifras citadas en la documentación corresponden al escenario generado con fecha 18/11/2026 y semilla 4711.
  Cambian al regenerar con otra fecha o semilla y al operar la demostración.

## Supuestos de negocio

- Una sola empresa y, en la práctica, una sola bodega con dos ubicaciones. No hay traslados entre bodegas.
- Moneda única (pesos colombianos) e IVA del 19 % en el escenario.
- El inventario se maneja por lote con fecha de vencimiento; las salidas siguen «primero vence, primero sale» y un
  lote vencido no se despacha.
- Cada referencia tiene un proveedor habitual (el de su última factura) y se compra en múltiplos de un empaque.
- Las condiciones de pronto pago son las de la factura y quedan congeladas al validarla.
- Las ventas se registran a precio de lista; un descuento requiere cupo de Administración o confirmación de
  Dirección.
- Los resultados económicos de la pantalla Resultados son una lectura de gestión con lo que registra la
  plataforma. No son un estado de resultados contable: no incluyen nómina, arriendo, servicios ni impuestos, y el
  costo de lo vendido es el costo de compra de cada lote.
- Las decisiones las toman personas. Los agentes proponen y la plataforma vuelve a comprobar al aprobar.

## Supuestos de los modelos

Pronóstico y reposición:

- El futuro cercano se parece al pasado reciente de cada referencia. No se usan variables externas (precios,
  promociones, clima, obras anunciadas).
- Las salidas registradas se toman como consumo observado, no como demanda. La demanda no atendida solo se conoce
  cuando alguien registró y rechazó una solicitud; las ventas perdidas que nadie registró no existen para el modelo.
- Una semana que terminó sin existencias y sin salidas se excluye. Una semana con existencias parciales no se
  corrige, aunque también pudo perder ventas.
- La política es de revisión periódica con punto de pedido y nivel objetivo. Parámetros por defecto: nivel de
  servicio del 95 %, revisión cada 7 días, ciclo de pedido de 14 días, margen de 30 días antes del vencimiento.
  Ninguno fue calibrado con costos; son valores de partida editables.
- No se modelan costos de mantener inventario, de pedir ni de faltante. La sugerencia busca un nivel de servicio,
  no un costo mínimo.
- El tiempo de entrega es la mediana observada por proveedor. No se modela su variabilidad dentro del inventario de
  seguridad más allá de lo que ya está en los errores medidos.
- El presupuesto se reparte con una regla de prioridad por urgencia, no con una optimización.
- El «nivel de servicio» es el cuantil usado para la seguridad. No es una garantía de disponibilidad.
- El intervalo del 80 % es empírico y su cobertura medida en prueba queda por debajo del 80 % en este escenario. Con
  entre 6 y 8 orígenes de prueba por referencia, la cobertura por referencia es poco informativa.
- Los errores de orígenes consecutivos a varias semanas se solapan; los `n` son conteos de orígenes.
- Los hiperparámetros del modelo entrenado son fijos. No hubo búsqueda de hiperparámetros.

Identificación de pagos:

- El modelo aprende solo de identificaciones confirmadas en la plataforma. Si una persona identifica mal un ingreso
  y no lo corrige, el modelo aprende ese error.
- El puntaje no está calibrado y no es una probabilidad.
- En el escenario sintético casi todos los ingresos traen titular o mención de factura, así que la evaluación no
  permite distinguir el aporte del modelo del de las reglas.

Cobranza:

- La ventana de evaluación es de 10 días y el resultado es «hubo pago, abono o no hubo pago en la ventana».
- No hay grupo de control. La tasa de referencia de cuotas sin recordatorio no lo sustituye.
- En el escenario, el generador adelanta algunos pagos tras un recordatorio con una probabilidad fija. Cualquier
  asociación entre recordatorio y pago que aparezca en pantalla es una propiedad del escenario.

Sobre el escenario como banco de prueba: las series se generaron con patrones conocidos y ruido controlado. Eso
favorece a los métodos sencillos y no dice cómo se comportarían los mismos métodos con datos reales, que tienen
errores de registro, cambios de catálogo y eventos que aquí no existen.

## Lo que es simulado

| Elemento | Cómo funciona en la demostración |
|---|---|
| Sistema contable | Simulador local. Sus facturas y recibos llevan prefijo «SIM» y la leyenda «Documento de demostración sin validez fiscal». |
| Banco | Archivos de extracto generados por el escenario e importados a mano. No hay conexión bancaria ni órdenes de pago. |
| Correo y mensajería | Cada envío es un evento «envío simulado registrado». Nada sale del equipo. |
| Pedido al proveedor | Se prepara y su envío se registra como simulado. |
| Facturas de proveedor | Documentos sintéticos en un formato propio de demostración, sin validez fiscal. |
| Lectura de códigos | Códigos de prueba con prefijo de uso interno, generados por el escenario. |
| Personas y empresas | Nombres, identificaciones, correos y teléfonos inventados, con rangos y dominios reservados. |
| Historia de 24 meses | Simulada día a día con reloj controlado; no ocurrió. |
| Decisiones pasadas | Las aprobaciones del historial anterior a la fecha de referencia las hizo el generador con las cuentas de demostración. |

## Lo no verificado

- Proveedor generativo del copiloto en vivo. Está implementado y sus pruebas usan un transporte simulado. No se ha
  hecho ninguna llamada real: no hay credenciales en el equipo de preparación. No se conocen su latencia, su costo
  ni la calidad de su redacción.
- Otros navegadores. Las pantallas se revisaron en un navegador basado en Chromium. No se verificaron Firefox,
  Safari ni navegadores de celular.
- Uso desde otro dispositivo de la red. La configuración lo permite (`FERIA_HOST` y `FERIA_HOSTS`), pero no se
  probó desde un celular real. Por HTTP fuera del propio equipo, los navegadores no permiten instalar la aplicación
  ni usar la cámara, y el selector de perfiles no aparece.
- Lectura con cámara y con lector de mano en condiciones reales. El recorrido usa códigos pegados o tecleados.
- Funcionamiento sin conexión de principio a fin. La aplicación no carga recursos externos y el modo por defecto
  del copiloto no usa la red; aun así, conviene hacer un ensayo completo sin conexión el día de la exposición.
- Carga y concurrencia. No se midió el comportamiento con varias personas a la vez ni con catálogos mayores que el
  del escenario.
- Otros sistemas operativos. Los guiones son de PowerShell y la demostración se preparó en Windows.
- Seguridad. Hay controles (permisos en servidor, política de contenido, límite de intentos), pero no una revisión
  de seguridad independiente ni pruebas de intrusión.
- Accesibilidad. No hubo una evaluación formal con lectores de pantalla.

## Límites operativos de la exposición

- Los pasos 3, 4 y 5 del recorrido consumen su caso preparado. Entre una exposición y la siguiente se vuelve al
  escenario inicial con `RESTAURAR_FERIA.ps1`, que tarda segundos y no exige detener el servidor.
- La restauración descarta todo lo hecho desde el último reinicio, no solo lo del recorrido, y cierra las sesiones
  abiertas. No hay forma de conservar una parte.
- Es una operación de consola. La aplicación no tiene un botón para restaurar ni para reiniciar.
- La copia inicial corresponde al día en que se generó el escenario. Como los casos dependen de la fecha, cada día
  se empieza con `REINICIAR_FERIA.ps1`, que tarda unos minutos y requiere el servidor detenido.
- La restauración se ensayó con un escenario de 5 meses en una carpeta de datos aparte y con el servidor encendido.
  Su duración con el escenario completo de 24 meses no se midió al preparar esta documentación.

## Confidencialidad

Antes de preparar el paquete se revisó que el código, los datos y la documentación no contuvieran información de
terceros. Esa revisión se hizo con listas de términos y patrones automáticos (nombres, dominios, rutas del equipo,
secretos, enlaces y correos fuera de dominios reservados, metadatos de archivos). Una revisión de ese tipo detecta
lo que está en la lista o coincide con un patrón; no es una garantía absoluta de que no quede ningún rastro.
Quien comparta el paquete debe tenerlo presente y no añadirle archivos sin revisarlos.

`datos/ACCESOS_DEMO.md`, `datos/secret.key`, la base `datos/feria.sqlite3` y su copia inicial
`datos/escenario_inicial.sqlite3` son locales de cada instalación. Las
claves de las cuentas de demostración se regeneran en cada reinicio.
