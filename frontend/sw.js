/* rain-on-trump service-worker
   – separate icon cache, stale-while-revalidate, notification clicks */

const VERSION     = '2-13-8';  // Move moon/sun up another 10px
const SHELL_CACHE = `rain-on-trump-shell-${VERSION}`;
const ICON_CACHE  = `rain-on-trump-icons-${VERSION}`;
const SHELL       = '/index.html';

// Assets we rarely change (shell + manifest)
const STATIC_SHELL = [
  SHELL,
  '/manifest.webmanifest'
];

// Big binaries we almost never bump
const STATIC_ICONS = [
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

// ─── INSTALL: precache shell & icons, then activate ───────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    Promise.all([
      caches.open(SHELL_CACHE).then(c => c.addAll(STATIC_SHELL)),
      caches.open(ICON_CACHE).then(c => c.addAll(STATIC_ICONS))
    ]).then(() => self.skipWaiting())
  );
});

// ─── ACTIVATE: take control, drop old caches ───────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    Promise.all([
      self.clients.claim(),
      caches.keys().then(keys =>
        Promise.all(
          keys
            .filter(k => k !== SHELL_CACHE && k !== ICON_CACHE)
            .map(caches.delete)
        )
      )
    ])
  );
});

// ─── FETCH: nav = network-first; others = stale-while-revalidate ───────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // HTML navigations: network-first, fallback to shell
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then(res => (res.redirected ? fetch(res.url) : res))
        .catch(() => caches.match(SHELL))
    );
    return;
  }

  // Same-origin resources: stale-while-revalidate
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(req).then(cached => {
        const networkFetch = fetch(req).then(res => {
          // Clone before using to avoid "body already used" error
          const resClone = res.clone();
          if (res.ok && !res.redirected) {
            caches.open(SHELL_CACHE).then(c => c.put(req, resClone));
          }
          return res;
        });
        return cached || networkFetch;
      })
    );
  }
});

// ─── MESSAGE: allow skipWaiting from page ─────────────────────────────
self.addEventListener('message', e => {
  if (e.data?.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ─── PUSH: show notification, carry URL for click-through ─────────────
self.addEventListener('push', e => {
  let data = { title: 'Rain on Trump', body: '', url: '/' };
  try { data = e.data.json(); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/icons/icon-192.png',
      data: { url: data.url }
    })
  );
});

// ─── NOTIFICATION CLICK: focus or open window ────────────────────────
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = e.notification.data?.url || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(list =>
        list.find(c => c.url === target && 'focus' in c)
          ? list.find(c => c.url === target).focus()
          : clients.openWindow(target)
      )
  );
});
