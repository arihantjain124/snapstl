// SnapSTL Helper on ShopDeck (2.5, SnapSTL 0.33): shipping labels, in the person's own signed-in Chrome.
//
// It runs on pro.shopdeck.com and only works when SnapSTL has a label job (Print → Completed → Print label).
// It never signs in: on a login page it tells SnapSTL, which waits for a person to sign in and press Continue.
// SnapSTL decides every step; this page only reports what it sees and does what SnapSTL answers:
//   find the order (Print list, else Expired AWB; Single or Clubbed tab) → report it (SnapSTL compares the
//   clubbed id, sub-orders, SKUs with the report) → Regenerate (expired) → change the courier (first one
//   offered from Settings' list; Save → Yes, Change; the AWB must change) → wait (SnapSTL's time) → find again
//   and report (AWB and courier must still match) → press Label (the PDF goes to Downloads; SnapSTL checks it).
// Anything unexpected stops the job with the reason; nothing is retried by itself.
(() => {
  if (window.top !== window) return;

  const PRINT = "/orders/process-orders/non-dispatched";
  const EXPIRED = "/orders/process-orders/expired-awb";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const bg = (type, data = {}) => chrome.runtime.sendMessage({ type, ...data });
  const txt = (e) => ((e && (e.innerText || e.textContent)) || "").trim();
  const shown = (e) => !!(e && e.offsetParent !== null);
  const buttons = (label) => [...document.querySelectorAll("button")].filter((b) => shown(b) && txt(b) === label);
  const buttonStarting = (p) => [...document.querySelectorAll("button")].find((b) => shown(b) && txt(b).startsWith(p));
  const leaves = (root) => [...root.querySelectorAll("*")].filter((e) => e.childElementCount === 0 && shown(e));
  const courierKey = (s) => (s || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  async function until(fn, ms, every = 250) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      try { const v = fn(); if (v) return v; } catch { /* page still changing */ }
      await sleep(every);
    }
    return null;
  }
  const onLoginPage = () => /\/login/i.test(location.pathname) || /Recent Logins/.test(document.body.innerText || "");
  const onOrders = () => location.pathname.startsWith("/orders/process-orders") &&
    /Process Orders/.test(document.body.innerText || "");

  // ---------- reading the list ----------
  // The search shows one order (Single) or one clubbed group: read the results area under the table header.
  function readResults(where, tab) {
    const t = document.body.innerText || "";
    const seen = { where, tab, count: "0", club: "", sub: "", ids: [], skus: [], awb: "", partner: "" };
    const i = t.search(/Orders\s+\d+\s+Product/);
    if (i < 0) return seen;
    const r = t.slice(i);
    seen.count = (r.match(/^Orders\s+(\d+)/) || [])[1] || "0";
    seen.club = (r.match(/Clubbed Orders\s+ID:\s*(NC[A-Z0-9]+)/) || [])[1] || "";
    seen.sub = (r.match(/Sub-Orders\s+(\d+)/) || [])[1] || "";
    seen.ids = [...new Set(r.match(/\bNS[0-9A-F]{16}\b/g) || [])];
    seen.skus = (r.match(/SKU ID:\s*\S+/g) || []).map((s) => s.replace(/SKU ID:\s*/, ""));
    const awbs = [...new Set((r.match(/AWB:\s*\d+/g) || []).map((s) => s.replace(/\D/g, "")))];
    seen.awb = awbs.length === 1 ? awbs[0] : "";
    const partners = [...new Set((r.match(/Partner:\s*[^\n]+/g) || []).map((s) => s.replace(/Partner:\s*/, "").trim()))];
    seen.partner = partners.length === 1 ? partners[0] : partners.join(" / ");
    return seen;
  }

  // ---------- moving around the Process Orders page ----------
  async function openList(where) {
    const path = where === "print" ? PRINT : EXPIRED;
    if (location.pathname !== path) {
      const label = where === "print" ? "Non-Dispatched Orders (" : "Expired AWB Orders (";
      const tabEl = [...document.querySelectorAll("span,div,a")].find((e) => e.childElementCount === 0 && txt(e).startsWith(label));
      if (!tabEl) throw new Error(`Can't find the "${label.trim()}" tab — the ShopDeck page may have changed`);
      tabEl.click();
      if (!(await until(() => location.pathname === path, 15000))) throw new Error(`ShopDeck didn't open ${label.trim()}`);
      await sleep(2000);
    }
    if (where === "print") {
      const p = buttons("Print")[0];
      if (!p) throw new Error("Can't find the Print / Pack / Handover buttons");
      p.click();
      await sleep(800);
    }
  }

  async function chooseTab(tab) {  // "Single" or "Clubbed"
    const b = buttonStarting(`${tab} (`);
    if (!b) throw new Error(`Can't find the ${tab} tab`);
    b.click();
    await sleep(1500);
  }

  const noResult = () => /No result found/i.test(document.body.innerText || "");
  const showsId = (id) => {  // the order (or clubbed ID) in the results under the table header
    const t = document.body.innerText || "";
    const i = t.search(/Orders\s+\d+\s+Product/);
    return i >= 0 && t.slice(i).includes(id);
  };

  const headCount = () => {  // "Orders NN Product": how many orders the list shows
    const m = (document.body.innerText || "").match(/Orders\s+(\d+)\s+Product/);
    return m ? Number(m[1]) : -1;
  };

  const setNative = (input, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };

  async function search(id) {
    const input = await until(() => document.querySelector('input[placeholder="Enter Value"]'), 10000);
    if (!input) throw new Error("Can't find the search box");
    // the search field must be "Order Id" (Expired AWB starts on "Expired AWB")
    const toggles = [...document.querySelectorAll("[role=combobox]")];
    const field = toggles.find((c) => /InputSearch/.test(String(c.parentElement && c.parentElement.className))) || toggles[0];
    if (!field) throw new Error("Can't find the search field picker");
    if (txt(field) !== "Order Id") {
      field.click();
      const opt = await until(() => [...document.querySelectorAll("[role=option]")].find((o) => txt(o) === "Order Id"), 5000);
      if (!opt) throw new Error('Can\'t choose "Order Id" in the search field');
      opt.click();
      await sleep(500);
      if (txt(field) !== "Order Id") throw new Error('The search field didn\'t change to "Order Id"');
    }
    const enter = (v) => {
      input.focus();
      setNative(input, "");
      setNative(input, v);
      for (const type of ["keydown", "keypress", "keyup"]) {
        input.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
      }
    };
    // 2.6: "not in this list" only on ShopDeck's own "No result found" for THIS search. A list that already says
    // so looks the same after a search that finds nothing, so show the whole list first.
    let empty = false;
    if (noResult()) {
      enter("");
      empty = !(await until(() => !noResult(), 6000));  // still nothing: this list is empty
    }
    // the order counts as found only once the list narrowed to it (a full list may show it too)
    const full = headCount();
    const hit = () => showsId(id) && (headCount() <= 1 || (full > 0 && headCount() < full));
    enter(id);
    if (empty) {
      await sleep(2500);
      if (hit()) { await sleep(800); return "found"; }
      if (noResult()) return "none";
    } else {
      const ans = await until(() => (hit() ? "found" : noResult() ? "none" : null), 20000, 200);
      if (ans) {
        await sleep(800);  // let the result settle, then read it again
        if (hit()) return "found";
        if (noResult()) return "none";
      }
    }
    throw new Error("ShopDeck's search didn't answer (neither the order nor No result found) — keep the ShopDeck tab in front");
  }

  async function find(job) {
    const tab = job.clubbed ? "Clubbed" : "Single";
    const why = [];
    for (const where of ["print", "expired"]) {
      await openList(where);
      await chooseTab(tab);
      if ((await search(job.search)) === "found") return readResults(where, tab.toLowerCase());
      why.push(`${where === "print" ? "Print" : "Expired AWB"}: No result found`);
    }
    return { where: "none", why: why.join("; ") };
  }

  // ---------- actions (each only after SnapSTL said so) ----------
  async function regenerate(job) {
    const b = buttons("Regenerate");
    if (b.length !== 1) throw new Error(`Expected one Regenerate button, found ${b.length}`);
    const ok = await bg("sd_step", { job_id: job.id, step: "regenerating" });
    if (!ok || ok.do !== "go") return;
    b[0].click();
    const end = Date.now() + (job.regenerate_timeout_s || 180) * 1000;
    while (Date.now() < end) {
      await sleep(8000);
      if (buttons("Yes").length || buttons("Confirm").length) {
        throw new Error("Regenerate asked for a confirmation SnapSTL doesn't know — finish it by hand on ShopDeck");
      }
      if ((await search(job.search)) === "none") {  // gone from Expired AWB (ShopDeck said No result found)
        await bg("sd_step", { job_id: job.id, step: "regenerated" });
        return;
      }
    }
    throw new Error("Regenerate didn't finish in time — the order is still in Expired AWB (regenerate it by hand)");
  }

  function drawer() {
    const h = leaves(document.body).find((e) => txt(e) === "Edit Logistics");
    if (!h) return null;
    let d = h;
    for (let i = 0; i < 12 && d; i++) {
      d = d.parentElement;
      if (d && /Available delivery partner/.test(txt(d)) && buttons("Save").some((b) => d.contains(b))) return d;
    }
    return null;
  }

  function offered(d) {
    // courier cards after the "Available delivery partner" heading (the "Current" card comes before it)
    const all = leaves(d);
    const h = all.findIndex((e) => txt(e) === "Available delivery partner");
    if (h < 0) return [];
    return all.slice(h + 1).filter((e) => /^[A-Z0-9]+(?:_[A-Z0-9]+)*$/.test(txt(e)) && txt(e).length > 3);
  }

  async function logistic(job, tab) {
    const b = buttons("Logistic");
    if (b.length !== 1) throw new Error(`Expected one Logistic button, found ${b.length}`);
    b[0].click();
    const d = await until(drawer, 10000);
    if (!d) throw new Error("The Edit Logistics panel didn't open");
    // 2.5.1: Show more is a button with an icon inside (not a leaf); Bluedart is often only behind it
    await until(() => offered(d).length, 8000);
    const more = () => [...d.querySelectorAll("button")].find((x) => shown(x) &&
      (x.dataset.sdEvent === "showMoreShippingPartner" || /^show more$/i.test(txt(x))));
    for (let i = 0; i < 3 && more(); i++) {
      more().click();
      await until(() => !more(), 5000);
    }
    if (more()) throw new Error("Show more didn't open in the Edit Logistics panel — couriers may be hidden");
    await sleep(800);
    const cards = offered(d);
    let pick = null;
    for (const c of job.couriers) {
      pick = cards.find((e) => courierKey(txt(e)) === courierKey(c));
      if (pick) break;
    }
    if (!pick) {
      throw new Error(`None of the couriers in Settings is offered (ShopDeck offers: ${cards.map(txt).join(", ") || "none"})`);
    }
    const courier = txt(pick);
    const ok = await bg("sd_step", { job_id: job.id, step: "saving", data: { courier, offered: cards.map(txt), more_hidden: !!more() } });
    if (!ok || ok.do !== "go") return;
    pick.click();
    await sleep(600);
    const save = [...d.querySelectorAll("button")].find((x) => shown(x) && txt(x) === "Save");
    if (!save) throw new Error("Can't find Save in the Edit Logistics panel");
    save.click();
    const yes = await until(() => buttons("Yes,Change")[0] || buttons("Yes, Change")[0], 8000);
    if (yes) yes.click();
    // the card shows the new AWB once ShopDeck has it
    const seen = await until(() => {
      if (drawer()) return null;
      const s = readResults("print", tab);
      return s.awb && s.awb !== job.old_awb ? s : null;
    }, 60000, 1000);
    if (!seen) throw new Error("The AWB didn't change after saving the courier — check the order on ShopDeck by hand");
    await bg("sd_step", { job_id: job.id, step: "saved", data: { awb: seen.awb, courier: seen.partner || courier } });
  }

  async function pressLabel(job, tab) {
    const b = buttons("Label");
    if (b.length !== 1) throw new Error(`Expected one Label button, found ${b.length}`);
    const s = readResults("print", tab);  // nothing changed since SnapSTL's check?
    if (s.awb !== job.new_awb) throw new Error(`The AWB on the page is ${s.awb || "unreadable"}, expected ${job.new_awb}`);
    // ShopDeck opens the label in a new window; Chrome blocks it as a pop-up when the helper clicks
    const pop = await bg("sd_popups");
    if (!pop || !pop.ok) throw new Error(`Pop-ups for ShopDeck are blocked in Chrome (${(pop && pop.error) || "can't allow them"}) — allow them, then try again`);
    const ok = await bg("sd_step", { job_id: job.id, step: "pressing" });
    if (!ok || ok.do !== "go") return;
    b[0].click();
    await sleep(1500);
    await bg("sd_step", { job_id: job.id, step: "pressed" });
  }

  // ---------- the loop ----------
  // 2.6: a heartbeat while a job is on, also while waiting — SnapSTL stops a job whose tab goes quiet
  // (Chrome slows background tabs to about once a minute and freezes them later).
  let current = null;
  const beat = () => { if (current) bg("sd_alive", { job_id: current, visible: document.visibilityState }).catch(() => {}); };
  setInterval(beat, 8000);
  document.addEventListener("visibilitychange", beat);
  let busy = false;
  async function tick() {
    if (busy) return;
    busy = true;
    let job = null;
    try {
      const r = await bg("sd_next");
      if (!r || r.error || !r.job) {  // nothing to do, another tab works, labels off, sign-in or PDF awaited
        if (!(r && (r.login || r.waiting))) current = null;
        return;
      }
      job = r.job;
      if (current !== job.id) { current = job.id; beat(); }
      if (onLoginPage()) { await bg("sd_login", { job_id: job.id }); return; }
      if (!location.pathname.startsWith("/orders/process-orders")) { location.assign(PRINT); return; }  // resumes after loading
      if (!(await until(onOrders, 20000))) { await bg("sd_login", { job_id: job.id }); return; }
      const tab = job.clubbed ? "clubbed" : "single";
      if (job.step === "regenerate") { await regenerate(job); return; }
      if (job.step === "logistic") { await logistic(job, tab); return; }
      if (job.step === "regenerating") {
        throw new Error("The page reloaded while regenerating — check the order on ShopDeck by hand");
      }
      // find / found_again / saving (reloaded mid-Save) / wait / label: find it and let SnapSTL compare
      const seen = await find(job);
      const ans = await bg("sd_seen", { job_id: job.id, seen });
      if (!ans || ans.error && ans.do !== "stop") throw new Error((ans && ans.error) || "SnapSTL didn't answer");
      if (ans.do === "regenerate") await regenerate({ ...job, step: "regenerate" });
      else if (ans.do === "logistic") await logistic({ ...job, old_awb: seen.awb }, tab);
      else if (ans.do === "wait") await sleep(Math.max(0, (ans.wait_until - ans.now) * 1000) + 500);
      else if (ans.do === "label") await pressLabel(job, tab);
      // "stop": SnapSTL stopped the job and shows why
    } catch (e) {
      if (job) await bg("sd_fail", { job_id: job.id, error: String((e && e.message) || e) }).catch(() => {});
    } finally {
      busy = false;
    }
  }
  setTimeout(tick, 2500);
  setInterval(tick, 4000);
})();
