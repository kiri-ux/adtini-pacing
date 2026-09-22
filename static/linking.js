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

    var current = btn.dataset.campaign || "";
    dlg.querySelectorAll('input[name="campaign"]').forEach(function (input) {
      input.checked = input.value !== "" && input.value === current;
    });

    // "Now on" is relative to whoever is being linked: a campaign already on
    // this row is not taken, it is the current answer.
    var mine = btn.dataset.li;
    dlg.querySelectorAll(".takenby").forEach(function (span) {
      span.closest("tr").classList.toggle("ismine", span.dataset.li === mine);
    });

    filter.value = "";
    apply("");
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "open");
    var picked = dlg.querySelector('input[name="campaign"]:checked');
    if (picked) picked.scrollIntoView({ block: "center" });
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

  var cancel = document.getElementById("dlgcancel");
  if (cancel) {
    cancel.addEventListener("click", function () {
      dlg.close();
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
