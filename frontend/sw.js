/* rain-on-trump service-worker
   – precached shell, network-first code, cache-first icons/fonts, notification clicks */

const VERSION     = '3-0-3';  // sky gradient on the root: status-bar strip and Safari bars match
const SHELL_CACHE = `rain-on-trump-shell-${VERSION}`;
const ICON_CACHE  = `rain-on-trump-icons-${VERSION}`;
const SHELL       = '/index.html';

// The app itself: every file is needed for an offline start
const STATIC_SHELL = [
  SHELL,
  '/',
  '/manifest.webmanifest',
  '/about.html',
  '/css/app.css',
  '/js/main.js',
  '/js/model.js',
  '/js/sky.js',
  '/js/api.js',
  '/js/notify.js',
  '/js/sheet.js',
  '/js/toast.js',
  '/js/dialog.js',
  '/js/settings.js'
];

// Nice to have: a missing one must not abort the install (that would also
// block push, which waits for an active worker). Cloudflare Pages serves
// /about.html at /about too.
const OPTIONAL_SHELL = ['/about'];

// Big binaries we almost never change: served cache-first
const STATIC_ICONS = [
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/icons/apple-touch-icon.png',
  '/icons/favicon-32.png',
  '/fonts/bricolage-grotesque-latin-800-normal.woff2',
  '/fonts/inter-tight-latin-wght-normal.woff2'
];
const CACHE_FIRST = /^\/(icons|fonts)\//;
// Only the app's own files go into the cache (not debug pages, not ?query variants)
const CACHEABLE = new Set([...STATIC_SHELL, ...OPTIONAL_SHELL]);
const cacheable = url => !url.search && (CACHEABLE.has(url.pathname) || /^\/(js|css)\//.test(url.pathname));

// ─── INSTALL: precache shell & icons, then activate ───────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    Promise.all([
      caches.open(SHELL_CACHE).then(c => Promise.all([
        c.addAll(STATIC_SHELL),
        ...OPTIONAL_SHELL.map(u => c.add(u).catch(() => {}))
      ])),
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

// ─── FETCH: nav + code = network-first; icons/fonts = cache-first ─────
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
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

  if (url.origin !== self.location.origin) return;

  // Icons and fonts: cache-first (they only change with a new VERSION)
  if (CACHE_FIRST.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        if (res.ok && !res.redirected) {
          const copy = res.clone();
          event.waitUntil(caches.open(ICON_CACHE).then(c => c.put(req, copy)).catch(() => {}));
        }
        return res;
      }))
    );
    return;
  }

  // Code, styles and everything else: network-first, so a new index.html is
  // never paired with old cached modules; the cache is the offline fallback.
  event.respondWith(
    fetch(req).then(res => {
      if (res.ok && !res.redirected && cacheable(url)) {
        const copy = res.clone();
        event.waitUntil(caches.open(SHELL_CACHE).then(c => c.put(req, copy)).catch(() => {}));
      }
      return res;
    }).catch(async () => (await caches.match(req)) || (await caches.match(url.pathname)) || Response.error())
  );
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
