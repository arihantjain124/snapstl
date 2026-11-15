// SnapSTL Helper service worker: relays between the working MakerWorld tab and the SnapSTL app
// on this Mac (http://127.0.0.1:8317). No tokens: the app accepts these calls only from this computer.
//
// The helper works in ONE tab ("working tab") and only while that tab is on the exact model page
// set in SnapSTL → Settings → MakerWorld model page link (matched by its designId).
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
    cfgCache = await app("/api/helper/config");  // {model_url, design_id}
    cfgAt = Date.now();
  }
  return cfgCache;
}

function onModelPage(url, cfg) {
  try {
    const u = new URL(url);
    return u.hostname.endsWith("makerworld.com") && u.pathname.includes("/makerlab/parametricModelMaker") &&
      !!cfg.design_id && u.searchParams.get("designId") === String(cfg.design_id);
  } catch { return false; }
}

async function tab(id) {
  try { return await chrome.tabs.get(id); } catch { return null; }
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
          status: (msg.status || "").slice(0, 150), on_page: msg.onPage ? "1" : "0",
        }));
      case "result": {
        const fd = new FormData();
        fd.append("plate_1", new Blob([msg.plate_1], { type: "text/plain" }), "plate_1.obj");
        fd.append("plate_2", new Blob([msg.plate_2], { type: "text/plain" }), "plate_2.obj");
        return await app(`/api/helper/jobs/${msg.job_id}/result`, { method: "POST", body: fd });
      }
      case "fail":
        return await postJSON(`/api/helper/jobs/${msg.job_id}/fail`, { error: msg.error });
      case "release":
        return await postJSON(`/api/helper/jobs/${msg.job_id}/release`);

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
          return { error: "This tab isn't the model page set in SnapSTL. Click 'Open model page' first." };
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
