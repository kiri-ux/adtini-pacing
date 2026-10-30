/* The Budgeting expand fills itself the first time it opens.
 *
 * A grid per product is the heaviest thing on the order page, and most
 * visits never open it, so the page is served without it.
 */
(function () {
  "use strict";

  var drawer = document.getElementById("budgetdrawer");
  var box = document.getElementById("budgetgrids");
  if (!drawer || !box) return;

  var loaded = false;
  var query = "";
  function load() {
    if (loaded || !drawer.open) return;
    loaded = true;
    fetch(box.dataset.src + (query ? "?" + query : ""), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) { box.innerHTML = html; box.style.opacity = ""; })
      .catch(function () {
        loaded = false;
        box.innerHTML = '<p class="empty">Could not load. Close and open again.</p>';
      });
  }
  drawer.addEventListener("toggle", load);

  // Month arrows and Whole flight refetch the grids for that window.
  box.addEventListener("click", function (event) {
    var link = event.target.closest("a.gridnav");
    if (!link) return;
    event.preventDefault();
    query = link.dataset.q || "";
    loaded = false;
    box.style.opacity = "0.5";
    load();
  });
  load();
})();
