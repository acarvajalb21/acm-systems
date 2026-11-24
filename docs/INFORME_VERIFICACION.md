# Informe de verificación

Este informe recoge solo comprobaciones que se ejecutaron de verdad sobre esta versión, con su alcance y sus límites.
Las cifras corresponden al escenario generado el 20/11/2026 con la semilla 4711 y cambian al regenerar con otra fecha.

## Funcional

| Comprobación | Resultado |
|---|---|
| `manage.py check` | Sin incidencias |
| `manage.py makemigrations --check --dry-run` | Sin cambios pendientes |
| `manage.py test` (suite completa, base de pruebas aislada) | 289 pruebas, todas correctas; 2 pruebas lentas se omiten salvo que se pidan con `FERIA_PRUEBAS_LENTAS=1` |
| `manage.py verificar_escenario` sobre el escenario de 24 meses | Las 54 comprobaciones cuadran (inventario, compras, cuentas por pagar, cartera, banco, generación, patrones y casos preparados) |
| Reproducibilidad (`verificar_escenario --reproducibilidad`, 24 meses) | Dos generaciones independientes con la misma semilla y fecha producen la misma huella de conteos y sumas por tabla |
| Restauración del escenario inicial tras consumir casos | Devuelve exactamente la huella inicial; medida entre 1,6 y 2,1 segundos con el servidor encendido |
| Instalación desde el paquete en una carpeta nueva | Ver «Instalación desde el paquete» |

Las pruebas automáticas cubren, entre otros, estos riesgos:

- Permisos por perfil verificados en el servidor en cada módulo, en el Resumen, el mapa de flujo, el recorrido y las
  herramientas del copiloto.
- Saldos de facturas de proveedor y de cuentas por cobrar, incluidos los redondeos a centavos en las consultas.
- Reservas y disponibilidad, primero vence primero sale, y recepción parcial por lotes.
- Doble envío: repetir una operación con la misma clave no duplica movimientos, pedidos, reservas ni aplicaciones.
- Propuestas de agentes desactualizadas: al aprobar se recalculan o quedan como «ya no aplica» sin tocar nada.
- Conciliación bancaria: importación de extractos, casos claros, ambiguos y sin coincidencias, reversión de una
  aplicación.
- Modelos: ausencia de fuga temporal (alterar el futuro no cambia un pronóstico), separación entre selección y prueba,
  WAPE sin denominador, cobertura del intervalo, respaldo con historial insuficiente, presupuesto de compra, corrección
  del modelo de pagos al revertir, y cobranza que solo aprende de envíos registrados al cumplirse la ventana.
- Aislamiento: la base, la clave y los adjuntos viven en la carpeta `datos/` de esta instalación; el reinicio y la
  restauración se niegan a operar fuera de ella.
- Comunicaciones simuladas: ninguna pantalla genera enlaces de correo, de mensajería ni a sitios externos.

## Visual y de uso

Revisión en un navegador basado en Chromium (panel integrado y Chrome sin interfaz controlado por guion):

- Barrido automático de 711 combinaciones de pantalla, ancho y perfil (237 por cada ancho: 1440, 1024 y 390 px; los ocho
  perfiles de demostración): ningún desbordamiento horizontal de página, ningún error de consola, ninguna petición
  fuera de `127.0.0.1`, ninguna respuesta con error y el indicador «Demostración con datos sintéticos» presente en
  todas.
- Revisión a ojo, captura por captura, de las pantallas principales en los tres anchos (presentación, acceso, Resumen,
  mapa de flujo, pronóstico, reposición, agentes, propuesta, recepción, conciliación, copiloto, lo aprendido,
  metodología, historial, resultados). El resto de las combinaciones solo tiene medidas automáticas.
- Recorrido guiado de siete pasos ejecutado dos veces, con clics y teclado reales. A ritmo de exposición dura
  4 minutos 27 segundos.
- Operaciones ejecutadas en pantalla: importar factura en PDF y en XML con revisión de campos, aprobar un pedido
  sugerido, recibir mercancía con códigos tecleados (incluidos un duplicado y un código desconocido), emitir una venta,
  facturar una entrega previa sin duplicar la salida, importar un extracto, aplicar un ingreso, pagar con pronto pago,
  registrar un abono, exportar a Excel, aprobar y entregar una solicitud, dar de baja un lote vencido, decidir un
  descuento y crear un usuario.
