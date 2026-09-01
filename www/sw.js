const CACHE_NAME = 'openstock-v1';
const REQUIRED_ASSETS = [
  '/',
  './index.html',
  './styles.css',
  './app.js',
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
  // Only cache GET requests for same-origin requests
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }

      // Fetch from network and put in cache
      return fetch(event.request).then((networkResponse) => {
        // Don't cache non-200 responses or non-GET requests
        if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== 'basic') {
          return networkResponse;
        }

        // Clone the response since it's a stream
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