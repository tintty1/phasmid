// Font metrics probe. Evaluate as an expression in a page on any
// https origin (page.evaluate(src)); resolves to a JSON-serialisable object.
// Same code for every browser so results compare key by key.
(async () => {
  const FONTS = {
    windows: ["Arial", "Segoe UI", "Calibri", "Cambria", "Consolas", "Courier New", "Times New Roman", "Tahoma",
              "Verdana", "Georgia", "Trebuchet MS", "Comic Sans MS", "Impact", "Segoe UI Emoji", "Segoe UI Symbol",
              "MS Gothic", "Microsoft YaHei", "Malgun Gothic"],
    linux: ["DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono", "Liberation Sans", "Liberation Serif",
            "Liberation Mono", "Noto Sans", "Noto Serif", "Noto Color Emoji", "Noto Sans CJK JP", "Ubuntu", "Cantarell"],
    generic: ["serif", "sans-serif", "monospace", "system-ui", "cursive", "fantasy", "emoji"],
    missing: ["PhasmidNoSuchFont"],
  };
  const TEXTS = ["mmmmmmmmmmlli", "Cwm fjordbank glyphs vext quiz, 😃", "日本語のテキスト", "0123456789.,;:"];
  const SIZES = [16, 72];
  const FIELDS = ["width", "actualBoundingBoxLeft", "actualBoundingBoxRight", "actualBoundingBoxAscent",
                  "actualBoundingBoxDescent", "fontBoundingBoxAscent", "fontBoundingBoxDescent", "emHeightAscent",
                  "emHeightDescent", "hangingBaseline", "alphabeticBaseline", "ideographicBaseline"];
  const all = Object.values(FONTS).flat();
  // A family's own stack falls back to a generic only when it is missing, so
  // equal widths against both fallbacks mean "not resolved".
  const measureCode = `
    const FIELDS = ${JSON.stringify(FIELDS)};
    function m(ctx, font, text) {
      ctx.font = font;
      const t = ctx.measureText(text), o = {};
      for (const f of FIELDS) o[f] = t[f];
      return o;
    }
    function run(ctx, families, texts, sizes) {
      const out = {};
      for (const fam of families) {
        const q = /^(serif|sans-serif|monospace|system-ui|cursive|fantasy|emoji)$/.test(fam) ? fam : JSON.stringify(fam);
        out[fam] = {};
        for (const s of sizes) for (const tx of texts) {
          out[fam][s + "|" + tx] = m(ctx, s + "px " + q, tx);
        }
        const a = m(ctx, "72px " + q + ", monospace", "mmmmmmmmmmlli").width;
        const b = m(ctx, "72px " + q + ", serif", "mmmmmmmmmmlli").width;
        const am = m(ctx, "72px monospace", "mmmmmmmmmmlli").width;
        const bs = m(ctx, "72px serif", "mmmmmmmmmmlli").width;
        out[fam].resolved = !(a === am && b === bs);
      }
      return out;
    }`;
  const fn = new Function(measureCode + "; return {m, run};")();
  const ctx = document.createElement("canvas").getContext("2d");
  const result = {
    ua: navigator.userAgent, platform: navigator.platform, dpr: devicePixelRatio,
    screen: [screen.width, screen.height], fonts: FONTS,
    main: fn.run(ctx, all, TEXTS, SIZES),
  };
  // Noise check: the same call 3 times on fresh canvases.
  result.repeat = [0, 1, 2].map(() => fn.m(document.createElement("canvas").getContext("2d"),
                                           "16px Arial", TEXTS[1]).width);
  // DOM layout of the same strings.
  result.dom = {};
  const box = document.createElement("div");
  box.style.cssText = "position:absolute;left:0;top:0;visibility:hidden;white-space:nowrap";
  document.body.appendChild(box);
  for (const fam of all) {
    const span = document.createElement("span");
    span.style.font = `16px ${/^(serif|sans-serif|monospace|system-ui|cursive|fantasy|emoji)$/.test(fam) ? fam : JSON.stringify(fam)}`;
    span.textContent = TEXTS[1];
    box.appendChild(span);
    const r = span.getBoundingClientRect();
    result.dom[fam] = { offsetWidth: span.offsetWidth, offsetHeight: span.offsetHeight, rectWidth: r.width, rectHeight: r.height };
    box.removeChild(span);
  }
  box.remove();
  result.fontsCheck = Object.fromEntries(all.map(f => [f, document.fonts.check(`16px ${JSON.stringify(f)}`)]));
  // Dedicated worker with OffscreenCanvas: must agree with the main frame.
  try {
    const src = measureCode + `; onmessage = e => { const c = new OffscreenCanvas(10, 10).getContext("2d");
      postMessage(run(c, e.data.f, e.data.t, e.data.s)); };`;
    const w = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
    result.worker = await new Promise((res, rej) => {
      w.onmessage = e => res(e.data); w.onerror = e => rej(e.message || "worker error");
      w.postMessage({ f: all, t: TEXTS, s: SIZES });
      setTimeout(() => rej("worker timeout"), 15000);
    });
    w.terminate();
  } catch (e) { result.worker = { error: String(e) }; }
  return result;
})()
