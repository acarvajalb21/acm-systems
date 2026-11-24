# Modelos predictivos y aprendizaje

Este documento explica qué calcula la plataforma, cómo se evalúa y qué resultados dio sobre el escenario
sintético. La pantalla Inteligencia → Metodología muestra lo mismo con cifras calculadas en el momento.

Todas las cifras de este documento se obtuvieron ejecutando el código sobre el escenario generado con fecha de
referencia 18/11/2026 y semilla 4711 (24 meses simulados, 32 referencias). Cambian al regenerar con otra fecha u
otra semilla, y también a medida que se opera la demostración.

## Qué es cada pieza

La plataforma separa cuatro categorías y las nombra así en pantalla. Conviene no mezclarlas al exponer.

| Categoría | Qué significa | Dónde aparece |
|---|---|---|
| Regla automática | Lógica fija con parámetros visibles. No aprende. | Política de reposición (punto de pedido, nivel objetivo, empaque, tope por vencimiento, recorte por presupuesto); agentes de pronto pago, vencimientos, solicitudes y conciliación; reglas de sugerencia de pagos (mención de factura, titular, valores). |
| Método estadístico | Fórmula que resume el historial, sin ajuste de coeficientes. | Promedio, último valor, medias móviles, suavizado exponencial, Croston-SBA, misma semana del año anterior; cuantiles empíricos del intervalo; asignación adaptativa de la cobranza (muestreo de Thompson). |
| Modelo entrenado | Coeficientes ajustados con datos. | Regresión ridge global del pronóstico; regresión logística en línea del modelo de pagos. |
| Asistente de consultas | Por defecto selecciona consultas mediante reglas y presenta los resultados con plantillas. | Copiloto. Las cifras y los pronósticos los calcula la plataforma. |

Los modelos están escritos en Python puro (`aprendizaje/series.py`, `reposicion.py`, `pagos.py`, `cobranza.py`) y
se ajustan únicamente con los movimientos registrados en la plataforma.

## 1. Abastecimiento

### Serie semanal y lo que no se observa

Para cada referencia se construye una serie por semana (de lunes a domingo) hasta la última semana completa, con
tres componentes que se muestran por separado:

- Salidas observadas: despachos y entregas registrados.
- Demanda no atendida registrada: unidades de solicitudes rechazadas. Se suman al consumo de su semana (el
  parámetro `incluir_rechazadas` permite excluirlas).
- Semanas sin existencias: una semana que terminó sin existencias y sin salidas está censurada. No se sabe cuánto se
  habría consumido, así que se excluye del ajuste y de la evaluación en lugar de tratarla como consumo cero.

Las salidas no son la demanda. La plataforma nunca las presenta como «demanda total»: solo conoce lo que salió y lo
que quedó registrado como rechazado. La demanda que nadie registró no existe para el modelo.

En este escenario: 10 semanas censuradas entre 3.155 semanas-referencia, y 91 unidades de demanda no atendida
registrada frente a 160.586 unidades de salidas observadas.

### Candidatos

Compiten, por referencia y en igualdad de condiciones:

- Métodos estadísticos de pronóstico plano: promedio de todo el historial, último valor, medias móviles de 4 y 8
  semanas, suavizado exponencial (alfa 0,2, 0,35 y 0,5) y Croston-SBA (alfa 0,1, 0,2 y 0,3). Croston-SBA solo entra
  cuando al menos el 30 % de las semanas anteriores a la prueba están en cero.
- Misma semana del año anterior, cuando la referencia tiene 60 o más semanas utilizables.
- Un modelo entrenado adicional: una regresión ridge global, la misma para todas las referencias. Sus 13 rasgos son
  los rezagos 1 a 4, las medias de 4, 8 y 13 semanas, el valor de la misma semana del año anterior y un indicador
  de si existe, seno y coseno de la época del año, la proporción de semanas en cero y el término independiente;
  todo escalado por la media de la referencia. Los hiperparámetros son fijos (penalización 1, tope 6 en los valores
  escalados); no se ajustan con los datos.

En un empate gana el candidato más simple. Si un método sencillo supera al modelo entrenado, se usa el sencillo y
la pantalla lo dice.

### Diseño de validación

Las semanas utilizables de cada referencia se reparten en orden temporal:

