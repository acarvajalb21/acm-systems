# Agentes

Un agente es un proceso que revisa los datos de la plataforma con reglas del negocio y deja propuestas concretas,
cada una con su razón y sus evidencias. No es un modelo de lenguaje ni toma decisiones por su cuenta sobre
inventario o dinero: las propuestas que cambian datos se ejecutan solo cuando una persona con el perfil permitido
las aprueba.

Pantallas: Inteligencia → Agentes (estado de cada agente y propuestas por aprobar), el detalle de cada propuesta y
Inteligencia → Historial de decisiones.

## El ciclo

Cada propuesta recorre seis pasos, y la pantalla de detalle muestra en cuál está:

1. Detectar. El agente corre en su turno (horario) o por un evento y lee el estado actual.
2. Analizar. Aplica sus reglas y, cuando corresponde, consulta un modelo (pronóstico, puntaje de pagos, estrategia
   de cobranza).
3. Proponer. Guarda la propuesta con título, razón, valores, registros en que se apoya y nivel de confianza.
4. Revisar. Una persona la abre, ve las evidencias con su estado de hoy y decide aprobar o descartar.
5. Ejecutar. Al aprobar, una herramienta del agente llama al servicio del módulo con la identidad de quien aprueba.
6. Registrar. Queda el resultado en la propuesta, en la auditoría y en el historial de decisiones.

Lo que se muestra como «por qué» es el texto y los datos que el agente guardó al calcular. No hay razonamientos
redactados después.

## Los nueve agentes

| Agente | Objetivo | Disparador | Qué consulta | Qué propone | Quién aprueba | Qué se ejecuta al aprobar |
|---|---|---|---|---|---|---|
| Reposición | Evitar faltantes sin comprar de más. | Todos los días a las 06:45. | Pronóstico por referencia, posición, tiempos de entrega, presupuesto de compra. | Un pedido por proveedor con las referencias en su punto de pedido. | Inventario, Administración, Dirección. | Se prepara el pedido con cantidades recalculadas ese día. El envío al proveedor es un paso aparte y simulado. |
| Vencimientos | Que el inventario refleje lo utilizable. | Todos los días a las 06:30. | Lotes vencidos con saldo libre y lotes por vencer. | La baja de cada lote vencido con causa «vencimiento». | Inventario, Administración, Dirección. | Baja de las unidades libres del lote. |
| Solicitudes | Atender solicitudes internas con el lote correcto. | Cada 30 minutos y al crear una solicitud. | Solicitudes pendientes y lotes vigentes. | Reservar el lote que vence primero y alcanza la cantidad. | Inventario, Administración, Dirección. | Aprobación de la solicitud y reserva; la entrega física se registra después. |
| Pronto pago | No perder descuentos y avisar de lo vencido. | Todos los días a las 07:00. | Facturas de proveedor por pagar y sus condiciones. | Programar el pago antes de que venza el descuento, o programar hoy las vencidas. | Contabilidad, Administración, Dirección. | Queda un pago programado. Confirmarlo con su soporte es otro paso. |
| Conciliación bancaria | Identificar ingresos del extracto. | Cada hora y al importar movimientos. | Ingresos por identificar, cartera abierta, reglas de sugerencia y puntaje del modelo de pagos. | Aplicar el ingreso a las facturas de un cliente, o conciliarlo con un recaudo de contado. Los ambiguos y los que no coinciden no generan propuesta: quedan para contabilidad. | Contabilidad, Administración, Dirección. | Aplicación del ingreso y recibo de caja en el sistema contable simulado. |
| Cobranza | Recordar la cartera vencida con un tono y un momento adecuados. | Todos los días a las 08:00. | Cartera vencida, contacto del cliente, historial de pago, resultados de recordatorios anteriores. | Un recordatorio por cliente con el texto preparado. | Contabilidad, Administración, Dirección. | Envío simulado registrado (o solo mensaje preparado si el cliente no tiene canal). Nada sale de la plataforma. |
| Envíos al sistema contable | Que ningún envío quede a medias. | Cada 30 minutos. | Envíos pendientes o inciertos. | Nada: actúa solo. | No aplica. | Reintenta cada envío con su misma clave de idempotencia. |
| Avisos internos | Que cada persona vea sus alertas nuevas. | Cada 5 minutos. | Alertas de cada perfil. | Nada: actúa solo. | No aplica. | Registra cada alerta nueva una vez por persona. |
| Resumen diario | Dar a la dirección el estado del día. | Todos los días a las 06:00. | Ventas y recaudo del día anterior, cartera y cuentas por pagar vencidas, pendientes. | Nada: actúa solo. | No aplica. | Guarda un texto con cifras y periodo explícito. |

