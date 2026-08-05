// Minimal service worker — makes it installable as a PWA. Deliberately does NOT
// cache API responses (incident data + agent transcript must always be live and
// the one-time session must never be served from cache).
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  // network-only; never intercept/caches API or session traffic
  return;
});
