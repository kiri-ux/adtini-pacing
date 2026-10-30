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
  function load() {
    if (loaded || !drawer.open) return;
    loaded = true;
    fetch(box.dataset.src, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) { box.innerHTML = html; })
      .catch(function () {
        loaded = false;
        box.innerHTML = '<p class="empty">Could not load. Close and open again.</p>';
      });
  }
  drawer.addEventListener("toggle", load);
  load();
})();
