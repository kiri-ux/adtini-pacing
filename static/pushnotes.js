// Pull StackAdapt pushes from campaign-insights into this order's Notes.
// Reloads once when notes were added or replaced so they show in the table.
// After a push from the recs card, the new note is put on its day in place.
(function () {
  function place(notes) {
    (notes || []).forEach(function (n) {
      document.querySelectorAll('td.note[data-day="' + n.date + '"]').forEach(function (td) {
        var mark = td.querySelector('.notemark');
        if (!mark) {
          mark = document.createElement('span');
          mark.className = 'notemark'; mark.tabIndex = 0; mark.innerHTML = '&#9679;';
          td.appendChild(mark);
        }
        var tip = mark.getAttribute('data-tip');
        mark.setAttribute('data-tip', tip ? tip + ' · ' + n.body : n.body);
      });
    });
  }
  var el = document.querySelector('[data-push-sync]');
  if (!el) return;
  var url = el.dataset.pushSync, key = 'pushsync:' + url;
  window.syncPushNotes = function (reload) {
    return fetch(url, {method: 'POST', credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : {added: 0}; })
      .then(function (j) {
        var again = false;
        try { again = sessionStorage.getItem(key) === '1'; } catch (e) {}
        if ((j.added > 0 || j.removed > 0) && reload && !again) {
          try { sessionStorage.setItem(key, '1'); } catch (e) {}
          location.reload();
        } else {
          try { sessionStorage.removeItem(key); } catch (e) {}
          if (!reload) place(j.notes);
        }
        return j;
      }).catch(function () { return {added: 0}; });
  };
  window.syncPushNotes(true);
})();
