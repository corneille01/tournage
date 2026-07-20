// sw.js — cache les assets statiques pour un chargement instantané
// et un minimum de fonctionnement hors-ligne. Les données films/lieux
// restent toujours en réseau (jamais en cache ici) car elles évoluent.

const CACHE_NAME = "cinetour-static-v1";
const ASSETS_STATIQUES = [
  "/", "/style.css", "/app.js", "/manifest.json",
  "/placeholder-poster.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_STATIQUES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Jamais de cache pour les appels API — toujours des données fraîches
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
