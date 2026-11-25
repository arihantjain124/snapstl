// SnapSTL Helper on SnapSTL's own page (2.6, SnapSTL 0.33.2).
// Print label / Continue → bring ShopDeck's tab to the front: Chrome slows down and freezes background tabs,
// and a label made in a ShopDeck tab behind SnapSTL hung. The page posts {snapstl: "label-start", job_id};
// nothing else is read from or sent to this page.
(() => {
  document.documentElement.dataset.snapstlHelper = chrome.runtime.getManifest().version;
  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin || !e.data || e.data.snapstl !== "label-start") return;
    const jobId = Number(e.data.job_id) || null;
    chrome.runtime.sendMessage({ type: "sd_front", job_id: jobId })
      .then((r) => window.postMessage({ snapstl: "label-front", ok: !!(r && r.ok), error: (r && r.error) || "" },
                                      location.origin))
      .catch((err) => window.postMessage({ snapstl: "label-front", ok: false, error: String(err) }, location.origin));
  });
})();
