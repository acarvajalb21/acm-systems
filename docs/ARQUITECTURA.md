# Arquitectura

Aplicación web Django 5.2 que corre completa en un solo equipo: un proceso de servidor (Waitress), una base SQLite y
archivos estáticos servidos por la propia aplicación. No hay servicios externos, colas ni contenedores. Todo lo que
en una operación real sería una integración (sistema contable, banco, correo, mensajería) aquí es un simulador
identificado como tal.

## Aplicaciones y responsabilidades

| App | Responsabilidad |
|---|---|
| `inventory` | Usuarios y perfiles, catálogo, lotes y movimientos, solicitudes y reservas, proveedores, pedidos, facturas de proveedor, recepción con lector de códigos, cuentas por pagar, alertas y auditoría. Las reglas están en `services.py`, `compras.py`, `recepcion.py` y `payables.py`. |
| `cartera` | Clientes, listas de precios, ventas, descuentos con cupo, cuentas por cobrar, extractos bancarios, identificación y aplicación de ingresos, resultados de gestión. |
| `contable` | Cola de envíos al sistema contable con clave de idempotencia y el simulador local que los atiende. No contiene cliente de red. |
| `agentes` | Definición de los nueve agentes, turnos, propuestas, aprobación y herramientas que ejecutan lo aprobado. |
| `aprendizaje` | Pronóstico semanal y política de reposición, modelo de identificación de pagos y estrategia de cobranza, con sus pantallas (Reposición, Pronóstico, Lo aprendido, Metodología). |
| `notificaciones` | Avisos internos, manifiesto de aplicación instalable, service worker y página sin conexión. |
| `feria` | Escenario sintético, reinicio y verificación, perfiles de demostración, Resumen por perfil, mapa de flujo y recorrido guiado. |
| `copiloto` | Consultas en lenguaje natural sobre herramientas de solo lectura. Ver [COPILOTO.md](COPILOTO.md). |

Perfiles (código y nombre visible): `owner` Dirección, `admin` Administración, `inventory` Inventario, `reception`
Recepción, `finance` Contabilidad, `billing` Facturación y `requester` Solicitante. Dirección tiene todos los
accesos; Administración opera todo salvo lo reservado a Dirección (por ejemplo, confirmar descuentos sin cupo).

La navegación está organizada por tareas: Resumen, Operaciones, Inventario, Compras, Finanzas e Inteligencia. Cada
grupo muestra solo las páginas que el perfil puede abrir. Usuarios, auditoría y alertas están en el menú de la
persona.

## Flujo de una operación

Toda operación que cambia datos (una entrada, una venta, un pago, la aprobación de una propuesta) sigue los mismos
cuatro pasos dentro del servicio del módulo, no en la vista:

1. Permiso en el servidor. `inventory.services.require(user, perfiles)` rechaza la operación si la cuenta no está
   activa o su perfil no figura entre los permitidos. Ocultar un botón en pantalla no es el control: el control es
   esta comprobación.
2. Transacción. El servicio corre en una transacción de base de datos; si una regla falla (por ejemplo, la cantidad
   supera las unidades libres del lote), no queda nada a medias.
3. Clave de idempotencia. Cada formulario lleva un identificador único de operación. `operation(user, token, tipo)`
   lo registra dentro de la misma transacción; si llega por segunda vez (doble clic, reenvío del navegador), la
   operación se rechaza con un mensaje en lugar de duplicarse.
4. Auditoría. `event(user, nombre, objeto, detalle)` deja quién hizo qué y cuándo. La pantalla Auditoría las lista
   para Administración y Dirección.

Los envíos al sistema contable añaden un quinto elemento: la intención se guarda en la misma transacción del
negocio (`contable.services.enqueue`) y se procesa después de confirmarla. Cada envío tiene una clave de 30
caracteres; si no hay respuesta confiable queda «incierto» y se reintenta con la misma clave, de modo que el
documento no se duplica.

Las salidas de inventario siguen la regla «primero vence, primero sale» y un lote vencido no se despacha ni se
recibe.

## Datos y configuración

Todo lo que cambia en ejecución vive en la carpeta `datos/` del proyecto:

| Elemento | Contenido |
|---|---|
| `datos/feria.sqlite3` | Base de la demostración. |
| `datos/escenario_inicial.sqlite3` y `.json` | Copia del escenario recién generado y su ficha, para restaurarlo en segundos. |
| `datos/secret.key` | Secreto de la instalación; se crea solo la primera vez. |
| `datos/adjuntos/` | Soportes cargados o generados (facturas de proveedor, extractos, PDF de ventas). |
| `datos/ACCESOS_DEMO.md` | Cuentas de demostración con las claves del último reinicio. |
| `documentos_demo/` | Documentos de prueba que se regeneran en cada reinicio (ver [DATOS_SINTETICOS.md](DATOS_SINTETICOS.md)). |

La aplicación solo lee variables de entorno que empiezan por `FERIA_`. Ninguna es obligatoria.

| Variable | Efecto | Valor por defecto |
|---|---|---|
| `FERIA_PRIVATE_DIR` | Carpeta de datos alternativa (por ejemplo, una temporal para ensayar). | `datos/` |
| `FERIA_HOST`, `FERIA_PORT` | Dirección y puerto de escucha de `serve.py`. | `127.0.0.1`, `8790` |
| `FERIA_HOSTS` | Nombres o direcciones adicionales admitidos, separados por coma. | ninguno |
| `FERIA_THREADS` | Hilos del servidor. | `4` |
| `FERIA_AGENTS` | Con `0`, el servidor no revisa los turnos de los agentes. | `1` |
| `FERIA_PERFILES` | Con `0`, desaparece el selector de perfiles de demostración. | `1` |
| `FERIA_LLM_PROVIDER`, `FERIA_LLM_API_KEY`, `FERIA_LLM_MODEL`, `FERIA_LLM_REFUSAL_FALLBACK` | Proveedor generativo opcional del copiloto. | sin configurar (asistente local) |
| `FERIA_PRUEBAS_LENTAS` | Con `1`, incluye la prueba lenta de reproducibilidad. | sin definir |

No existe un modo de despliegue: la configuración usa siempre SQLite local y no contempla base de datos externa,
dominio ni certificado.

## Seguridad

- Política de contenido estricta en todas las respuestas: `default-src 'self'; style-src 'self'; script-src 'self'`,
  sin marcos ajenos y con formularios solo hacia la propia aplicación. No hay estilos ni guiones en línea; los
  gráficos son SVG generados en el servidor.
- Cookies `feria_sesion` y `feria_csrf` con `HttpOnly` y `SameSite=Lax`. La sesión dura ocho horas como máximo y
  termina al cerrar el navegador. Las páginas con datos se envían con `Cache-Control: no-store`.
- Contraseñas con Argon2 y validadores de longitud mínima (12), similitud y claves comunes. Al cambiar la clave se
  cierran las demás sesiones de la cuenta.
- Límite de intentos de ingreso: 5 por cuenta y 30 por dirección de origen cada 15 minutos. Solo se guardan
  resúmenes criptográficos de esos intentos, no los usuarios ni las claves digitadas.
- Selector de perfiles de demostración: solo responde a peticiones que llegan desde el propio equipo (dirección de
  bucle local) y solo para las cuentas registradas al generar el escenario. Desde otro dispositivo no se muestra y
  la ruta responde «acceso denegado». No se confía en cabeceras de reenvío.
- Archivos cargados: se validan por extensión y por firma del contenido, con tope de tamaño; se descargan solo a
  través de vistas que comprueban el perfil.
- El servidor no expone la carpeta del proyecto: solo las rutas de la aplicación y `static/`.
- El copiloto es de solo lectura y aplica los mismos perfiles. Sus límites están en [COPILOTO.md](COPILOTO.md).

Lo que esta lista no cubre está en [SUPUESTOS_Y_LIMITES.md](SUPUESTOS_Y_LIMITES.md): la demostración sirve por HTTP
en el equipo local y no ha tenido una revisión de seguridad independiente.

## Integraciones simuladas