```
[ entrenamiento ........ | selección (origen móvil) | prueba final ]
  inicio del historial     hasta 26 semanas           últimas 8 semanas
```

- En cada origen el pronóstico usa únicamente las semanas anteriores. El modelo global se ajusta, para cada origen,
  solo con filas (de cualquier referencia) cuya ventana objetivo terminó antes de esa fecha.
- El método se elige con el error absoluto medio de la ventana de selección. La ventana de prueba no interviene en
  la elección: su error es el que se informa como desempeño, con su número de orígenes.
- Con menos de 20 semanas utilizables la prueba se reduce a 4 semanas. Con menos de 12 no se elige nada: se usa el
  promedio como respaldo y la referencia queda marcada «historial insuficiente».

En este escenario, 30 de las 32 referencias tienen historial suficiente (entre 101 y 105 semanas utilizables; en 26
de ellas el reparto es 71 de entrenamiento, 26 de selección y 8 de prueba). Una referencia lanzada hace pocas
semanas tiene 5 semanas y usa el respaldo; otra, recién creada, no tiene movimientos.

Los errores de orígenes consecutivos a varias semanas se solapan. Los `n` que se muestran son conteos de orígenes,
no tamaños de muestra independientes.

### Horizonte

La decisión de pedir depende del consumo acumulado durante el periodo de protección (tiempo de entrega del
proveedor más el periodo de revisión, 7 días por defecto). Por eso el pronóstico se evalúa a una semana y también
acumulado a `H = ceil(días de protección / 7)` semanas, y la elección del método se hace con el error al horizonte
`H` cuando hay al menos 4 orígenes de selección. El tiempo de entrega es la mediana observada entre la fecha del
pedido y la primera recepción de cada proveedor; si no hay dato, se usa el parámetro.

En este escenario los tiempos de entrega medianos van de 4 a 14 días según el proveedor (entre 65 y 83 pedidos
observados por proveedor) y `H` vale 2 semanas en 15 referencias y 3 semanas en las otras 15.

### Métricas

- Error absoluto medio (MAE) y sesgo (observado menos pronóstico; positivo significa que se pronosticó de menos).
  Se informan siempre.
- WAPE: suma de errores absolutos dividida entre la suma de lo observado. Cuando en la ventana no hubo salidas, el
  denominador es cero y el WAPE no está definido: se muestra como «no definido (sin salidas en la ventana)» y no se
  promedia con los demás. En este escenario ocurre en una referencia, la que no tiene rotación reciente.
- No se usa ninguna cifra de «precisión» agregada.

### Resultados medidos

Método elegido en las 30 referencias con historial suficiente:

| Método | Referencias |
|---|---|
| Promedio de todo el historial | 17 |
| Misma semana del año anterior | 4 |
| Media móvil de 8 semanas | 3 |
| Regresión ridge global (modelo entrenado) | 2 |
| Media móvil de 4 semanas | 1 |
| Suavizado exponencial, alfa 0,2 | 1 |
| Suavizado exponencial, alfa 0,5 | 1 |
| Croston-SBA, alfa 0,1 | 1 |

Comparación al horizonte de la decisión. Cada cifra es el error absoluto total del candidato dividido entre el del
promedio simple en las mismas referencias y orígenes (1,000 significa igual; menos es mejor). Selección: 735
orígenes; prueba: 195 orígenes.

| Candidato | Referencias en que compitió | Selección | Prueba |
|---|---|---|---|
| Promedio de todo el historial | 30 | 1,000 | 1,000 |
| Último valor | 30 | 1,367 | 1,347 |
| Media móvil de 4 semanas | 30 | 1,038 | 0,985 |
| Media móvil de 8 semanas | 30 | 1,020 | 0,940 |
| Suavizado exponencial, alfa 0,2 | 30 | 0,979 | 0,960 |
| Suavizado exponencial, alfa 0,35 | 30 | 1,010 | 1,005 |
| Suavizado exponencial, alfa 0,5 | 30 | 1,061 | 1,068 |
| Croston-SBA, alfa 0,1 | 3 | 1,019 | 1,017 |
| Croston-SBA, alfa 0,2 | 3 | 1,043 | 1,047 |
| Croston-SBA, alfa 0,3 | 3 | 1,064 | 1,096 |
| Misma semana del año anterior | 30 | 1,215 | 1,032 |
| Regresión ridge global | 30 | 1,024 | 0,975 |
| Método elegido en cada referencia (lo que se usa) | 30 | 0,774 | 0,814 |

