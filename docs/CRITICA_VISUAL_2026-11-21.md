# Crítica visual · ACM Systems · `/presentacion/`

El 21 de noviembre de 2026 se revisaron las pantallas `/presentacion/` e `/ingresar/` en Chromium con Playwright. Se comprobaron vistas de 1440 × 900, 390 × 844, 1366 × 768, 1440 × 780, 1280 × 720, 768 × 1024 y 390 × 664.

La revisión se basó en capturas, medidas con `getBoundingClientRect`, estilos calculados y mensajes de la consola del navegador.

En los nombres de las capturas, `d` identifica las de 1440 × 900, `m` las de 390 × 844 y `x` las de otros tamaños.

## Veredicto

La página está muy por encima de una landing genérica: la maqueta, la unidad que cambia de estado y la paleta con significado salen del producto y no de una plantilla. El titular y la acción principal están en el primer pantallazo en todos los tamaños probados. Lo que falla es la ejecución en tres puntos concretos: el acceso no se puede usar por debajo de 981 px, la portada no deja ver ni el núcleo ni la unidad (los dos protagonistas del concepto) y el selector del recorrido se corta en ventanas de portátil corrientes.

## Criterios visuales de salida

| Criterio | Estado | Nota |
| --- | --- | --- |
| Identidad explicable desde el negocio | Resuelto | Circuito, unidad y colores (menta sistema, ámbar decisión, coral faltante) son propios de este producto |
| Jerarquía: oferta y acción en el primer pantallazo | Resuelto | Titular y «Entrar a la demostración» visibles a 1440 × 900, 1366 × 768 y 390 × 844 |
| Recurso protagonista: calidad, recorte, tamaño | Corregible | Núcleo tapado, cuatro rótulos ilegibles o cortados, unidad casi imperceptible en la portada (problema 2) |
| Composición con ritmo deliberado | Corregible | Controles del recorrido cortados entre 761 y 818 px de alto (problema 3); titulares con líneas huérfanas |
| Movimiento con motivo y estado final | Resuelto en lo visto | Scroll nativo, botón que desplaza, reproducción guiada, scroll inverso sincronizado y cortes con movimiento reducido. Fluidez (fps) no medida |
| Móvil conserva intención y utilidad | Corregible | El recorrido móvil funciona bien; la maqueta de portada queda en miniatura y el acceso está bloqueado (problema 1) |
| Coherencia en pie, formularios y secciones posteriores | Corregible | Pie y cierre sólidos; el acceso antepone usuario y contraseña a los perfiles de un clic |

## Tres problemas prioritarios

### 1. [P0] En `/ingresar/`, por debajo de 981 px, el panel de marca tapa el formulario y los perfiles

- **Dónde:** `/ingresar/` a 390 × 844 y a 768 × 1024 (cualquier ancho ≤ 980 px). Es el destino de todos los botones «Entrar a la demostración» y «Abrir demo».
- **Capturas:** `acm-critica-m15-ingresar-390.png` (carga) y `acm-critica-m16-ingresar-scroll1000-390.png` (tras desplazar 1000 px: la pantalla es idéntica).
- **Lo medido:** `.auth-intro` queda con `display: grid`, `position: sticky`, `top: 0` y alto igual al de la ventana (844 px). El campo «Usuario» está en y = 121 px de la ventana con scroll 1000, pero `document.elementFromPoint` devuelve un elemento de `.auth-intro` en los cinco puntos de scroll probados (0, 400, 900, 1300 y 1510, que es el máximo). El formulario y los ocho perfiles nunca se ven ni se pueden tocar. A 1440 px el acceso funciona.
- **Causa probable:** `static/app.css` línea 997 oculta el panel en móvil (`@media (max-width: 980px) { .auth-intro { display: none; } }`), pero la línea 1220, añadida dentro de `@media screen` (línea 1182) para la continuidad con la portada, vuelve a declarar `.auth-intro { position: sticky; display: grid; height: 100dvh; … }` sin condición de ancho. Misma especificidad y posterior en el archivo: gana.
- **Cambio propuesto:** limitar las reglas de las líneas 1220 a 1225 a `@media screen and (min-width: 981px)`, o añadir a continuación `@media screen and (max-width: 980px) { .auth-intro { display: none; } }`. La marca de `.auth-card .brand` ya está prevista para ese ancho (línea 998).
- **Criterio de cierre:** a 390 × 844 y 768 × 1024, al cargar `/ingresar/` se ven «Iniciar sesión» y el primer perfil sin nada encima; `elementFromPoint` en el centro de la pantalla devuelve un elemento dentro de `.auth-panel`; un toque en un perfil entra a la demostración.
- **Nota de alcance:** el archivo está fuera de los cuatro de la portada; lo incluyo porque corta la acción principal de la página revisada.

