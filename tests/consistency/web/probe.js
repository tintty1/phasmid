// Phasmid consistency probe.
//
// One function, self.__phasmidProbe(ctx), that works in a window, a dedicated
// worker, a shared worker and a service worker. Each section is independent:
// a failure is recorded as {error: "..."} under that section instead of
// aborting the probe, so a missing API is itself data (its absence is a
// surface). Output keys are stable so dumps can be diffed.
//
// Sections that need a document (screen, media queries, fonts, system colors,
// rects, speech, WebRTC, WebAuthn) only run where `document` exists.

(() => {
  const hasDOM = typeof document !== "undefined" && typeof window !== "undefined";
  const isServiceWorker = typeof ServiceWorkerGlobalScope !== "undefined" && self instanceof ServiceWorkerGlobalScope;

  // ---------- helpers ----------

  async function sha256(data) {
    const buf = data instanceof Blob ? await data.arrayBuffer() : data;
    const digest = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function errStr(e) {
    if (!e) return "unknown error";
    return `${e.name || "Error"}: ${e.message || String(e)}`;
  }

  async function section(fn) {
    try {
      return await fn();
    } catch (e) {
      return { error: errStr(e) };
    }
  }

  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} timed out after ${ms} ms`)), ms)),
    ]);
  }

  function sortedKeys(obj) {
    return Object.keys(obj).sort();
  }

  function ownTree(obj, depth) {
    // Shape of an object: own property names, recursing into plain objects.
    if (obj === null || obj === undefined) return typeof obj;
    const out = {};
    for (const k of Object.getOwnPropertyNames(obj).sort()) {
      let v;
      try { v = obj[k]; } catch (e) { out[k] = `<throws ${errStr(e)}>`; continue; }
      const t = typeof v;
      if (t === "object" && v !== null && depth > 0 && Object.getPrototypeOf(v) === Object.prototype) out[k] = ownTree(v, depth - 1);
      else out[k] = t === "function" ? "function" : t;
    }
    return out;
  }

  function makeCanvas(w, h) {
    if (hasDOM) {
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      return c;
    }
    return new OffscreenCanvas(w, h);
  }

  // The same scene is drawn by every canvas-related probe so that noise, if
  // any, can be compared across readback paths and contexts.
  function drawScene(ctx2d, w, h) {
    ctx2d.fillStyle = "#f60";
    ctx2d.fillRect(10, 10, 100, 50);
    ctx2d.fillStyle = "#069";
    ctx2d.font = '16px "Arial"';
    ctx2d.textBaseline = "alphabetic";
    ctx2d.fillText("Phasmid 🦎 Cwm fjord bank glyphs vext quiz, 😃", 2, 30);
    ctx2d.fillStyle = "rgba(102, 204, 0, 0.7)";
    ctx2d.font = '18px "Times New Roman"';
    ctx2d.fillText("Phasmid 🦎 Cwm fjord bank glyphs vext quiz, 😃", 4, 45);
    const g = ctx2d.createLinearGradient(0, 0, w, 0);
    g.addColorStop(0, "red"); g.addColorStop(0.5, "lime"); g.addColorStop(1, "blue");
    ctx2d.fillStyle = g;
    ctx2d.fillRect(0, 60, w, 20);
    ctx2d.globalCompositeOperation = "multiply";
    ctx2d.beginPath();
    ctx2d.arc(60, 100, 25, 0, Math.PI * 2, true);
    ctx2d.fillStyle = "rgb(255,0,255)";
    ctx2d.fill();
    ctx2d.globalCompositeOperation = "source-over";
    ctx2d.shadowBlur = 4; ctx2d.shadowColor = "black";
    ctx2d.strokeStyle = "#333";
    ctx2d.lineWidth = 2.5;
    ctx2d.beginPath();
    ctx2d.moveTo(120, 90); ctx2d.bezierCurveTo(140, 60, 180, 140, 220, 100); ctx2d.stroke();
    ctx2d.shadowBlur = 0;
    ctx2d.save();
    ctx2d.translate(240, 100); ctx2d.rotate(0.37); ctx2d.scale(1.3, 0.8);
    ctx2d.fillStyle = "#0a0"; ctx2d.fillRect(-15, -15, 30, 30);
    ctx2d.restore();
  }

  // ---------- sections ----------

  async function navigatorSection() {
    const n = navigator;
    const out = {
      userAgent: n.userAgent,
      appVersion: n.appVersion,
      appName: n.appName,
      appCodeName: n.appCodeName,
      product: n.product,
      productSub: n.productSub,
      vendor: n.vendor,
      vendorSub: n.vendorSub,
      platform: n.platform,
      oscpu: n.oscpu,
      language: n.language,
      languages: [...(n.languages || [])],
      hardwareConcurrency: n.hardwareConcurrency,
      deviceMemory: n.deviceMemory,
      cookieEnabled: n.cookieEnabled,
      doNotTrack: n.doNotTrack,
      globalPrivacyControl: n.globalPrivacyControl,
      onLine: n.onLine,
      pdfViewerEnabled: n.pdfViewerEnabled,
      maxTouchPoints: n.maxTouchPoints,
      webdriver: n.webdriver,
      has: {},
    };
    for (const k of ["bluetooth", "share", "canShare", "getBattery", "usb", "hid", "serial", "xr", "keyboard", "virtualKeyboard", "wakeLock", "locks", "gpu", "ink", "windowControlsOverlay", "contacts", "credentials", "mediaSession", "presentation", "scheduling", "userActivation", "clipboard", "geolocation", "mediaCapabilities", "mediaDevices", "permissions", "storage", "serviceWorker", "connection", "login", "devicePosture", "managed", "adAuctionComponents", "joinAdInterestGroup"]) {
      out.has[k] = k in n;
    }
    if (n.connection) {
      const c = n.connection;
      out.connection = { effectiveType: c.effectiveType, rtt: c.rtt, downlink: c.downlink, downlinkMax: c.downlinkMax, saveData: c.saveData, type: c.type };
    }
    if (n.userAgentData) {
      const u = n.userAgentData;
      out.userAgentData = { brands: u.brands, mobile: u.mobile, platform: u.platform };
      out.userAgentData.highEntropy = await section(() => u.getHighEntropyValues([
        "architecture", "bitness", "model", "platformVersion", "uaFullVersion", "fullVersionList", "wow64", "formFactors",
      ]));
    } else {
      out.userAgentData = null;
    }
    if (hasDOM) {
      out.plugins = [...n.plugins].map((p) => ({ name: p.name, filename: p.filename, description: p.description, mimeTypes: [...p].map((m) => m.type) }));
      out.mimeTypes = [...n.mimeTypes].map((m) => ({ type: m.type, suffixes: m.suffixes, description: m.description }));
    }
    if (n.getBattery) {
      out.battery = await section(async () => {
        const b = await withTimeout(n.getBattery(), 3000, "getBattery");
        return { charging: b.charging, chargingTime: b.chargingTime, dischargingTime: b.dischargingTime, level: b.level };
      });
    }
    if (n.bluetooth && n.bluetooth.getAvailability) {
      out.bluetoothAvailability = await section(() => withTimeout(n.bluetooth.getAvailability(), 3000, "getAvailability"));
    }
    if (n.canShare) {
      out.canShare = await section(() => n.canShare({ title: "x", text: "y", url: "https://example.com/" }));
    }
    return out;
  }

  function intlSection() {
    const dtf = Intl.DateTimeFormat().resolvedOptions();
    const jan = new Date(2024, 0, 1, 12);
    const jul = new Date(2024, 6, 1, 12);
    return {
      dateTimeFormat: { locale: dtf.locale, timeZone: dtf.timeZone, calendar: dtf.calendar, numberingSystem: dtf.numberingSystem, hourCycle: dtf.hourCycle },
      numberFormatLocale: new Intl.NumberFormat().resolvedOptions().locale,
      collatorLocale: new Intl.Collator().resolvedOptions().locale,
      pluralRulesLocale: new Intl.PluralRules().resolvedOptions().locale,
      relativeTimeLocale: new Intl.RelativeTimeFormat().resolvedOptions().locale,
      timezoneOffsetNow: new Date().getTimezoneOffset(),
      timezoneOffsetJan: jan.getTimezoneOffset(),
      timezoneOffsetJul: jul.getTimezoneOffset(),
      dateToString: jan.toString(),
      dateToLocaleString: jan.toLocaleString(),
      numberToLocaleString: (1234567.891).toLocaleString(),
      dateTimeFormatLongName: new Intl.DateTimeFormat(undefined, { timeZoneName: "long" }).format(jan),
      supportedValuesOfTimeZoneLength: (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone").length : null),
    };
  }

  function screenSection() {
    const s = screen;
    return {
      width: s.width, height: s.height,
      availWidth: s.availWidth, availHeight: s.availHeight, availLeft: s.availLeft, availTop: s.availTop,
      colorDepth: s.colorDepth, pixelDepth: s.pixelDepth,
      isExtended: s.isExtended,
      orientation: s.orientation ? { type: s.orientation.type, angle: s.orientation.angle } : null,
      innerWidth, innerHeight, outerWidth, outerHeight, screenX, screenY, screenLeft, screenTop,
      devicePixelRatio,
      visualViewport: visualViewport ? { width: visualViewport.width, height: visualViewport.height, scale: visualViewport.scale } : null,
      documentClient: { width: document.documentElement.clientWidth, height: document.documentElement.clientHeight },
      scrollbarWidth: innerWidth - document.documentElement.clientWidth,
    };
  }

  function mediaQueriesSection() {
    const q = (s) => matchMedia(s).matches;
    const out = {};
    const queries = {
      pointerFine: "(pointer: fine)", pointerCoarse: "(pointer: coarse)", pointerNone: "(pointer: none)",
      hoverHover: "(hover: hover)", hoverNone: "(hover: none)",
      anyPointerFine: "(any-pointer: fine)", anyPointerCoarse: "(any-pointer: coarse)", anyPointerNone: "(any-pointer: none)",
      anyHoverHover: "(any-hover: hover)", anyHoverNone: "(any-hover: none)",
      colorGamutSrgb: "(color-gamut: srgb)", colorGamutP3: "(color-gamut: p3)", colorGamutRec2020: "(color-gamut: rec2020)",
      dynamicRangeHigh: "(dynamic-range: high)", dynamicRangeStandard: "(dynamic-range: standard)",
      videoDynamicRangeHigh: "(video-dynamic-range: high)",
      prefersColorSchemeDark: "(prefers-color-scheme: dark)", prefersColorSchemeLight: "(prefers-color-scheme: light)",
      prefersReducedMotion: "(prefers-reduced-motion: reduce)",
      prefersReducedTransparency: "(prefers-reduced-transparency: reduce)",
      prefersContrastMore: "(prefers-contrast: more)", prefersContrastLess: "(prefers-contrast: less)", prefersContrastNoPreference: "(prefers-contrast: no-preference)",
      forcedColorsActive: "(forced-colors: active)",
      invertedColors: "(inverted-colors: inverted)",
      displayModeBrowser: "(display-mode: browser)",
      updateFast: "(update: fast)",
      scriptingEnabled: "(scripting: enabled)",
      monochrome: "(monochrome)",
      color8: "(min-color: 8)", color10: "(min-color: 10)",
      deviceWidthMatchesScreen: `(device-width: ${screen.width}px)`,
      deviceHeightMatchesScreen: `(device-height: ${screen.height}px)`,
      widthMatchesInner: `(width: ${innerWidth}px)`,
      resolutionMatchesDpr: `(resolution: ${devicePixelRatio}dppx)`,
      orientationLandscape: "(orientation: landscape)",
      deviceAspectRatio: `(device-aspect-ratio: ${screen.width}/${screen.height})`,
    };
    for (const [k, v] of Object.entries(queries)) out[k] = q(v);
    // Exact resolution via binary search over dppx.
    let lo = 0.5, hi = 8;
    for (let i = 0; i < 24; i++) { const mid = (lo + hi) / 2; if (q(`(min-resolution: ${mid}dppx)`)) lo = mid; else hi = mid; }
    out.resolutionDppx = Math.round(lo * 1e4) / 1e4;
    return out;
  }

  function systemColorsSection() {
    const names = ["Canvas", "CanvasText", "LinkText", "VisitedText", "ActiveText", "ButtonFace", "ButtonText", "ButtonBorder", "Field", "FieldText", "Highlight", "HighlightText", "SelectedItem", "SelectedItemText", "Mark", "MarkText", "GrayText", "AccentColor", "AccentColorText",
      // deprecated but still resolvable
      "ActiveBorder", "ActiveCaption", "AppWorkspace", "Background", "ButtonHighlight", "ButtonShadow", "CaptionText", "InactiveBorder", "InactiveCaption", "InactiveCaptionText", "InfoBackground", "InfoText", "Menu", "MenuText", "Scrollbar", "ThreeDDarkShadow", "ThreeDFace", "ThreeDHighlight", "ThreeDLightShadow", "ThreeDShadow", "Window", "WindowFrame", "WindowText"];
    const el = document.createElement("span");
    // The set for the preferred color scheme, which is what a profile's
    // system_colors describes (display.prefers_color_scheme).
    el.style.colorScheme = "light dark";
    document.body.appendChild(el);
    const out = {};
    for (const n of names) {
      el.style.color = "";
      el.style.color = n;
      out[n] = el.style.color === "" ? null : getComputedStyle(el).color;
    }
    // Default form control rendering characteristics.
    const input = document.createElement("input");
    document.body.appendChild(input);
    const cs = getComputedStyle(input);
    out.inputDefaults = { font: cs.font, height: cs.height, border: cs.border, backgroundColor: cs.backgroundColor, color: cs.color, padding: cs.padding };
    const button = document.createElement("button");
    button.textContent = "x";
    document.body.appendChild(button);
    const bs = getComputedStyle(button);
    out.buttonDefaults = { font: bs.font, height: bs.height, border: bs.border, backgroundColor: bs.backgroundColor, color: bs.color, padding: bs.padding };
    const body = getComputedStyle(document.body);
    out.bodyDefaults = { font: body.font, color: body.color, backgroundColor: body.backgroundColor };
    el.remove(); input.remove(); button.remove();
    return out;
  }

  // Families whose presence distinguishes OS families and versions. Detection
  // compares rendered width and height against three generic fallbacks; a
  // family counts as present if any fallback pairing differs.
  const FONT_FAMILIES = [
    // Windows
    "Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria", "Cambria Math", "Candara", "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New", "Ebrima", "Franklin Gothic Medium", "Gabriola", "Gadugi", "Georgia", "HoloLens MDL2 Assets", "Impact", "Ink Free", "Javanese Text", "Leelawadee UI", "Lucida Console", "Lucida Sans Unicode", "Malgun Gothic", "Marlett", "Microsoft Himalaya", "Microsoft JhengHei", "Microsoft New Tai Lue", "Microsoft PhagsPa", "Microsoft Sans Serif", "Microsoft Tai Le", "Microsoft YaHei", "Microsoft Yi Baiti", "MingLiU-ExtB", "Mongolian Baiti", "MS Gothic", "MS UI Gothic", "MV Boli", "Myanmar Text", "Nirmala UI", "Palatino Linotype", "Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe Print", "Segoe Script", "Segoe UI", "Segoe UI Emoji", "Segoe UI Historic", "Segoe UI Symbol", "Segoe UI Variable", "SimSun", "Sitka", "Sylfaen", "Symbol", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana", "Webdings", "Wingdings", "Yu Gothic",
    // Windows: the rest of Pixelscan's 55 core fonts, which it needs all
    // present before it judges masking. Older dumps do not probe these.
    "Franklin Gothic", "Microsoft JhengHei UI", "MingLiU_HKSCS-ExtB", "MS PGothic", "PMingLiU-ExtB", "SimSun-ExtB", "Sitka Small",
    // macOS
    "American Typewriter", "Andale Mono", "Apple Braille", "Apple Chancery", "Apple Color Emoji", "Apple SD Gothic Neo", "Apple Symbols", "AppleGothic", "AppleMyungjo", "Arial Hebrew", "Arial Narrow", "Arial Rounded MT Bold", "Arial Unicode MS", "Avenir", "Avenir Next", "Avenir Next Condensed", "Baskerville", "Big Caslon", "Bodoni 72", "Bradley Hand", "Brush Script MT", "Chalkboard", "Chalkboard SE", "Chalkduster", "Charter", "Cochin", "Copperplate", "Courier", "Didot", "DIN Alternate", "DIN Condensed", "Futura", "Geneva", "Gill Sans", "Helvetica", "Helvetica Neue", "Herculanum", "Hiragino Kaku Gothic ProN", "Hiragino Mincho ProN", "Hiragino Sans", "Hoefler Text", "Kefa", "Krungthep", "Lucida Grande", "Luminari", "Marker Felt", "Menlo", "Monaco", "Noteworthy", "Optima", "Palatino", "Papyrus", "Phosphate", "PingFang SC", "PingFang TC", "Rockwell", "San Francisco", "Savoye LET", "SignPainter", "Skia", "Snell Roundhand", "STIXGeneral", "Superclarendon", "Thonburi", "Trattatello", "Zapfino", ".AppleSystemUIFont", "-apple-system", "BlinkMacSystemFont",
    // Linux
    "Bitstream Vera Sans", "Cantarell", "DejaVu Sans", "DejaVu Sans Mono", "DejaVu Serif", "Droid Sans", "FreeMono", "FreeSans", "FreeSerif", "Liberation Mono", "Liberation Sans", "Liberation Serif", "Nimbus Mono PS", "Nimbus Roman", "Nimbus Sans", "Noto Color Emoji", "Noto Mono", "Noto Sans", "Noto Sans CJK JP", "Noto Sans Mono", "Noto Serif", "Open Sans", "Roboto", "Source Code Pro", "Source Sans Pro", "Ubuntu", "Ubuntu Mono", "URW Bookman", "URW Gothic",
    // Office / Adobe / common extras
    "Book Antiqua", "Bookman Old Style", "Century Gothic", "Garamond", "Lucida Bright", "Lucida Handwriting", "Monotype Corsiva", "Myriad Pro", "Minion Pro", "Segoe UI Light", "Segoe UI Semibold", "Source Serif Pro", "Fira Code", "Fira Sans", "JetBrains Mono", "Inter", "Montserrat", "Lato",
  ];

  function fontsSection() {
    const canvas = makeCanvas(600, 100);
    const c = canvas.getContext("2d", { willReadFrequently: true });
    const text = "mmmmmmmmmmlli1Oo0WwQq@#%&";
    const size = "72px";
    const bases = ["monospace", "sans-serif", "serif"];
    const measure = (family) => {
      c.font = `${size} ${family}`;
      const m = c.measureText(text);
      return [m.width, m.actualBoundingBoxAscent + m.actualBoundingBoxDescent];
    };
    const baseline = Object.fromEntries(bases.map((b) => [b, measure(b)]));
    const present = [];
    const absent = [];
    for (const f of FONT_FAMILIES) {
      let detected = false;
      for (const b of bases) {
        const m = measure(`"${f}", ${b}`);
        if (m[0] !== baseline[b][0] || m[1] !== baseline[b][1]) { detected = true; break; }
      }
      (detected ? present : absent).push(f);
    }
    // Full metrics of a fixed string for a handful of families, including the
    // generic fallbacks, so metric spoofing can be checked beyond presence.
    const metricsFor = {};
    for (const f of ["sans-serif", "serif", "monospace", "system-ui", "Arial", "Times New Roman", "Courier New", "Helvetica", "Segoe UI", "Calibri", "Roboto", "DejaVu Sans", "Liberation Sans"]) {
      c.font = `16px ${f.includes(" ") || /^[A-Z]/.test(f) ? `"${f}"` : f}`;
      const m = c.measureText("The quick brown fox jumps over the lazy dog 0123456789");
      metricsFor[f] = {
        width: m.width,
        actualBoundingBoxAscent: m.actualBoundingBoxAscent, actualBoundingBoxDescent: m.actualBoundingBoxDescent,
        actualBoundingBoxLeft: m.actualBoundingBoxLeft, actualBoundingBoxRight: m.actualBoundingBoxRight,
        fontBoundingBoxAscent: m.fontBoundingBoxAscent, fontBoundingBoxDescent: m.fontBoundingBoxDescent,
        alphabeticBaseline: m.alphabeticBaseline, hangingBaseline: m.hangingBaseline, ideographicBaseline: m.ideographicBaseline,
      };
    }
    const out = { present, absentCount: absent.length, metrics: metricsFor, baseline };
    if (hasDOM) {
      out.queryLocalFontsPresent = "queryLocalFonts" in window;
      // DOM text measurement: the layout path, as opposed to the canvas path.
      const span = document.createElement("span");
      span.style.cssText = "position:absolute;left:-9999px;font-size:16px;white-space:nowrap";
      span.textContent = "The quick brown fox jumps over the lazy dog 0123456789";
      document.body.appendChild(span);
      out.domMetrics = {};
      for (const f of ["sans-serif", "serif", "monospace", "system-ui", "Arial", "Times New Roman", "Courier New"]) {
        span.style.fontFamily = f;
        const r = span.getBoundingClientRect();
        out.domMetrics[f] = { width: r.width, height: r.height };
      }
      span.remove();
      out.documentFontsCheckArial = document.fonts.check('16px "Arial"');
      // CSS system font keywords resolve to the OS UI font: the family name
      // and size are an OS tell on their own.
      out.systemFontKeywords = {};
      const kw = document.createElement("span");
      document.body.appendChild(kw);
      for (const k of ["caption", "icon", "menu", "message-box", "small-caption", "status-bar"]) {
        kw.style.font = k;
        const cs = getComputedStyle(kw);
        out.systemFontKeywords[k] = { fontFamily: cs.fontFamily, fontSize: cs.fontSize, fontWeight: cs.fontWeight };
      }
      kw.style.font = "";
      kw.style.fontFamily = "system-ui";
      out.systemUiComputed = getComputedStyle(kw).fontFamily;
      kw.remove();
      // Canvas reads the keyword back as a concrete family.
      c.font = "caption";
      out.canvasCaptionFont = c.font;
      c.font = "menu";
      out.canvasMenuFont = c.font;
    }
    return out;
  }

  // The same names through @font-face local(), the lookup fingerprinters
  // run (fpgen's allFonts): full or PostScript names of installed faces,
  // with no family matching and no aliases.
  async function fontsLocalSection() {
    const present = [];
    for (const f of FONT_FAMILIES) {
      try {
        await new FontFace("phasmid-probe-local", `local("${f}")`).load();
        present.push(f);
      } catch (e) {
        // Not an installed face.
      }
    }
    return { present };
  }

  async function canvasSection() {
    const w = 300, h = 150;
    const out = {};
    const c1 = makeCanvas(w, h);
    const ctx1 = c1.getContext("2d", { willReadFrequently: true });
    drawScene(ctx1, w, h);
    const img1 = ctx1.getImageData(0, 0, w, h);
    out.getImageData = await sha256(img1.data.buffer);
    // Determinism: the same scene drawn again must read back identically.
    const c2 = makeCanvas(w, h);
    const ctx2 = c2.getContext("2d", { willReadFrequently: true });
    drawScene(ctx2, w, h);
    const img2 = ctx2.getImageData(0, 0, w, h);
    out.getImageDataSecondDraw = await sha256(img2.data.buffer);
    out.deterministic = out.getImageData === out.getImageDataSecondDraw;
    // Sub-rectangle consistency: reading a region must match the slice of the full read.
    const sub = ctx1.getImageData(20, 20, 50, 40);
    let subMatches = true;
    for (let y = 0; y < 40 && subMatches; y++) {
      for (let x = 0; x < 50; x++) {
        const a = ((y + 20) * w + (x + 20)) * 4, b = (y * 50 + x) * 4;
        if (img1.data[a] !== sub.data[b] || img1.data[a + 1] !== sub.data[b + 1] || img1.data[a + 2] !== sub.data[b + 2] || img1.data[a + 3] !== sub.data[b + 3]) { subMatches = false; break; }
      }
    }
    out.subRectMatchesFull = subMatches;
    // Encoded readback paths.
    if (hasDOM) {
      out.toDataURL = await sha256(new TextEncoder().encode(c1.toDataURL("image/png")));
      out.toDataURLJpeg = await sha256(new TextEncoder().encode(c1.toDataURL("image/jpeg", 0.8)));
      out.toBlob = await sha256(await new Promise((res) => c1.toBlob(res, "image/png")));
    }
    if (typeof OffscreenCanvas !== "undefined") {
      const oc = new OffscreenCanvas(w, h);
      const octx = oc.getContext("2d", { willReadFrequently: true });
      drawScene(octx, w, h);
      out.offscreenGetImageData = await sha256(octx.getImageData(0, 0, w, h).data.buffer);
      out.offscreenConvertToBlob = await sha256(await oc.convertToBlob({ type: "image/png" }));
      if (hasDOM) {
        // transferToImageBitmap + drawImage back into a DOM canvas: another readback route.
        const oc2 = new OffscreenCanvas(w, h);
        drawScene(oc2.getContext("2d"), w, h);
        const bmp = oc2.transferToImageBitmap();
        const c3 = makeCanvas(w, h);
        const ctx3 = c3.getContext("2d", { willReadFrequently: true });
        ctx3.drawImage(bmp, 0, 0);
        out.viaImageBitmap = await sha256(ctx3.getImageData(0, 0, w, h).data.buffer);
      }
    }
    // Text-only canvas: isolates font rasterization from geometry.
    const ct = makeCanvas(400, 60);
    const ctt = ct.getContext("2d", { willReadFrequently: true });
    ctt.font = '20px sans-serif';
    ctt.fillText("Phasmid text raster ✓ ∑ ж 漢字", 5, 40);
    out.textOnly = await sha256(ctt.getImageData(0, 0, 400, 60).data.buffer);
    out.contextAttributes = ctx1.getContextAttributes ? ctx1.getContextAttributes() : null;
    return out;
  }

  // Invariants detectors check on canvas readback (CreepJS, CanvasBlocker,
  // TZP). Each holds in stock Chrome, and graphics/canvas-readback.patch must
  // keep it with noise on.
  // "Random" data comes from a fixed generator so runs and contexts agree.
  async function canvasNoiseSection() {
    const out = {};
    let state = 0x2545f491;
    const byte = () => {
      state ^= state << 13; state ^= state >>> 17; state ^= state << 5;
      return (state >>> 0) & 255;
    };
    const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
    const pngBlob = (c) => (c.convertToBlob ? c.convertToBlob({ type: "image/png" }) : new Promise((res) => c.toBlob(res, "image/png")));
    const opts = { willReadFrequently: true };
    const context2d = (w, h, attrs) => makeCanvas(w, h).getContext("2d", attrs || opts);

    // CreepJS getPixelMods: random opaque 1x1 fillRects on 8x8 canvases
    // read back exactly, also after copying each read into a second canvas.
    {
      const flags = { willReadFrequently: true, desynchronized: true };
      const a = context2d(8, 8, flags);
      const b = context2d(8, 8, flags);
      const colors = [];
      for (let x = 0; x < 8; x++) for (let y = 0; y < 8; y++) {
        const rgb = [byte(), byte(), byte()];
        colors.push(rgb);
        a.fillStyle = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, 255)`;
        a.fillRect(x, y, 1, 1);
      }
      let mods = 0;
      let i = 0;
      for (let x = 0; x < 8; x++) for (let y = 0; y < 8; y++, i++) {
        const d = a.getImageData(x, y, 1, 1).data;
        if (!same([...d], [...colors[i], 255])) mods++;
        b.fillStyle = `rgba(${d[0]}, ${d[1]}, ${d[2]}, ${d[3]})`;
        b.fillRect(x, y, 1, 1);
        if (!same([...b.getImageData(x, y, 1, 1).data], [...d])) mods++;
      }
      out.pixelMods = mods;
    }
    // A cleared canvas reads back as zeros.
    {
      const c = context2d(8, 8);
      drawScene(c, 8, 8);
      c.clearRect(0, 0, 8, 8);
      out.clearedIsZero = Math.max(...c.getImageData(0, 0, 8, 8).data) === 0;
    }
    // Random opaque data put at several sizes reads back exactly.
    {
      let exact = true;
      for (const [w, h] of [[1, 1], [10, 10], [16, 8], [300, 150]]) {
        const c = context2d(w, h);
        const img = c.createImageData(w, h);
        for (let i = 0; i < img.data.length; i += 4) {
          img.data[i] = byte(); img.data[i + 1] = byte(); img.data[i + 2] = byte(); img.data[i + 3] = 255;
        }
        c.putImageData(img, 0, 0);
        exact = exact && same(c.getImageData(0, 0, w, h).data, img.data);
      }
      out.putRandomExact = exact;
    }
    // A solid fill reads back exactly; its PNG is compared across runs.
    {
      const canvas = makeCanvas(16, 16);
      const c = canvas.getContext("2d", opts);
      c.fillStyle = "rgb(10, 200, 30)";
      c.fillRect(0, 0, 16, 16);
      const d = c.getImageData(0, 0, 16, 16).data;
      let exact = true;
      for (let i = 0; i < d.length; i += 4) exact = exact && d[i] === 10 && d[i + 1] === 200 && d[i + 2] === 30 && d[i + 3] === 255;
      out.solidExact = exact;
      out.solidPng = await sha256(await pngBlob(canvas));
    }
    // The scene read, put into a second canvas and read again is
    // unchanged; reading or encoding twice gives the same bytes.
    const scene = makeCanvas(300, 150);
    const sc = scene.getContext("2d", opts);
    drawScene(sc, 300, 150);
    const sceneData = sc.getImageData(0, 0, 300, 150);
    {
      const b = context2d(300, 150);
      b.putImageData(sceneData, 0, 0);
      out.inOutStable = same(b.getImageData(0, 0, 300, 150).data, sceneData.data);
      out.readTwiceIdentical = same(sc.getImageData(0, 0, 300, 150).data, sceneData.data);
      out.encodeTwiceIdentical = (await sha256(await pngBlob(scene))) === (await sha256(await pngBlob(scene)));
    }
    // CreepJS: a 2x2 canvas too small for any pixel to have two neighbours.
    {
      const c = context2d(2, 2);
      c.fillStyle = "#000";
      c.fillRect(0, 0, 2, 2);
      c.fillStyle = "#fff";
      c.beginPath();
      c.arc(0, 0, 2, 0, 1, true);
      c.closePath();
      c.fill();
      out.lowEntropy2x2 = [...c.getImageData(0, 0, 2, 2).data].join("");
    }
    // A blank canvas encodes the same everywhere.
    {
      const canvas = makeCanvas(300, 150);
      canvas.getContext("2d");
      out.blankPng = await sha256(await pngBlob(canvas));
    }
    // The scene copied 1:1 to another position reads back the same inside
    // its 1-pixel border.
    {
      const b = context2d(320, 170);
      b.drawImage(scene, 7, 5);
      const moved = b.getImageData(7, 5, 300, 150).data;
      let inside = true;
      for (let y = 1; y < 149 && inside; y++) {
        for (let x = 1; x < 299; x++) {
          const i = (y * 300 + x) * 4;
          if (moved[i] !== sceneData.data[i] || moved[i + 1] !== sceneData.data[i + 1] || moved[i + 2] !== sceneData.data[i + 2] || moved[i + 3] !== sceneData.data[i + 3]) { inside = false; break; }
        }
      }
      out.translatedInteriorMatches = inside;
    }
    // Encoded, decoded and drawn again 1:1, through an ImageBitmap and, in a
    // window, an <img>.
    {
      const bitmap = await createImageBitmap(await pngBlob(scene));
      const c = context2d(300, 150);
      c.drawImage(bitmap, 0, 0);
      out.decodeRoundTrip = same(c.getImageData(0, 0, 300, 150).data, sceneData.data);
      if (hasDOM) {
        const img = new Image();
        img.src = URL.createObjectURL(await pngBlob(scene));
        await img.decode();
        const d = context2d(300, 150);
        d.drawImage(img, 0, 0);
        out.decodeRoundTripImg = same(d.getImageData(0, 0, 300, 150).data, sceneData.data);
        URL.revokeObjectURL(img.src);
      }
    }
    // Text on a transparent canvas (alpha ramps) survives read, put, read.
    {
      const c = context2d(220, 40);
      c.font = "20px sans-serif";
      c.fillStyle = "#069";
      c.fillText("Phasmid text ✓ ∑", 4, 28);
      const first = c.getImageData(0, 0, 220, 40);
      const d = context2d(220, 40);
      d.putImageData(first, 0, 0);
      out.transparentTextStable = same(d.getImageData(0, 0, 220, 40).data, first.data);
      out.transparentText = await sha256(first.data.buffer);
    }
    return out;
  }

  const GL_PARAMS = [
    "VENDOR", "RENDERER", "VERSION", "SHADING_LANGUAGE_VERSION",
    "MAX_TEXTURE_SIZE", "MAX_CUBE_MAP_TEXTURE_SIZE", "MAX_RENDERBUFFER_SIZE", "MAX_VIEWPORT_DIMS",
    "MAX_VERTEX_ATTRIBS", "MAX_VERTEX_UNIFORM_VECTORS", "MAX_VARYING_VECTORS", "MAX_FRAGMENT_UNIFORM_VECTORS",
    "MAX_TEXTURE_IMAGE_UNITS", "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_COMBINED_TEXTURE_IMAGE_UNITS",
    "ALIASED_LINE_WIDTH_RANGE", "ALIASED_POINT_SIZE_RANGE",
    "RED_BITS", "GREEN_BITS", "BLUE_BITS", "ALPHA_BITS", "DEPTH_BITS", "STENCIL_BITS",
    "SAMPLES", "SAMPLE_BUFFERS", "SUBPIXEL_BITS", "IMPLEMENTATION_COLOR_READ_FORMAT", "IMPLEMENTATION_COLOR_READ_TYPE",
    // WebGL2
    "MAX_3D_TEXTURE_SIZE", "MAX_ARRAY_TEXTURE_LAYERS", "MAX_COLOR_ATTACHMENTS", "MAX_DRAW_BUFFERS", "MAX_ELEMENT_INDEX",
    "MAX_ELEMENTS_INDICES", "MAX_ELEMENTS_VERTICES", "MAX_FRAGMENT_INPUT_COMPONENTS", "MAX_FRAGMENT_UNIFORM_BLOCKS",
    "MAX_FRAGMENT_UNIFORM_COMPONENTS", "MAX_PROGRAM_TEXEL_OFFSET", "MIN_PROGRAM_TEXEL_OFFSET", "MAX_SAMPLES",
    "MAX_SERVER_WAIT_TIMEOUT", "MAX_TEXTURE_LOD_BIAS", "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS",
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS", "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS", "MAX_UNIFORM_BLOCK_SIZE",
    "MAX_UNIFORM_BUFFER_BINDINGS", "MAX_VARYING_COMPONENTS", "MAX_VERTEX_OUTPUT_COMPONENTS", "MAX_VERTEX_UNIFORM_BLOCKS",
    "MAX_VERTEX_UNIFORM_COMPONENTS", "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS", "MAX_COMBINED_UNIFORM_BLOCKS",
    "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS", "UNIFORM_BUFFER_OFFSET_ALIGNMENT", "MAX_CLIENT_WAIT_TIMEOUT_WEBGL",
  ];

  async function webglSection(kind) {
    const canvas = makeCanvas(256, 128);
    const gl = canvas.getContext(kind, { preserveDrawingBuffer: true });
    if (!gl) return { supported: false };
    const out = { supported: true, params: {}, extensions: (gl.getSupportedExtensions() || []).slice().sort() };
    out.contextAttributes = gl.getContextAttributes();
    const norm = (v) => (v && typeof v === "object" && typeof v.length === "number") ? [...v] : v;
    for (const name of GL_PARAMS) {
      if (gl[name] === undefined) continue;
      try { out.params[name] = norm(gl.getParameter(gl[name])); } catch (e) { out.params[name] = { error: errStr(e) }; }
    }
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    out.unmaskedVendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null;
    out.unmaskedRenderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null;
    const aniso = gl.getExtension("EXT_texture_filter_anisotropic");
    out.maxAnisotropy = aniso ? gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT) : null;
    const dbs = gl.getExtension("WEBGL_draw_buffers");
    out.maxDrawBuffersWebgl = dbs ? gl.getParameter(dbs.MAX_DRAW_BUFFERS_WEBGL) : null;
    // Shader precision formats.
    out.precision = {};
    for (const shader of ["VERTEX_SHADER", "FRAGMENT_SHADER"]) {
      for (const prec of ["LOW_FLOAT", "MEDIUM_FLOAT", "HIGH_FLOAT", "LOW_INT", "MEDIUM_INT", "HIGH_INT"]) {
        const f = gl.getShaderPrecisionFormat(gl[shader], gl[prec]);
        out.precision[`${shader}.${prec}`] = f ? { rangeMin: f.rangeMin, rangeMax: f.rangeMax, precision: f.precision } : null;
      }
    }
    // Render a gradient triangle and hash readPixels.
    const vsSrc = kind === "webgl2"
      ? "#version 300 es\nin vec2 p;in vec3 c;out vec3 vc;void main(){vc=c;gl_Position=vec4(p,0.0,1.0);gl_PointSize=2.0;}"
      : "attribute vec2 p;attribute vec3 c;varying vec3 vc;void main(){vc=c;gl_Position=vec4(p,0.0,1.0);}";
    const fsSrc = kind === "webgl2"
      ? "#version 300 es\nprecision mediump float;in vec3 vc;out vec4 o;void main(){o=vec4(vc*sin(gl_FragCoord.x*0.1)*0.5+0.5,1.0);}"
      : "precision mediump float;varying vec3 vc;void main(){gl_FragColor=vec4(vc*sin(gl_FragCoord.x*0.1)*0.5+0.5,1.0);}";
    const compile = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, vsSrc));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fsSrc));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-0.9, -0.8, 1, 0, 0, 0.85, -0.7, 0, 1, 0, 0.1, 0.9, 0, 0, 1]), gl.STATIC_DRAW);
    const p = gl.getAttribLocation(prog, "p"), c = gl.getAttribLocation(prog, "c");
    gl.enableVertexAttribArray(p); gl.vertexAttribPointer(p, 2, gl.FLOAT, false, 20, 0);
    gl.enableVertexAttribArray(c); gl.vertexAttribPointer(c, 3, gl.FLOAT, false, 20, 8);
    gl.viewport(0, 0, 256, 128);
    gl.clearColor(0.2, 0.3, 0.4, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    const px = new Uint8Array(256 * 128 * 4);
    gl.readPixels(0, 0, 256, 128, gl.RGBA, gl.UNSIGNED_BYTE, px);
    out.readPixels = await sha256(px.buffer);
    if (hasDOM && canvas.toDataURL) out.toDataURL = await sha256(new TextEncoder().encode(canvas.toDataURL()));
    const lose = gl.getExtension("WEBGL_lose_context");
    if (lose) lose.loseContext();
    return out;
  }

  async function detachedDocumentsSection() {
    const out = {};
    const docs = {
      createHTMLDocument: document.implementation.createHTMLDocument(""),
      domParser: new DOMParser().parseFromString("<html><body></body></html>", "text/html"),
    };
    for (const [name, doc] of Object.entries(docs)) {
      out[name] = await section(async () => {
        const c = doc.createElement("canvas");
        c.width = 300; c.height = 150;
        const ctx2d = c.getContext("2d", { willReadFrequently: true });
        const r = {};
        if (ctx2d) {
          drawScene(ctx2d, 300, 150);
          r.canvasGetImageData = await sha256(ctx2d.getImageData(0, 0, 300, 150).data.buffer);
        } else {
          r.canvas2d = null;
        }
        const g = doc.createElement("canvas");
        const gl = g.getContext("webgl");
        if (gl) {
          const dbg = gl.getExtension("WEBGL_debug_renderer_info");
          r.webglVendor = gl.getParameter(gl.VENDOR);
          r.webglRenderer = gl.getParameter(gl.RENDERER);
          r.unmaskedRenderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null;
          r.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
        } else {
          r.webgl = null;
        }
        return r;
      });
    }
    return out;
  }

  async function webgpuSection() {
    if (!navigator.gpu) return { supported: false };
    // Both exist without an adapter, so they are recorded either way.
    const preferredCanvasFormat = navigator.gpu.getPreferredCanvasFormat();
    const wgslLanguageFeatures = navigator.gpu.wgslLanguageFeatures ? [...navigator.gpu.wgslLanguageFeatures].sort() : null;
    const adapter = await withTimeout(navigator.gpu.requestAdapter(), 5000, "requestAdapter");
    if (!adapter) return { supported: true, adapter: null, preferredCanvasFormat, wgslLanguageFeatures };
    const info = adapter.info || {};
    const limits = {};
    for (const k of Object.getOwnPropertyNames(Object.getPrototypeOf(adapter.limits)).sort()) {
      if (k === "constructor") continue;
      try { const v = adapter.limits[k]; if (typeof v === "number") limits[k] = v; } catch (_) { /* skip */ }
    }
    return {
      supported: true,
      adapter: {
        vendor: info.vendor, architecture: info.architecture, device: info.device, description: info.description,
        subgroupMinSize: info.subgroupMinSize, subgroupMaxSize: info.subgroupMaxSize, isFallbackAdapter: info.isFallbackAdapter ?? adapter.isFallbackAdapter,
        features: [...adapter.features].sort(),
        limits,
      },
      preferredCanvasFormat,
      wgslLanguageFeatures,
    };
  }

  async function audioSection() {
    const out = {};
    if (typeof OfflineAudioContext !== "undefined") {
      const off = new OfflineAudioContext(1, 44100, 44100);
      const osc = off.createOscillator();
      osc.type = "triangle";
      osc.frequency.value = 10000;
      const comp = off.createDynamicsCompressor();
      comp.threshold.value = -50; comp.knee.value = 40; comp.ratio.value = 12; comp.attack.value = 0; comp.release.value = 0.25;
      const analyser = off.createAnalyser();
      analyser.fftSize = 2048;
      osc.connect(comp); comp.connect(analyser); analyser.connect(off.destination);
      osc.start(0);
      const rendered = await withTimeout(off.startRendering(), 10000, "startRendering");
      const data = rendered.getChannelData(0);
      let sum = 0;
      for (let i = 4500; i < 5000; i++) sum += Math.abs(data[i]);
      out.offlineSum4500to5000 = sum;
      out.offlineHash = await sha256(data.buffer);
      out.offlineSampleRate = off.sampleRate;
      out.offlineLength = rendered.length;
      const freq = new Float32Array(analyser.frequencyBinCount);
      analyser.getFloatFrequencyData(freq);
      out.analyserFrequencyHash = await sha256(freq.buffer);
      const timeDomain = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(timeDomain);
      out.analyserTimeDomainHash = await sha256(timeDomain.buffer);
      // Determinism check.
      const off2 = new OfflineAudioContext(1, 44100, 44100);
      const osc2 = off2.createOscillator(); osc2.type = "triangle"; osc2.frequency.value = 10000;
      const comp2 = off2.createDynamicsCompressor();
      comp2.threshold.value = -50; comp2.knee.value = 40; comp2.ratio.value = 12; comp2.attack.value = 0; comp2.release.value = 0.25;
      osc2.connect(comp2); comp2.connect(off2.destination); osc2.start(0);
      const r2 = await withTimeout(off2.startRendering(), 10000, "startRendering");
      out.offlineHashSecondRender = await sha256(r2.getChannelData(0).buffer);
      out.deterministic = out.offlineHash === out.offlineHashSecondRender;
      // Two reads of the same analyser state must be identical.
      const freqAgain = new Float32Array(analyser.frequencyBinCount);
      analyser.getFloatFrequencyData(freqAgain);
      out.analyserRepeatReadIdentical = freq.every((v, i) => Object.is(v, freqAgain[i]));
      const bytes1 = new Uint8Array(analyser.frequencyBinCount), bytes2 = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(bytes1); analyser.getByteFrequencyData(bytes2);
      out.analyserByteRepeatReadIdentical = bytes1.every((v, i) => v === bytes2[i]);
      // getChannelData vs copyFromChannel must be the same bytes.
      const copy = new Float32Array(rendered.length);
      rendered.copyFromChannel(copy, 0);
      out.copyFromChannelMatchesGetChannelData = (await sha256(copy.buffer)) === out.offlineHash;
    } else {
      out.offlineAudioContext = "unavailable";
    }
    if (typeof AudioContext !== "undefined") {
      const ac = new AudioContext();
      out.sampleRate = ac.sampleRate;
      out.baseLatency = ac.baseLatency;
      out.outputLatency = ac.outputLatency;
      out.state = ac.state;
      out.maxChannelCount = ac.destination.maxChannelCount;
      out.destinationChannelCount = ac.destination.channelCount;
      out.sinkId = "sinkId" in ac ? ac.sinkId : undefined;
      await ac.close();
    } else {
      out.audioContext = "unavailable";
    }
    return out;
  }

  // Invariants detectors check on Web Audio (CreepJS, CanvasBlocker, TZP,
  // FPJS). Each holds in stock Chrome, and audio/webaudio.patch must keep it
  // with noise on.
  async function audioInvariantsSection() {
    if (typeof OfflineAudioContext === "undefined") return { offlineAudioContext: "unavailable" };
    const out = {};
    const render = (ctx) => withTimeout(ctx.startRendering(), 10000, "startRendering");
    // The CreepJS and FPJS graph. 5120 frames, a whole number of render
    // quanta, so the analyser saw exactly the buffer's last samples; the
    // first 5000 samples and the reduction equal a 5000-frame render.
    {
      const off = new OfflineAudioContext(1, 5120, 44100);
      const osc = off.createOscillator();
      osc.type = "triangle";
      osc.frequency.value = 10000;
      const comp = off.createDynamicsCompressor();
      comp.threshold.value = -50; comp.knee.value = 40; comp.ratio.value = 12; comp.attack.value = 0; comp.release.value = 0.25;
      const analyser = off.createAnalyser();
      osc.connect(comp); comp.connect(analyser); analyser.connect(off.destination);
      osc.start(0);
      const buffer = await render(off);
      const data = buffer.getChannelData(0);
      let zeros = 0;
      while (zeros < data.length && data[zeros] === 0) zeros++;
      out.leadingZeros = zeros;
      out.reduction = comp.reduction;
      let sum = 0;
      for (let i = 4500; i < 5000; i++) sum += Math.abs(data[i]);
      out.sum4500to5000 = sum;
      // The analyser tail, every read path in either order and in chunks,
      // and the live array agree.
      const tail = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(tail);
      const start = data.length - tail.length;
      out.analyserTailMatchesBuffer = tail.every((v, i) => Object.is(v, data[start + i]));
      const copy = new Float32Array(data.length);
      buffer.copyFromChannel(copy, 0);
      const chunk = new Float32Array(100);
      buffer.copyFromChannel(chunk, 0, 4500);
      out.copyMatchesGetChannelData = copy.every((v, i) => Object.is(v, data[i])) && chunk.every((v, i) => Object.is(v, data[4500 + i]));
      const again = buffer.getChannelData(0);
      again[0] = 0.25;
      out.getChannelDataLive = again === data && buffer.getChannelData(0)[0] === 0.25;
    }
    // A fresh analyser reports -Infinity everywhere.
    {
      const analyser = new OfflineAudioContext(1, 128, 44100).createAnalyser();
      const freq = new Float32Array(analyser.frequencyBinCount);
      analyser.getFloatFrequencyData(freq);
      out.freshAnalyserSilent = freq.every((v) => v === -Infinity);
    }
    // An oscillator connected to nothing renders exact silence.
    {
      const off = new OfflineAudioContext(1, 2048, 44100);
      off.createOscillator().start(0);
      out.silentIsZero = (await render(off)).getChannelData(0).every((v) => v === 0);
    }
    // A script-made buffer is never modified and plays back exactly.
    {
      const values = new Float32Array(1280);
      let s = 0x1234567;
      for (let i = 0; i < values.length; i++) {
        s ^= s << 13; s ^= s >>> 17; s ^= s << 5;
        values[i] = ((s >>> 0) / 4294967296) * 2 - 1;
      }
      const off = new OfflineAudioContext(1, values.length, 44100);
      const source = new AudioBuffer({ length: values.length, sampleRate: 44100, numberOfChannels: 1 });
      source.copyToChannel(values, 0);
      out.scriptBufferUntouched = source.getChannelData(0).every((v, i) => Object.is(v, values[i]));
      const node = off.createBufferSource();
      node.buffer = source;
      node.connect(off.destination);
      node.start(0);
      out.pageBufferPlaysExactly = (await render(off)).getChannelData(0).every((v, i) => Object.is(v, values[i]));
    }
    // A constant input renders as a constant.
    {
      const off = new OfflineAudioContext(1, 1280, 44100);
      const constant = off.createConstantSource();
      constant.offset.value = 0.5;
      constant.connect(off.destination);
      constant.start(0);
      out.constantStaysConstant = (await render(off)).getChannelData(0).every((v) => v === 0.5);
    }
    return out;
  }

  async function speechSection() {
    if (!("speechSynthesis" in window)) return { supported: false };
    const synth = speechSynthesis;
    let voices = synth.getVoices();
    if (!voices.length) {
      await new Promise((res) => {
        const t = setTimeout(res, 1500);
        synth.addEventListener("voiceschanged", () => { clearTimeout(t); res(); }, { once: true });
      });
      voices = synth.getVoices();
    }
    // The list can arrive in parts (Windows: the Google network voices, then
    // the local Microsoft voices), so read again after each voiceschanged
    // until none fires for settleMs.
    const settleMs = 750;
    for (let i = 0; i < 4; i++) {
      const changed = await new Promise((res) => {
        const t = setTimeout(() => res(false), settleMs);
        synth.addEventListener("voiceschanged", () => { clearTimeout(t); res(true); }, { once: true });
      });
      if (!changed) break;
      voices = synth.getVoices();
    }
    return {
      settleMs,
      supported: true,
      voices: voices.map((v) => ({ name: v.name, lang: v.lang, voiceURI: v.voiceURI, localService: v.localService, default: v.default })),
    };
  }

  async function mediaDevicesSection() {
    if (!navigator.mediaDevices) return { supported: false };
    const devs = await withTimeout(navigator.mediaDevices.enumerateDevices(), 5000, "enumerateDevices");
    return {
      supported: true,
      devices: devs.map((d) => ({ kind: d.kind, labelEmpty: d.label === "", deviceIdLength: d.deviceId.length, groupIdLength: d.groupId.length })),
      counts: devs.reduce((acc, d) => { acc[d.kind] = (acc[d.kind] || 0) + 1; return acc; }, {}),
      supportedConstraints: sortedKeys(navigator.mediaDevices.getSupportedConstraints()),
      hasGetDisplayMedia: "getDisplayMedia" in navigator.mediaDevices,
    };
  }

  async function codecsSection() {
    const out = {};
    const types = [
      'video/mp4; codecs="avc1.42E01E"', 'video/mp4; codecs="avc1.64002A"', 'video/mp4; codecs="hev1.1.6.L93.B0"', 'video/mp4; codecs="hvc1.1.6.L93.B0"',
      'video/mp4; codecs="av01.0.08M.08"', 'video/mp4; codecs="vp09.00.10.08"', 'video/mp4; codecs="mp4a.40.2"', 'video/mp4; codecs="ac-3"', 'video/mp4; codecs="ec-3"',
      'video/webm; codecs="vp8"', 'video/webm; codecs="vp9"', 'video/webm; codecs="av01.0.08M.08"', 'video/webm; codecs="vorbis"', 'video/webm; codecs="opus"',
      'video/ogg; codecs="theora"', 'audio/ogg; codecs="vorbis"', 'audio/ogg; codecs="opus"', 'audio/ogg; codecs="flac"', 'audio/flac', 'audio/wav', 'audio/mpeg', 'audio/aac', 'audio/mp4; codecs="mp4a.40.2"',
      'video/mp2t', 'application/vnd.apple.mpegurl', 'application/x-mpegURL', 'video/3gpp', 'video/x-matroska; codecs="avc1.42E01E"', 'video/quicktime',
      'video/mp4; codecs="dvh1.05.06"', 'video/mp4; codecs="dvhe.08.07"', 'video/mp4; codecs="avc3.42E01E"', 'audio/mp4; codecs="opus"', 'audio/mp4; codecs="flac"', 'audio/mp4; codecs="ac-4"',
    ];
    if (hasDOM) {
      const v = document.createElement("video");
      out.canPlayType = Object.fromEntries(types.map((t) => [t, v.canPlayType(t)]));
    }
    if (typeof MediaSource !== "undefined") {
      out.mediaSourceIsTypeSupported = Object.fromEntries(types.map((t) => [t, MediaSource.isTypeSupported(t)]));
    } else {
      out.mediaSource = "unavailable";
    }
    if (typeof ManagedMediaSource !== "undefined") out.managedMediaSource = true;
    if (navigator.mediaCapabilities) {
      const configs = {
        h264_1080p: { type: "file", video: { contentType: 'video/mp4; codecs="avc1.64002A"', width: 1920, height: 1080, bitrate: 8000000, framerate: 30 } },
        hevc_1080p: { type: "file", video: { contentType: 'video/mp4; codecs="hev1.1.6.L93.B0"', width: 1920, height: 1080, bitrate: 8000000, framerate: 30 } },
        vp9_4k: { type: "file", video: { contentType: 'video/webm; codecs="vp09.00.50.08"', width: 3840, height: 2160, bitrate: 20000000, framerate: 60 } },
        av1_1080p: { type: "file", video: { contentType: 'video/mp4; codecs="av01.0.08M.08"', width: 1920, height: 1080, bitrate: 6000000, framerate: 30 } },
        av1_4k_hdr: { type: "file", video: { contentType: 'video/mp4; codecs="av01.0.13M.10"', width: 3840, height: 2160, bitrate: 20000000, framerate: 60, transferFunction: "pq", colorGamut: "rec2020" } },
        aac: { type: "file", audio: { contentType: 'audio/mp4; codecs="mp4a.40.2"', channels: "2", bitrate: 128000, samplerate: 48000 } },
        opus: { type: "file", audio: { contentType: 'audio/webm; codecs="opus"', channels: "2", bitrate: 128000, samplerate: 48000 } },
        ac3: { type: "file", audio: { contentType: 'audio/mp4; codecs="ac-3"', channels: "6", bitrate: 384000, samplerate: 48000 } },
        h264_mse: { type: "media-source", video: { contentType: 'video/mp4; codecs="avc1.64002A"', width: 1920, height: 1080, bitrate: 8000000, framerate: 30 } },
      };
      out.decodingInfo = {};
      for (const [k, cfg] of Object.entries(configs)) {
        out.decodingInfo[k] = await section(async () => {
          const r = await withTimeout(navigator.mediaCapabilities.decodingInfo(cfg), 5000, "decodingInfo");
          return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
        });
      }
      const encConfigs = {
        vp8_record: { type: "record", video: { contentType: 'video/webm; codecs="vp8"', width: 1280, height: 720, bitrate: 2000000, framerate: 30 } },
        h264_record: { type: "record", video: { contentType: 'video/mp4; codecs="avc1.42E01E"', width: 1280, height: 720, bitrate: 2000000, framerate: 30 } },
      };
      out.encodingInfo = {};
      for (const [k, cfg] of Object.entries(encConfigs)) {
        out.encodingInfo[k] = await section(async () => {
          const r = await withTimeout(navigator.mediaCapabilities.encodingInfo(cfg), 5000, "encodingInfo");
          return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
        });
      }
    }
    if (navigator.requestMediaKeySystemAccess) {
      const ks = {
        widevine: ["com.widevine.alpha", [{ initDataTypes: ["cenc"], videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"', robustness: "SW_SECURE_DECODE" }], audioCapabilities: [{ contentType: 'audio/mp4; codecs="mp4a.40.2"' }] }]],
        widevine_l1: ["com.widevine.alpha", [{ initDataTypes: ["cenc"], videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"', robustness: "HW_SECURE_ALL" }] }]],
        widevine_hevc: ["com.widevine.alpha", [{ initDataTypes: ["cenc"], videoCapabilities: [{ contentType: 'video/mp4; codecs="hev1.1.6.L93.B0"', robustness: "SW_SECURE_DECODE" }] }]],
        clearkey: ["org.w3.clearkey", [{ initDataTypes: ["cenc", "keyids", "webm"], videoCapabilities: [{ contentType: 'video/webm; codecs="vp8"' }] }]],
        playready: ["com.microsoft.playready.recommendation", [{ initDataTypes: ["cenc"], videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"' }] }]],
        fairplay: ["com.apple.fps", [{ initDataTypes: ["sinf"], videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"' }] }]],
      };
      out.keySystems = {};
      for (const [k, [name, cfg]] of Object.entries(ks)) {
        out.keySystems[k] = await section(async () => {
          try {
            const a = await withTimeout(navigator.requestMediaKeySystemAccess(name, cfg), 8000, "requestMediaKeySystemAccess");
            const c = a.getConfiguration();
            return { supported: true, keySystem: a.keySystem, videoRobustness: (c.videoCapabilities || []).map((v) => v.robustness), persistentState: c.persistentState, distinctiveIdentifier: c.distinctiveIdentifier };
          } catch (e) {
            return { supported: false, error: errStr(e) };
          }
        });
      }
    }
    return out;
  }

  async function webauthnSection() {
    if (typeof PublicKeyCredential === "undefined") return { supported: false };
    const out = { supported: true };
    out.isUserVerifyingPlatformAuthenticatorAvailable = await section(() => withTimeout(PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(), 5000, "uvpaa"));
    out.isConditionalMediationAvailable = PublicKeyCredential.isConditionalMediationAvailable
      ? await section(() => withTimeout(PublicKeyCredential.isConditionalMediationAvailable(), 5000, "cma")) : "absent";
    out.getClientCapabilities = PublicKeyCredential.getClientCapabilities
      ? await section(() => withTimeout(PublicKeyCredential.getClientCapabilities(), 5000, "gcc")) : "absent";
    out.staticMethods = Object.getOwnPropertyNames(PublicKeyCredential).filter((k) => typeof PublicKeyCredential[k] === "function").sort();
    return out;
  }

  async function permissionsSection() {
    if (!navigator.permissions) return { supported: false };
    const names = ["geolocation", "notifications", "push", "midi", "camera", "microphone", "background-fetch", "background-sync", "persistent-storage", "ambient-light-sensor", "accelerometer", "gyroscope", "magnetometer", "screen-wake-lock", "nfc", "display-capture", "clipboard-read", "clipboard-write", "payment-handler", "idle-detection", "periodic-background-sync", "storage-access", "window-management", "local-fonts", "top-level-storage-access", "captured-surface-control", "speaker-selection", "bluetooth"];
    const out = {};
    for (const name of names) {
      out[name] = await section(async () => {
        const desc = { name };
        if (name === "push") desc.userVisibleOnly = true;
        if (name === "midi") desc.sysex = true;
        const s = await withTimeout(navigator.permissions.query(desc), 3000, "permissions.query");
        return s.state;
      });
      if (out[name] && out[name].error) out[name] = `error:${out[name].error.split(":")[0]}`;
    }
    if (hasDOM && typeof Notification !== "undefined") out.notificationPermission = Notification.permission;
    return out;
  }

  async function storageSection() {
    if (!navigator.storage || !navigator.storage.estimate) return { supported: false };
    const est = await withTimeout(navigator.storage.estimate(), 5000, "estimate");
    const out = { quota: est.quota, usage: est.usage, usageDetails: est.usageDetails ? sortedKeys(est.usageDetails) : null };
    if (navigator.storage.persisted) out.persisted = await section(() => navigator.storage.persisted());
    if (typeof performance !== "undefined" && performance.memory) {
      out.jsHeapSizeLimit = performance.memory.jsHeapSizeLimit;
    }
    if (typeof navigator.storageBuckets !== "undefined") out.storageBuckets = true;
    return out;
  }

  function chromeObjectSection() {
    if (typeof chrome === "undefined") return { present: false };
    return { present: true, shape: ownTree(chrome, 2), toString: String(chrome) };
  }

  function rectsSection() {
    const host = document.createElement("div");
    host.style.cssText = "position:absolute;left:10.3px;top:20.7px;width:123.45px;height:67.89px;font:14px sans-serif;transform:rotate(0.5deg) scale(1.013)";
    host.innerHTML = "<span>Phasmid client rect probe</span><br><span>second line of text</span>";
    document.body.appendChild(host);
    const r = host.getBoundingClientRect();
    const span = host.querySelector("span");
    const sr = span.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(span);
    const rr = range.getBoundingClientRect();
    const rects = [...span.getClientRects()].map((q) => [q.x, q.y, q.width, q.height]);
    const plain = document.createElement("div");
    plain.style.cssText = "position:absolute;left:33px;top:44px;width:100px;height:50px";
    document.body.appendChild(plain);
    const pr = plain.getBoundingClientRect();
    // Noise that spares absolutely positioned elements with fixed-px left/top
    // still moves an in-flow child and a child at left:10%; inside a 500px
    // container both sit on whole pixels, so their rects relative to the
    // container equal offsetLeft/offsetTop exactly.
    const box = document.createElement("div");
    box.style.cssText = "position:absolute;left:0;top:0;width:500px;height:200px";
    box.innerHTML = '<div style="margin:7px 0 0 9px;width:40px;height:30px"></div>' +
      '<div style="position:absolute;left:10%;top:5%;width:40px;height:30px"></div>';
    document.body.appendChild(box);
    const br = box.getBoundingClientRect();
    const matchesOffsets = (el) => {
      const q = el.getBoundingClientRect();
      return q.x - br.x === el.offsetLeft && q.y - br.y === el.offsetTop && q.width === el.offsetWidth && q.height === el.offsetHeight;
    };
    const out = {
      hostRect: [r.x, r.y, r.width, r.height, r.top, r.right, r.bottom, r.left],
      spanRect: [sr.x, sr.y, sr.width, sr.height],
      rangeRect: [rr.x, rr.y, rr.width, rr.height],
      spanClientRects: rects,
      rangeEqualsSpan: rr.x === sr.x && rr.y === sr.y && rr.width === sr.width && rr.height === sr.height,
      plainRect: [pr.x, pr.y, pr.width, pr.height],
      plainMatchesOffsets: pr.x === plain.offsetLeft + (document.documentElement.getBoundingClientRect().x) && pr.width === plain.offsetWidth,
      plainOffsets: [plain.offsetLeft, plain.offsetTop, plain.offsetWidth, plain.offsetHeight],
      flowMatchesOffsets: matchesOffsets(box.children[0]),
      percentMatchesOffsets: matchesOffsets(box.children[1]),
    };
    host.remove(); plain.remove(); box.remove();
    return out;
  }

  async function webrtcSection(stun) {
    if (typeof RTCPeerConnection === "undefined") return { supported: false };
    const pc = new RTCPeerConnection({ iceServers: stun ? [{ urls: stun }] : [] });
    const cands = [];
    pc.createDataChannel("p");
    const done = new Promise((res) => {
      pc.onicecandidate = (e) => {
        if (!e.candidate) return res();
        const c = e.candidate;
        cands.push({ candidate: c.candidate, type: c.type, protocol: c.protocol, address: c.address, port: c.port, relatedAddress: c.relatedAddress, foundation: c.foundation, component: c.component, priority: c.priority });
      };
      setTimeout(res, 4000);
    });
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await done;
    const sdp = pc.localDescription ? pc.localDescription.sdp : "";
    pc.close();
    return {
      supported: true,
      candidates: cands,
      addresses: [...new Set(cands.map((c) => c.address).filter(Boolean))].sort(),
      candidateTypes: [...new Set(cands.map((c) => c.type))].sort(),
      sdpHasMdns: /\.local\b/.test(sdp),
      sdpLines: sdp.split("\r\n").filter((l) => l.startsWith("a=candidate") || l.startsWith("a=fingerprint") || l.startsWith("a=msid-semantic")).length,
    };
  }

  function globalsSection() {
    const g = typeof globalThis !== "undefined" ? globalThis : self;
    const names = ["OffscreenCanvas", "SharedArrayBuffer", "Atomics", "WebAssembly", "RTCPeerConnection", "speechSynthesis", "webkitSpeechRecognition", "SpeechRecognition", "Notification", "PushManager", "ServiceWorker", "SharedWorker", "Worklet", "AudioWorklet", "WebGL2RenderingContext", "GPU", "GPUAdapter", "Bluetooth", "USB", "HID", "Serial", "NDEFReader", "XRSystem", "PaymentRequest", "Sanitizer", "BarcodeDetector", "FileSystemHandle", "showOpenFilePicker", "EyeDropper", "DocumentPictureInPicture", "LaunchQueue", "Scheduler", "LargestContentfulPaint", "ReportingObserver", "TextDetector", "FaceDetector", "Ink", "WindowControlsOverlay", "navigation", "cookieStore", "trustedTypes", "CSSPropertyRule", "ContentVisibilityAutoStateChangeEvent", "Fence", "SharedStorage", "ModelGeneric", "LanguageModel", "Translator", "Summarizer", "ai", "chrome", "CSS", "queueMicrotask", "scheduler", "PressureObserver", "VideoDecoder", "AudioDecoder", "ImageDecoder", "MediaStreamTrackProcessor", "onbeforeinstallprompt", "onappinstalled", "DeviceOrientationEvent", "DeviceMotionEvent", "ondeviceorientationabsolute", "InputDeviceCapabilities", "Touch", "TouchEvent", "ontouchstart", "PointerEvent", "onpointerrawupdate", "webkitRequestFileSystem", "openDatabase", "caches", "BackgroundFetchManager", "IdleDetector", "ContactsManager", "VirtualKeyboard", "DevicePosture", "AbsoluteOrientationSensor", "AmbientLightSensor", "Accelerometer", "Gyroscope", "Magnetometer", "ScreenDetails", "getScreenDetails", "Highlight", "WebTransport", "HTMLFencedFrameElement", "CloseWatcher", "CookieDeprecationLabel", "ViewTransition", "AnimationTimeline", "ScrollTimeline", "CSSStartingStyleRule", "CompressionStream", "TrustedTypePolicyFactory", "SpeechSynthesisUtterance", "webkitMediaStream", "MIDIAccess", "PerformanceLongAnimationFrameTiming", "Serial", "FileSystemObserver", "AudioData", "VideoFrame", "WebSocketStream", "CaptureController", "CropTarget", "RestrictionTarget", "Gamepad", "GamepadHapticActuator", "Credential", "FederatedCredential", "IdentityCredential", "PasswordCredential", "OTPCredential", "DigitalCredential", "Profiler", "AudioSession", "MediaSession", "WakeLock", "WakeLockSentinel", "Keyboard", "KeyboardLayoutMap", "Presentation", "PresentationRequest", "RemotePlayback", "StorageBucket", "StorageBucketManager", "HTMLPortalElement", "SpeculationRules", "TaskController", "TaskPriorityChangeEvent", "TaskSignal", "SharedWorkerGlobalScope", "DedicatedWorkerGlobalScope"];
    const out = {};
    for (const n of names) out[n] = n in g;
    out.userAgentDataInGlobal = "userAgentData" in navigator;
    return out;
  }

  function errorsSection() {
    // JS engine fingerprints: error message formats and stack shapes.
    const out = {};
    try { null.x; } catch (e) { out.nullProperty = e.message; }
    try { undefinedFunction(); } catch (e) { out.undefinedFunction = e.message; }
    try { new Array(-1); } catch (e) { out.negativeArray = e.message; }
    try { (function f() { f(); })(); } catch (e) { out.recursion = `${e.name}: ${e.message}`; }
    out.stackDepth = (() => { let d = 0; const f = () => { d++; f(); }; try { f(); } catch (_) { /* overflow */ } return d; })();
    out.functionToString = Function.prototype.toString.call(Array.prototype.push);
    out.mathTan = Math.tan(-1e300);
    out.mathSinh = Math.sinh(1);
    out.mathExpm1 = Math.expm1(1);
    out.mathCbrt = Math.cbrt(100);
    out.dateParse = Date.parse("2024-02-30");
    out.intlLocaleOfUndefined = new Intl.DateTimeFormat(undefined).resolvedOptions().locale;
    return out;
  }

  // ---------- entry ----------

  self.__phasmidProbe = async function probe(ctx) {
    const opts = ctx || {};
    const out = {
      meta: {
        slot: opts.slot || null,
        scope: hasDOM ? "window" : (isServiceWorker ? "service_worker" : (typeof SharedWorkerGlobalScope !== "undefined" && self instanceof SharedWorkerGlobalScope ? "shared_worker" : "dedicated_worker")),
        origin: self.location ? self.location.origin : null,
        isSecureContext: typeof isSecureContext !== "undefined" ? isSecureContext : null,
        crossOriginIsolated: typeof crossOriginIsolated !== "undefined" ? crossOriginIsolated : null,
        probeVersion: 1,
      },
    };
    out.navigator = await section(navigatorSection);
    out.intl = await section(intlSection);
    out.globals = await section(globalsSection);
    out.errors = await section(errorsSection);
    out.storage = await section(storageSection);
    out.permissions = await section(permissionsSection);
    out.canvas = await section(canvasSection);
    out.canvasNoise = await section(canvasNoiseSection);
    out.webgl = await section(() => webglSection("webgl"));
    out.webgl2 = await section(() => webglSection("webgl2"));
    out.webgpu = await section(webgpuSection);
    out.audio = await section(audioSection);
    out.audioInvariants = await section(audioInvariantsSection);
    out.codecs = await section(codecsSection);
    out.mediaDevices = await section(mediaDevicesSection);
    out.fonts = await section(fontsSection);
    out.fontsLocal = await section(fontsLocalSection);
    if (hasDOM) {
      out.screen = await section(screenSection);
      out.mediaQueries = await section(mediaQueriesSection);
      out.systemColors = await section(systemColorsSection);
      out.rects = await section(rectsSection);
      out.detachedDocuments = await section(detachedDocumentsSection);
      out.chrome = await section(chromeObjectSection);
      out.speech = await section(speechSection);
      out.webauthn = await section(webauthnSection);
      out.webrtc = await section(() => webrtcSection(opts.stun || null));
    }
    return out;
  };
})();