Lectura sin maquillaje:

- El modelo entrenado rara vez gana. Fue elegido en 2 de 30 referencias. En conjunto quedó 2,4 % por encima del
  promedio simple en selección y 2,5 % por debajo en prueba: una diferencia pequeña y de signo cambiante, que no
  permite afirmar que sea mejor. Una media móvil de 8 semanas o un suavizado suave rinden igual o mejor en prueba.
- En 17 de 30 referencias el promedio de todo el historial fue el mejor en selección. Es lo esperable en series
  estables generadas con ruido alrededor de una media fija.
- El procedimiento completo (elegir un método por referencia) tuvo en prueba un error 18,6 % menor que usar el
  promedio en todas. La cifra de selección (0,774) es optimista por construcción, porque se eligió con esa misma
  ventana; la que cuenta es la de prueba (0,814).
- Referencia por referencia, en prueba el método elegido fue mejor que el promedio en 8, igual en 17 (son las que
  usan el promedio) y peor en 5.
- WAPE agregado del método elegido: 14,6 % en selección y 17,1 % en prueba.

### Intervalos con cobertura medida

El intervalo del 80 % es el pronóstico más los cuantiles empíricos 10 % y 90 % de los errores del método elegido en
la ventana de selección, para el mismo horizonte. El intervalo siempre contiene al pronóstico. Su cobertura se mide
en la ventana de prueba: proporción de orígenes en que lo observado cayó dentro.

Cobertura medida en este escenario, sumando las 30 referencias:

| Horizonte | Dentro del intervalo | Cobertura |
|---|---|---|
| Una semana | 165 de 240 | 68,8 % |
| Acumulado a `H` semanas | 125 de 195 | 64,1 % |

La cobertura medida queda por debajo del 80 % nominal. Los cuantiles salen de 24 a 26 errores por referencia y de
un periodo anterior al de prueba, de modo que el intervalo resulta más angosto de lo que debería. La pantalla
muestra la cobertura medida junto al intervalo, con su `n`, en lugar del valor nominal; por referencia, la prueba
tiene entre 6 y 8 orígenes y la pantalla lo señala como pocas observaciones.

Además de esta evaluación retrospectiva hay un registro prospectivo: cada lunes el agente de reposición guarda el
pronóstico de la semana con su intervalo antes de conocerla, y lo compara cuando la semana cierra. En las últimas 8
semanas cerradas (244 pares referencia-semana): MAE de 12,8 unidades, sesgo de +2,3 unidades, WAPE de 23,4 % y 174
de 244 observaciones dentro del intervalo (71,3 %).

### De pronóstico a sugerencia

La sugerencia es una regla automática sobre el pronóstico, con sus supuestos a la vista:

1. Punto de pedido = consumo esperado en el periodo de protección + inventario de seguridad. La seguridad es el
   cuantil empírico del nivel de servicio sobre los errores acumulados de selección cuando hay 12 o más errores; si
   no, una aproximación normal con el error medido a una semana. La pantalla indica cuál se usó (en este escenario,
   el cuantil empírico en 29 referencias y la aproximación en 1).
2. Nivel objetivo = punto de pedido + consumo del ciclo de pedido (14 días por defecto).
3. Posición = disponible (físico menos reservado, solo de lotes vigentes) + por recibir de facturas no anuladas +
   pedidos abiertos sin factura vinculada. Cada unidad pendiente cuenta una sola vez: cuando la factura del
   proveedor ya está vinculada al pedido, sus unidades se cuentan como «por recibir» y no otra vez como «pedidas».
   Las reservas no se restan dos veces. Las solicitudes pendientes sin reservar se muestran como dato y no se
   restan.
4. Cantidad = lo necesario para llevar la posición al nivel objetivo, redondeado al empaque de compra y limitado por
   lo que alcanza a usarse antes del vencimiento (vida útil al recibir menos un margen de 30 días).
