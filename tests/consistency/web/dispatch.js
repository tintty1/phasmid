// Runs in the main document. Collects the probe from every context into
// window.__phasmid.results and sets window.__phasmid.done when finished.
//
// Contexts:
//   main            this document
//   iframe_same     <iframe> on the same origin
//   iframe_cross    <iframe> on the second origin (passed in ?cross=)
//   worker          new Worker()
//   shared_worker   new SharedWorker()
//   service_worker  registered at /sw.js, scope /
//
// Results travel by postMessage so the harness exercises the same paths a page
// would, and so it works through connect_over_cdp without Playwright-specific
// worker handles.
//
// The harness page runs it on load. collect.html loads it with data-manual
// and calls self.__phasmidDispatch({cross, timeoutMs, onRecord, run}) itself;
// the returned promise resolves with the same state once every slot is filled.
// A run id is appended as ?run= to every request the contexts make, so a
// server shared by several visitors can tell whose headers it recorded.

(() => {
  const params = new URLSearchParams(location.search);
  const manual = document.currentScript && document.currentScript.hasAttribute("data-manual");

  self.__phasmidDispatch = function dispatch({ cross, timeoutMs, onRecord, run }) {
    const q = run ? `?run=${encodeURIComponent(run)}` : "";
    const state = { results: {}, errors: {}, done: false, started: Date.now() };
    window.__phasmid = state;

    const expected = new Set(["main", "iframe_same", "iframe_cross", "worker", "shared_worker", "service_worker"]);
    let finish;
    const finished = new Promise((resolve) => { finish = resolve; });

    function record(slot, result) {
      if (!expected.has(slot)) return;
      state.results[slot] = result;
      expected.delete(slot);
      if (onRecord) onRecord(slot, [...expected]);
      if (expected.size === 0) { state.done = true; finish(state); }
    }

    function fail(slot, err) {
      state.errors[slot] = String(err && err.stack || err);
      record(slot, { error: state.errors[slot] });
    }

    window.addEventListener("message", (ev) => {
      const d = ev.data;
      if (d && d.phasmid && d.slot) record(d.slot, d.result);
    });

    // Main document.
    (async () => {
      try {
        const result = await self.__phasmidProbe({ slot: "main" });
        try { await fetch("/h/fetch-main" + q, { cache: "no-store" }); } catch (e) { result.fetchError = String(e); }
        record("main", result);
      } catch (e) { fail("main", e); }
    })();

    // Frames.
    function addFrame(slot, origin) {
      const f = document.createElement("iframe");
      f.src = `${origin}/frame.html${q}#${slot}`;
      f.style.cssText = "width:300px;height:150px";
      f.onerror = (e) => fail(slot, e);
      document.body.appendChild(f);
    }
    addFrame("iframe_same", location.origin);
    if (cross) addFrame("iframe_cross", cross); else fail("iframe_cross", "no ?cross= origin given");

    // Dedicated worker.
    try {
      const w = new Worker("/worker.js" + q);
      w.onmessage = (ev) => { if (ev.data && ev.data.phasmid) record(ev.data.slot, ev.data.result); };
      w.onerror = (e) => fail("worker", e.message || e);
      w.postMessage({ phasmid: true, q });
    } catch (e) { fail("worker", e); }

    // Shared worker.
    try {
      const sw = new SharedWorker("/shared.js" + q);
      sw.port.onmessage = (ev) => { if (ev.data && ev.data.phasmid) record(ev.data.slot, ev.data.result); };
      sw.onerror = (e) => fail("shared_worker", e.message || e);
      sw.port.start();
      sw.port.postMessage({ phasmid: true, q });
    } catch (e) { fail("shared_worker", e); }

    // Service worker.
    (async () => {
      try {
        if (!("serviceWorker" in navigator)) throw new Error("navigator.serviceWorker missing");
        await navigator.serviceWorker.register("/sw.js" + q, { scope: "/" });
        const reg = await navigator.serviceWorker.ready;
        const active = reg.active;
        if (!active) throw new Error("no active service worker");
        const ch = new MessageChannel();
        ch.port1.onmessage = (ev) => { if (ev.data && ev.data.phasmid) record(ev.data.slot, ev.data.result); };
        active.postMessage({ phasmid: true, q }, [ch.port2]);
      } catch (e) { fail("service_worker", e); }
    })();

    setTimeout(() => {
      for (const slot of [...expected]) fail(slot, `timeout after ${timeoutMs} ms`);
    }, timeoutMs);

    return finished;
  };

  if (!manual) {
    self.__phasmidDispatch({
      cross: params.get("cross"),
      timeoutMs: Number(params.get("timeout") || 45000),
      onRecord: (_slot, pending) => { document.getElementById("status").textContent = `pending: ${pending.join(", ") || "none"}`; },
    });
  }
})();
