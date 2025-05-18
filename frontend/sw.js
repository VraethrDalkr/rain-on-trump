// Basic offline cache + (future) push-notification handler
const CACHE_NAME = "rain-on-trump-v1";
self.addEventListener("install", e => {
    e.waitUntil(
        caches.open(CACHE_NAME).then(c =>
        c.addAll(["/", "/index.html", "/manifest.webmanifest"])
        )
    );
});
self.addEventListener("fetch", e => {
    e.respondWith(
        caches.match(e.request).then(r => r || fetch(e.request))
    );
});
self.addEventListener("push", e => {
    const data = e.data.json();
    self.registration.showNotification(data.title, {
        body: data.body,
        icon: "/icons/icon-192.png",
    });
});
