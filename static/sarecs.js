// Campaign recs for one line item, served by campaign-insights. Push sends the
// rec's id to insights, which makes the StackAdapt change.
document.querySelectorAll('.sarecs').forEach(function (card) {
  var api = card.dataset.api, line = card.dataset.line;
  var body = card.querySelector('[data-recs-body]'), sub = card.querySelector('[data-recs-sub]');
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function pct(x, d) { return x == null ? '—' : (x * 100).toFixed(d == null ? 2 : d) + '%'; }
  function num(x) { return x ? Math.round(x).toLocaleString() : '—'; }
  function label(a) { return a === 'Shift budget to' ? 'Bid up 20%' : a; }
  fetch(api + '/api/line_recs/' + encodeURIComponent(line))
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (!j) return;
      if (!j.groups || !j.groups.length) {
        if (j.impressions == null) return;
        sub.textContent = 'StackAdapt, last 30 days';
        body.innerHTML = '<p class="dsub">' + Math.round(j.impressions).toLocaleString() + ' impressions. Recs start at ' + num(j.min_impr || 10000) + '.</p>';
        card.hidden = false;
        return;
      }
      var report = j.report;
      var pushUrl = api + (j.push_url || '/reports/' + encodeURIComponent(report) + '/sa_push');
      if (j.updated) {
        var t = new Date(j.updated);
        sub.textContent = 'StackAdapt, last 30 days · updated ' + t.toLocaleString([], {month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit'});
      } else {
        sub.textContent = 'From Insights report ' + (j.report_name || report);
      }
      body.innerHTML = j.groups.map(function (g) {
        var rows = (g.recs || []).map(function (r) {
          var cell = r.pushed ? '<span class="recdone" title="' + esc(r.pushed.at) + '">' + esc(r.pushed.result) + '</span>'
            : r.can_push ? '<button type="button" class="recpush" data-rid="' + esc(r.rid) + '" data-link="' + esc(r.link || '') + '">Push</button>' : '';
          return '<tr><td class="nowrap"><span class="recflag ' + esc(r.tone) + '">' + esc(label(r.action)) + '</span></td>' +
            '<td>' + esc(r.scope || 'All in group') + '</td><td>' + esc(r.kind) + '</td><td>' + esc(r.target) + '</td>' +
            '<td class="num">' + num(r.impressions) + '</td><td class="num">' + (r.share ? pct(r.share, 0) : '—') + '</td>' +
            '<td class="num">' + (r.impressions ? pct(r.ctr) : '—') + '</td><td class="nowrap pushcell">' + cell + '</td></tr>';
        }).join('');
        var head = '<p class="recgroup"><a href="' + esc(g.sa_url) + '" target="_blank" rel="noopener">' + esc(g.name) + '</a>' +
          ' <span class="pace ' + (g.status === 'On goal' ? 'on-pace' : 'under') + '">CTR ' + pct(g.ctr) + ' vs ' + pct(g.goal) + ' goal</span></p>';
        return head + (rows ? '<div class="scroller"><table class="overview satable"><thead><tr><th>Action</th><th>Applies to</th><th>Type</th><th>Target</th><th>Impr</th><th>Share</th><th>CTR</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
          : '<p class="dsub">No recs.</p>');
      }).join('');
      card.hidden = false;
      body.addEventListener('click', function (e) {
        var b = e.target.closest('.recpush');
        if (!b || b.disabled) return;
        b.disabled = true; b.textContent = 'Pushing…';
        fetch(pushUrl, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rid: b.dataset.rid})
        }).then(function (r) { return r.json().catch(function () { return {ok: false, error: 'HTTP ' + r.status}; }); })
          .then(function (j) {
            var td = b.parentNode;
            if (j.ok) {
              if (window.syncPushNotes) window.syncPushNotes(false);
              var cp = /Copied to (\d+)/.exec(j.result || '');
              var link = cp ? 'https://www.stackadapt.com/editor?ids=' + cp[1] + '&tab=inventory' : b.dataset.link;
              td.innerHTML = '<span class="recdone">' + esc(j.result) + '</span>' +
                (link ? ' <a href="' + esc(link) + '" target="_blank" rel="noopener">See live changes</a>' : '');
            } else {
              td.innerHTML = '<span class="recerr">' + esc(j.error || 'Push failed') + '</span>';
            }
          }).catch(function () { b.disabled = false; b.textContent = 'Push'; });
      });
    }).catch(function () {});
});
