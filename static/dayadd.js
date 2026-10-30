/* The + over each day in the details grid opens that day's update.
 *
 * Shown when the day's column is hovered; the dialog's date fields take
 * the day clicked, so a note or check lands on the day it is about.
 */
(function () {
  "use strict";

  var dlg = document.getElementById("daydlg");
  if (!dlg) return;

  document.addEventListener("click", function (event) {
    var plus = event.target.closest(".dayplus");
    if (plus) {
      dlg.querySelectorAll("[data-dayfield]").forEach(function (f) { f.value = plus.dataset.day; });
      dlg.querySelectorAll("[data-daylabel]").forEach(function (l) { l.textContent = plus.dataset.label; });
      if (typeof dlg.showModal === "function") dlg.showModal();
      else dlg.setAttribute("open", "open");
      var first = dlg.querySelector("textarea, input[type=text]");
      if (first) first.focus();
      return;
    }
    if (event.target.closest("[data-close]") || event.target === dlg) dlg.close();
  });

  // Hovering anywhere in a day's column shows its +.
  function column(cell) {
    var row = cell.parentElement;
    var table = cell.closest("table.detailgrid");
    if (!table || !row) return null;
    var dayCells = row.querySelectorAll("th, td");
    var fromEnd = dayCells.length - Array.prototype.indexOf.call(dayCells, cell);
    var head = table.querySelector("tr.addhead");
    var heads = head ? head.querySelectorAll("th") : [];
    return heads[heads.length - fromEnd] || null;
  }
  var shown = null;
  document.addEventListener("mouseover", function (event) {
    var cell = event.target.closest("table.detailgrid th, table.detailgrid td");
    var head = cell ? column(cell) : null;
    var target = head && head.classList.contains("dayadd") ? head : null;
    if (target === shown) return;
    if (shown) shown.classList.remove("hot");
    shown = target;
    if (shown) shown.classList.add("hot");
  });
})();
