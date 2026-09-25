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

  tabs.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-scope]");
    if (!button) return;
    var scope = button.dataset.scope;
    table.classList.remove("show-open", "show-closed");
    table.classList.add("show-" + scope);
    tabs.querySelectorAll("button[data-scope]").forEach(function (other) {
      other.classList.toggle("on", other === button);
    });
  });
})();