- Estados: formularios con error que conservan lo escrito y dicen cómo continuar, pantallas vacías, confirmaciones,
  acceso restringido con explicación.
- Teclado: enlace de salto, foco visible, menús con Enter y Escape, mapa de flujo con flechas.
- Movimiento reducido (emulado): ninguna animación activa.
- Descargas con contenido correcto: Excel de cuentas por pagar, tres CSV, PDF de factura simulada y documentos de
  demostración.
- Funcionamiento sin recursos externos: comprobado como ausencia total de peticiones fuera del equipo. No se probó
  desconectando físicamente la red.

Las capturas de escritorio y de celular y la grabación del recorrido están en `docs/capturas/`.

## Confidencialidad

La revisión se hizo con una herramienta interna que no forma parte del paquete. Para cada archivo, de texto o
binario, busca:

- una lista de expresiones que identificarían a la organización con la que se trabajó, su sector, sus productos,
  proveedores y personas, construida a partir del material de origen;
- secretos (claves privadas, tokens, contraseñas escritas, claves de configuración);
- rutas absolutas del equipo y nombres de usuario del sistema;
- enlaces externos, enlaces de correo y de mensajería, y correos fuera de dominios reservados;
- metadatos de imágenes y PDF, y el contenido de archivos comprimidos y de bases SQLite (celda por celda).

Resultados de la última pasada:

| Objeto revisado | Resultado |
|---|---|
| Carpeta completa del proyecto (incluye la base generada y los documentos de demostración) | Sin hallazgos |
| Base del escenario de 24 meses, celda por celda | Sin hallazgos |
| Paquete público (`plataforma-operaciones-feria.zip`), entrando en su contenido | Sin hallazgos |

Además se comprobó que el paquete no contiene base de datos, claves, cuentas de acceso, entorno de Python, adjuntos,
documentos generados ni cachés, y que las fechas internas de sus archivos están fijadas a un valor constante. Las
imágenes generadas no llevan metadatos de autor.

Límites de esta revisión. Detecta lo que está en la lista o coincide con los patrones; una palabra que no esté en
la lista no se detecta. La búsqueda textual se complementó con la lectura de binarios, pero no equivale a una garantía
de anonimato. El contenido de la sección «Quiénes somos» de la página de presentación lo decide quien expone: lo que se
escriba en `config/presentacion.json` viaja en el paquete.

## Lo que no se verificó

- El modo generativo del copiloto con un proveedor real: no había credenciales autorizadas. Está implementado y probado
  con un transporte simulado; el modo por defecto es el asistente local.
- Otros navegadores (Firefox, Safari) y un celular físico: los 390 px son emulación. Tampoco se usó una cámara ni un
  lector de códigos de mano reales.
- La instalación como aplicación en el celular.
- El equipo desconectado físicamente de la red.

## Instalación desde el paquete

El paquete se descomprimió en una carpeta nueva, sin entorno, base ni claves, y se instaló con `INSTALAR.ps1`
indicando un intérprete de Python 3.13.11:

| Paso | Resultado |
|---|---|
| Creación del entorno e instalación de dependencias fijadas | Correcta |
| Generación del escenario de 24 meses | 277 segundos; la instalación completa tomó 5 minutos 20 segundos |
| Huella del escenario generado | Igual a la del escenario del equipo de desarrollo (misma semilla y fecha) |
| `manage.py check` y `manage.py verificar_escenario` | Sin incidencias; las 54 comprobaciones cuadran |
| Suite de pruebas en la copia instalada | 289 pruebas correctas, 2 lentas omitidas |
| Servidor (`serve.py`) | Presentación, acceso, estado, estáticos y manifiesto responden; la raíz lleva a la presentación |
| `RESTAURAR_FERIA.ps1` con el servidor encendido | Correcta en unos 4 segundos |

Hallazgos de esta prueba, ya incorporados:

- El instalador solo acepta Python 3.13. Si no lo encuentra en el lanzador `py` ni en el `python` del sistema, se
  detiene con un mensaje claro y admite la ruta del intérprete con `-Interprete`.
- En una carpeta de ruta muy larga la instalación de dependencias falla por el límite de 260 caracteres de Windows.
  El paquete debe descomprimirse en una ruta corta; el README lo indica.
- En un primer intento el instalador usó por descuido Python 3.12: también allí instaló y generó un escenario con la
  misma huella, pero la suite de pruebas solo se ejecutó con Python 3.13.
