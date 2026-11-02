// Service worker: same probe, answered over a MessageChannel port.
importScripts("/probe.js");

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener("message", (ev) => {
  if (!ev.data || !ev.data.phasmid) return;
  const port = ev.ports[0];
  ev.waitUntil((async () => {
    let result;
    try {
      result = await self.__phasmidProbe({ slot: "service_worker" });
    } catch (e) {
      result = { error: String(e && e.stack || e) };
    }
    try { await fetch("/h/fetch-service_worker" + (ev.data.q || ""), { cache: "no-store" }); } catch (e) { result.fetchError = String(e); }
    port.postMessage({ phasmid: true, slot: "service_worker", result });
  })());
});
