// SnapSTL Helper service worker: relays between the MakerWorld tab and the SnapSTL app
// on this Mac (http://127.0.0.1:8317). No settings, no tokens: the app only accepts these
// calls from this computer.
const APP = "http://127.0.0.1:8317";
const MODEL_URL =
  "https://makerworld.com/en/makerlab/parametricModelMaker?designId=2600234&from=model_page&modelName=Custom_Name_Desk_Organizer_v2.34.scad&protected=true&unikey=70189420-efcd-4593-a07c-771fe72d52b3";

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

async function tabAlive(id) {
  try { await chrome.tabs.get(id); return true; } catch { return false; }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const tabId = sender.tab && sender.tab.id;
  (async () => {
    switch (msg.type) {
      case "claimTab": {
        const { activeTab } = await session.get("activeTab");
        if (activeTab && activeTab !== tabId && (await tabAlive(activeTab))) return { ok: false };
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
        await session.set({ lastStatus: msg.text, lastStatusAt: Date.now() });
        postJSON("/api/helper/ping", { status: msg.text }).catch(() => {});
        return {};
      case "next":
        return await app("/api/helper/next?" + new URLSearchParams({ status: (msg.status || "").slice(0, 150) }));
      case "result": {
        const fd = new FormData();
        fd.append("plate_1", new Blob([msg.plate_1], { type: "text/plain" }), "plate_1.obj");
        fd.append("plate_2", new Blob([msg.plate_2], { type: "text/plain" }), "plate_2.obj");
        return await app(`/api/helper/jobs/${msg.job_id}/result`, { method: "POST", body: fd });
      }
      case "fail":
        return await postJSON(`/api/helper/jobs/${msg.job_id}/fail`, { error: msg.error });
      // ----- popup -----
      case "popupState": {
        const s = await session.get(["activeTab", "lastStatus", "lastStatusAt"]);
        let appOk = false;
        try { await postJSON("/api/helper/ping", { probe: true }); appOk = true; } catch { /* app not running */ }
        const tabOk = s.activeTab ? await tabAlive(s.activeTab) : false;
        return { appOk, tabOk, status: s.lastStatus || "", at: s.lastStatusAt || 0 };
      }
      case "openTab": {
        const { activeTab } = await session.get("activeTab");
        if (activeTab && (await tabAlive(activeTab))) {
          const t = await chrome.tabs.update(activeTab, { active: true });
          await chrome.windows.update(t.windowId, { focused: true });
        } else {
          await chrome.tabs.create({ url: MODEL_URL, active: true });
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