### 2. [P1] La portada no deja ver ni el núcleo ni la unidad, y cuatro de los seis rótulos no se leen

- **Dónde:** portada (`#inicio`), maqueta 3D y póster, a 1440 × 900, 1366 × 768 y 390 × 844. Afecta también al recorrido en Solicitud, Inventario y Compra.
- **Capturas:** `acm-critica-d01-hero-1440.png`, `acm-critica-d02-hero-1440-b.png`, `acm-critica-d24-teclado-cta-1440.png`, `acm-critica-x01-hero-1366x768.png`, `acm-critica-d27-hero-sin3d-1440.png` (póster), `acm-critica-d05-recorrido-3-compra-1440.png`.
- **Lo que se ve:**
  - El titular dice «Seis áreas. Una sola operación.», pero el núcleo con la marca (la «sola operación») queda tapado a la izquierda por la planilla de Solicitud y a la derecha por la estantería de Inventario. Solo asoma la franja central de la marca; los radios no se distinguen.
  - De los seis rótulos del suelo solo «01 Solicitud» y «02 Inventario» se leen. «05 Venta» y «04 Recepción» aparecen invertidos, «06 Cobro» en vertical y «03 Compra» cortado por el borde derecho de la ventana.
  - La unidad que recorre el circuito es un contorno de líneas de 1 px y unos 65 px de lado. En la portada no se percibe sin buscarla, y la frase «Sigue una unidad por las seis áreas» no encuentra a qué referirse. En Compra, su contorno ámbar se confunde con los cinco contornos ámbar de la estiba.
  - Los dos discos delanteros se ven vacíos y los objetos se amontonan hacia el centro.
- **Causa probable (`static/modulos/escena3d.js`):**
  - Los objetos de cada estación se colocan hacia el interior del disco (z local de −0,6 a −1,0, escala 1,2; líneas 107 a 132 y 467) y miden hasta unas 2,4 unidades de alto. La cámara de portada mira con unos 30° de elevación (`vB.set(0, 8.3 * dHero, 14.5 * dHero)`, línea 498), así que las dos estaciones delanteras proyectan su altura sobre el núcleo, cuya placa está a y = 0,62 (línea 315).
  - Los rótulos son planos fijos orientados hacia fuera de cada estación (líneas 457 y 458): correctos cuando la cámara se sitúa frente a su estación en el recorrido, invertidos en la vista general para las cuatro estaciones traseras y laterales.
  - El encuadre de portada sangra a la derecha: `.acm-poster { right: max(-5vw, -72px) }` (`presentacion.css` línea 85) deja la caja del póster entre 533 y 1512 px en una ventana de 1440, y `span: 0.92 * box.width / view.width` (`presentacion.js` línea 81) lleva el vértice derecho fuera.
  - La unidad se dibuja con `THREE.LineSegments` y `LineBasicMaterial` (líneas 330 a 333): WebGL no engrosa esas líneas, y el relleno es de opacidad 0,1.
