/* Running / Ended on the pacing table.
 *
 * Both sets of rows stay in the form, so Save still posts every line item
 * whichever tab is showing. The tab only switches a class on the table,
 * which hides the other set and the Total that goes with it.
 */
(function () {
  "use strict";

  var tabs = document.getElementById("scopetabs");
  var table = document.getElementById("elements");
  if (!tabs || !table) return;

  // Live only: a toggle on top of Current / All, remembered per browser.
  var live = document.getElementById("liveonly");
  function setLive(on) {
    document.body.classList.toggle("liveonly", on);
    if (live) {
      live.classList.toggle("on", on);
      live.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }
  try { setLive(localStorage.getItem("pacing.liveonly") === "1"); } catch (e) {}
  if (live) {
    live.addEventListener("click", function () {
      var on = !document.body.classList.contains("liveonly");
      setLive(on);
      try { localStorage.setItem("pacing.liveonly", on ? "1" : "0"); } catch (e) {}
    });
  }

  tabs.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-scope]");
    if (!button) return;
    var scope = button.dataset.scope;
    table.classList.remove("show-open", "show-closed", "show-all");
    table.classList.add("show-" + scope);
    tabs.querySelectorAll("button[data-scope]").forEach(function (other) {
      other.classList.toggle("on", other === button);
    });
  });
})();