Quién puede ver cada agente coincide con quién puede aprobar, con dos excepciones: Contabilidad también ve el de
reposición y el resumen diario.

Administración y Dirección pueden pausar un agente y ajustar sus parámetros (por ejemplo, con cuántos días de margen
programar un pago o cada cuánto repetir un recordatorio). Quien puede aprobar las propuestas de un agente también
puede pedirle una revisión inmediata con «Revisar ahora».

## Qué actúa solo y por qué

Tres agentes no piden aprobación porque lo que hacen no cambia inventario ni dinero y se puede repetir sin efecto
adicional:

- Envíos al sistema contable: no crea operaciones nuevas. Retoma envíos que una persona ya originó (una venta
  emitida, un recibo) usando la misma clave, de modo que el documento no se duplica.
- Avisos internos: solo escribe registros de aviso, uno por alerta y por persona.
- Resumen diario: solo lee y guarda un texto.

Todo lo demás (pedidos, bajas, reservas, pagos programados, aplicación de ingresos, recordatorios) requiere
aprobación.

## Revalidación al aprobar

Una propuesta puede tener horas o días. Por eso aprobar no ejecuta lo que el agente calculó: la herramienta vuelve a
comprobar con los datos del momento.

- Permisos: se verifican otra vez, con el perfil de quien aprueba, en el servicio del módulo.
- Vigencia: si la base de la propuesta cambió (la factura ya se pagó, el ingreso ya se identificó, el lote ya no
  tiene unidades libres, la solicitud ya se atendió, el cliente ya pagó), la propuesta pasa a «Ya no aplica» con la
  explicación y no se cambia nada.
- Cantidades: el pedido de reposición se arma con el consumo, la posición y el presupuesto de ese momento. Si las
  cantidades difieren de las propuestas, el resultado lo dice.
- Reglas del negocio: si el servicio rechaza la operación, la propuesta queda «No se pudo ejecutar» con el motivo.

Además, cada vez que un agente corre recalcula todas sus propuestas abiertas. Las que ya no aparecen en el nuevo
cálculo pasan solas a «Ya no aplica», con la nota «La situación cambió antes de aprobarla».

## Idempotencia y deduplicación

- Un turno, una vez. Cada ejecución programada lleva una clave de periodo (agente y día, o agente, día y franja).
  Una restricción de la base impide dos ejecuciones con la misma clave, aunque dos procesos lo intenten a la vez.
  Como el estado está en la base, reiniciar el servidor no repite turnos.
- Una propuesta abierta por asunto. Cada propuesta tiene una clave de deduplicación (por ejemplo, proveedor y
  facturas, o ingreso y cliente). Si el agente vuelve a encontrar lo mismo, actualiza la propuesta existente en
  lugar de crear otra.
- Aprobar dos veces no duplica. La clave de operación se deriva de la propuesta, así que un segundo intento choca
  con la operación ya registrada. Una propuesta ya decidida responde con su resultado.
- Eventos sin ráfagas. Un mismo agente no se dispara por evento más de una vez cada 15 segundos.
- Un agente que falla no interrumpe la operación que lo disparó: el error queda en su bitácora.
- Una ejecución que quedó «en curso» más de 30 minutos (por ejemplo, porque se apagó el equipo) se marca como
  interrumpida en la siguiente revisión.

## Cómo corren

Mientras `serve.py` está encendido, un hilo revisa cada minuto qué agentes tienen turno pendiente (`FERIA_AGENTS=0`
lo apaga). También se pueden correr a mano:

```
.venv\Scripts\python.exe manage.py agentes --lista
.venv\Scripts\python.exe manage.py agentes
.venv\Scripts\python.exe manage.py agentes --agente cobranza
```

## Historial de decisiones

Lista lo aprobado, lo descartado y lo que dejó de aplicar, con quién decidió, cuándo, qué se ejecutó y el resultado.
Se filtra por agente, estado, persona, rango de fechas y texto.

En el escenario generado con fecha 18/11/2026 y semilla 4711, los agentes entran en turno en los últimos cuatro
meses simulados: 520 turnos y 614 propuestas, de las cuales 423 se ejecutaron, 169 dejaron de aplicar y 22 quedan
por aprobar en la fecha de referencia. Son cifras del escenario sintético y cambian al regenerarlo o al operar la
demostración.
