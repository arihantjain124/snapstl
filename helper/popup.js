const $ = (id) => document.getElementById(id);
const send = (type) => chrome.runtime.sendMessage({ type });
const set = (id, text, cls = "") => { $(id).textContent = text; $(id).className = cls; };

async function refresh() {
  const s = await send("popupState");
  set("app", s.appOk ? "Connected" : "Not running", s.appOk ? "ok" : "err");
  set("model", s.cfg && s.cfg.design_id ? `designId ${s.cfg.design_id}` : "—", "model");
  if (!s.working) set("work", "None yet", "warn");
  else set("work", s.working.onPage ? "On the model page" : "Not on the model page", s.working.onPage ? "ok" : "err");
  $("status").textContent = s.status || "—";
  const cur = s.current;
  $("use").disabled = !s.appOk || !cur || !cur.onPage || cur.isWorking;
  $("use").textContent = cur && cur.isWorking ? "This is the working tab" : "Use this tab";
  $("open").disabled = !s.appOk;
}

$("open").onclick = async () => { await send("openTab"); window.close(); };
$("use").onclick = async () => {
  const r = await send("useThisTab");
  $("msg").textContent = r && r.error ? r.error : "This tab is now the working tab.";
  $("msg").className = "hint " + (r && r.error ? "err" : "ok");
  refresh();
};
refresh();
setInterval(refresh, 2000);
