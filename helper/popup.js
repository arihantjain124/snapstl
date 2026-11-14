const $ = (id) => document.getElementById(id);
async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: "popupState" });
  $("app").textContent = s.appOk ? "Connected" : "Not running";
  $("app").className = s.appOk ? "ok" : "err";
  $("tab").textContent = s.tabOk ? "Open" : "Not open";
  $("tab").className = s.tabOk ? "ok" : "err";
  $("status").textContent = s.status || "—";
}
$("open").onclick = async () => { await chrome.runtime.sendMessage({ type: "openTab" }); window.close(); };
refresh();
setInterval(refresh, 2000);
