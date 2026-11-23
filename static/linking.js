/* The campaign-linking dialog on the order page.
 *
 * One dialog serves every row: the candidate list is the same whichever line
 * item is being linked, so it is rendered once and the row's own context is
 * poured into it on open. Rendering one per row would multiply a table of
 * several hundred campaigns by the number of line items.
 */
(function () {
  var dlg = document.getElementById("linkdlg");
  if (!dlg) return;

  var liField = document.getElementById("dlg-li");
  var labelField = document.getElementById("dlg-label");
  var verified = document.getElementById("dlg-verified");
  var by = document.getElementById("dlg-by");
  var filter = document.getElementById("dlgfilter");

  function open(btn) {
    liField.value = btn.dataset.li || "";
    labelField.textContent = btn.dataset.label || "";
    verified.checked = btn.dataset.verified === "1";
    by.value = btn.dataset.by || "";

    // A line item can be on several campaigns, so this is a set.
    var current = (btn.dataset.campaigns || "").split(",").filter(Boolean);
    dlg.querySelectorAll('input[name="campaign"]').forEach(function (input) {
      input.checked = current.indexOf(input.value) !== -1;
    });

    // "Now on" is relative to whoever is being linked: a campaign already on
    // this row is not taken, it is the current answer.
    var mine = btn.dataset.li;
    dlg.querySelectorAll(".takenby").forEach(function (span) {
      span.closest("tr").classList.toggle("ismine", span.dataset.li === mine);
    });

    // A campaign that started within a week of the line item's start date
    // is marked and listed first: the likeliest match.
    var start = btn.dataset.start ? Date.parse(btn.dataset.start) : NaN;
    var body = dlg.querySelector(".dlgtable tbody");
    if (body) {
      var rows = Array.prototype.slice.call(body.rows);
      rows.forEach(function (row, i) {
        if (row.dataset.order === undefined) row.dataset.order = i;
        var first = row.dataset.first ? Date.parse(row.dataset.first) : NaN;
        var hit = !isNaN(start) && !isNaN(first) &&
          Math.abs(first - start) <= 7 * 86400000;
        row.classList.toggle("datehit", hit);
      });
      rows.sort(function (a, b) {
        var ha = a.classList.contains("datehit") ? 0 : 1;
        var hb = b.classList.contains("datehit") ? 0 : 1;
        return ha - hb || a.dataset.order - b.dataset.order;
      });
      rows.forEach(function (row) { body.appendChild(row); });
    }

    filter.value = "";
    apply("");
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "open");
    var picked = dlg.querySelector('input[name="campaign"]:checked');
    if (picked) picked.scrollIntoView({ block: "center" });
    count();
  }

  function apply(term) {
    dlg.querySelectorAll("tbody tr").forEach(function (row) {
      var hay = row.dataset.hay || "";
      row.hidden = term !== "" && hay.indexOf(term) === -1;
    });
  }

  document.querySelectorAll(".linkbtn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      open(btn);
    });
  });

  if (filter) {
    filter.addEventListener("input", function () {
      apply(filter.value.trim().toLowerCase());
    });
  }

  // How many are ticked, so the count is visible without scrolling the list.
  function count() {
    var tally = dlg.querySelector("#dlgcount");
    if (!tally) return;
    var n = dlg.querySelectorAll('input[name="campaign"]:checked').length;
    tally.textContent = n === 0 ? "none picked" : n + " picked";
    tally.classList.toggle("none", n === 0);
  }

  dlg.addEventListener("change", function (event) {
    if (event.target.name === "campaign") count();
  });

  var cancel = document.getElementById("dlgcancel");
  if (cancel) {
    cancel.addEventListener("click", function () {
      dlg.close();
    });
  }

  // Save without reloading the page: the write is quick, the order page
  // rebuild is not, so it happens in the background.
  var form = dlg.querySelector("form");
  if (form && window.fetch && window.FormData) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var save = form.querySelector(".save");
      if (save) { save.disabled = true; save.textContent = "Saving…"; }
      fetch(form.action, {method: "POST", body: new FormData(form), credentials: "same-origin",
                          headers: {"X-Requested-With": "fetch"}})
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (j) {
          var li = liField.value;
          var picked = Array.prototype.slice.call(dlg.querySelectorAll('input[name="campaign"]:checked'));
          var btn = document.querySelector('.linkbtn[data-li="' + li + '"]');
          if (btn) {
            btn.dataset.campaigns = picked.map(function (i) { return i.value; }).join(",");
            btn.dataset.verified = verified.checked ? "1" : "";
            btn.dataset.by = by.value;
            var cell = btn.closest("tr").querySelector(".campcell");
            if (cell) {
              cell.innerHTML = "";
              if (picked.length) {
                var ul = document.createElement("ul");
                ul.className = "camplist";
                picked.forEach(function (i) {
                  var item = document.createElement("li");
                  var lab = dlg.querySelector('label[for="' + i.id + '"]');
                  item.textContent = lab ? lab.textContent : i.value;
                  ul.appendChild(item);
                });
                cell.appendChild(ul);
              } else {
                cell.innerHTML = '<span class="muted">—</span>';
              }
            }
          }
          var note = document.createElement("p");
          note.className = "flash";
          note.textContent = (j.message || "Saved.") + " Pacing numbers update on the next page load.";
          var main = document.querySelector("main") || document.body;
          main.insertBefore(note, main.firstChild);
          dlg.close();
        })
        .catch(function () { form.submit(); })
        .then(function () { if (save) { save.disabled = false; save.textContent = "Save links"; } });
    });
  }

  // Lifetime vs month to date, on the table and in the dialog at once.
  var toggle = document.getElementById("lifetime");
  if (toggle) {
    var sync = function () {
      document.body.classList.toggle("showlife", toggle.checked);
    };
    toggle.addEventListener("change", sync);
    sync();
  }
})();
