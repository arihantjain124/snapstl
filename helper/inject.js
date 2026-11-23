// Runs inside the MakerWorld page. It only *watches* — it never changes or blocks a request.
//  1. Model files the page downloads after Generate (plate_1.obj = base, plate_2.obj = name; a name
//     stand's one OpenSCAD .obj with both colours, 2.4) are copied to the helper.
//  2. While the helper is pressing Generate ("armed"), the bodies of the page's own outgoing
//     requests are handed to the helper (content.js), which checks they carried the license
//     password. Nothing is sent anywhere else; bodies are dropped once checked.
(() => {
  if (window.__snapstlHooked) return;
  window.__snapstlHooked = true;
  const PLATE = /\/(plate_\d+|plate_single)\.obj(\?|$)/;
  const MODEL = /\/openscadObj\/[^?]*\.obj(\?|$)/;  // a name stand (2.4)
  const isModel = (url) => PLATE.test(url) || MODEL.test(url);
  const post = (msg) => window.postMessage({ __snapstl: true, ...msg }, location.origin);
  const send = (url, text) => {
    const m = PLATE.exec(url);
    if (m) post({ kind: "obj", plate: m[1], text });
    else if (MODEL.test(url)) post({ kind: "obj", plate: "model", text });
  };
  let armedUntil = 0;
  window.addEventListener("message", (e) => {
    if (e.source === window && e.data && e.data.__snapstlArm) armedUntil = Date.now() + 60000;
    if (e.source === window && e.data && e.data.__snapstlDisarm) armedUntil = 0;
  });
  const bodyText = (b) => {
    try {
      if (b == null) return null;
      if (typeof b === "string") return b;
      if (b instanceof URLSearchParams) return b.toString();
      if (b instanceof FormData) return [...b.entries()].map(([k, v]) => `${k}=${typeof v === "string" ? v : ""}`).join("&");
    } catch (e) { /* ignore */ }
    return null;
  };
  const watchBody = (url, method, body) => {
    if (Date.now() > armedUntil || !method || method.toUpperCase() === "GET") return;
    const t = bodyText(body);
    if (t != null) post({ kind: "req", url: String(url).slice(0, 200), body: t.slice(0, 200000) });
  };

  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    try {
      const url = typeof input === "string" ? input : input && input.url;
      watchBody(url, (init && init.method) || (input && input.method) || "GET", init && init.body);
    } catch (e) { /* ignore */ }
    const res = await origFetch.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : input && input.url;
      if (url && isModel(url) && res.ok) res.clone().text().then((t) => send(url, t)).catch(() => {});
    } catch (e) { /* ignore */ }
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__snapstlUrl = String(url);
    this.__snapstlMethod = String(method || "GET");
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try { watchBody(this.__snapstlUrl, this.__snapstlMethod, body); } catch (e) { /* ignore */ }
    if (this.__snapstlUrl && isModel(this.__snapstlUrl)) {
      this.addEventListener("load", () => {
        try {
          if (this.status < 200 || this.status >= 300) return;
          if (this.responseType === "" || this.responseType === "text") send(this.__snapstlUrl, this.responseText);
          else if (this.responseType === "arraybuffer") send(this.__snapstlUrl, new TextDecoder().decode(this.response));
          else if (this.responseType === "blob") this.response.text().then((t) => send(this.__snapstlUrl, t));
        } catch (e) { /* ignore */ }
      });
    }
    return origSend.apply(this, arguments);
  };
})();
