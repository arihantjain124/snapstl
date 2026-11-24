// SnapSTL Helper service worker: relays between the working MakerWorld tab and the SnapSTL app
// on this Mac (http://127.0.0.1:8317). No tokens: the app accepts these calls only from this computer.
//
// The helper works in ONE tab ("working tab") and only while that tab is on one of SnapSTL's model pages
// (the organizer's from Settings → MakerWorld model page link, and each name stand's, 2.4), matched by designId.
const APP = "http://127.0.0.1:8317";
const session = chrome.storage.session;

async function app(path, opts = {}) {
  const r = await fetch(APP + path, opts);
  const text = await r.text();
  let j; try { j = JSON.parse(text); } catch { j = { detail: text.slice(0, 200) }; }
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}
const postJSON = (path, body) =>
  app(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

let cfgCache = null, cfgAt = 0;
async function config() {
  if (!cfgCache || Date.now() - cfgAt > 15000) {
    cfgCache = await app("/api/helper/config");  // {model_url, design_id, helper_version}
    cfgAt = Date.now();
    await reloadIfUpdated(cfgCache.helper_version);
  }
  return cfgCache;
}

// After SnapSTL updates itself (git pull), the helper folder on disk has a newer version: reload so
// Chrome uses it. Never mid-name, and only once per version (if Chrome loads the helper from another
// folder the version can't change by reloading — then the popup says to reload it by hand).
async function reloadIfUpdated(want) {
  const have = chrome.runtime.getManifest().version;
  if (!want || want === have) return;
  const { workingJob } = await session.get("workingJob");
  if (workingJob) return;
  const { reloadTried } = await chrome.storage.local.get("reloadTried");
  if (reloadTried === want) return;
  await chrome.storage.local.set({ reloadTried: want });
  chrome.runtime.reload();
}

function onModelPage(url, cfg) {
  try {
    const u = new URL(url);
    const d = u.searchParams.get("designId");
    const known = Object.keys(cfg.pages || {}).concat(cfg.design_id ? [String(cfg.design_id)] : []);
    return u.hostname.endsWith("makerworld.com") && u.pathname.includes("/makerlab/parametricModelMaker") &&
      !!d && known.includes(d);
  } catch { return false; }
}

async function tab(id) {
  try { return await chrome.tabs.get(id); } catch { return null; }
}

// Installed, updated or reloaded: MakerWorld model-maker tabs still run the old page script (Chrome
// doesn't swap it), so reload them to pick up this version.
chrome.runtime.onInstalled.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: "https://makerworld.com/*" });
  for (const t of tabs) if ((t.url || "").includes("/makerlab/parametricModelMaker")) chrome.tabs.reload(t.id);
  // 2.5: ShopDeck's Process Orders tabs get the shipping-label page script
  const sd = await chrome.tabs.query({ url: "https://pro.shopdeck.com/orders/*" });
  for (const t of sd) chrome.tabs.reload(t.id);
});