| Integración | Qué hace el simulador | Qué no hace nunca |
|---|---|---|
| Sistema contable (`contable/simulator.py`) | Atiende catálogos, facturas de venta, recibos de caja, consulta de saldo, PDF y registro de correo. Respeta la idempotencia por clave. Guarda sus documentos en la base, con un código de prefijo «SIM» y un PDF marcado «Documento de demostración sin validez fiscal». Permite ensayar rechazos y respuestas perdidas. | No se comunica con ninguna entidad ni servicio; sus documentos no tienen validez fiscal. `contable/client.py` describe la interfaz donde se conectaría un proveedor real, pero no incluye ningún cliente de red. |
| Banco | No hay conexión bancaria. Los movimientos llegan como archivos de extracto (CSV o XLSX) que una persona importa, revisa en vista previa y confirma. El escenario genera esos extractos durante la simulación y deja uno nuevo para importar en vivo. | No consulta saldos, no ordena pagos ni transferencias. «Pagar» a un proveedor registra el pago en la plataforma; el egreso aparece después en un extracto importado. |
| Correo | Enviar una factura de venta por correo o un recordatorio de cobro deja un evento «envío simulado registrado» con el destinatario y la hora, y el texto queda visible en pantalla. La recuperación de clave no envía correos: la restablece Administración. | No abre el cliente de correo, no usa SMTP ni enlaces `mailto`. |
| Mensajería | Igual que el correo: el recordatorio o el pedido al proveedor quedan como mensaje preparado y, al «enviar», como envío simulado en la auditoría. | No abre aplicaciones de mensajería ni enlaces externos. |

Un recordatorio de cobro sin canal de contacto registrado queda solo como «mensaje preparado» y no cuenta como
envío.

El único código que puede abrir una conexión de red es el proveedor generativo opcional del copiloto
(`copiloto/providers.py`), y solo si se configura una clave en el servidor. Sin clave, la aplicación no hace
ninguna llamada externa.

## Aplicación instalable y avisos internos

- `manifest.webmanifest` y `sw.js` permiten instalar la aplicación en el dispositivo. El service worker guarda
  únicamente la página «sin conexión» y los recursos estáticos; las páginas con datos nunca se almacenan en el
  dispositivo.
- En pantallas angostas la navegación pasa a una barra inferior con los grupos y una hoja «Más», y las tablas se
  muestran como registros apilados.
- Avisos internos: el agente de avisos registra, una sola vez por alerta y por persona, las alertas nuevas de cada
  perfil. Se consultan en la pantalla Alertas. No hay notificaciones fuera de la plataforma (ni al navegador, ni al
  celular, ni por correo).
- La recepción acepta códigos tecleados, pegados o leídos con un lector de mano; con cámara usa el detector del
  navegador o, si no existe, la biblioteca ZXing incluida.

## Agentes en segundo plano

Mientras `serve.py` está encendido, un hilo revisa cada minuto qué agentes tienen turno pendiente. El estado de los
turnos está en la base, de modo que reiniciar el servidor no repite un turno ya hecho. Detalle en
[AGENTES.md](AGENTES.md).

## Dependencias y licencias

Dependencias directas (`requirements.txt`; versiones exactas en `requirements-lock.txt`):

| Paquete | Uso | Licencia declarada |
|---|---|---|
| Django 5.2.17 | Marco web. | BSD de 3 cláusulas |
| waitress 3.0.2 | Servidor local. | ZPL 2.1 |
| whitenoise 6.12.0 | Archivos estáticos. | MIT |
| argon2-cffi 25.1.0 | Resguardo de contraseñas. | MIT |
| pdfplumber 0.11.9 | Lectura del PDF de factura de demostración. | MIT |
| openpyxl 3.1.5 | Extractos y exportaciones en XLSX. | MIT |

Los modelos de pronóstico y aprendizaje están escritos en Python puro, sin bibliotecas numéricas.

Recursos incluidos en `static/` (tipografías IBM Plex con licencia OFL 1.1, íconos Feather con licencia MIT y
ZXing 0.21.3 con licencia Apache 2.0): ver [static/LICENCIAS.md](../static/LICENCIAS.md). La aplicación no carga
recursos de internet.
