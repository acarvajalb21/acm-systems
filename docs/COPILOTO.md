# Copiloto: resumen para quien expone

El copiloto responde preguntas sobre la operación consultando los mismos servicios que usan las pantallas. Está en
Inteligencia → Copiloto y en el botón «Copiloto» de la barra superior. El detalle técnico (herramientas, activación
del modo generativo, validaciones) está en [copiloto/README.md](../copiloto/README.md); aquí va lo que hace falta
saber para mostrarlo y para responder preguntas.

## Qué hace y qué no

- Solo consulta. No modifica la base ni ejecuta consultas libres. Aprobar, pedir o aplicar sigue ocurriendo en la
  pantalla de cada módulo; el copiloto enlaza el registro.
- Cada respuesta trae el periodo, las fuentes consultadas, las limitaciones y enlaces internos a los registros que
  la sustentan. Las cifras y tablas las calcula la plataforma.
- Respeta el perfil de quien pregunta. Una consulta fuera del perfil devuelve «acceso restringido», con la lista de
  perfiles que sí pueden hacerla, y ningún dato.
- Si no reconoce la pregunta o los datos no alcanzan, lo dice y enumera lo que sí puede responder.

## Dos modos

El modo activo se ve siempre en la pantalla y en cada respuesta.

Asistente local (por defecto). Se identifica como «Asistente local · consultas y plantillas». Reconoce la intención
de la pregunta con reglas y arma la respuesta con plantillas sobre el resultado de una consulta. No es una
conversación generativa ni un modelo de lenguaje, y no se presenta como tal. No usa la red: funciona sin internet.
Este es el modo con el que se expone.

Modelo generativo (opcional). Si se configura en el servidor un proveedor y una clave por variables de entorno, un
modelo de lenguaje elige qué consultas hacer y redacta un texto breve sobre sus resultados. Las cifras, los
enlaces y las fuentes siguen saliendo de las consultas de la plataforma, no del modelo. Estado de este modo: está
implementado y probado con un transporte simulado, pero no se ha verificado en vivo, porque en el equipo de
preparación no hay credenciales. No se conocen su tiempo de respuesta, su costo por pregunta ni la calidad de su
redacción. Activarlo envía a un servicio externo la pregunta y los resultados de las consultas; con datos reales
eso requeriría autorización expresa. No lo active para exponer sin haberlo probado antes con una clave propia.

## Preguntas que funcionan

El asistente local reconoce estas preguntas y variantes cercanas:

| Pregunta | Perfiles |
|---|---|
| ¿Qué necesita atención hoy y por qué? | Todos |
| ¿Qué referencias podrían agotarse próximamente? | Inventario, Contabilidad, Administración, Dirección |
| Prepara una propuesta de compra con este presupuesto | Inventario, Contabilidad, Administración, Dirección |
| ¿Qué pagos conviene revisar y qué evidencia existe? | Contabilidad, Administración, Dirección |
| Resume el estado de la operación para dirección | Contabilidad, Administración, Dirección |

También responde sobre el detalle de una referencia, la cartera vencida, los lotes por vencer y el historial de
decisiones. Si se abre desde una referencia, un cliente o un ingreso bancario, la pregunta puede referirse a ese
registro.

Sobre la propuesta de compra: el presupuesto se indica en la pregunta («con 30 millones», «$30.000.000»). La
propuesta descuenta lo ya comprometido en pedidos abiertos del periodo de revisión, igual que la pantalla de
Reposición. En el escenario del 18/11/2026 con semilla 4711 hay $23.511.600 comprometidos, así que con 8 millones
no entra ninguna referencia, con 30 millones entran dos y con 40 millones entran las cuatro sugeridas. Si antes se
aprobó un pedido durante el recorrido, lo comprometido sube y esas cifras cambian. La propuesta no crea pedidos:
enlaza a Reposición con ese presupuesto.

## Límites

- Preguntas de hasta 500 caracteres y 8 por persona y minuto.
- Cada pregunta queda registrada con la persona, el modo, las consultas hechas, las fuentes y la duración.
- El asistente local entiende un conjunto acotado de preguntas. Una pregunta fuera de ese conjunto recibe una
  respuesta que lo reconoce.
- El texto que viene de la base (descripciones de extractos, notas, nombres) se trata como información, no como
  instrucciones: no cambia permisos ni activa consultas.

## Cómo mostrarlo

1. Haga una de las preguntas sugeridas, por ejemplo «¿Qué necesita atención hoy y por qué?».
2. Señale el distintivo del modo, el bloque de fuentes y periodo y las limitaciones.
3. Abra uno de los enlaces de la respuesta para mostrar que la cifra coincide con la del registro.
4. Si le preguntan si es «una IA que conversa», la respuesta correcta es que no: en esta demostración es un
   asistente de consultas con reglas, y el modo generativo es opcional y no se verificó en vivo.
