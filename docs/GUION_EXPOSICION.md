# Guion de exposición

Recorrido de siete pasos pensado para cinco a siete minutos. La aplicación trae el mismo guion en Inteligencia →
Recorrido guiado: al iniciarlo, una barra acompaña en cada pantalla con el paso actual, qué mostrar y el botón
«Siguiente», que abre directamente el registro preparado de cada paso.

Los nombres y cifras de este guion corresponden al escenario generado con fecha 18/11/2026 y semilla 4711. Si se
reinicia con otra fecha cambian números de documento, cantidades y valores; la estructura del recorrido es la
misma.

## Antes de empezar

- Frase de presentación: «Desarrollamos una aplicación a partir del trabajo con una empresa para integrar
  información operativa, anticipar necesidades de abastecimiento y asistir la ejecución de procesos mediante
  modelos predictivos y agentes supervisados. Esta demostración utiliza un caso empresarial y datos sintéticos.»
- Perfil. El recorrido completo se puede hacer con el perfil Dirección, que tiene acceso a todos los pasos. Entre
  con un clic en «Dirección» en la pantalla de ingreso. Si prefiere mostrar que cada área ve lo suyo, cambie de
  perfil desde el menú de la persona (arriba a la derecha) en los pasos indicados; el recorrido conserva el paso al
  cambiar de perfil.
- Inicio. Inteligencia → Recorrido guiado → «Iniciar el recorrido».

## Los siete pasos

### 1. El problema y el tablero general

Perfil: cualquiera (sugerido: Dirección).

Qué decir: «Una distribuidora compra, almacena, vende y cobra con información repartida en varias áreas. Aquí todo
queda en un solo lugar: qué necesita atención, qué puede ocurrir y qué se recomienda hacer. El mapa muestra el
recorrido de la solicitud al cobro y dónde se está acumulando el trabajo.»

Qué pulsar: el recorrido abre el Resumen. Señale los bloques «Qué necesita atención», «Qué puede ocurrir» y «Qué
se recomienda», y el indicador «Demostración con datos sintéticos». Luego «Abrir el mapa de flujo» y una estación
(por ejemplo, Inventario) para ver sus registros.

### 2. Una referencia con riesgo de faltante

Perfil: Inventario, Contabilidad, Administración o Dirección (sugerido: Inventario).

Qué decir: «Esta referencia sale rápido y lo disponible no alcanza hasta que llegue un nuevo pedido. El pronóstico
usa las salidas semanales registradas y muestra su incertidumbre; con el tiempo de entrega del proveedor se calcula
cuándo y cuánto pedir.»

Qué pulsar: «Siguiente». Se abre el pronóstico de «Aflojatodo multiuso 400 ml»: 110 unidades disponibles, unas 137
por semana, cobertura de 5 días frente a 5 días de entrega, estado «Pedir ya». Señale el gráfico (salidas,
pronóstico y banda), el método elegido con su error de prueba y la cobertura medida del intervalo. «Ver el cálculo
paso a paso» despliega los supuestos.

### 3. El agente de reposición propone un pedido

Perfil: Inventario, Administración o Dirección (sugerido: Inventario).

Qué decir: «El agente no decide solo: deja una propuesta con los datos que consultó. Al aprobarla, la plataforma
vuelve a calcular con la información de este momento. Si algo cambió, lo dice. El pedido al proveedor es simulado;
nada sale de la plataforma.»

Qué pulsar: «Siguiente». Se abre la propuesta «Preparar pedido a Químicos y Lubricantes del Valle S.A.S.» con tres
referencias. Muestre la franja de seis pasos (detectar, analizar, proponer, revisar, ejecutar, registrar) y las
evidencias. Pulse «Aprobar y ejecutar». El resultado indica el número del pedido preparado; ábralo con el enlace del
resultado y pulse «Registrar envío simulado».

### 4. Recibir la mercancía

Perfil: Recepción, Inventario, Administración o Dirección (sugerido: Recepción).