- **Cambio propuesto (para quien lleve la escena; no cambia la dirección):**
  1. Núcleo: que sea lo más alto o lo más despejado de la maqueta. Dos vías: elevar la placa de marca sobre un pedestal (y ≈ 2,2 a 2,6) e inclinarla hacia la cámara en la vista general, o subir la cámara de portada a unos 42 a 45° (por ejemplo `vB` ≈ `(0, 11.5 * d, 12 * d)`). En ambos casos, comprobar que los seis radios se ven.
  2. Rótulos: orientarlos según la cámara (girar 180° los de las estaciones cuyo vector hacia fuera se aleja de la cámara) y cerrar el encuadre para que la placa entera quepa (`right: 0` o `span` ≈ 0,84).
  3. Unidad: darle cuerpo en la vista general. Aristas con geometría (cajas finas o `Line2`, grosor ≥ 0,03), relleno ≥ 0,3 con emisión, un halo que la siga y escala de 1,3 a cerca de 1,7. En Compra, atenuar los contornos de la estiba para que la unidad sea el más marcado.
  4. Regenerar `static/presentacion/maqueta.webp` con el nuevo encuadre.
- **Criterio de cierre:** en capturas a 1440 × 900 y 1366 × 768 tomadas en cada una de las seis paradas del ciclo automático: (a) la placa de marca del núcleo se ve completa, sin ningún objeto encima; (b) los seis rótulos se leen de izquierda a derecha, ninguno invertido ni cortado por la ventana; (c) la unidad se localiza de un vistazo en cada captura y en Compra se distingue de la estiba; (d) el póster de `?3d=0` cumple lo mismo.

### 3. [P1] El selector de etapas y «Reproducir recorrido» se cortan en ventanas de 761 a 818 px de alto

- **Dónde:** sección «El recorrido» (`#funciona`) fijada, anchos de escritorio (≥ 961 px).
- **Capturas:** `acm-critica-x02-recorrido-inventario-1366x768.png` y `acm-critica-x03-recorrido-recepcion-1440x780.png` (cortado); `acm-critica-d04-recorrido-2-inventario-1440.png` (900 px, correcto) y `acm-critica-x04-recorrido-inventario-1280x720.png` (720 px, correcto).
- **Lo medido:** el borde inferior de `.acm-journey-controls` queda en 783 px con una ventana de 768, y en 793 px con una de 780. Los nombres de las seis etapas y el botón de reproducción salen partidos por el borde. A 900 px termina en 874 (cabe) y a 720 px en 694 (cabe, porque ya actúa el modo compacto). El contenido necesita unos 819 px: 102 de margen superior + 216 de titular + 359 de tarjeta + 80 de controles + 26 de margen inferior + dos separaciones de 18.
- **Por qué importa:** 1366 × 768 es una resolución de portátil habitual, y un navegador sin pantalla completa en una pantalla de 1440 × 900 deja una ventana de esa franja. Es la sección firma y pierde justo el control con el que se maneja.
- **Causa probable (`static/modulos/presentacion.css`):** el modo compacto solo se activa con `@media (max-height: 760px)` (líneas 496 a 499), 58 px por debajo de lo que el contenido necesita. `.acm-journey-pin` tiene alto fijo `var(--vh)` (línea 132) y el titular escala con el ancho, no con el alto (línea 135).
- **Cambio propuesto:** subir el umbral del modo compacto a `max-height: 840px` (ocultar ahí `.acm-journey-head p` y el enlace de la tarjeta), hacer que el titular dependa también del alto (`clamp(1.9rem, min(3.5vw, 6vh), 3.3rem)`), reducir el relleno de la tarjeta en ese tramo y declarar la fila central como `minmax(0, 1fr)` en la línea 133.
- **Criterio de cierre:** a 1440 × 900, 1440 × 780, 1366 × 768, 1280 × 720 y 1024 × 768, en las seis estaciones, el borde inferior de `.acm-journey-controls` queda al menos 12 px por encima del borde de la ventana y se leen completos los seis nombres y el botón.

