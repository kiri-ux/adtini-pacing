/* The % column on the details grid: a share of the product's goal.
 *
 * Typing a % fills the row's monthly and total from the product's own
 * goals; typing a monthly figure moves the % to match. Only the figures
 * are saved, so the % is never out of step with what is stored. The button
 * sets the % to what each row actually served over the last 7 days.
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

  function rows(li) {
    return Array.prototype.slice.call(
      document.querySelectorAll('.pctin[data-li="' + li + '"]'));
  }
  function fill(el) {
    var share = num(el.value);
    var c = cells(el);
    var money = el.dataset.money === "1";
    var mon = num(el.dataset.mon), tot = num(el.dataset.tot);
    if (share === null) return;
    if (mon !== null && c.monthly) c.monthly.value = fmt(mon * share / 100, money);
    if (tot !== null && c.total) c.total.value = fmt(tot * share / 100, money);
  }
  function sum(li) {
    var out = document.querySelector('.pctsum[data-li="' + li + '"]');
    if (!out) return;
    var total = rows(li).reduce(function (acc, el) { return acc + (num(el.value) || 0); }, 0);
    out.textContent = Math.round(total) + "%";
    out.classList.toggle("off", Math.abs(total - 100) > 0.5);
  }

  // Set every row of the product to its share of the last 7 days' serve.
  // With nothing served yet, scale the typed shares to 100% instead,
  // keeping their proportions. The last row takes the rounding.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".pctfix");
    if (!btn) return;
    var list = rows(btn.dataset.li);
    var live = list.some(function (el) { return num(el.dataset.live) !== null; });
    var pick = function (el) { return num(live ? el.dataset.live : el.value) || 0; };
    var total = list.reduce(function (acc, el) { return acc + pick(el); }, 0);
    var used = 0;
    list.forEach(function (el, i) {
      var share;
      if (!total) share = 100 / list.length;
      else share = pick(el) * 100 / total;
      share = i === list.length - 1 ? 100 - used : Math.round(share * 10) / 10;
      used += share;
      el.value = share.toFixed(1);
      fill(el);
    });
    sum(btn.dataset.li);
  });

  document.addEventListener("input", function (event) {
    var el = event.target;
    if (el.classList.contains("pctin")) {
      fill(el);
      sum(el.dataset.li);
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
      sum(pct.dataset.li);
    }
  });
})();
