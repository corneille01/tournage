// sw.js — vitesse + minimum de fonctionnement hors-ligne.
//
// Stratégie volontairement DIFFÉRENTE selon le type de fichier :
// - Code de l'app (HTML/JS/CSS) : réseau d'abord, cache en secours
//   uniquement si hors-ligne — jamais de version périmée servie tant
//   qu'il y a du réseau, vu qu'on modifie ces fichiers très souvent.
// - Assets vraiment statiques (images, manifest) : cache d'abord,
//   ça ne change jamais une fois publié.
// - API (/api/*) : jamais de cache ici (Cloudflare s'en charge déjà
//   sélectivement, voir main.py) — toujours le réseau direct.

const CACHE_NAME = "cinetour-static-v2";

const COQUILLE_APP = [
  "/", "/index.html", "/analyse.html",
  "/style.css", "/app.js", "/analyse.css", "/analyse.js",
  "/manifest.json",
];
const ASSETS_STATIQUES = [
  "/icons/placeholder-poster.png",
  "/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.addAll([...COQUILLE_APP, ...ASSETS_STATIQUES]).catch(() => {
        return Promise.allSettled(
          [...COQUILLE_APP, ...ASSETS_STATIQUES].map((url) => cache.add(url))
        );
      })
    )
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

function _estCoquilleApp(pathname) {
  return COQUILLE_APP.includes(pathname) || pathname.endsWith(".html") || pathname.endsWith(".js") || pathname.endsWith(".css");
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (url.pathname.startsWith("/api/")) return;

  if (_estCoquilleApp(url.pathname)) {
    event.respondWith(
      fetch(event.request)
        .then((reponse) => {
          const copie = reponse.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copie));
          return reponse;
        })
        .catch(() =>
          caches.match(event.request).then((cached) => cached || caches.match("/offline.html"))
        )
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});