5. Estados: «Pedir ya», «Pedir pronto», «Cubierta», «Más de lo que se usa», «Sin consumo reciente», «Sin historial»
   y «No calculable». Una referencia cuya tasa esperada cae por debajo de 0,05 unidades por semana se trata como
   «Sin consumo reciente»: no se proyecta cobertura ni se sugiere pedir, y sus existencias se señalan para revisar.
   Si el cálculo de una referencia falla, queda «No calculable» con su motivo y las demás no se afectan.

A la fecha de referencia: 2 referencias en «Pedir ya», 2 en «Pedir pronto», 26 cubiertas, 1 sin consumo reciente y 1
sin historial. El costo estimado de todo lo sugerido es $9.670.500.

### Nivel de servicio y presupuesto

Dos supuestos se editan desde la pantalla de Reposición (solo Administración y Dirección; el cambio queda en
auditoría) y se muestran donde afectan la decisión:

- Nivel de servicio: entre 90 % y 99 %; 95 % por defecto. Es el cuantil con que se calcula la seguridad, no una
  promesa de disponibilidad.
- Presupuesto de compra por periodo de revisión, en pesos; 0 significa sin tope. Con presupuesto, las referencias se
  ordenan por urgencia (días de cobertura menos tiempo de entrega), cada una recibe su cantidad completa si cabe o
  la mayor cantidad en múltiplos de empaque que quepa, y lo que queda por fuera se lista con su motivo. Los pedidos
  abiertos preparados en el periodo de revisión vigente ya consumen presupuesto.

`?presupuesto=` en la pantalla de Reposición prueba una cifra sin guardarla. El copiloto usa la misma función
(`reposicion.budget_plan`) para su propuesta de compra.

A la fecha de referencia hay $23.511.600 comprometidos en pedidos abiertos de los últimos 7 días. Por eso, con un
presupuesto de $8.000.000 no entra ninguna referencia; con $30.000.000 entran 2 por $6.478.920 y con $40.000.000
entran las 4.

### Rendimiento

La lista de sugerencias se memoriza con una huella de los datos que intervienen (movimientos, lotes, facturas,
pedidos, solicitudes, parámetros y fecha) y se invalida cuando cualquiera cambia. Calcular las 32 referencias desde
cero, junto con las tablas de este documento, tomó menos de 3 segundos en el equipo de preparación.

## 2. Identificación de pagos

Cuando contabilidad importa un extracto, la plataforma sugiere a qué cliente corresponde cada ingreso. La sugerencia
combina reglas (el texto menciona una factura, la identificación del titular coincide, el pagador ya fue
confirmado, el valor coincide con un saldo o una suma de saldos) con un modelo entrenado que aporta una razón más.

- El modelo es una regresión logística en línea sobre rasgos binarios del par (movimiento, cliente). Incluye
  rasgos «palabra × cliente» que funcionan como memoria de relaciones: por ejemplo, el nombre de quien suele
  transferir a nombre de una empresa.
- El modelo no aplica pagos. Opina a partir de 8 ejemplos, y la decisión es de contabilidad o del agente de
  conciliación con aprobación. Los casos ambiguos quedan para revisión humana.

### Evaluación secuencial

Cada vez que se confirma una identificación, el modelo primero predice con lo que sabía antes (sin el vínculo que
esa identificación crea y sin pagos posteriores), se anota si acertó y solo después aprende de ese ejemplo. Así,
cada acierto contado es sobre un caso que el modelo no había visto.

Resultado en este escenario: 745 ejemplos vigentes. Ya entrenado, el primer candidato del modelo fue el correcto en
735 de 737 casos (99,7 %). Las reglas solas habrían acertado en 735 de 737 (99,7 %); en los otros 2 no sugerían a
ningún cliente.

El modelo acierta prácticamente igual que las reglas solas. En este escenario no hay evidencia de que mejore la
identificación: la mayoría de los ingresos sintéticos traen el titular o mencionan la factura, y eso lo resuelven
las reglas. Lo que la demostración muestra es el mecanismo (evaluación antes de aprender, correcciones, puntajes
con su fiabilidad), no una ganancia.

### Correcciones

Si una aplicación se revierte o el ingreso cambia de cliente, el ejemplo aprendido deja de ser verdadero. Se marca
como anulado y el modelo se reconstruye solo con el historial vigente, en el orden en que se identificó cada
ingreso. Una prueba automática comprueba que, tras revertir y reconstruir, los pesos son iguales a los de un modelo
que nunca vio ese ejemplo. En el escenario recién generado no hay ejemplos anulados; revertir una aplicación en la
pantalla del movimiento bancario permite verlo.

