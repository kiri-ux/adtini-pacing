/* The hover panels on meters and product pills.
 *
 * They lived inside the cell, absolutely positioned. Every table they sit in
 * scrolls horizontally, and a box that scrolls on one axis clips on both -
 * CSS gives you no way to overflow vertically out of one. So the panel was
 * sliced off at the row boundary and showed as a dark stub.
 *
 * One panel, on the body, positioned against the thing being hovered.
 */
(function () {
  "use strict";

  var tip = null;
  var current = null;

  function panel() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "mtip floating";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    return tip;
  }

  function place(host) {
    var source = host.querySelector(".mtip");
    if (!source) return;
    var box = panel();
    box.innerHTML = source.innerHTML;
    box.style.visibility = "hidden";
    box.style.opacity = "0";
    box.style.display = "block";

    var at = host.getBoundingClientRect();
    var size = box.getBoundingClientRect();
    var left = Math.min(at.left, window.innerWidth - size.width - 12);
    var top = at.bottom + 7;
    // Flip above when there is no room below.
    if (top + size.height > window.innerHeight - 8) {
      top = Math.max(8, at.top - size.height - 7);
    }
    box.style.left = Math.max(8, left) + "px";
    box.style.top = top + "px";
    box.style.visibility = "visible";
    box.style.opacity = "1";
    current = host;
  }

  function hide() {
    if (tip) {
      tip.style.opacity = "0";
      tip.style.visibility = "hidden";
    }
    current = null;
  }

  function hosts(node) {
    return node.closest ? node.closest(".meter, .pill-prod") : null;
  }

  document.addEventListener("mouseover", function (event) {
    var host = hosts(event.target);
    if (host && host !== current) place(host);
  });
  document.addEventListener("mouseout", function (event) {
    var host = hosts(event.target);
    if (host && host === current && !host.contains(event.relatedTarget)) hide();
  });
  document.addEventListener("focusin", function (event) {
    var host = hosts(event.target);
    if (host) place(host);
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, true);
})();
