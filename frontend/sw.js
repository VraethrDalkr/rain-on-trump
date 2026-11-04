/* rain-on-trump service-worker
   – separate icon cache, stale-while-revalidate, notification clicks */

const VERSION     = '2-16-2';  // new app icon (storm over the swoop)
const SHELL_CACHE = `rain-on-trump-shell-${VERSION}`;
const ICON_CACHE  = `rain-on-trump-icons-${VERSION}`;
const SHELL       = '/index.html';

// Assets we rarely change (shell + manifest)
// Note: Cloudflare Pages redirects /about.html → /about, so cache both paths
const STATIC_SHELL = [
  SHELL,
  '/',
  '/manifest.webmanifest',
  '/about.html',
  '/about'
];

// Big binaries we almost never bump
const STATIC_ICONS = [
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/icons/apple-touch-icon.png',
  '/icons/favicon-32.png'
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
            .map(k => caches.delete(k))
        )
      )
    ])
  );
});

// ─── FETCH: nav = network-first; others = stale-while-revalidate ───────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // HTML navigations: network-first; offline -> exact cached page, else the shell.
  // Cached copies are re-wrapped so a redirected response is never served to a navigation.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(async () => {
        const hit = (await caches.match(req)) ||
                    (await caches.match(url.pathname)) ||
                    (await caches.match(SHELL)) ||
                    (await caches.match('/'));
        if (!hit) return Response.error();
        return hit.redirected
          ? new Response(await hit.blob(), {
              status: hit.status,
              statusText: hit.statusText,
              headers: hit.headers
            })
          : hit;
      })
    );
    return;
  }

  // Same-origin resources: stale-while-revalidate
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(req).then(cached => {
        const networkFetch = fetch(req).then(res => {
          if (res.ok && !res.redirected) {
            const resClone = res.clone();
            event.waitUntil(
              caches.open(SHELL_CACHE).then(c => c.put(req, resClone)).catch(() => {})
            );
          }
          return res;
        });
        if (cached) {
          networkFetch.catch(() => {});  // offline: keep serving the cached copy quietly
          return cached;
        }
        return networkFetch;
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
  const target = new URL(e.notification.data?.url || '/', self.location.origin).href;
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(list => {
        // Any open window of this app will do; prefer a visible one
        const mine = list.filter(c => new URL(c.url).origin === self.location.origin);
        const win = mine.find(c => c.visibilityState === 'visible') || mine[0];
        if (win && 'focus' in win) return win.focus();
        return clients.openWindow(target);
      })
  );
});

// ─── PUSH SUBSCRIPTION CHANGE: browser rotated the endpoint ───────────
self.addEventListener('pushsubscriptionchange', e => {
  e.waitUntil((async () => {
    try {
      if (e.oldSubscription) {
        await self.registration.pushManager.subscribe(e.oldSubscription.options);
      }
    } catch (err) { /* page will resubscribe on next visit */ }
    const list = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    list.forEach(c => c.postMessage({ type: 'PUSH_SUB_CHANGED' }));
  })());
});