### Puntajes no calibrados y tabla de fiabilidad

La salida del modelo es un puntaje, no una probabilidad. Se muestra como «puntaje (0 a 100)» y nunca como
porcentaje de confianza. A su lado va la tasa de acierto medida en la evaluación secuencial para puntajes
similares:

| Puntaje | Casos | Aciertos | Tasa medida |
|---|---|---|---|
| 0 a 49 | 16 | 14 | 87,5 % |
| 50 a 69 | 7 | 7 | 100 % (pocos casos) |
| 70 a 84 | 14 | 14 | 100 % |
| 85 a 100 | 700 | 700 | 100 % |

La tabla muestra por qué el puntaje no debe leerse como probabilidad: con puntajes por debajo de 50, el primer
candidato fue correcto 14 de 16 veces. Con menos de 10 casos en un tramo, la pantalla advierte «pocos casos».

## 3. Cobranza

El agente de cobranza prepara un recordatorio por cliente con cartera vencida y elige tono y momento.

- Segmentos: tipo de cliente (persona o empresa) por historial de pago (sin historial, puntual, con retraso, con
  mucho retraso).
- Estrategias: 9 por segmento, tono (cordial, formal, firme) por momento (3, 10 o 21 días de mora).
- Elección: bandido bayesiano con muestreo de Thompson. Cada estrategia tiene una creencia Beta que parte de un
  criterio inicial suave y se actualiza con cada recordatorio evaluado. La elección es estable dentro de la semana.
  Es un método estadístico de asignación adaptativa.

Tres reglas gobiernan lo que se aprende y lo que se dice:

- Preparado no es enviado. Un recordatorio aprobado para un cliente sin canal de contacto queda solo como mensaje
  preparado y no entra al aprendizaje. Solo cuentan los que tuvieron un envío registrado, que en esta demostración
  es simulado y queda marcado así.
- Ventana. El resultado se evalúa únicamente al cumplirse 10 días desde el envío: hubo pago, hubo abono o no hubo
  pago dentro de la ventana. Antes de eso el intento sigue abierto, aunque el cliente ya haya pagado, y un pago
  posterior a la ventana no cuenta.
- Sin lectura causal. La pantalla dice «hubo pago en la ventana», no «funcionó». Como referencia muestra la tasa de
  pago, en una ventana igual, de cuotas comparables que no tuvieron recordatorio. Esa referencia tampoco es un grupo
  de control.

Resultado en este escenario: 56 recordatorios con envío simulado, todos evaluados; en 26 hubo pago o abono dentro de
la ventana (18 pagos y 8 abonos) y en 30 no. Entre las cuotas comparables sin recordatorio, 147 de 278 tuvieron
pago en una ventana igual.

| Segmento | Con recordatorio: pago en ventana | Sin recordatorio (referencia) |
|---|---|---|
| Empresa que paga con retraso | 18 de 21 | 117 de 125 |
| Empresa que paga con mucho retraso | 5 de 16 | 0 de 81 |
| Persona que paga con retraso | 1 de 6 | 30 de 32 |
| Persona que paga con mucho retraso | 2 de 8 | 0 de 40 |
| Empresa que paga puntual | 0 de 3 | sin datos |
| Persona que paga puntual | 0 de 2 | sin datos |

Estas cifras no permiten concluir nada sobre el efecto de un recordatorio. En dos segmentos la tasa con
recordatorio es menor que la de referencia y en otros dos es mayor; los grupos no son comparables y los tamaños son
pequeños. Además, el generador del escenario adelanta algunos pagos después de un recordatorio con una probabilidad
fija, de modo que cualquier asociación observada es una propiedad del escenario sintético y no un hallazgo.

## Alcance de estos resultados

Estos resultados demuestran funcionamiento y metodología sobre datos sintéticos; no prueban eficacia en una empresa
real. Las series del escenario se generaron con patrones conocidos (estables, estacionales, con tendencia,
intermitentes) y ruido controlado. Con datos reales cambiarían los métodos elegidos, los errores y la cobertura, y
habría que repetir la evaluación completa antes de usar cualquier sugerencia.
