/* The % column on the details grid: a share of the product's goal.
 *
 * Typing a % fills the row's monthly and total from the product's own
 * goals; typing a monthly figure moves the % to match. Only the figures
 * are saved, so the % is never out of step with what is stored.
 */
(function () {
  "use strict";

  function num(raw) {
    var n = parseFloat(String(raw || "").replace(/[$,\s%]/g, ""));
    return isNaN(n) ? null : n;
  }
  function fmt(n, money) {
    return n.toLocaleString("en-US", { maximumFractionDigits: money ? 2 : 0 });
  }
  function cells(pct) {
    var row = pct.closest("tr");
    return {
      monthly: row.querySelector('input[name$="-monthly"]'),
      total: row.querySelector('input[name$="-total"]'),
    };
  }

  document.addEventListener("input", function (event) {
    var el = event.target;
    if (el.classList.contains("pctin")) {
      var share = num(el.value);
      var c = cells(el);
      var money = el.dataset.money === "1";
      var mon = num(el.dataset.mon), tot = num(el.dataset.tot);
      if (share === null) return;
      if (mon !== null && c.monthly) c.monthly.value = fmt(mon * share / 100, money);
      if (tot !== null && c.total) c.total.value = fmt(tot * share / 100, money);
      return;
    }
    if (/-monthly$|-total$/.test(el.name || "")) {
      var row = el.closest("tr");
      var pct = row && row.querySelector(".pctin");
      if (!pct) return;
      var isMonthly = /-monthly$/.test(el.name);
      var base = num(isMonthly ? pct.dataset.mon : pct.dataset.tot);
      // The % follows monthly when the product has a monthly goal.
      if (!isMonthly && num(pct.dataset.mon) !== null) return;
      var v = num(el.value);
      if (base && v !== null) pct.value = (v / base * 100).toFixed(1);
    }
  });
})();
