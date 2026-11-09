// Drag the Campaign Elements column wider or narrower; kept per browser.
(function () {
  var KEY = "pacing.pinw", MIN = 220, MAX = 900;
  var root = document.documentElement;
  function apply(w) { root.style.setProperty("--pinw", w + "px"); }
  try { var saved = parseInt(localStorage.getItem(KEY), 10); if (saved) apply(saved); } catch (e) {}
  document.addEventListener("mousedown", function (ev) {
    var grip = ev.target.closest && ev.target.closest(".colgrip");
    if (!grip) return;
    ev.preventDefault();
    var th = grip.parentElement, startX = ev.clientX, startW = th.getBoundingClientRect().width, w = startW;
    document.body.classList.add("colsizing");
    function move(e) { w = Math.max(MIN, Math.min(MAX, Math.round(startW + e.clientX - startX))); apply(w); }
    function up() {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove("colsizing");
      try { localStorage.setItem(KEY, String(w)); } catch (e) {}
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  document.addEventListener("dblclick", function (ev) {
    if (!(ev.target.closest && ev.target.closest(".colgrip"))) return;
    root.style.removeProperty("--pinw");
    try { localStorage.removeItem(KEY); } catch (e) {}
  });
})();
