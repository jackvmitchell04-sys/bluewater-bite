/* Bluewater Bite offline cache: network first, falls back to the last copy saved on the phone. */
const C = "bwb-v6";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icon.svg"];
const DATA = ["ocean.json", "bathy_hd.json", "bathy_hd.png", "bathy.json", "himawari.json", "currents.json", "forecast.json", "tides.json", "reserves.json", "alerts.json", "model.json", "tiles/index.json"].map(f => "data/" + f);
self.addEventListener("install", e => {
  e.waitUntil(caches.open(C).then(c => Promise.all([...SHELL, ...DATA].map(u => c.add(u).catch(() => null)))));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(k => Promise.all(k.filter(x => x !== C).map(x => caches.delete(x)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url), own = u.origin === location.origin;
  if (!own && !/open-meteo\.com$/.test(u.hostname)) return;
  const key = own ? u.origin + u.pathname : u.href;
  e.respondWith(fetch(r).then(res => {
    if (res.ok) { const cp = res.clone(); caches.open(C).then(c => c.put(key, cp)); }
    return res;
  }).catch(() => caches.match(key).then(m => m || (own && u.pathname.endsWith("/") ? caches.match(u.origin + u.pathname + "index.html") : null)).then(m => m || Response.error())));
});
