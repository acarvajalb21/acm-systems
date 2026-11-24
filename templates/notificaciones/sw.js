/* Service worker de la plataforma: página sin conexión y copia de respaldo de los recursos estáticos. Versión {{ version }}.
   No recibe ni muestra avisos externos: los avisos son internos y se consultan en la pantalla de alertas. */
'use strict';
const CACHE = 'operaciones-{{ version }}';
const OFFLINE = '/sin-conexion/';
const ICON = '{{ icon }}';
const STYLES = '{{ styles }}';

self.addEventListener('install', (event) => {
  // Las páginas van directo a la red sin pasar por el service worker cuando el navegador lo permite: así no se
  // interrumpen las transiciones entre páginas. En los demás navegadores, el manejador de abajo da la página sin conexión.
  if (event.addRoutes) {
    event.waitUntil(event.addRoutes([{ condition: { requestMode: 'navigate' }, source: 'network' }]).catch(() => {}));
  }
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll([OFFLINE, ICON, STYLES])).then(() => self.skipWaiting()));
});

// Sin clients.claim(): el service worker controla las páginas desde la siguiente navegación, para no interrumpir una
// transición entre páginas que ya esté en curso.
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith('operaciones-') && k !== CACHE).map((k) => caches.delete(k)))));
});

// Las páginas con datos nunca se guardan en el dispositivo; solo la página «sin conexión» y los recursos estáticos.
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate') {
    // Con conexión, el navegador navega por su cuenta; sin conexión, la página propia.
    if (self.navigator.onLine === false) event.respondWith(caches.match(OFFLINE));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    // Primero la red (los estáticos no llevan huella en el nombre); la copia guardada solo responde sin conexión.
    event.respondWith(fetch(req).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req)));
  }
});
