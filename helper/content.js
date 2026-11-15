// SnapSTL Helper — runs on makerworld.com pages, but only WORKS in the working tab and only while
// that tab is on the exact model page set in SnapSTL (Settings → MakerWorld model page link,
// matched by designId). MakerWorld changes pages without reloading, so the page is re-checked
// before every name.
// Loop: ask the SnapSTL app for the next name → fill the form → press Generate → send the two
// model files back. One name at a time, a short pause between names. Security checks are left to
// the person using the tab: the helper simply waits.
(() => {
  if (window.top !== window) return; // not inside iframes

  const GENERATE_TIMEOUT_MS = 180000;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const bg = (type, data = {}) => chrome.runtime.sendMessage({ type, ...data });

  // ---------- model files seen by inject.js ----------
  let captured = {};
  let requests = [];  // bodies of the page's own requests while Generate is pressed (see inject.js)
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.__snapstl !== true) return;
    if (e.data.kind === "obj") captured[e.data.plate] = e.data.text;
    if (e.data.kind === "req" && requests.length < 50) requests.push(e.data.body || "");
  });
  // Did the Generate request carry the password? "sent" / "missing" / "unknown".
  // "missing" only when a request plainly carried the name but none carried the password.
  function requestCheck(name, password) {
    const forms = (v) => [v, JSON.stringify(v).slice(1, -1), encodeURIComponent(v), encodeURIComponent(v).replace(/%20/g, "+")];
    const has = (body, v) => forms(v).some((f) => f && body.includes(f));
    if (requests.some((b) => has(b, password))) return "sent";
    if (requests.some((b) => has(b, name))) return "missing";
    return "unknown";
  }

  // ---------- page state ----------
  const generateButton = () =>
    [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Generate" && !b.disabled) || null;
  const formShown = () => [...document.querySelectorAll("button")].some((b) => b.textContent.trim() === "Generate");
  const blocked = () => {
    const t = (document.title || "").toLowerCase();
    return t.includes("just a moment") || !!document.querySelector("iframe[src*='challenges.cloudflare.com']");
  };
  const menuOpen = () => [...document.querySelectorAll(".MuiMenu-root, .MuiPopover-root")]
    .some((m) => m.getAttribute("aria-hidden") !== "true" && getComputedStyle(m).visibility !== "hidden");

  // ---------- form ----------
  const TEXT = "input:not([type=range]):not([type=hidden]):not(.MuiSelect-nativeInput)";
  const field = (label) => {
    const xp = `//div[contains(@class,'MuiAccordionDetails-root')]//*[normalize-space(text())='${label}']/ancestor::div[.//input][1]`;
    const n = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    if (!n) throw new Error(`Can't find the "${label}" control (the MakerWorld page may have changed)`);
    return n;
  };
  const setNative = (input, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  async function setText(label, value) {
    const box = field(label).querySelector(TEXT);
    box.focus(); setNative(box, value); box.blur(); await sleep(80);
    if (field(label).querySelector(TEXT).value !== value) throw new Error(`Couldn't set ${label}`);
  }
  async function setNumber(label, value) {
    const box = field(label).querySelector(TEXT);
    box.focus(); setNative(box, String(value));
    for (const t of ["keydown", "keyup"]) box.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
    box.blur(); await sleep(150);
    const r = field(label).querySelector("input[type=range]");
    if (r && r.value !== String(value)) { setNative(r, String(value)); await sleep(150); }
    const r2 = field(label).querySelector("input[type=range]");
    if (r2 && r2.value !== String(value)) throw new Error(`Couldn't set ${label} to ${value}`);
  }
  async function setSelect(label, value) {
    const combo = () => field(label).querySelector("[role=combobox]");
    if (combo().innerText.trim() === value) return;
    combo().dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
    let item = null;
    for (let i = 0; i < 40 && !item; i++) {
      await sleep(100);
      item = [...document.querySelectorAll("[role=option],[role=menuitem]")].find((li) => li.innerText.trim() === value);
    }
    if (!item) throw new Error(`Option "${value}" not found for ${label}`);
    item.click(); await sleep(250);
    if (combo().innerText.trim() !== value) throw new Error(`Couldn't select ${value} for ${label}`);
  }
  function errorToast() {
    for (const a of document.querySelectorAll("[role=alert]")) {
      const t = a.innerText.trim();
      if (/fail|error|invalid/i.test(t)) return t;
    }
    return "";
  }

  async function waitWhileBlocked(say) {
    while (blocked()) {
      await say("Complete the MakerWorld security check in this tab");
      await sleep(2000);
    }
  }

  const valueOf = (label) => field(label).querySelector(TEXT).value;
  const PASSWORD = "License password";

  async function runJob(job, say) {
    const p = job.params;
    const pw = job.license_password || "";
    // SnapSTL only downloads with the model's license password (no password = watermarked model).
    if (!pw) throw new Error("No license password for this model in SnapSTL — not generating");
    requests = [];  // watch the page's requests from now on (a password check may happen before Generate)
    window.postMessage({ __snapstlArm: true }, location.origin);
    await setText(PASSWORD, pw);  // first, so the page has the most time to take it in
    await setSelect("Print version", "NO AMS");
    await setText("Name line 1", p.name);
    await setText("Name line 2", "");
    await setSelect("Font", p.font);
    await setNumber("Font size", p.font_size);
    await setNumber("Thickness", p.thickness);
    await setNumber("Outline", p.outline);
    await setNumber("Compartments", p.compartments);
    await setSelect("Rotate text", p.rotate_text);
    await setText(PASSWORD, pw);  // and again last

    let gen = null;  // the page may still be busy with an earlier build: wait up to 90 s
    for (let i = 0; i < 900 && !gen; i++) {
      if (!menuOpen()) gen = generateButton();
      if (!gen) await sleep(100);
    }
    if (!gen) throw new Error("The Generate button isn't available");
    // Let the page take in every value, then confirm them all right before pressing Generate.
    await sleep(800);
    for (let i = 0; i < 3 && valueOf(PASSWORD) !== pw; i++) { await setText(PASSWORD, pw).catch(() => {}); await sleep(600); }
    if (valueOf(PASSWORD) !== pw) throw new Error("The License password box didn't keep the password — not generating");
    if (valueOf("Name line 1") !== p.name) throw new Error("The name box changed before Generate — not generating");
    gen = generateButton();
    if (!gen) throw new Error("The Generate button isn't available");
    captured = {};
    window.postMessage({ __snapstlArm: true }, location.origin);  // re-arm (60 s window)
    gen.click();
    await sleep(300);
    const keptAfter = valueOf(PASSWORD) === pw;
    const end = Date.now() + GENERATE_TIMEOUT_MS;
    let lastPing = Date.now();
    while (Date.now() < end) {
      if (captured.plate_1 && captured.plate_2) {
        window.postMessage({ __snapstlDisarm: true }, location.origin);
        // password_set: the box held the password right before the click (checked above — we never
        // click otherwise). kept_after is informational only: MakerWorld may clear or redraw the
        // box once Generate starts, so it is not a reason to reject.
        const check = { password_set: true, kept_after: keptAfter, request: requestCheck(p.name, pw),
                        helper: chrome.runtime.getManifest().version };
        requests = [];
        return { plate_1: captured.plate_1, plate_2: captured.plate_2, check: JSON.stringify(check) };
      }
      if (Date.now() - lastPing > 5000) { await say(`Generating ${p.name} (${p.compartments} compartments)`); lastPing = Date.now(); }
      if (blocked()) await waitWhileBlocked(say);
      const err = errorToast();
      if (err) throw new Error(`MakerWorld: ${err}`);
      await sleep(500);
    }
    window.postMessage({ __snapstlDisarm: true }, location.origin);
    requests = [];
    throw new Error("Timed out waiting for MakerWorld to generate the model");
  }

  // ---------- main loop ----------
  function onModelPage(cfg) {
    return location.pathname.includes("/makerlab/parametricModelMaker") && !!cfg.design_id &&
      new URLSearchParams(location.search).get("designId") === String(cfg.design_id);
  }

  async function main() {
    let lastSaid = "", recovered = false, cfg = null;
    let onPage = false;
    const say = async (text) => { lastSaid = text; await bg("status", { text, onPage }); };
    while (true) {
      try {
        try { cfg = await bg("config"); } catch { cfg = null; }
        if (!cfg || cfg.error) { await sleep(5000); continue; } // SnapSTL app not running
        onPage = onModelPage(cfg);
        const claim = await bg("claimTab", { onPage });
        if (!claim.ok) { await sleep(4000); continue; } // another tab is the working tab: stay quiet
        if (!recovered) { await bg("recover"); recovered = true; } // a name left half-done goes back to the queue
        if (!onPage) {
          await say("This tab isn't on the SnapSTL model page — use 'Open model page' in the helper");
          await bg("next", { status: lastSaid, onPage: false }); // tells the app; never gets work
          await sleep(3000);
          continue;
        }
        if (blocked()) { await waitWhileBlocked(say); continue; }
        if (!formShown()) { await say("Waiting for the MakerWorld page to load"); await sleep(2000); continue; }
        const r = await bg("next", { status: lastSaid, onPage: true });
        if (r.error) { await say(`SnapSTL app not reachable — is it running? (${r.error})`); await sleep(5000); continue; }
        if (r.paused) { await say("Paused in SnapSTL"); await sleep(4000); continue; }
        if (!r.job) { await say("Ready on the model page — nothing to download"); await sleep(4000); continue; }
        const job = r.job;
        await bg("working", { job_id: job.job_id });
        await say(`Generating ${job.name} (${job.compartments} compartments)`);
        try {
          const files = await runJob(job, say);
          await say(`Sending ${job.name} to SnapSTL`);
          const up = await bg("result", { job_id: job.job_id, ...files });
          if (up && up.error) throw new Error(up.error);
          await say(`Done ${job.name}`);
        } catch (e) {
          if (!onModelPage(cfg)) {
            await bg("release", { job_id: job.job_id }); // user navigated away mid-name: back to the queue
          } else {
            await bg("fail", { job_id: job.job_id, error: String(e.message || e) });
            await say(`Failed ${job.name}: ${e.message || e}`);
          }
        }
        await bg("working", { job_id: null });
        await sleep(Math.max(1, job.pause_after_s || 3) * 1000);
      } catch (e) {
        await sleep(3000);
      }
    }
  }
  main();
})();
