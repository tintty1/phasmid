// Shared worker: same probe, reported over the connecting port.
importScripts("/probe.js");

self.onconnect = (ev) => {
  const port = ev.ports[0];
  port.onmessage = async (msg) => {
    let result;
    try {
      result = await self.__phasmidProbe({ slot: "shared_worker" });
    } catch (e) {
      result = { error: String(e && e.stack || e) };
    }
    try { await fetch("/h/fetch-shared_worker" + (msg.data.q || ""), { cache: "no-store" }); } catch (e) { result.fetchError = String(e); }
    port.postMessage({ phasmid: true, slot: "shared_worker", result });
  };
  port.start();
};
