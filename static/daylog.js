/* The day log panel, and the daily grid's scroll position.
 *
 * Both are about the same thing: a long flight is hundreds of columns and
 * the days worth looking at are always the most recent, so the grid opens
 * scrolled to its right-hand end rather than at a January nobody is asking
 * about.
 */
(function () {
  "use strict";

  var panel = document.getElementById("daylog");
  var button = document.getElementById("notebtn");
  if (panel && button) {
    var close = document.getElementById("noteclose");

    function show(open) {
      panel.hidden = !open;
      button.setAttribute("aria-expanded", open ? "true" : "false");
      document.body.classList.toggle("logopen", open);
      if (open) {
        var body = document.getElementById("note-body");
        if (body) body.focus();
      }
    }

    button.addEventListener("click", function () {
      show(panel.hidden);
    });
    if (close) {
      close.addEventListener("click", function () {
        show(false);
      });
    }
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
