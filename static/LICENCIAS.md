# Licencias de los recursos incluidos en `static/`

Todo se sirve desde este equipo; la plataforma no carga recursos de internet.

## Íconos de interfaz — `iconos.svg`

Los símbolos provienen de la colección **Feather** (Cole Bennett y colaboradores), con licencia MIT. Se
reunieron en un solo archivo de símbolos, se ajustó el grosor del trazo a 1,8 y un símbolo se renombró
(`dollar`). No se añadieron símbolos de otras colecciones.

```
The MIT License (MIT)

Copyright (c) 2013-2023 Cole Bennett

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Ícono de la aplicación — `icono/`

`icono.svg`, `favicon.png`, `app-192.png`, `app-512.png` y `apple-180.png` son un dibujo propio de esta
plataforma (tres barras decrecientes y un punto sobre un cuadrado). No derivan de ninguna marca ni colección.
Los PNG se generan con un guion propio a partir de la misma geometría y no llevan metadatos.

## Tipografías — `fuentes/`

**IBM Plex Sans** (400, 500, 600) e **IBM Plex Sans Condensed** (500, 600), subconjunto latino en formato
WOFF2. Copyright 2019 IBM Corp. Licencia SIL Open Font License 1.1. El texto completo está en
`fuentes/LICENCIA-IBM-Plex-OFL.txt`. «Plex» es un nombre reservado de su titular; los archivos se usan sin
modificar.

## Lector de códigos — `vendor/zxing-0.21.3/`

Biblioteca **ZXing** para JavaScript, versión 0.21.3, usada por la recepción con cámara cuando el navegador no
trae un lector propio. Licencia Apache 2.0. El texto completo está en `vendor/zxing-0.21.3/LICENSE`. El archivo
`zxing.min.js` se distribuye sin modificar.

## Motor 3D de la presentación — `vendor/three-0.187.0/`

Biblioteca **Three.js**, versión 0.187.0 (r187), usada solo por la maqueta 3D de la página de presentación
(`modulos/escena3d.js`). Copyright 2010-2026 three.js authors. Licencia MIT; el texto completo está en
`vendor/three-0.187.0/LICENSE`. Los archivos `three.module.min.js` y `three.core.min.js` son los módulos
`build/three.module.js` y `build/three.core.js` del paquete oficial de npm, reducidos con Terser sin otro
cambio que el nombre del archivo importado; conservan su aviso de licencia. Los `.gz` son las mismas copias
comprimidas, que el servidor entrega a los navegadores que las aceptan.

## Maqueta de la presentación — `modulos/escena3d.js` y `presentacion/maqueta.webp`

La maqueta del circuito (placa, estaciones, unidad y rótulos) es geometría propia generada en el navegador:
no usa modelos, texturas ni entornos de terceros. `presentacion/maqueta.webp` es una captura de esa misma
escena y se muestra cuando el navegador no puede ejecutar WebGL, mientras carga la maqueta y en el acceso.