// ----- ShopDeck shipping labels (2.5, SnapSTL 0.33): one ShopDeck tab works at a time -----
async function shopdeckTab(tabId) {
  const { sdTab } = await session.get("sdTab");
  if (sdTab === tabId) return true;
  if (sdTab && (await tab(sdTab))) return false;
  await session.set({ sdTab: tabId });
  return true;
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const tabId = sender.tab && sender.tab.id;
  (async () => {
    switch (msg.type) {
      // ----- from the MakerWorld tab -----
      case "config":
        return await config();
      case "claimTab": {
        // The working tab is the one picked with "Use this tab", or else the first tab on the model page.
        const { activeTab } = await session.get("activeTab");
        if (activeTab === tabId) return { ok: true };
        if (activeTab && (await tab(activeTab))) return { ok: false };
        if (!msg.onPage) return { ok: false };
        await session.set({ activeTab: tabId });
        return { ok: true };
      }
      case "recover": {
        const { workingJob } = await session.get("workingJob");
        if (workingJob) {
          await postJSON(`/api/helper/jobs/${workingJob}/release`).catch(() => {});
          await session.set({ workingJob: null });
        }
        return {};
      }
      case "working":
        await session.set({ workingJob: msg.job_id });
        return {};
      case "status":
        await session.set({ lastStatus: msg.text, lastStatusAt: Date.now(), lastOnPage: !!msg.onPage });
        postJSON("/api/helper/ping", { status: msg.text, on_page: !!msg.onPage }).catch(() => {});
        return {};
      case "next":
        return await app("/api/helper/next?" + new URLSearchParams({
          status: (msg.status || "").slice(0, 150), on_page: msg.onPage ? "1" : "0", design: msg.design || "",
        }));
      case "result": {
        const fd = new FormData();
        if (msg.model) {  // a name stand (2.4): one OBJ with both colours
          fd.append("model", new Blob([msg.model], { type: "text/plain" }), "model.obj");
          return await app(`/api/helper/jobs/${msg.job_id}/result`, { method: "POST", body: fd });
        }
        fd.append("plate_1", new Blob([msg.plate_1], { type: "text/plain" }), "plate_1.obj");
        fd.append("plate_2", new Blob([msg.plate_2], { type: "text/plain" }), "plate_2.obj");
        if (msg.check) fd.append("check", msg.check);  // license password confirmation (no password inside)
        return await app(`/api/helper/jobs/${msg.job_id}/result`, { method: "POST", body: fd });
      }
      case "fail":
        return await postJSON(`/api/helper/jobs/${msg.job_id}/fail`, { error: msg.error });
      case "release":
        return await postJSON(`/api/helper/jobs/${msg.job_id}/release`);

      // ----- from a ShopDeck tab (shopdeck.js) -----
      case "sd_next":
        if (!(await shopdeckTab(tabId))) return { job: null, other_tab: true };
        return await app("/api/helper/shopdeck/next");
      case "sd_seen":
        return await postJSON(`/api/helper/shopdeck/${msg.job_id}/seen`, msg.seen);
      case "sd_step":
        return await postJSON(`/api/helper/shopdeck/${msg.job_id}/step`, { step: msg.step, ...(msg.data || {}) });
      case "sd_fail":
        return await postJSON(`/api/helper/shopdeck/${msg.job_id}/fail`, { error: msg.error });
      case "sd_login":
        return await postJSON("/api/helper/shopdeck/login", { job_id: msg.job_id || null });

      // ----- from the popup -----
      case "popupState": {
        let cfg = null, appOk = false;
        try { await postJSON("/api/helper/ping", { probe: true }); cfg = await config(); appOk = true; } catch { /* not running */ }
        const { activeTab, lastStatus = "" } = await session.get(["activeTab", "lastStatus"]);
        const work = activeTab ? await tab(activeTab) : null;
        const [cur] = await chrome.tabs.query({ active: true, currentWindow: true });
        return {
          appOk, cfg, status: lastStatus,
          working: work ? { id: work.id, title: work.title || "", onPage: cfg ? onModelPage(work.url || "", cfg) : false } : null,
          current: cur ? { id: cur.id, onPage: cfg ? onModelPage(cur.url || "", cfg) : false, isWorking: work && cur.id === work.id } : null,
        };
      }
      case "useThisTab": {
        const cfg = await config();
        const [cur] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!cur || !onModelPage(cur.url || "", cfg)) {
          return { error: "This tab isn't one of SnapSTL's model pages. Click 'Open model page' first." };
        }
        await session.set({ activeTab: cur.id });
        return { ok: true };
      }
      case "openTab": {
        const cfg = await config();
        const { activeTab } = await session.get("activeTab");
        const work = activeTab ? await tab(activeTab) : null;
        if (work) {
          if (!onModelPage(work.url || "", cfg)) await chrome.tabs.update(work.id, { url: cfg.model_url });
          await chrome.tabs.update(work.id, { active: true });
          await chrome.windows.update(work.windowId, { focused: true });
        } else {
          const t = await chrome.tabs.create({ url: cfg.model_url, active: true });
          await session.set({ activeTab: t.id });
        }
        return {};
      }
      default:
        return { error: "unknown message" };
    }
  })()
    .then((r) => reply(r))
    .catch((e) => reply({ error: String(e.message || e) }));
  return true;
});
