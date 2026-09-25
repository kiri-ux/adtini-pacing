/* The day log panel, and the daily grid's scroll position.
 *
 * Both are about the same thing: a long flight is hundreds of columns and
 * the days worth looking at are always the most recent, so the grid opens
 * scrolled to its right-hand end rather than at a January nobody is asking
 * about.
 */
(function () {
  "use strict";

  var wrap = document.querySelector(".logwrap");
  var panel = document.getElementById("daylog");
  var button = document.getElementById("notebtn");
  if (wrap && panel && button) {
    // Rendered at the end of the page so it is not inside a form, then moved
    // up beside Export - the header is a block, and a form cannot be nested
    // in the one the sold terms already use.
    var slot = document.getElementById("logslot");
    if (slot) slot.appendChild(wrap);

    function show(open) {
      panel.hidden = !open;
      button.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var body = document.getElementById("note-body");
        if (body) body.focus();
      }
    }

    button.addEventListener("click", function (event) {
      event.stopPropagation();
      show(panel.hidden);
    });
    document.addEventListener("click", function (event) {
      if (!panel.hidden && !wrap.contains(event.target)) show(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) show(false);
    });
    // Land here after saving a note.
    if (window.location.hash === "#daylog") show(true);
  }

  var grid = document.querySelector(".gridscroll");
  if (grid) {
    grid.scrollLeft = grid.scrollWidth;
  }
})();
