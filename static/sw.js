// NYC Free Events — Service Worker
// Strategy:
//   - Static shell (HTML/icons): cache-first
//   - Leaflet CDN tiles & JS/CSS: stale-while-revalidate
//   - API (/api/events): network-first with short cache fallback

const CACHE_VERSION = 'v1';
const STATIC_CACHE = `nycfree-static-${CACHE_VERSION}`;
const RUNTIME_CACHE = `nycfree-runtime-${CACHE_VERSION}`;

const STATIC_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/static/favicon-32.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE && k !== RUNTIME_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle GETs
  if (request.method !== 'GET') return;

  // API: network-first, fall back to cache if offline
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(RUNTIME_CACHE).then((c) => c.put(request, copy));
          return resp;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // Leaflet tiles & CDN assets: stale-while-revalidate
  if (
    url.host.includes('basemaps.cartocdn.com') ||
    url.host.includes('unpkg.com')
  ) {
    event.respondWith(
      caches.open(RUNTIME_CACHE).then((cache) =>
        cache.match(request).then((cached) => {
          const fetchPromise = fetch(request)
            .then((resp) => {
              cache.put(request, resp.clone());
              return resp;
            })
            .catch(() => cached);
          return cached || fetchPromise;
        })
      )
    );
    return;
  }

  // Same-origin static: cache-first, fall back to network
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((resp) => {
        const copy = resp.clone();
        caches.open(RUNTIME_CACHE).then((c) => c.put(request, copy));
        return resp;
      }))
    );
  }
});