## Problemas secundarios

1. **Vista «Pronóstico» (`#preview-pronostico`, 1440 y 390):** el gráfico `.acm-mini-chart` (viewBox 520 × 168 en una caja de 710 × 178) deja 80 px vacíos a cada lado y la leyenda no coincide con sus extremos; en móvil el hueco queda arriba y abajo. Además es la única de las tres vistas sin nada que manipular, ni eje, ni unidades, ni banda de error. Capturas `d12` y `m08`.
2. **Laboratorio (`#simulador`, consumo 10 y entrega 1):** la leyenda «En stock» pinta una sola caja (20 ud.) mientras la cabecera dice «120 unidades en stock»; las otras cinco son «Holgura» en contorno hueco y parecen ausentes. La línea de semanas, que es lo más informativo, mide 307 × 22 px con un vacío encima. Captura `d18`.
3. **Titulares con salto forzado (`<br>`) en columnas estrechas:** «¿Y si la / demanda / sube mañana?» (`#simulator-title`, 1440), «El siguiente / paso / es verlo / funcionar.» (`#close-title`, 1440, cuatro líneas y 435 px) y «La confianza / también / se construye con / registros.» (`#trust-title`, 390). Capturas `d16`, `d21`, `m13`.
4. **Enlace «Saltar al contenido» invisible al recibir foco:** texto `rgb(21, 38, 38)` sobre fondo `rgb(24, 43, 43)`, porque `.pres a { color: inherit; }` (`presentacion.css` línea 25) anula el `color: #fff` de `.skip-link` (`app.css` línea 108). Captura `d23`.
5. **Maqueta de portada en móvil (390 × 844):** se dibuja a 350 px de ancho (unos 300 × 190 px útiles) porque `img, svg { max-width: 100%; }` (`app.css` línea 89) anula el `width: min(118%, 760px)` de `.acm-poster`; rótulos y unidad son ilegibles. Captura `m01`.
6. **`/ingresar/` a 1440:** lo primero es «Iniciar sesión» con usuario y contraseña; los perfiles de un clic que promete la portada («Sin registro») empiezan en y = 480 px y cinco de ocho quedan bajo el pliegue. Cambia además el trato de «tú» a «Ingrese». Captura `d29`.

## Qué conservar

- **El bloque tipográfico de la portada:** titular condensado a 119,5 px, entradilla y acción principal con anillo de foco ámbar bien visible. Oferta y acción caben en el primer pantallazo en los tres tamaños probados.
- **Los primeros planos de Recepción, Venta y Cobro:** la caja con «LOTE SL-041» bajo el escáner, la caja sellada sobre el mostrador y el banco con las monedas explican el cambio de estado sin texto. La línea «La unidad: …» de cada tarjeta y el tinte ámbar en Compra y Cobro son coherentes con el significado de la paleta.
- **La mecánica del recorrido:** scroll nativo con sección fijada, botones que desplazan, reproducción guiada (unos 3,6 s por parada, termina en «Repetir recorrido»), scroll inverso sincronizado, cortes con movimiento reducido y alternativa completa con `?3d=0`. Consola sin errores y sin desborde horizontal a 1440 ni a 390.
- **La vista «Agentes supervisados»:** situación, motivo, revisión humana y resultado, con aprobar, rechazar y reiniciar. Es la mejor explicación de «el sistema propone, tú decides» de toda la página.
- **El laboratorio en los extremos y el recorrido en móvil:** con consumo 100 y entrega 6, la franja coral rayada, «Llega el pedido» y el mensaje se entienden de un vistazo. En 390 px, maqueta arriba, tarjeta abajo y selector al alcance del pulgar funcionan.
- **La honestidad del rotulado:** «Ejemplo local», «datos sintéticos», «no genera pedidos». No hay testimonios ni cifras inventadas.

## Otras observaciones