Qué decir: «Cuando llega la mercancía se recibe contra la factura. Cada código suma unidades a su lote con su fecha
de vencimiento. Al confirmar, las existencias cambian y el lote queda con su trazabilidad: de qué factura vino y
cada movimiento que tenga después.»

Qué pulsar: «Siguiente». Se abre la recepción de la factura preparada (cinco cajas, 120 unidades). Los códigos de
prueba están en el paso 4 de la página del recorrido («Códigos de prueba para teclear (sin cámara)»), en la página
Documentos de demostración y en `documentos_demo/codigos_recepcion.txt`. Pegue cada código en el campo y pulse
«Agregar»; el avance por línea se actualiza. Luego «Confirmar recepción». Para la trazabilidad: Inventario →
Existencias por lote y abra uno de los lotes recién recibidos.

Sugerencia: tenga los cinco códigos abiertos en un editor de texto para copiarlos sin cambiar de página.

### 5. Una conciliación sugerida

Perfil: Contabilidad, Administración o Dirección (sugerido: Contabilidad).

Qué decir: «El extracto del banco trae un ingreso. La plataforma lo cruza con la cartera y sugiere a qué cliente y a
qué facturas corresponde, con sus razones. Al confirmarlo, el saldo del cliente baja y queda un recibo en el
sistema contable simulado.»

Qué pulsar: «Siguiente». Se abre la propuesta del agente de conciliación para el ingreso de $384.370 cuyo texto
menciona la factura FE 2123. Señale las razones y el reparto, y pulse «Aprobar y ejecutar». El resultado dice a qué
factura se aplicó. Si hay tiempo, en Finanzas → Banco muestre el ingreso ambiguo de $623.322: su valor coincide con
el saldo de dos clientes, no trae referencia y por eso el agente no lo propone.

### 6. El copiloto responde con fuentes

Perfil: cualquiera.

Qué decir: «El copiloto responde preguntas con los datos de la plataforma y con los permisos de quien pregunta. Cada
respuesta enumera los registros que consultó, para comprobarla en un clic. Si no tiene el dato, lo dice en vez de
suponerlo.»

Qué pulsar: «Siguiente». Pulse la pregunta sugerida «¿Qué necesita atención hoy y por qué?». Señale el distintivo
«Asistente local · consultas y plantillas», el bloque de fuentes y periodo y las limitaciones, y abra un enlace de
la respuesta. Aclare que en esta demostración el copiloto es un asistente de consultas con reglas, no un modelo de
lenguaje.

Si quiere mostrar la propuesta de compra con presupuesto, use una cifra mayor que lo ya comprometido en pedidos
abiertos (ver [COPILOTO.md](COPILOTO.md)); con una cifra baja la respuesta correcta es que no entra ninguna
referencia.

### 7. Las decisiones y lo que aprende el sistema

Perfil: cualquiera que vea agentes (sugerido: Dirección).

Qué decir: «Todo lo aprobado, descartado o que dejó de aplicar queda registrado con su resultado. Con esas
decisiones y con lo que pasa después, los modelos ajustan sus pronósticos y sugerencias. Son resultados de
funcionamiento sobre datos sintéticos, no de una operación real.»

Qué pulsar: «Siguiente». En el Historial de decisiones aparecen arriba las dos aprobaciones que acaba de hacer, con
quién, cuándo y qué se ejecutó. Luego Inteligencia → Lo aprendido (evaluación de los tres aprendizajes en lenguaje
llano) y, si el evaluador quiere detalle, Inteligencia → Metodología.

Cierre: «Terminar» en la barra del recorrido.

## Si algo falla

- Un paso abre la pantalla general y no el registro preparado. El caso ya se usó (los pasos 3, 4 y 5 consumen su
  caso al aprobar o confirmar) o el escenario se generó para otra fecha. Si ya se usó, restaure el escenario
  inicial (ver «Entre una exposición y la siguiente»). Mientras tanto el recorrido sigue funcionando: en el paso 3
  abre otra propuesta abierta de reposición si la hay; en los demás, explique el paso sobre la pantalla del módulo.
