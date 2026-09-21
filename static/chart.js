/* Daily delivery per strategy.
 *
 * Inline SVG built from the JSON in #chartdata. One line per strategy, a
 * vertical crosshair that snaps to the nearest day, and one tooltip listing
 * every series at that day - so a reader aims at a date, never at a 2px line.
 *
 * Every label here comes from the delivery feed, and the orders export is
 * known to carry HTML in its fields. Labels go in with textContent only.
 */
(function () {
  "use strict";

  var host = document.getElementById("strategychart");
  var dataEl = document.getElementById("chartdata");
  if (!host || !dataEl) return;

  var data;
  try {
    data = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }
  if (!data.dates || !data.dates.length || !data.series.length) return;

  // Validated against the six checks in the dataviz palette validator
  // (light, #FFFFFF surface): lightness band, chroma floor, CVD separation,
  // normal-vision floor and 3:1 contrast all pass. The order is the
  // CVD-safety mechanism - do not reorder, and never cycle past the eighth.
  var PALETTE = [
    "#1C5BC4", "#E2761B", "#0E9AAF", "#7E6BD6",
    "#B8860B", "#C2418A", "#6E8B1F", "#3182C8"
  ];
  var OTHER = "#8A93A3";

  var NS = "http://www.w3.org/2000/svg";
  var PAD = { top: 16, right: 18, bottom: 34, left: 62 };

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    for (var key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }

  function color(index) {
    return index < PALETTE.length ? PALETTE[index] : OTHER;
  }

  function isMoney() {
    return data.metric !== "impressions";
  }

  function format(value) {
    if (value === null || value === undefined) return "—";
    if (isMoney()) {
      return "$" + value.toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      });
    }
    return Math.round(value).toLocaleString();
  }

  function axisFormat(value) {
    if (isMoney()) {
      if (value >= 1000) return "$" + Math.round(value / 1000) + "k";
      return "$" + Math.round(value);
    }
    if (value >= 1000000) return (value / 1000000).toFixed(1) + "M";
    if (value >= 1000) return Math.round(value / 1000) + "k";
    return String(Math.round(value));
  }

  function dayLabel(iso) {
    var parts = iso.split("-");
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return Number(parts[2]) + " " + months[Number(parts[1]) - 1];
  }

  /* Nice round ticks, so the axis reads in human numbers. */
  function ticks(max) {
    if (max <= 0) return [0, 1];
    var raw = max / 4;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; })
      .filter(function (s) { return s >= raw; })[0] || 10 * mag;
    var out = [];
    for (var v = 0; v <= max + step * 0.001; v += step) out.push(v);
    return out;
  }

  var svg, plot, crosshair, tooltip, geom;

  function draw() {
    host.textContent = "";

    var width = host.clientWidth || 900;
    var height = 300;
    var innerW = width - PAD.left - PAD.right;
    var innerH = height - PAD.top - PAD.bottom;

    var max = 0;
    data.series.forEach(function (s) {
      s.values.forEach(function (v) { if (v > max) max = v; });
    });
    if (max <= 0) max = 1;

    var tickValues = ticks(max);
    var top = tickValues[tickValues.length - 1];
    var n = data.dates.length;

    function x(i) { return PAD.left + (n === 1 ? innerW / 2 : innerW * i / (n - 1)); }
    function y(v) { return PAD.top + innerH - (v / top) * innerH; }

    geom = { x: x, y: y, innerH: innerH, n: n, width: width };

    svg = el("svg", {
      viewBox: "0 0 " + width + " " + height,
      width: "100%", height: String(height),
      role: "img",
      "aria-label": "Daily delivery by strategy"
    });

    /* Gridlines and the y axis, recessive. */
    tickValues.forEach(function (value) {
      svg.appendChild(el("line", {
        x1: PAD.left, x2: width - PAD.right, y1: y(value), y2: y(value),
        class: "cgrid"
      }));
      var label = el("text", { x: PAD.left - 10, y: y(value) + 4, class: "caxis cright" });
      label.textContent = axisFormat(value);
      svg.appendChild(label);
    });

    /* X labels: first, last and a handful between, so they never collide. */
    var every = Math.max(1, Math.ceil(n / 8));
    data.dates.forEach(function (iso, i) {
      if (i % every !== 0 && i !== n - 1) return;
      var label = el("text", {
        x: x(i), y: height - 12, class: "caxis cmid"
      });
      label.textContent = dayLabel(iso);
      svg.appendChild(label);
    });

    plot = el("g", {});
    svg.appendChild(plot);

    data.series.forEach(function (series, index) {
      var d = "";
      series.values.forEach(function (value, i) {
        d += (i === 0 ? "M" : "L") + x(i).toFixed(1) + " " + y(value).toFixed(1);
      });
      plot.appendChild(el("path", {
        d: d, fill: "none", stroke: color(index), "stroke-width": "2",
        "stroke-linejoin": "round", "stroke-linecap": "round"
      }));
    });

    crosshair = el("g", { class: "ccross", visibility: "hidden" });
    crosshair.appendChild(el("line", {
      x1: 0, x2: 0, y1: PAD.top, y2: PAD.top + innerH, class: "chair"
    }));
    svg.appendChild(crosshair);

    host.appendChild(svg);

    svg.addEventListener("pointermove", onMove);
    svg.addEventListener("pointerleave", onLeave);
  }

  function nearestIndex(clientX) {
    var box = svg.getBoundingClientRect();
    var scale = geom.width / box.width;
    var px = (clientX - box.left) * scale;
    var best = 0, bestDistance = Infinity;
    for (var i = 0; i < geom.n; i++) {
      var distance = Math.abs(geom.x(i) - px);
      if (distance < bestDistance) { bestDistance = distance; best = i; }
    }
    return best;
  }

  function onMove(event) {
    var i = nearestIndex(event.clientX);
    var cx = geom.x(i);

    crosshair.setAttribute("visibility", "visible");
    var hair = crosshair.firstChild;
    hair.setAttribute("x1", cx);
    hair.setAttribute("x2", cx);

    /* Dots sit on the crosshair, one per series, each ringed in the surface
       colour so overlapping points stay countable. */
    while (crosshair.childNodes.length > 1) {
      crosshair.removeChild(crosshair.lastChild);
    }
    data.series.forEach(function (series, index) {
      crosshair.appendChild(el("circle", {
        cx: cx, cy: geom.y(series.values[i]), r: "4.5",
        fill: color(index), stroke: "#FFFFFF", "stroke-width": "2"
      }));
    });

    showTooltip(i, event.clientX);
  }

  function onLeave() {
    crosshair.setAttribute("visibility", "hidden");
    if (tooltip) tooltip.hidden = true;
  }

  function showTooltip(i, clientX) {
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "ctip";
      host.appendChild(tooltip);
    }
    tooltip.textContent = "";

    var head = document.createElement("b");
    head.textContent = dayLabel(data.dates[i]);
    tooltip.appendChild(head);

    /* Every series at this day, biggest first, so the tooltip reads as a
       ranking rather than in whatever order the query returned. */
    data.series
      .map(function (series, index) {
        return { label: series.label, value: series.values[i], index: index };
      })
      .sort(function (a, b) { return b.value - a.value; })
      .forEach(function (row) {
        var line = document.createElement("div");
        line.className = "ctiprow";

        var swatch = document.createElement("i");
        swatch.style.background = color(row.index);
        line.appendChild(swatch);

        var name = document.createElement("span");
        name.textContent = row.label;          // feed data - never innerHTML
        line.appendChild(name);

        var value = document.createElement("em");
        value.textContent = format(row.value);
        line.appendChild(value);

        tooltip.appendChild(line);
      });

    tooltip.hidden = false;

    var box = host.getBoundingClientRect();
    var left = clientX - box.left + 16;
    if (left + tooltip.offsetWidth > box.width) {
      left = clientX - box.left - tooltip.offsetWidth - 16;
    }
    tooltip.style.left = Math.max(0, left) + "px";
  }

  /* The legend is markup, not canvas, so identity survives with images off
     and is reachable by a screen reader. */
  function legend() {
    var host2 = document.getElementById("chartlegend");
    if (!host2) return;
    host2.textContent = "";
    data.series.forEach(function (series, index) {
      var item = document.createElement("span");
      item.className = "clegend";
      var swatch = document.createElement("i");
      swatch.style.background = color(index);
      item.appendChild(swatch);
      var name = document.createElement("span");
      name.textContent = series.label;
      item.appendChild(name);
      host2.appendChild(item);
    });
  }

  draw();
  legend();

  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { draw(); legend(); }, 150);
  });
})();
