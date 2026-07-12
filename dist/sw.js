// SwingSense service worker: network-first, cached shell for offline opens
const CACHE = 'swingsense-v1';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['/','/manifest.json','/icon-192.png'])));
  self.skipWaiting();
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (e.request.url.startsWith(self.location.origin) && r.ok) {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return r;
    }).catch(() => caches.match(e.request))
  );
});
