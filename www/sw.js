const CACHE_NAME = 'openstock-v4';
const REQUIRED_ASSETS = [
  '/',
  './index.html',
  './styles.css?v=2',
  './app.js?v=2',
  './site.webmanifest',
  './favicon-16.png',
  './favicon-32.png',
  './favicon-48.png',
  './favicon-64.png',
  './favicon-128.png',
  './favicon-192.png',
  './favicon-256.png',
  './favicon.ico',
  './apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(REQUIRED_ASSETS);
    })
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache API requests — always fetch fresh data
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/refresh-prices') || url.pathname.endsWith('.js') || url.pathname.endsWith('.css')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Only cache GET requests for same-origin static assets
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }

      return fetch(event.request).then((networkResponse) => {
        if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== 'basic') {
          return networkResponse;
        }

        const responseToCache = networkResponse.clone();
        caches.open(CACHE_NAME).then((cache) => {
          cache.put(event.request, responseToCache);
        });

        return networkResponse;
      });
    })
  );
});

self.addEventListener('activate', (event) => {
  const currentCachelist = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (currentCachelist.indexOf(key) === -1) {
            return caches.delete(key);
          }
        })
      );
    })
  );
});