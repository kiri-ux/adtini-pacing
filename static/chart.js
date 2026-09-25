/* Daily delivery per strategy.
 *
 * Inline SVG built from the JSON in #chartdata. One line per strategy, a
 * vertical crosshair that snaps to the nearest day, and one tooltip listing
 * every series at that day - so a reader aims at a date, never at a 2px line.
 *
 * Every label here comes from the delivery feed, and the orders export is
 * known to carry HTML in its fields. Labels go in with textContent only.
 */
/* One page can hold several of these - an order sold in impressions beside
 * one sold in spend needs a chart each, because the two cannot share an
 * axis. So the whole thing is a function over a suffix rather than a lookup
 * of two fixed ids. */
function adtiniChart(suffix) {
  "use strict";

  var host = document.getElementById("strategychart" + suffix);
  var dataEl = document.getElementById("chartdata" + suffix);
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

  /* A series can be pinned to a second axis. Counts and ratios live on one
     chart that way: clicks in the thousands on the left, CTR at 0.4% on the
     right, instead of the ratio lying flat along the floor. */
  function onRight(series) {
    return series.axis === "right";
  }
  var HAS_RIGHT = data.series.some(onRight);

  var PAD = { top: 16, right: HAS_RIGHT ? 58 : 18, bottom: 34, left: 62 };

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    for (var key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }

  function color(index) {
    return index < PALETTE.length ? PALETTE[index] : OTHER;
  }

  function mode() {
    return data.metric === "impressions" || data.metric === "count"
      ? "count" : "money";
  }

  /* A series may say how it reads; otherwise it reads like the chart. */
  function modeOf(series) {
    return (series && series.format) || mode();
  }

  function format(value, how) {
    if (value === null || value === undefined) return "—";
    if (how === "percent") {
      return (value * 100).toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      }) + "%";
    }
    if (how === "money") {
      return "$" + value.toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      });
    }
    return Math.round(value).toLocaleString();
  }

  function axisFormat(value, how) {
    if (how === "percent") {
      return (value * 100).toFixed(value < 0.01 ? 2 : 1) + "%";
    }
    if (how === "money") {
      if (value >= 1000) return "$" + Math.round(value / 1000) + "k";
      return "$" + Math.round(value);
    }
    if (value >= 1000000) return (value / 1000000).toFixed(1) + "M";
    if (value >= 1000) return Math.round(value / 1000) + "k";
    return String(Math.round(value));
  }

  /* The right axis is whatever the right-hand series say they are. */
  function rightMode() {
    for (var i = 0; i < data.series.length; i += 1) {
      if (onRight(data.series[i])) return modeOf(data.series[i]);
    }
    return mode();
  }

  function dayLabel(iso) {
    var parts = iso.split("-");
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return Number(parts[2]) + " " + months[Number(parts[1]) - 1];
  }

  /* Nice round ticks, so the axis reads in human numbers. */
  function niceStep(raw) {
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    return [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; })
      .filter(function (s) { return s >= raw; })[0] || 10 * mag;
  }

  function ticks(max) {
    if (max <= 0) return [0, 1];
    var step = niceStep(max / 4);
    var out = [];
    for (var v = 0; v <= max + step * 0.001; v += step) out.push(v);
    return out;
  }

  /* The second axis reuses the first one's gridlines: same number of
     intervals, its own round step. Two sets of gridlines on one frame is
     a grid nobody can read. */
  function alignedTicks(max, intervals) {
    if (max <= 0 || intervals <= 0) return [0, 1];
    var step = niceStep(max / intervals);
    var out = [];
    for (var i = 0; i <= intervals; i += 1) out.push(step * i);
    return out;
  }

  var svg, plot, crosshair, tooltip, geom;

  function draw() {
    host.textContent = "";

    var width = host.clientWidth || 900;
    var height = 300;
    var innerW = width - PAD.left - PAD.right;
    var innerH = height - PAD.top - PAD.bottom;

    var max = 0, maxRight = 0;
    data.series.forEach(function (s) {
      s.values.forEach(function (v) {
        if (onRight(s)) { if (v > maxRight) maxRight = v; }
        else if (v > max) max = v;
      });
    });
    if (max <= 0) max = 1;

    var tickValues = ticks(max);
    var top = tickValues[tickValues.length - 1];
    var rightTicks = HAS_RIGHT
      ? alignedTicks(maxRight, tickValues.length - 1) : null;
    var rightTop = rightTicks ? rightTicks[rightTicks.length - 1] : 1;
    if (rightTop <= 0) rightTop = 1;
    var n = data.dates.length;

    function x(i) { return PAD.left + (n === 1 ? innerW / 2 : innerW * i / (n - 1)); }
    function y(v) { return PAD.top + innerH - (v / top) * innerH; }
    function yr(v) { return PAD.top + innerH - (v / rightTop) * innerH; }
    function scale(series) { return onRight(series) ? yr : y; }

    geom = {
      x: x, y: y, yr: yr, scale: scale,
      innerH: innerH, n: n, width: width
    };

    svg = el("svg", {
      viewBox: "0 0 " + width + " " + height,
      width: "100%", height: String(height),
      role: "img",
      "aria-label": "Daily delivery by strategy"
    });

    /* Gridlines and the y axis, recessive. */
    var leftMode = mode();
    var rmode = rightMode();
    tickValues.forEach(function (value, i) {
      svg.appendChild(el("line", {
        x1: PAD.left, x2: width - PAD.right, y1: y(value), y2: y(value),
        class: "cgrid"
      }));
      var label = el("text", { x: PAD.left - 10, y: y(value) + 4, class: "caxis cright" });
      label.textContent = axisFormat(value, leftMode);
      svg.appendChild(label);

      if (rightTicks) {
        var rlabel = el("text", {
          x: width - PAD.right + 10, y: y(value) + 4, class: "caxis"
        });
        rlabel.textContent = axisFormat(rightTicks[i], rmode);
        svg.appendChild(rlabel);
      }
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
      var scaleFor = scale(series);
      var d = "";
      series.values.forEach(function (value, i) {
        d += (i === 0 ? "M" : "L") + x(i).toFixed(1) + " "
          + scaleFor(value).toFixed(1);
      });
      var attrs = {
        d: d, fill: "none", stroke: color(index), "stroke-width": "2",
        "stroke-linejoin": "round", "stroke-linecap": "round"
      };
      /* Dashed on the right axis, so which scale a line is read against
         survives a black-and-white print and a colour-blind reader. */
      if (onRight(series)) attrs["stroke-dasharray"] = "5 3";
      plot.appendChild(el("path", attrs));
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
        cx: cx, cy: geom.scale(series)(series.values[i]), r: "4.5",
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
       ranking rather than in whatever order the query returned. Left-axis
       series first: ranking a count against a ratio is meaningless, so the
       two scales are sorted apart rather than mixed. */
    data.series
      .map(function (series, index) {
        return {
          label: series.label, value: series.values[i], index: index,
          right: onRight(series), how: modeOf(series)
        };
      })
      .sort(function (a, b) {
        if (a.right !== b.right) return a.right ? 1 : -1;
        return b.value - a.value;
      })
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
        value.textContent = format(row.value, row.how);
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
    var host2 = document.getElementById("chartlegend" + suffix);
    if (!host2) return;
    host2.textContent = "";
    data.series.forEach(function (series, index) {
      var item = document.createElement("span");
      item.className = "clegend";
      var swatch = document.createElement("i");
      swatch.style.background = color(index);
      item.appendChild(swatch);
      var name = document.createElement("span");
      name.textContent = onRight(series)
        ? series.label + " (right)" : series.label;
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
}

/* Draw every chart the page carries: the bare ids, and any numbered ones. */
(function () {
  adtiniChart("");
  var nodes = document.querySelectorAll('[id^="chartdata-"]');
  for (var i = 0; i < nodes.length; i += 1) {
    adtiniChart(nodes[i].id.slice("chartdata".length));
  }
})();