- Inventario por lote: antes de despachar, las tres barras están al 100 % y los lotes de 48, 120 y 80 ud. parecen iguales (cada barra se mide contra su propio lote).
- La ventana de ejemplo cambia de alto según la vista (619, 586 y 557 px a 1440) y mueve lo que hay debajo.
- Al llegar por el ancla `#incluye` a 1440 × 900, «Despachar 60 unidades» queda bajo el pliegue (la ventana empieza en y = 395 y mide 619).
- Laboratorio en móvil: entre el primer deslizador y el final del mensaje de resultado hay 835 px, con 780 útiles; el mensaje queda fuera mientras se arrastra (`m10`).
- Selector del recorrido en móvil: rótulos de 12,8 px en botones de 41 a 59 px de ancho, sin separación; se leen como una frase corrida. «Venta» y «Cobro» miden 41 px.
- Recorrido en móvil: el titular cae sobre la placa luminosa del núcleo y pierde contraste (390 × 664 en Recepción, `x05`; roza en 390 × 844 en Cobro).
- Inventario en 3D: las cajas se ven casi blancas y el lote ámbar que vence primero y el hueco coral apenas se distinguen del resto (`d04`).
- Recepción: el poste del escáner tapa la etiqueta del lote; con movimiento reducido queda así de forma permanente (`d06`, `d26`).
- Registros (`#comprobar`): el párrafo de cabecera arranca en x = 827 y el texto de las filas en x = 648; son tres filas de icono, título y texto que hablan de registros sin enseñar ninguno.
- «Resultado simulado» pendiente se muestra al 50 % de opacidad (contraste no medido).
- El botón de cabecera dice «Abrir demo» pero su `aria-label` es «Entrar a la demostración».
- El póster y la escena viva no coinciden del todo en encuadre (`d27` frente a `d01`); puede notarse un salto al sustituirse.
- «Quiénes somos» se muestra sin lista de integrantes.
- Texto: «El lote que vence primero, sale primero.» lleva coma entre sujeto y verbo.

## Puntuación heurística (Nielsen), solo sobre lo recorrido

| # | Heurística | Nota | Motivo |
| --- | --- | --- | --- |
| 1 | Visibilidad del estado | 3 | Paso N de 6, barra de progreso, etiquetas de estado y mensajes `role="status"` |
| 2 | Lenguaje del usuario | 3 | Claro en general; «FEFO» y «Holgura» piden explicación |
| 3 | Control y libertad | 2 | Reinicio en cada ejemplo y pausa del recorrido; el acceso bloqueado en ≤ 980 px no tiene salida |
| 4 | Consistencia | 2 | «Abrir demo» y «Entrar a la demostración», tú y usted, leyenda «En stock» contra la cabecera |
| 5 | Prevención de errores | 3 | Ejemplos rotulados como locales, botones deshabilitados tras actuar |
| 6 | Reconocer antes que recordar | 3 | Todo visible; el selector queda cortado en la franja del problema 3 |
| 7 | Flexibilidad y eficiencia | n/a | Página de persuasión |
| 8 | Diseño estético y sobrio | 3 | Portada amontonada en el centro de la maqueta; vacío en el panel del laboratorio |
| 9 | Recuperación de errores | n/a | No se recorrió ningún estado de error |
| 10 | Ayuda | 3 | Preguntas claras y «Cómo se calcula este ejemplo» |
| | **Total** | **22/32** | 69 %: aceptable, en el límite de bueno; lo bajan el acceso móvil y el selector cortado |

## Lo que no se verificó

Rendimiento y fluidez de la maqueta (sin medición), dispositivos táctiles reales, Safari y Firefox, lector de pantalla, pérdida de contexto WebGL, impresión y el alto de 1000 px.

## Preguntas que podrían cambiar la dirección

