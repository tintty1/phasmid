// Dedicated worker: run the worker-scope probe and report back.
importScripts("/probe.js");

self.onmessage = async (ev) => {
  let result;
  try {
    result = await self.__phasmidProbe({ slot: "worker" });
  } catch (e) {
    result = { error: String(e && e.stack || e) };
  }
  try { await fetch("/h/fetch-worker" + (ev.data.q || ""), { cache: "no-store" }); } catch (e) { result.fetchError = String(e); }
  self.postMessage({ phasmid: true, slot: "worker", result });
};
