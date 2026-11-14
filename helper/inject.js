// Runs inside the MakerWorld page. It only *watches* the model files the page itself
// downloads after Generate (plate_1.obj = base, plate_2.obj = name) and passes a copy
// to the helper. It never changes any request.
(() => {
  if (window.__snapstlHooked) return;
  window.__snapstlHooked = true;
  const PLATE = /\/(plate_\d+|plate_single)\.obj(\?|$)/;
  const send = (url, text) => {
    const m = PLATE.exec(url);
    if (m) window.postMessage({ __snapstl: "obj", plate: m[1], text }, location.origin);
  };

  const origFetch = window.fetch;
  window.fetch = async function (input) {
    const res = await origFetch.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : input && input.url;
      if (url && PLATE.test(url) && res.ok) res.clone().text().then((t) => send(url, t)).catch(() => {});
    } catch (e) { /* ignore */ }
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__snapstlUrl = String(url);
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this.__snapstlUrl && PLATE.test(this.__snapstlUrl)) {
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
