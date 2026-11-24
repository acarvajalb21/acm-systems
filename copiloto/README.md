# Copiloto

Responde preguntas sobre la operación consultando los servicios de la plataforma. **Solo consulta**: no modifica la
base ni ejecuta SQL libre. Aprobar, pedir o aplicar sigue ocurriendo en cada pantalla; el copiloto enlaza el registro.

Cada respuesta trae periodo, fuentes, limitaciones y enlaces internos a los registros. Si los datos no alcanzan, lo dice.

## Dos modos

| | Asistente local (por defecto) | Modelo generativo (opcional) |
|---|---|---|
| Cómo entiende la pregunta | Reglas y patrones | Un modelo de lenguaje elige las consultas |
| Cómo redacta | Plantillas sobre el resultado | Texto breve del modelo, validado |
| Cifras, tablas, enlaces, fuentes | Los calcula la plataforma | Los calcula la plataforma (el modelo no los escribe) |
| Red | No usa | Sale a un servicio externo |

El modo activo se muestra siempre en la pantalla y en cada respuesta. El asistente local se identifica como
«Asistente local · consultas y plantillas»: no es una conversación generativa.

## Herramientas (`tools.py`)

Todas son de lectura, validan sus argumentos (tipos y rangos) y verifican el perfil en el servidor con las mismas
tuplas de `inventory.services`. Fuera del perfil devuelven «acceso restringido», sin datos.

| Herramienta | Perfiles |
|---|---|
| `atencion_hoy` | Todos |
| `riesgo_agotamiento(dias)` · `propuesta_compra(presupuesto)` · `detalle_referencia(texto)` · `historial_decisiones(dias)` | Inventario, Contabilidad, Administración, Dirección |
| `pagos_por_revisar` · `estado_operacion(periodo)` · `cartera_vencida` | Contabilidad, Administración, Dirección |
| `lotes_por_vencer(dias)` | Inventario, Administración, Dirección |

`propuesta_compra` usa la misma regla de presupuesto de la pantalla de Reposición y no crea pedidos.

## Activar el modelo generativo

Se configura solo en el servidor, con variables de entorno, antes de iniciar la aplicación:

```
FERIA_LLM_PROVIDER=anthropic
FERIA_LLM_API_KEY=<clave del servicio>
FERIA_LLM_MODEL=claude-opus-5-5        (opcional; ese es el valor por defecto)
```

Sin clave, o con un proveedor no reconocido, se usa el asistente local. `FERIA_LLM_REFUSAL_FALLBACK=0` apaga la
opción del servicio que reintenta con otro modelo cuando el primero declina.

**Estado: implementado y probado con un transporte falso; no verificado en vivo.** En este equipo no hay credenciales
autorizadas, así que nunca se ha hecho una llamada real. Antes de usarlo en público hay que probarlo con una clave
propia: tiempos de respuesta, costo por pregunta y calidad del texto no están medidos.

### Qué sale del servidor al activarlo

Hacia el servicio de mensajes del proveedor (`api.anthropic.com`, por HTTPS, sin seguir redirecciones):

- la pregunta escrita (máximo 500 caracteres) y el perfil de quien pregunta (por ejemplo, «Contabilidad»);
- el nombre, la descripción y el esquema de argumentos de las herramientas que ese perfil puede usar;
- los resultados de las consultas que el modelo pida: resúmenes y hasta ocho renglones por tabla, **con los nombres,
  cifras y textos que haya en la base** (en la demostración, datos sintéticos). No se envían rutas ni enlaces.

No sale: la clave en el cuerpo de la solicitud (viaja solo en el encabezado de autenticación), contraseñas, sesiones,
variables de entorno, ni datos de herramientas que el perfil no puede usar. La clave no llega al navegador, a las
plantillas, al registro de turnos ni a los mensajes de error.

Con datos reales, activar este modo significa compartirlos con un tercero: requiere autorización expresa.

### Límites y validaciones

- Máximo 4 consultas por pregunta y 25 s en total; si el servicio falla, tarda, declina o responde algo que no se
  puede validar, contesta el asistente local y se avisa en la respuesta.
- El texto de la base (descripciones de extractos, notas, nombres) llega al modelo delimitado como datos; no puede
  cambiar permisos, activar herramientas ni revelar configuración. Los permisos siempre se verifican en el servidor.
- Una herramienta inexistente o con argumentos fuera de rango se rechaza. Del texto del modelo se retiran formato,
  marcado y cualquier dirección o enlace: los únicos enlaces son los internos que entregan las herramientas.
- El texto del modelo es redacción, no cálculo: puede resumir mal. Las cifras que valen son las de las tablas,
  marcadas «Calculado por la plataforma».

## Límites generales

Preguntas de hasta 500 caracteres, 8 por persona y minuto, solo por POST con CSRF. Cada turno queda en `CopilotTurn`
(persona, pregunta, modo, modelo, herramientas y argumentos, fuentes, duración y código de error).

## Enlazar desde otra pantalla

```
{% include 'copiloto/_acceso.html' %}
{% include 'copiloto/_acceso.html' with tipo='referencia' id=product.pk %}
```

`tipo` admite `referencia`, `cliente` e `ingreso` (URL: `copiloto/?contexto=<tipo>:<id>`). El contexto se valida en el
servidor y se ignora si el perfil no puede consultar ese registro.

## Pruebas

`manage.py test copiloto`. Ninguna sale a internet: el transporte real está bloqueado y solo se ejercita contra un
servidor del propio equipo.