1. ¿Los visitantes de la feria abrirán la página en su teléfono (QR o red local) o solo en el equipo del puesto? De eso depende la urgencia del problema 1 y de la maqueta móvil.
2. ¿Con qué resolución y en qué modo se mostrará en el puesto (pantalla completa o ventana de navegador)? Define los altos que debe cubrir el problema 3.
3. ¿El núcleo debe ser el protagonista de la portada, más alto y más luminoso que las estaciones? La dirección lo describe como «el sistema único», pero hoy es lo menos visible; elevarlo cambia la silueta de la maqueta y conviene decidirlo antes de tocar la escena.
4. ¿Faltan por cargar los nombres del equipo en «Quiénes somos», o la sección debe quedar sin personas?

## Verificación posterior (la rellena el crítico tras las correcciones)

| Problema | Resuelto / no resuelto | Captura |
| --- | --- | --- |
| 1. Acceso tapado en ≤ 980 px | | |
| 2. Núcleo, rótulos y unidad en la portada | | |
| 3. Controles del recorrido cortados | | |

---

## Estado de las correcciones (21 de noviembre de 2026, después de esta crítica)

Las capturas `acm-critica-*` citadas arriba eran archivos de trabajo del revisor y muestran la versión anterior a
las correcciones; no se incluyen en el paquete. El resultado corregido está en `docs/capturas/acm3d-01` a `acm3d-17`.

| Hallazgo | Corrección aplicada | Comprobación |
|---|---|---|
| P0 · El acceso quedaba tapado por debajo de 981 px | `static/app.css`: la columna de presentación del acceso se oculta en `max-width: 980px` | A 390 × 844 y 768 × 1024 el punto central pertenece a `.auth-panel`, `.auth-intro` tiene `display: none` y un toque en el primer perfil entra al Resumen |
| P1 · Portada sin núcleo, unidad ni rótulos legibles | `escena3d.js`: la marca sube a un mástil y gira hacia la cámara; los rótulos se colocan del lado de la cámara y se leen de izquierda a derecha; la unidad tiene aristas con grosor, relleno y halo, y crece en la vista general; la estiba de Compra es más tenue; cámara de portada más alta; encuadre completo; póster regenerado | Capturas `acm3d-01`, `acm3d-02` a `acm3d-04`, `acm3d-10` y `acm3d-13` |
| P1 · Controles del recorrido cortados en ventanas bajas | `presentacion.css`: modo compacto hasta 860 px de alto, titular dependiente del alto y fila central `minmax(0, 1fr)` | Margen inferior de los controles: 26 px a 1440 × 900 y 16 px a 1440 × 780, 1366 × 768, 1280 × 720 y 1024 × 768 |
| Secundario 1 · Gráfico de pronóstico con huecos | El gráfico conserva su proporción y la leyenda indica unidad y horizonte | Revisión visual; sigue sin controles propios, enlaza al laboratorio |
| Secundario 2 · Laboratorio en mínimos | El stock se dibuja siempre como cajas llenas; el sobrante es «Stock de sobra», en un tono más oscuro; línea de semanas más alta | Consumo 10 y plazo 1: 1 caja en uso y 5 de sobra |
| Secundario 3 · Titulares partidos con saltos fijos | Se quitaron los saltos de `#simulator-title`, `#trust-title` y `#close-title`; columnas más anchas | Revisión visual a 1440 y 390 px |
| Secundario 4 · «Saltar al contenido» invisible | `.pres .skip-link { color: #fff }` | No se volvió a capturar con foco |
| Secundario 5 · Maqueta pequeña en celular | El póster ya no queda limitado por `max-width: 100%` | Ancho medido: 434 px en una ventana de 390 px |
| Secundario 6 · Perfiles bajo el pliegue y trato de «usted» en el acceso | Sin cambio: el orden y la voz del acceso son de la aplicación | Pendiente de decisión |

Además se corrigió un desborde horizontal de 35 px a 320 px de ancho (marca del pie) y las barras de los lotes ahora
son proporcionales al lote mayor. Las catorce observaciones menores restantes del informe no se abordaron.