- Una propuesta responde «Ya no aplica». Es el comportamiento esperado cuando la situación cambió; sirve para
  explicar la revalidación al aprobar.
- «Su perfil puede verla, pero no decidirla» o un paso que pide otro perfil. Cambie de perfil desde el menú de la
  persona y continúe; el recorrido no cambia permisos.
- La página no carga. Compruebe que la ventana de `INICIAR_FERIA.ps1` sigue abierta y que la dirección es
  http://127.0.0.1:8790/.
- La restauración dice que falta la copia o que la huella no coincide. No cambia nada más: detenga el servidor con
  `Ctrl+C`, ejecute `.\REINICIAR_FERIA.ps1` y vuelva a iniciarlo. El reinicio tarda unos minutos (cerca de tres y
  medio en el equipo de preparación) y deja exactamente los mismos casos si la fecha y la semilla son las mismas.

Mientras el servidor está encendido los agentes revisan su turno cada minuto y pueden actualizar propuestas. Si
quiere que el escenario no cambie por su cuenta durante la exposición, inicie el servidor con esa revisión apagada
(`$env:FERIA_AGENTS = '0'` antes de `.\INICIAR_FERIA.ps1`, en la misma ventana); el botón «Revisar ahora» de cada
agente sigue funcionando.

## Entre una exposición y la siguiente

Los pasos 3, 4 y 5 consumen su caso: la propuesta de reposición queda aprobada, la factura queda recibida y el
ingreso queda aplicado. Para dejar todo como al comienzo:

```
.\RESTAURAR_FERIA.ps1
```

- Tarda unos segundos. No genera el escenario de nuevo: copia sobre la base la versión guardada al final del último
  reinicio y comprueba que su huella coincida con la de esa copia.
- No hace falta detener el servidor. Ejecútelo en otra ventana de PowerShell, sin cerrar la de
  `INICIAR_FERIA.ps1`.
- Las sesiones abiertas se cierran. En el navegador vuelva a la pantalla de ingreso y entre de nuevo con un clic
  en el perfil; después inicie otra vez el recorrido.
- Se descarta todo lo hecho desde el último reinicio, no solo lo del recorrido: pedidos, recepciones, ventas,
  preguntas al copiloto y documentos cargados.
- Es una operación de consola. La aplicación no tiene un botón para restaurar.

Cada día de feria se empieza con `.\REINICIAR_FERIA.ps1`, con el servidor detenido, y no con la restauración. La
copia inicial corresponde al día en que se generó el escenario, y los casos (vencimientos, pronto pago, cartera,
turnos de los agentes) dependen de la fecha. Si se restaura una copia de otro día, el comando lo advierte.

## Preguntas frecuentes de un evaluador

¿Los datos son de una empresa real?
No. Todo el escenario es sintético y se genera con un programa a partir de una semilla. El caso (una distribuidora
de insumos) es ilustrativo.

¿Entonces qué demuestra?
Que el flujo completo funciona de extremo a extremo con reglas coherentes, y que los modelos se evalúan con un
método defendible. No demuestra que reduzca faltantes ni costos en una operación real.

¿El modelo predictivo es mejor que un promedio?
En este escenario, poco. El modelo entrenado (una regresión ridge global) fue elegido en 2 de 30 referencias; en 17
ganó el promedio de todo el historial. El procedimiento de elegir un método por referencia tuvo en la ventana de
prueba un error 18,6 % menor que usar siempre el promedio. Son series sintéticas: con datos reales el resultado
sería otro.

¿Cómo evitan engañarse con la evaluación?
El método se elige en una ventana de selección y el error que se informa es el de una ventana de prueba posterior
que no se usó para elegir. Las semanas sin existencias se excluyen y la demanda no atendida se muestra aparte.

