const CACHE_NAME = 'openstock-v3.0.0-alpaca-only-kpi-colors-1-demo-banner-1-configure-2-session-window-2-popover-grace-1-session-0900-banner-algo-root-removed-1-research-lab-2-system-gauge-2';
const REQUIRED_ASSETS = [
  './',
  './index.html',
  './styles.css?v=3.0.0&pull-refresh=1&algo-account-switch=1&strategy-pnl=1&appearance=1&optimizer-table=1&quote-theme=1&algo-universe=1&universe-cards=1&demo-mode=1&chart-range-theme=2&alpaca-usd-profile=1&alpaca-price-yellow=2&universe-chart-black=1&kpi-colors=2&research-lab=1&system-gauge=1',
  './themes.css?v=3.0.0&appearance=9',
  './app.js?v=3.0.0&pull-refresh=1&strategy-scorecard=2&appearance=2&optimizer-table=1&chart-range=1&coordinator-regime=1&trade-pagination=2&title-cleanup=1&algo-universe=1&universe-cards=1&research-page=1&demo-mode=1&alpaca-usd-profile=1&demo-banner=1',
  './modules/api.js',
  './modules/state.js',
  './modules/escape.js',
  './modules/formatting.js',
  './modules/charts.js?demo-mode=1&alpaca-usd-only=1',
  './modules/dashboard.js?title-cleanup=1&demo-mode=1&alpaca-usd-only=1',
  './modules/journal.js?trade-pagination=2&title-cleanup=1&alpaca-usd-only=1',
  './modules/algo-trader.js?v=3.0.0&research-lab=3&options-api-guard=1&strategy-scorecard=2&appearance=1&optimizer-table=1&chart-range=1&coordinator-regime=1&research-page=1&demo-mode=1&split-modules=1',
  './modules/algo-trader/account-view.js',
  './modules/algo-trader/equity-chart.js',
  './modules/algo-trader/format.js',
  './modules/algo-trader/research-view.js',
  './modules/algo-trader/settings-form.js',
  './modules/algo-trader/strategies.js',
  './modules/algo-universe.js?cards=1&demo-mode=1',
  './modules/settings.js',
  './modules/config.js',
  './modules/session-window.js',
  './modules/dashboard-utils.js',
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
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(REQUIRED_ASSETS);
    })
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const scopePath = new URL(self.registration.scope).pathname.replace(/\/$/, '');
  const apiPrefixes = ['/api', `${scopePath}/api`];
  const isApiRequest = apiPrefixes.some((prefix) =>
    url.pathname === prefix || url.pathname.startsWith(`${prefix}/`)
  );

  // Never cache API requests — always fetch fresh data
  if (isApiRequest) {
    event.respondWith(fetch(event.request, { cache: 'no-store' }));
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
    }).then(() => self.clients.claim())
  );
});