¿El intervalo del 80 % cubre el 80 %?
No. Medido en prueba cubre el 68,8 % a una semana y el 64,1 % al horizonte de la decisión. La pantalla muestra la
cobertura medida, no la nominal.

¿Qué parte es inteligencia artificial?
Hay dos modelos entrenados (la regresión del pronóstico y el modelo de identificación de pagos) y métodos
estadísticos. Los agentes son reglas con aprobación humana. No hay IA generativa en uso: el copiloto funciona por
defecto con reglas y plantillas, y su modo generativo es opcional y no se verificó en vivo.

¿El modelo de pagos mejora la conciliación?
En este escenario no se observa: acierta 735 de 737 casos, lo mismo que las reglas solas. Lo que se muestra es el
mecanismo de evaluación y corrección.

¿Los recordatorios de cobro funcionan?
No se puede afirmar. La plataforma registra si hubo pago en los 10 días siguientes al envío y lo compara con cuotas
similares sin recordatorio, pero eso es una coincidencia en el tiempo, no una prueba de causa. Además, los envíos
son simulados.

¿Los agentes actúan solos?
Solo tres, y ninguno mueve inventario ni dinero: el que reintenta envíos al sistema contable con la misma clave, el
de avisos internos y el del resumen diario. Lo demás requiere aprobación de un perfil autorizado.

¿Qué pasa si alguien aprueba dos veces o dos personas a la vez?
La segunda aprobación no tiene efecto: la propuesta ya está decidida y la operación lleva una clave que impide
duplicarla.

¿Se conecta con el sistema contable, el banco o el correo?
No. Los tres son simuladores locales identificados como tales. Conectarlos a sistemas reales no está incluido.

¿Cómo saben que los saldos cuadran?
`verificar_escenario` hace 54 comprobaciones de coherencia sobre la base (lotes contra movimientos, facturas contra
pagos, cartera contra el simulador contable, banco) y las pruebas automáticas cubren las reglas.

¿Qué faltaría para usarlo en una empresa?
Integraciones reales, carga y validación de datos propios, repetir la evaluación de los modelos con esos datos,
revisión de seguridad, despliegue en un servidor y un periodo de prueba supervisado. Nada de eso forma parte de
esta demostración.

## Lista de comprobación antes de la feria

El mismo día:

- [ ] Reiniciar el escenario ese día (`.\REINICIAR_FERIA.ps1`, con el servidor detenido), para que los casos queden
      preparados para la fecha de la exposición, y confirmar que la verificación termina sin fallas.
- [ ] Confirmar que el reinicio dejó la copia inicial: su salida la menciona y existe
      `datos/escenario_inicial.sqlite3`.
- [ ] Iniciar el servidor (`.\INICIAR_FERIA.ps1`) y dejar su ventana abierta.
- [ ] Desconectar el equipo de internet y hacer el recorrido completo una vez: todo debe funcionar sin conexión.
- [ ] Restaurar después de ese ensayo (`.\RESTAURAR_FERIA.ps1`), porque consume los casos de los pasos 3, 4 y 5, y
      comprobar que termina con «Listo para la siguiente demostración».
- [ ] Entrar con un clic como Dirección y comprobar que la pantalla de ingreso muestra los perfiles.
- [ ] Tener a mano `documentos_demo/`: los códigos de recepción abiertos en un editor y, si se va a mostrar la
      carga de documentos, las facturas y el extracto.
- [ ] Confirmar que el copiloto muestra «Asistente local · consultas y plantillas».
- [ ] Dejar abierta una segunda ventana de PowerShell en la carpeta del proyecto para restaurar entre
      exposiciones.

Del equipo:

- [ ] Portátil conectado a la corriente, suspensión automática desactivada.
- [ ] Navegador basado en Chromium, ventana maximizada, zoom legible en el proyector.
- [ ] Este guion y `datos/ACCESOS_DEMO.md` accesibles para quien expone. Ese archivo de claves no se muestra al
      público ni se comparte.
