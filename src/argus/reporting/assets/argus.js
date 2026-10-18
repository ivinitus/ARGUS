
(function() {
  document.documentElement.classList.add('argus-js');
  // Mark <body> so the no-JS fallback CSS gets disabled (drawer
  // stays off-screen until opened, trigger pill becomes visible).
  document.body.classList.add('argus-coverage-js');

  // ---- Coverage drawer (open / close / dismiss) ---------------------------
  // The trigger pill, drawer panel, and backdrop are SSR'd at body-end
  // and styled position:fixed. JS just toggles the open class on
  // both elements + the aria-* state for screen readers. ESC and
  // clicking the backdrop both dismiss.
  var covTrigger = document.getElementById('argus-coverage-open');
  var covDrawer = document.getElementById('argus-coverage-drawer');
  var covBackdrop = document.getElementById('argus-coverage-backdrop');
  var covClose = document.getElementById('argus-coverage-close');
  if (covTrigger && covDrawer && covBackdrop && covClose) {
    var openCoverage = function() {
      covDrawer.classList.add('argus-coverage-open');
      covBackdrop.classList.add('argus-coverage-open');
      covBackdrop.hidden = false;
      covDrawer.setAttribute('aria-hidden', 'false');
      covTrigger.setAttribute('aria-expanded', 'true');
      // Move focus into the drawer so keyboard users land somewhere
      // sensible. Close button is the safest target — Tab from there
      // walks into the tabs.
      covClose.focus();
    };
    var closeCoverage = function() {
      covDrawer.classList.remove('argus-coverage-open');
      covBackdrop.classList.remove('argus-coverage-open');
      covDrawer.setAttribute('aria-hidden', 'true');
      covTrigger.setAttribute('aria-expanded', 'false');
      // Hide the backdrop after the transition so it can't capture
      // clicks while invisible. 220ms matches the CSS transition.
      setTimeout(function() {
        if (covDrawer.getAttribute('aria-hidden') === 'true') {
          covBackdrop.hidden = true;
        }
      }, 220);
      covTrigger.focus();
    };
    covTrigger.addEventListener('click', openCoverage);
    covClose.addEventListener('click', closeCoverage);
    covBackdrop.addEventListener('click', closeCoverage);
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape'
          && covDrawer.classList.contains('argus-coverage-open')) {
        closeCoverage();
      }
    });
  }

  // ---- Coverage tabs ------------------------------------------------------
  // Pure stdlib tab pattern: clicking a tab flips aria-selected on the
  // tab strip and toggles the hidden attribute on each tabpanel. No-JS
  // fallback: only the first panel renders un-hidden via the SSR'd
  // markup, so a user without JS still sees the most populated rule.
  var tabStrip = document.querySelector('.argus-coverage-tabs');
  if (tabStrip) {
    var tabs = tabStrip.querySelectorAll('[role="tab"]');
    tabs.forEach(function(tab) {
      tab.addEventListener('click', function() {
        tabs.forEach(function(t) {
          t.setAttribute('aria-selected', 'false');
          t.tabIndex = -1;
          var panelId = t.getAttribute('aria-controls');
          var panel = document.getElementById(panelId);
          if (panel) panel.hidden = true;
        });
        tab.setAttribute('aria-selected', 'true');
        tab.tabIndex = 0;
        var ownPanel = document.getElementById(
          tab.getAttribute('aria-controls'));
        if (ownPanel) ownPanel.hidden = false;
      });
      // Keyboard nav: arrow keys cycle through tabs (WAI-ARIA pattern).
      tab.addEventListener('keydown', function(e) {
        var idx = Array.prototype.indexOf.call(tabs, tab);
        var next = null;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
          next = tabs[(idx + 1) % tabs.length];
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
          next = tabs[(idx - 1 + tabs.length) % tabs.length];
        }
        if (next) {
          e.preventDefault();
          next.click();
          next.focus();
        }
      });
    });
  }

  // ---- Row click / Enter / Space toggles the paired detail row ------------
  // Audited rows carry tabindex=0 + aria-expanded so keyboard users can
  // Tab to a row and Enter/Space it open; aria-expanded tracks the
  // detail row's visibility for screen readers.
  // ---- Detail panels load on demand ----------------------------------------
  // The page carries only the audit rows. Each detail panel lives in
  // _argus_details/NNNN.json next to the page (50 per file, keyed by row
  // index) and is fetched the first time a row from that file is opened.
  var CHUNKS = new Map();
  function detailBase() {
    var configured = document.querySelector('meta[name="argus-detail-base"]');
    if (configured) {
      var value = configured.getAttribute('content') || '';
      if (value && value.indexOf('__ARGUS_') !== 0) {
        return value.endsWith('/') ? value : value + '/';
      }
    }
    // `_argus.html` -> `_argus_details/`; a variant page `_argus_v2.html` -> `_argus_details_v2/`
    var p = location.pathname, cut = p.lastIndexOf('/') + 1;
    var suffix = p.slice(cut).replace(/^_argus/, '').replace(/\.html$/, '');
    return p.slice(0, cut) + '_argus_details' + suffix + '/';
  }
  function loadChunk(k) {
    if (CHUNKS.has(k)) return CHUNKS.get(k);
    var pending = fetch(detailBase() + String(k).padStart(4, '0') + '.json', { credentials: 'same-origin' })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(map) {
        Object.keys(map).forEach(function(idx) {
          var td = document.querySelector('tr[data-detail="' + idx + '"] > td.argus-detail-slot');
          if (!td) return;
          td.innerHTML = map[idx];
          td.classList.remove('argus-detail-slot');
          td.removeAttribute('data-chunk');
        });
      });
    pending.catch(function() { CHUNKS.delete(k); });   // a later click retries
    CHUNKS.set(k, pending);
    return pending;
  }
  function ensureDetail(detail) {
    var td = detail.querySelector('td.argus-detail-slot');
    if (!td) return Promise.resolve();
    if (!td.firstChild) td.innerHTML = '<div class="argus-detail-loading">Loading details\u2026</div>';
    return loadChunk(Number(td.dataset.chunk)).catch(function() {
      td.innerHTML = '<div class="argus-detail-loading argus-detail-error">Details could not be loaded. '
        + 'Open this report through the ARGUS hub and try again.</div>';
    });
  }

  function toggleDetailRow(row) {
    var idx = row.dataset.idx;
    var detail = document.querySelector('tr[data-detail="' + idx + '"]');
    if (!detail) return;
    if (detail.classList.contains('hidden')) ensureDetail(detail);
    detail.classList.toggle('hidden');
    if (row.hasAttribute('aria-expanded')) {
      row.setAttribute('aria-expanded',
                       String(!detail.classList.contains('hidden')));
    }
  }
  document.querySelectorAll('tr.argus-row').forEach(function(row) {
    row.addEventListener('click', function(e) {
      // Don't toggle if the user clicked a thumbnail link inside the
      // detail row (target.closest fails for cells outside this row).
      if (e.target.closest('a, button, input, select')) return;
      toggleDetailRow(row);
    });
    row.addEventListener('keydown', function(e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      // Only when the row itself is focused — Enter on a nested link
      // or button must keep its native behaviour.
      if (e.target !== row) return;
      e.preventDefault();
      toggleDetailRow(row);
    });
  });

  // ---- Filtering ----------------------------------------------------------
  var search = document.getElementById('argus-search');
  var verdict = document.getElementById('argus-verdict');
  var status = document.getElementById('argus-status');
  var tester = document.getElementById('argus-tester');
  var run = document.getElementById('argus-run');
  var executionDay = document.getElementById('argus-day');
  // Two independent, inclusive custom date ranges. Execution date filters the
  // tester's Jira calendar date (data-day); audit date filters the UTC day the
  // audit ran (data-audited). Both compose (AND) with every other filter and
  // with each other, mirroring the discrete-facet model in applyFilters().
  var execFrom = document.getElementById('argus-exec-from');
  var execTo = document.getElementById('argus-exec-to');
  var auditFrom = document.getElementById('argus-audit-from');
  var auditTo = document.getElementById('argus-audit-to');
  var runNavigation = null;
  var action = document.getElementById('argus-action');
  var category = document.getElementById('argus-category');
  var highOnly = document.getElementById('argus-high-only');
  var flaggedOnly = document.getElementById('argus-flagged-only');
  var bookmarkedOnly = document.getElementById('argus-bookmarked-only');
  var clearBtn = document.getElementById('argus-clear');
  var countEl = document.getElementById('argus-count');
  var totalRows = document.querySelectorAll('tr.argus-row').length;

  // ---- Bookmarks (per-key, persisted to localStorage) --------------------
  // Key: argus.bookmarks.<folder-name> → array of audit keys.
  // Folder-scoped so multiple folders open in different tabs don't share
  // state. Falls back to in-memory Set when localStorage is unavailable
  // (private browsing, file:// in some browsers).
  var BOOKMARK_STORAGE_KEY = 'argus.bookmarks.' + (location.pathname || '');
  function loadBookmarks() {
    try {
      var raw = localStorage.getItem(BOOKMARK_STORAGE_KEY);
      if (!raw) return new Set();
      return new Set(JSON.parse(raw));
    } catch (e) {
      return new Set();
    }
  }
  function saveBookmarks(set) {
    try {
      localStorage.setItem(BOOKMARK_STORAGE_KEY,
                           JSON.stringify(Array.from(set)));
    } catch (e) { /* quota / disabled — bookmarks become session-only */ }
  }
  var bookmarks = loadBookmarks();

  function refreshBookmarkButtons() {
    document.querySelectorAll('button.argus-bookmark').forEach(function(btn) {
      var k = btn.dataset.key;
      var on = bookmarks.has(k);
      btn.classList.toggle('argus-bookmarked', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      // Swap the glyph itself (★ vs ☆) so users see a clear filled-vs-
      // hollow change. Earlier we tried a CSS ::before pseudo-content
      // trick — that rendered as a tiny square on Linux fonts that
      // don't carry the outline-star glyph. Setting textContent
      // directly to U+2605 / U+2606 sidesteps the pseudo-content path
      // and lets the font stack handle whichever glyph it has.
      var icon = btn.querySelector('.argus-bookmark-icon');
      if (icon) icon.textContent = on ? '★' : '☆';
    });
  }
  refreshBookmarkButtons();

  // Active issue-label filter. Set by clicking a per-tester top-issue
  // chip. null = no filter. Composes with all other filters (so chip
  // click + verdict=fail = "this tester's failed audits with this issue
  // type"). Cleared by clicking the same chip again, by Clear, or by
  // the dismiss X on the active-issue pill.
  var activeIssue = null;
  var activeIssueTester = null;

  function isAnyFilterActive() {
    return Boolean(
      (search && search.value) ||
      (verdict && verdict.value) ||
      (status && status.value) ||
      (tester && tester.value) ||
      (run && run.value) ||
      (executionDay && executionDay.value) ||
      (execFrom && execFrom.value) ||
      (execTo && execTo.value) ||
      (auditFrom && auditFrom.value) ||
      (auditTo && auditTo.value) ||
      (action && action.value) ||
      (category && category.value) ||
      (highOnly && highOnly.checked) ||
      (flaggedOnly && flaggedOnly.checked) ||
      (bookmarkedOnly && bookmarkedOnly.checked) ||
      activeIssue
    );
  }

  // Sync visual cues + URL after every filter change so an operator can
  // share the URL with a teammate and reproduce exactly what they're
  // looking at. Uses replaceState so the back button doesn't fill up
  // with intermediate filter states.
  function syncStateAfterFilter() {
    var filtersBar = document.getElementById('argus-filters');
    if (filtersBar) {
      filtersBar.classList.toggle('argus-filters-active', isAnyFilterActive());
    }
    // Highlight the active issue-chip (and clear all others) so the
    // user sees which chip drove the current filter.
    document.querySelectorAll('.argus-issue-chip').forEach(function(chip) {
      var on = !!(activeIssue
                  && chip.dataset.issue === activeIssue
                  && chip.dataset.tester === activeIssueTester);
      chip.classList.toggle('argus-issue-chip-active', on);
    });
    // Highlight the stat card matching the current verdict / flagged
    // selection so the click-shortcut affordance stays visible after use.
    document.querySelectorAll('.argus-stat').forEach(function(card) {
      var action = card.dataset.statAction;
      var value = card.dataset.statValue;
      var active = false;
      if (action === 'verdict' && verdict && value === verdict.value && value) {
        active = true;
      }
      if (action === 'flagged' && flaggedOnly && flaggedOnly.checked) {
        active = true;
      }
      card.classList.toggle('argus-stat-active', active);
    });
    document.querySelectorAll('[data-action-filter]').forEach(function(card) {
      var on = !!(action && action.value
                  && card.dataset.actionFilter === action.value
                  && (!card.dataset.tester
                      || (tester && card.dataset.tester === tester.value)));
      card.classList.toggle('argus-action-active', on);
    });
    document.querySelectorAll('[data-run-summary]').forEach(function(row) {
      row.classList.toggle('argus-run-selected', !!(run && run.value === row.dataset.runSummary));
    });
    if (runNavigation) runNavigation.sync();
    // URL persistence — only write params that are non-default to keep
    // the URL short. URLSearchParams gives stable encoding (spaces as +).
    try {
      var p = new URLSearchParams();
      if (search && search.value) p.set('q', search.value);
      if (verdict && verdict.value) p.set('verdict', verdict.value);
      if (status && status.value) p.set('status', status.value);
      if (tester && tester.value) p.set('tester', tester.value);
      if (run && run.value) p.set('run', run.value);
      if (executionDay && executionDay.value) p.set('day', executionDay.value);
      if (execFrom && execFrom.value) p.set('execFrom', execFrom.value);
      if (execTo && execTo.value) p.set('execTo', execTo.value);
      if (auditFrom && auditFrom.value) p.set('audFrom', auditFrom.value);
      if (auditTo && auditTo.value) p.set('audTo', auditTo.value);
      if (action && action.value) p.set('action', action.value);
      if (category && category.value) p.set('category', category.value);
      if (highOnly && highOnly.checked) p.set('high', '1');
      if (flaggedOnly && flaggedOnly.checked) p.set('flagged', '1');
      if (bookmarkedOnly && bookmarkedOnly.checked) p.set('bookmarked', '1');
      var qs = p.toString();
      var newUrl = location.pathname + (qs ? '?' + qs : '') + location.hash;
      if (newUrl !== location.pathname + location.search + location.hash) {
        history.replaceState(null, '', newUrl);
      }
    } catch (err) { /* URLSearchParams missing on ancient browsers — fine */ }
  }

  // ---- indexed filtering: inverted indexes per facet + cached row state, set intersection ----
  // The original re-queried the DOM and re-split every row's attributes on each keystroke. Here each
  // facet is a Map(value -> Set(rowIndex)) built once, row search text and detail rows are cached, and
  // a filter is the intersection of the active sets, smallest first; the search box is verified with the
  // same substring test as before so results are identical. (A trigram index was tried and measured:
  // no gain over V8's indexOf at these sizes, so it was removed.)
  var ROWS = Array.prototype.slice.call(document.querySelectorAll('tr.argus-row'));
  var DETAIL_OF = ROWS.map(function(r){ return document.querySelector('tr[data-detail="' + r.dataset.idx + '"]'); });
  var ALL = new Set(ROWS.map(function(_, i){ return i; }));
  var MATCHED = ALL;
  var exportCsv = document.getElementById('argus-export-csv');
  var exportAllCsv = document.getElementById('argus-export-all-csv');
  var RUN_HEADINGS = Array.from(document.querySelectorAll('tr.argus-run-heading'));
  var RUN_ORDER = new Map();
  RUN_HEADINGS.forEach(function(heading, i) { RUN_ORDER.set(heading.dataset.run, i); });
  function dayGroup(row) { return JSON.stringify([row.dataset.run, row.dataset.day]); }
  var DAY_HEADINGS = new Map();
  var DAY_ORDER = new Map();
  document.querySelectorAll('tr.argus-day-heading').forEach(function(heading, i) {
    DAY_HEADINGS.set(dayGroup(heading), heading);
    DAY_ORDER.set(dayGroup(heading), i);
  });
  var collapsedRuns = new Set();
  var EMPTY = new Set();
  function buildIndex(getter){ var m = new Map(); ROWS.forEach(function(r, i){ getter(r).forEach(function(k){ if (!k) return; var s = m.get(k); if (!s){ s = new Set(); m.set(k, s); } s.add(i); }); }); return m; }
  var IDX = {
    verdict:  buildIndex(function(r){ return [r.dataset.verdict || '']; }),
    analysis: buildIndex(function(r){ return [r.dataset.analysisStatus || '']; }),
    status:   buildIndex(function(r){ return [r.dataset.status || '']; }),
    tester:   buildIndex(function(r){ return [r.dataset.tester || '']; }),
    run:      buildIndex(function(r){ return [r.dataset.run || '']; }),
    day:      buildIndex(function(r){ return [r.dataset.day || '']; }),
    action:   buildIndex(function(r){ return (r.dataset.actions || '').split('|'); }),
    category: buildIndex(function(r){ return (r.dataset.categories || '').split('|'); }),
    issue:    buildIndex(function(r){ return (r.dataset.issues || '').split('|'); }),
    high:     buildIndex(function(r){ return [r.dataset.high === 'true' ? 'y' : '']; }).get('y') || EMPTY,
    flagged:  buildIndex(function(r){ return [r.dataset.flagged === 'true' ? 'y' : '']; }).get('y') || EMPTY,
    key:      ROWS.map(function(r){ var b = r.querySelector('button.argus-bookmark'); return b ? b.dataset.key : null; })
  };
  var SEARCH = ROWS.map(function(r){ return r.dataset.search || ''; });
  function intersect(a, b){ if (a.size > b.size){ var tmp = a; a = b; b = tmp; } var out = new Set(); a.forEach(function(v){ if (b.has(v)) out.add(v); }); return out; }
  function issueSet(label){
    var exact = IDX.issue.get(label); if (exact) return exact;
    var out = new Set(); IDX.issue.forEach(function(set, k){ if (k.indexOf(label) >= 0) set.forEach(function(v){ out.add(v); }); }); return out;
  }
  window.__argusFilterStats = { rows: ROWS.length, facets: Object.keys(IDX).length };

  // Inclusive custom date range over a per-row ISO date attribute (data-day
  // for execution date, data-audited for audit date). Ranges are computed on
  // demand rather than prebuilt into IDX buckets because their endpoints are
  // arbitrary, not the discrete facet values IDX is built from. ISO 'YYYY-MM-DD'
  // strings compare correctly with plain string comparison. A row is a member
  // only when it HAS a real date in that dimension: empty and the '_unknown'
  // execution-date sentinel are excluded whenever either endpoint is set, so a
  // range never silently pulls in undated rows. Returns null when the range is
  // inactive (both endpoints blank) so the caller can skip pushing a set.
  function rangeSet(attr, from, to) {
    if (!from && !to) return null;
    var out = new Set();
    ROWS.forEach(function(row, i) {
      var day = row.dataset[attr] || '';
      if (!day || day === '_unknown') return;
      if (from && day < from) return;
      if (to && day > to) return;
      out.add(i);
    });
    return out;
  }

  var summaryCards = Array.from(document.querySelectorAll('.argus-stat[data-stat-action]'));
  function updateSummary(matched) {
    var total = 0;
    var verdictCounts = new Map();
    // Summarize every match, including other pages and collapsed runs.
    // Unaudited executions have no audit; incomplete audits have no final verdict.
    matched.forEach(function(i) {
      var data = ROWS[i].dataset;
      if (data.analysisStatus === 'not_audited') return;
      total++;
      if (data.analysisStatus !== 'incomplete') {
        verdictCounts.set(data.verdict, (verdictCounts.get(data.verdict) || 0) + 1);
      }
    });
    summaryCards.forEach(function(card) {
      var value;
      var description;
      if (card.dataset.statAction === 'reset') {
        value = total;
        description = isAnyFilterActive() ? 'Matching audited executions' : 'All audited executions';
      } else if (card.dataset.statAction === 'verdict') {
        value = verdictCounts.get(card.dataset.statValue) || 0;
        description = total ? Math.round(100 * value / total) + '% of audits' : '\u2014';
      } else {
        return;
      }
      card.querySelector('.argus-stat-value').textContent = value;
      card.querySelector('.metric-description').textContent = description;
    });
  }

  function applyFilters() {
    if (!search) return;
    var q = search.value.toLowerCase();
    var v = verdict.value, st = status ? status.value : '', t = tester.value;
    var act = action ? action.value : '', cat = category ? category.value : '';
    var selectedRun = run ? run.value : '';
    var selectedDay = executionDay ? executionDay.value : '';
    var execFromVal = execFrom ? execFrom.value : '';
    var execToVal = execTo ? execTo.value : '';
    var auditFromVal = auditFrom ? auditFrom.value : '';
    var auditToVal = auditTo ? auditTo.value : '';
    var sets = [];
    if (v) {
      sets.push(
        v === 'incomplete'
          ? (IDX.analysis.get('incomplete') || EMPTY)
          : (IDX.verdict.get(v) || EMPTY)
      );
    }
    if (st) sets.push(IDX.status.get(st) || EMPTY);
    if (t) sets.push(IDX.tester.get(t) || EMPTY);
    if (selectedRun) sets.push(IDX.run.get(selectedRun) || EMPTY);
    if (selectedDay) sets.push(IDX.day.get(selectedDay) || EMPTY);
    var execRange = rangeSet('day', execFromVal, execToVal);
    if (execRange) sets.push(execRange);
    var auditRange = rangeSet('audited', auditFromVal, auditToVal);
    if (auditRange) sets.push(auditRange);
    if (act) sets.push(IDX.action.get(act) || EMPTY);
    if (cat) sets.push(IDX.category.get(cat) || EMPTY);
    if (highOnly.checked) sets.push(IDX.high);
    if (flaggedOnly.checked) sets.push(IDX.flagged);
    if (activeIssue) sets.push(issueSet(activeIssue));
    if (bookmarkedOnly && bookmarkedOnly.checked) { var bset = new Set(); IDX.key.forEach(function(k, i){ if (k && bookmarks.has(k)) bset.add(i); }); sets.push(bset); }
    sets.sort(function(a, b){ return a.size - b.size; });
    var cand = sets.length ? sets[0] : ALL;
    for (var i = 1; i < sets.length && cand.size; i++) cand = intersect(cand, sets[i]);
    var matched = new Set();
    ROWS.forEach(function(row, i) { if (cand.has(i) && (!q || SEARCH[i].indexOf(q) >= 0)) matched.add(i); });
    MATCHED = matched;
    updateSummary(matched);
    if (exportCsv) exportCsv.disabled = matched.size === 0;
    // pagination: 50 rows per page over the matched rows, in current (possibly sorted) DOM order
    var sig = [q, v, st, t, selectedRun, selectedDay, execFromVal, execToVal, auditFromVal, auditToVal, act, cat, highOnly.checked, flaggedOnly.checked, bookmarkedOnly && bookmarkedOnly.checked, activeIssue].join('\u0001');
    if (sig !== PAGE.sig) { PAGE.sig = sig; PAGE.n = 1; }
    var tbody = ROWS.length ? ROWS[0].parentNode : null;
    var ordered = tbody ? Array.prototype.slice.call(tbody.querySelectorAll('tr.argus-row')) : ROWS;
    var visibleRows = ordered.filter(function(r){ return matched.has(ROW_INDEX.get(r)); });
    var pages = Math.max(1, Math.ceil(visibleRows.length / PAGE.size));
    if (PAGE.n > pages) PAGE.n = pages;
    var lo = (PAGE.n - 1) * PAGE.size, hi = lo + PAGE.size;
    var onPage = new Set(visibleRows.slice(lo, hi).map(function(r){ return ROW_INDEX.get(r); }));
    var pageRuns = new Set();
    var pageDays = new Set();
    var collapsedCount = 0;
    ROWS.forEach(function(row, i) {
      if (onPage.has(i)) pageRuns.add(row.dataset.run);
      var collapsed = collapsedRuns.has(row.dataset.run);
      if (onPage.has(i) && collapsed) collapsedCount++;
      var show = onPage.has(i) && !collapsed;
      if (show) pageDays.add(dayGroup(row));
      row.style.display = show ? '' : 'none';
      var detail = DETAIL_OF[i];
      if (detail) {
        if (!show) {
          detail.classList.add('hidden');
          if (row.hasAttribute('aria-expanded')) row.setAttribute('aria-expanded', 'false');
        }
        detail.style.display = show ? '' : 'none';
      }
    });
    RUN_HEADINGS.forEach(function(heading) {
      heading.style.display = pageRuns.has(heading.dataset.run) ? '' : 'none';
      var toggle = heading.querySelector('[data-run-toggle]');
      if (toggle) toggle.setAttribute('aria-expanded', String(!collapsedRuns.has(heading.dataset.run)));
    });
    DAY_HEADINGS.forEach(function(heading, key) {
      heading.style.display = pageDays.has(key) ? '' : 'none';
    });
    var noMatches = document.getElementById('argus-no-matches');
    if (noMatches) noMatches.hidden = matched.size !== 0;
    if (countEl) countEl.textContent = (visibleRows.length ? (lo + 1) + '\u2013' + Math.min(hi, visibleRows.length) : '0') + ' of ' + visibleRows.length + (visibleRows.length !== totalRows ? ' (' + totalRows + ' total)' : '') + (collapsedCount ? ' \u00b7 ' + collapsedCount + ' hidden in collapsed runs' : '');
    renderPagination(pages);
    syncStateAfterFilter();
  }
  var PAGE = { size: 50, n: 1, sig: null };
  var ROW_INDEX = new Map(); ROWS.forEach(function(r, i){ ROW_INDEX.set(r, i); });
  function downloadCsv(indices, runKey) {
      if (!exportCsv || !indices.size) return;
      function cell(value) {
        var text = value == null ? '' : String(value);
        // Spreadsheet applications can interpret quoted cells as formulas.
        if (/^[\s\u0000-\u001f]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
        return '"' + text.replace(/"/g, '""') + '"';
      }
      var values = [JSON.parse(exportCsv.dataset.csvHeaders)];
      // DOM order preserves sorting; exports include other pages and collapsed sections.
      document.querySelectorAll('tr.argus-row').forEach(function(row) {
        if (indices.has(ROW_INDEX.get(row))) values.push(JSON.parse(row.dataset.csv));
      });
      var csv = '\uFEFF' + values.map(function(row) { return row.map(cell).join(','); }).join('\r\n') + '\r\n';
      var url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
      var link = document.createElement('a');
      link.href = url;
      link.download = runKey
        ? exportCsv.dataset.filename.replace(/\.csv$/i, '') + '-' + runKey.replace(/[^a-zA-Z0-9_-]/g, '_') + '.csv'
        : exportCsv.dataset.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
  }
  if (exportCsv) {
    exportCsv.disabled = ROWS.length === 0;
    exportCsv.addEventListener('click', function() {
      downloadCsv(MATCHED, [
        run && run.value,
        executionDay && executionDay.value,
        execFrom && execFrom.value && ('exec' + execFrom.value),
        execTo && execTo.value && ('exec' + execTo.value),
        auditFrom && auditFrom.value && ('audit' + auditFrom.value),
        auditTo && auditTo.value && ('audit' + auditTo.value)
      ].filter(Boolean).join('-'));
    });
  }
  if (exportAllCsv) {
    exportAllCsv.disabled = ROWS.length === 0;
    exportAllCsv.addEventListener('click', function() { downloadCsv(ALL); });
  }
  document.querySelectorAll('[data-run-export]').forEach(function(button) {
    var rows = IDX.run.get(button.dataset.runExport) || EMPTY;
    button.disabled = rows.size === 0;
    button.addEventListener('click', function() { downloadCsv(rows, button.dataset.runExport); });
  });
  document.querySelectorAll('[data-run-select]').forEach(function(button) {
    button.addEventListener('click', function() {
      if (!run) return;
      run.value = button.dataset.runSelect;
      collapsedRuns.delete(run.value);
      applyFilters();
      if (runNavigation) runNavigation.closeDrawer();
      var table = document.getElementById('argus-table');
      if (table) table.scrollIntoView({ block: 'start' });
    });
  });
  document.querySelectorAll('[data-run-toggle]').forEach(function(button) {
    button.addEventListener('click', function() {
      var key = button.dataset.runToggle;
      if (collapsedRuns.has(key)) collapsedRuns.delete(key); else collapsedRuns.add(key);
      applyFilters();
    });
  });
  if (run) run.addEventListener('change', function() {
    collapsedRuns.delete(run.value);
    applyFilters();
  });

  function setupRunNavigation() {
    var picker = document.getElementById('argus-run-picker');
    if (!run || !picker) return null;
    var trigger = document.getElementById('argus-run-picker-open');
    var label = document.getElementById('argus-run-picker-label');
    var panel = document.getElementById('argus-run-picker-panel');
    var input = document.getElementById('argus-run-picker-search');
    var list = document.getElementById('argus-run-picker-options');
    var empty = document.getElementById('argus-run-picker-empty');
    var choices = Array.from(run.options).map(function(option, index) {
      return { key: option.value, label: option.textContent, id: 'argus-run-option-' + index };
    });
    var matches = [];
    var active = -1;

    function positionPicker() {
      if (panel.hidden) return;
      var rect = trigger.getBoundingClientRect();
      var width = Math.min(Math.max(rect.width, 320), document.documentElement.clientWidth - 24);
      var below = window.innerHeight - rect.bottom - 20;
      var above = rect.top - 20;
      var upwards = below < 240 && above > below;
      panel.style.width = width + 'px';
      panel.style.left = Math.max(12, Math.min(rect.left, document.documentElement.clientWidth - width - 12)) + 'px';
      panel.style.top = upwards ? 'auto' : (rect.bottom + 8) + 'px';
      panel.style.bottom = upwards ? (window.innerHeight - rect.top + 8) + 'px' : 'auto';
      panel.style.maxHeight = Math.min(360, Math.max(80, upwards ? above : below)) + 'px';
    }
    function setActive(index) {
      active = index;
      Array.from(list.children).forEach(function(option, i) {
        option.classList.toggle('argus-run-option-active', i === active);
      });
      if (active >= 0 && matches[active]) {
        input.setAttribute('aria-activedescendant', matches[active].id);
        list.children[active].scrollIntoView({ block: 'nearest' });
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }
    function renderChoices() {
      var query = input.value.trim().toLowerCase();
      matches = choices.filter(function(choice) { return choice.label.toLowerCase().includes(query); });
      list.replaceChildren();
      matches.forEach(function(choice) {
        var option = document.createElement('li');
        option.id = choice.id;
        option.dataset.runChoice = choice.key;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(choice.key === run.value));
        option.textContent = choice.label;
        list.appendChild(option);
      });
      empty.hidden = matches.length > 0;
      var selected = matches.findIndex(function(choice) { return choice.key === run.value; });
      setActive(matches.length ? Math.max(0, selected) : -1);
    }
    function closePicker(restoreFocus) {
      panel.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      if (restoreFocus) trigger.focus();
    }
    function openPicker() {
      panel.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      input.setAttribute('aria-expanded', 'true');
      input.value = '';
      positionPicker();
      renderChoices();
      input.focus({ preventScroll: true });
    }
    function choose(key) {
      run.value = key;
      collapsedRuns.delete(key);
      closePicker(true);
      applyFilters();
    }
    trigger.addEventListener('click', function() {
      if (panel.hidden) openPicker(); else closePicker(true);
    });
    trigger.addEventListener('keydown', function(event) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        openPicker();
      }
    });
    input.addEventListener('input', renderChoices);
    input.addEventListener('keydown', function(event) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (matches.length) setActive((active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length);
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (matches[active]) choose(matches[active].key);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closePicker(true);
      } else if (event.key === 'Tab') {
        closePicker(true);
      }
    });
    list.addEventListener('mousedown', function(event) { event.preventDefault(); });
    list.addEventListener('click', function(event) {
      var option = event.target.closest('[data-run-choice]');
      if (option && list.contains(option)) choose(option.dataset.runChoice);
    });
    document.addEventListener('click', function(event) {
      if (!panel.hidden && !picker.contains(event.target)) closePicker(false);
    });
    document.addEventListener('focusin', function(event) {
      if (!panel.hidden && !picker.contains(event.target)) closePicker(false);
    });
    window.addEventListener('resize', positionPicker);
    window.addEventListener('scroll', positionPicker);
    // Keep the original select as the filter's source of truth and as a
    // fallback if scripts are unavailable. Only replace it after setup.
    run.hidden = true;
    picker.hidden = false;

    var drawer = document.getElementById('argus-runs-drawer');
    var drawerOpen = document.getElementById('argus-runs-open');
    var drawerClose = document.getElementById('argus-runs-close');
    var drawerSearch = document.getElementById('argus-runs-search');
    var drawerCount = document.getElementById('argus-runs-count');
    var drawerEmpty = document.getElementById('argus-runs-empty');
    var cards = Array.from(drawer.querySelectorAll('[data-run-summary]')).map(function(card) {
      return { node: card, label: card.querySelector('[data-run-select]').textContent.toLowerCase() };
    });
    function filterDrawer() {
      var query = drawerSearch.value.trim().toLowerCase();
      var count = 0;
      cards.forEach(function(card) {
        card.node.hidden = !card.label.includes(query);
        if (!card.node.hidden) count++;
      });
      drawerCount.textContent = count + ' of ' + cards.length + ' runs';
      drawerEmpty.hidden = count > 0;
    }
    function closeDrawer() {
      if (drawer.open) drawer.close();
    }
    if (typeof drawer.showModal === 'function') {
      drawerOpen.hidden = false;
      drawerOpen.addEventListener('click', function() {
        closePicker(false);
        drawerSearch.value = '';
        filterDrawer();
        drawer.showModal();
        document.body.classList.add('argus-runs-open');
        drawerOpen.setAttribute('aria-expanded', 'true');
        drawerSearch.focus();
      });
      drawerClose.addEventListener('click', closeDrawer);
      drawerSearch.addEventListener('input', filterDrawer);
      drawer.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          closeDrawer();
        }
      });
      drawer.addEventListener('close', function() {
        document.body.classList.remove('argus-runs-open');
        drawerOpen.setAttribute('aria-expanded', 'false');
        drawerOpen.focus({ preventScroll: true });
      });
      drawer.addEventListener('click', function(event) {
        var rect = drawer.getBoundingClientRect();
        if (event.target === drawer && (event.clientX < rect.left || event.clientX > rect.right ||
            event.clientY < rect.top || event.clientY > rect.bottom)) closeDrawer();
      });
    }
    return {
      closeDrawer: closeDrawer,
      isDrawerOpen: function() { return drawer.open; },
      sync: function() {
        var selected = choices.find(function(choice) { return choice.key === run.value; });
        label.textContent = selected ? selected.label : run.value;
        trigger.title = label.textContent;
        trigger.setAttribute('aria-label', 'Filter by test run: ' + label.textContent);
        if (!panel.hidden) renderChoices();
      }
    };
  }

  function renderPagination(pages) {
    var nav = document.getElementById('cs-pagination'); if (!nav) return;
    while (nav.firstChild) nav.removeChild(nav.firstChild);
    if (pages <= 1) return;
    function btn(label, page, opts) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'cs-page'; b.textContent = label;
      if (opts && opts.current) b.setAttribute('aria-current', 'page');
      if (opts && opts.disabled) b.disabled = true;
      if (opts && opts.label) b.setAttribute('aria-label', opts.label);
      b.addEventListener('click', function(){ PAGE.n = page; applyFilters(); var tbl = document.getElementById('argus-table'); if (tbl) tbl.scrollIntoView({ block: 'start' }); });
      return b;
    }
    nav.appendChild(btn('\u2039', Math.max(1, PAGE.n - 1), { disabled: PAGE.n === 1, label: 'Previous page' }));
    var shown = new Set([1, pages, PAGE.n, PAGE.n - 1, PAGE.n + 1].filter(function(p){ return p >= 1 && p <= pages; }));
    var last = 0;
    for (var p = 1; p <= pages; p++) {
      if (!shown.has(p)) continue;
      if (p - last > 1) { var gap = document.createElement('span'); gap.className = 'cs-page-gap'; gap.textContent = '\u2026'; nav.appendChild(gap); }
      nav.appendChild(btn(String(p), p, { current: p === PAGE.n })); last = p;
    }
    nav.appendChild(btn('\u203a', Math.min(pages, PAGE.n + 1), { disabled: PAGE.n === pages, label: 'Next page' }));
  }
  // sorting re-orders the DOM after its own handler runs; re-slice the current page afterwards
  document.querySelectorAll('.argus-sortable').forEach(function(th){ th.addEventListener('click', function(){ setTimeout(applyFilters, 0); }); });

  ['input', 'change'].forEach(function(e) {
    [search, verdict, status, tester, executionDay, execFrom, execTo,
     auditFrom, auditTo, action, category, highOnly,
     flaggedOnly, bookmarkedOnly].forEach(function(el) {
      if (el) el.addEventListener(e, applyFilters);
    });
  });

  if (clearBtn) {
    clearBtn.addEventListener('click', function() {
      search.value = '';
      verdict.value = '';
      if (status) status.value = '';
      tester.value = '';
      if (run) run.value = '';
      if (executionDay) executionDay.value = '';
      if (execFrom) execFrom.value = '';
      if (execTo) execTo.value = '';
      if (auditFrom) auditFrom.value = '';
      if (auditTo) auditTo.value = '';
      collapsedRuns.clear();
      if (action) action.value = '';
      if (category) category.value = '';
      highOnly.checked = false;
      flaggedOnly.checked = false;
      if (bookmarkedOnly) bookmarkedOnly.checked = false;
      activeIssue = null;
      activeIssueTester = null;
      applyFilters();
    });
  }

  // ---- Per-tester top-issue chips: click → filter audits to that
  // tester + that issue, scroll to the table. Same chip again → clear.
  document.querySelectorAll('button.argus-issue-chip').forEach(function(chip) {
    chip.addEventListener('click', function() {
      var issue = chip.dataset.issue;
      var t = chip.dataset.tester;
      // Toggle: re-clicking the active chip clears the filter.
      if (activeIssue === issue && activeIssueTester === t) {
        activeIssue = null;
        activeIssueTester = null;
        if (tester) tester.value = '';
      } else {
        activeIssue = issue;
        activeIssueTester = t;
        if (tester) tester.value = t;
      }
      applyFilters();
      var table = document.getElementById('argus-table');
      if (table) {
        table.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    });
  });

  // ---- Per-tester issue lists: "+N more" swaps the top-3 chips for the
  // full list (worst severity first); the header button does it for every
  // tester at once so a reviewer can work the whole folder person by person.
  function setIssuesExpanded(cell, on) {
    cell.classList.toggle('argus-issues-expanded', on);
  }
  document.querySelectorAll('button.argus-issues-more').forEach(function(btn) {
    btn.addEventListener('click', function() {
      setIssuesExpanded(btn.closest('td'), true);
    });
  });
  document.querySelectorAll('button.argus-issues-fewer').forEach(function(btn) {
    btn.addEventListener('click', function() {
      setIssuesExpanded(btn.closest('td'), false);
    });
  });
  var issuesToggleAll = document.getElementById('argus-issues-toggle-all');
  if (issuesToggleAll) {
    issuesToggleAll.addEventListener('click', function() {
      var expand = issuesToggleAll.dataset.state !== 'expanded';
      document.querySelectorAll('td.argus-issues-cell').forEach(function(cell) {
        if (cell.querySelector('.argus-issues-full')) setIssuesExpanded(cell, expand);
      });
      issuesToggleAll.dataset.state = expand ? 'expanded' : 'compact';
      issuesToggleAll.textContent = expand ? 'Show top 3 only' : 'Show all issues';
    });
  }

  // ---- Bookmark click: toggle, persist, refresh button + filter ----------
  // Click handler stops propagation so the row's expand-detail click
  // doesn't also fire (those two clicks would otherwise both run).
  document.querySelectorAll('button.argus-bookmark').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var k = btn.dataset.key;
      if (bookmarks.has(k)) bookmarks.delete(k);
      else bookmarks.add(k);
      saveBookmarks(bookmarks);
      refreshBookmarkButtons();
      // If "Bookmarked only" is currently on, an unbookmark needs to
      // immediately hide the row (and re-bookmark needs to reveal it).
      if (bookmarkedOnly && bookmarkedOnly.checked) applyFilters();
    });
  });

  // ---- Stat cards as filter shortcuts ------------------------------------
  // Audits = reset all; Compliant/Concerns/Deviation = set verdict; Flagged = toggle
  // the flagged-only checkbox. Re-clicking an already-active stat clears
  // it (toggle behaviour keeps the cards composable with the other
  // filters).
  document.querySelectorAll('.argus-stat').forEach(function(card) {
    card.addEventListener('click', function() {
      var action = card.dataset.statAction;
      var value = card.dataset.statValue;
      if (action === 'reset') {
        if (clearBtn) clearBtn.click(); else applyFilters();
        return;
      }
      if (action === 'verdict' && verdict) {
        verdict.value = (verdict.value === value) ? '' : value;
      }
      if (action === 'flagged' && flaggedOnly) {
        flaggedOnly.checked = !flaggedOnly.checked;
      }
      applyFilters();
    });
  });

  // ---- Keyboard shortcut: `/` focuses the search input -------------------
  // Single-key shortcut, no modifier, ignored when the user is already
  // typing in a form field (so it doesn't hijack `/` in an actual search
  // term — e.g. a URL or a path containing /).
  document.addEventListener('keydown', function(e) {
    if (e.key !== '/') return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (runNavigation && runNavigation.isDrawerOpen()) return;
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' ||
              t.tagName === 'TEXTAREA' || t.isContentEditable)) {
      return;
    }
    e.preventDefault();
    if (search) { search.focus(); search.select(); }
  });

  // ---- Restore filter state from the URL on page load --------------------
  // Lets `?tester=Akalyaa%20E&verdict=fail` deep-link a teammate to the
  // exact filtered view. Runs once before the first applyFilters() call.
  (function hydrateFromUrl() {
    try {
      var p = new URLSearchParams(location.search);
      if (search && p.has('q')) search.value = p.get('q');
      if (verdict && p.has('verdict')) verdict.value = p.get('verdict');
      if (status && p.has('status')) status.value = p.get('status');
      if (tester && p.has('tester')) tester.value = p.get('tester');
      if (run && p.has('run')) {
        var requestedRun = p.get('run');
        // A link to a removed run must show no matches, never silently show all runs.
        if (requestedRun && !IDX.run.has(requestedRun)) {
          var missingRun = document.createElement('option');
          missingRun.value = requestedRun;
          missingRun.textContent = requestedRun + ' (not in this report)';
          run.appendChild(missingRun);
        }
        run.value = requestedRun;
      }
      if (executionDay && p.has('day')) {
        var requestedDay = p.get('day');
        if (requestedDay && !IDX.day.has(requestedDay)) {
          var missingDay = document.createElement('option');
          missingDay.value = requestedDay;
          missingDay.textContent = requestedDay + ' (not in this report)';
          executionDay.appendChild(missingDay);
        }
        executionDay.value = requestedDay;
      }
      if (action && p.has('action')) action.value = p.get('action');
      if (execFrom && p.has('execFrom')) execFrom.value = p.get('execFrom');
      if (execTo && p.has('execTo')) execTo.value = p.get('execTo');
      if (auditFrom && p.has('audFrom')) auditFrom.value = p.get('audFrom');
      if (auditTo && p.has('audTo')) auditTo.value = p.get('audTo');
      // Expand the collapsible date-range panel when a shared link restores a
      // range, so the reviewer can see (and clear) the active endpoints.
      var dateDetails = document.getElementById('argus-daterange');
      if (dateDetails && ((execFrom && execFrom.value) || (execTo && execTo.value)
          || (auditFrom && auditFrom.value) || (auditTo && auditTo.value))) {
        dateDetails.open = true;
      }
      if (category && p.has('category')) category.value = p.get('category');
      if (highOnly && p.get('high') === '1') highOnly.checked = true;
      if (flaggedOnly && p.get('flagged') === '1') flaggedOnly.checked = true;
      if (bookmarkedOnly && p.get('bookmarked') === '1') {
        bookmarkedOnly.checked = true;
      }
    } catch (err) { /* ignore — fall through to defaults */ }
    applyFilters();
  })();
  runNavigation = setupRunNavigation();
  if (runNavigation) runNavigation.sync();

  // ---- Per-tester panel: click a name to filter audits to that tester -
  // and scroll to the audits table so the operator sees the filtered
  // result without losing context.
  document.querySelectorAll('button.argus-tester-link').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var name = btn.dataset.tester;
      if (!tester) return;
      tester.value = name;
      applyFilters();
      var table = document.getElementById('argus-table');
      if (table) {
        table.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    });
  });

  // ---- No-evidence panel: dismiss with the X button. The notice is
  // ephemeral UI sugar — every R12 audit is also flagged in the table
  // row itself, so dismissing the banner doesn't lose information.
  var noevdClose = document.getElementById('argus-noevd-close');
  var noevdPanel = document.getElementById('argus-noevd-panel');
  if (noevdClose && noevdPanel) {
    noevdClose.addEventListener('click', function() {
      noevdPanel.style.display = 'none';
    });
  }

  // ---- No-evidence panel: click a key chip to filter the audits table
  // to that single key and scroll into view.
  document.querySelectorAll('button.argus-noevd-key').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var key = btn.dataset.key;
      var search = document.getElementById('argus-search');
      if (search) {
        search.value = key.toLowerCase();
        applyFilters();
      }
      var table = document.getElementById('argus-table');
      if (table) {
        table.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    });
  });

  // ---- Column sort -------------------------------------------------------
  // Click any sortable header to sort by that column. Click again to
  // flip direction. Audit-row + detail-row pairs move together so the
  // expand-into-detail relationship survives sorting.
  var tbody = document.getElementById('argus-tbody');
  var sortState = { key: null, dir: 1 };
  document.querySelectorAll('#argus-table thead th.argus-sortable')
    .forEach(function(th) {
      th.addEventListener('click', function() {
        var key = th.dataset.sortKey;
        var numeric = th.dataset.sortNumeric === '1';
        var dir = (sortState.key === key) ? -sortState.dir : 1;
        sortState = { key: key, dir: dir };
        // Remove sort indicators on all headers, then mark the active
        // one. aria-sort mirrors the visual indicator for screen readers.
        document.querySelectorAll('#argus-table thead th.argus-sortable')
          .forEach(function(h) {
            h.classList.remove('argus-sorted-asc', 'argus-sorted-desc');
            h.setAttribute('aria-sort', 'none');
          });
        th.classList.add(dir > 0 ? 'argus-sorted-asc' : 'argus-sorted-desc');
        th.setAttribute('aria-sort', dir > 0 ? 'ascending' : 'descending');
        // Pair audit-rows with their following detail-row (unaudited
        // rows have no detail row, so pair on the actual class rather
        // than assuming a fixed stride of 2).
        var pairs = [];
        var rows = Array.from(tbody.children);
        for (var i = 0; i < rows.length; i++) {
          if (!rows[i].classList.contains('argus-row')) continue;
          var next = rows[i + 1];
          if (next && next.classList.contains('argus-detail-row')) {
            pairs.push([rows[i], next]);
            i++;
          } else {
            pairs.push([rows[i], null]);
          }
        }
        pairs.sort(function(a, b) {
          // Keep run/day sections together; sort columns inside each section.
          var ar = RUN_ORDER.get(a[0].dataset.run), br = RUN_ORDER.get(b[0].dataset.run);
          if (ar !== br) return (ar == null ? -1 : ar) - (br == null ? -1 : br);
          var ad = DAY_ORDER.get(dayGroup(a[0])), bd = DAY_ORDER.get(dayGroup(b[0]));
          if (ad !== bd) return (ad == null ? -1 : ad) - (bd == null ? -1 : bd);
          var av, bv;
          if (key === 'findings') {
            av = parseInt(a[0].dataset.findings, 10) || 0;
            bv = parseInt(b[0].dataset.findings, 10) || 0;
            return (av - bv) * dir;
          }
          if (key === 'verdict') {
            // Sort verdicts by severity rank (pass=0, concerns=1, fail=2)
            // so a verdict-sort surfaces fails together intuitively.
            var rank = { pass: 0, concerns: 1, fail: 2, unknown: 3 };
            av = rank[a[0].dataset.verdict] !== undefined
                  ? rank[a[0].dataset.verdict] : 99;
            bv = rank[b[0].dataset.verdict] !== undefined
                  ? rank[b[0].dataset.verdict] : 99;
            return (av - bv) * dir;
          }
          if (key === 'status') {
            // Sort by attention rank, not alphabetically: statuses that
            // need action (Blocked, In Progress, Fail, PWI) group first,
            // clean Passes last. Unknown statuses sort between.
            var srank = {
              'blocked': 0, 'in progress': 1, 'fail': 2,
              'passed with issue': 3, 'retest': 4, 'not executed': 5,
              'not applicable': 6, 'inactive': 7, 'pass': 9
            };
            av = (a[0].dataset.status || '').trim().toLowerCase();
            bv = (b[0].dataset.status || '').trim().toLowerCase();
            av = srank[av] !== undefined ? srank[av] : 8;
            bv = srank[bv] !== undefined ? srank[bv] : 8;
            return (av - bv) * dir;
          }
          if (key === 'key') {
            // Compare the row's data-key, NOT the cell text — the
            // cell also contains the bookmark ★/☆ glyph, so reading
            // innerText would let bookmarking reorder a key-sort.
            av = (a[0].dataset.key || '').toLowerCase();
            bv = (b[0].dataset.key || '').toLowerCase();
            if (av < bv) return -1 * dir;
            if (av > bv) return 1 * dir;
            return 0;
          }
          // Text-based columns. Pull from the visible cell content so
          // the sort matches what the user sees.
          var cellIdx = { tester: 1, test_case: 4 }[key] || 0;
          av = (a[0].children[cellIdx].innerText || '').trim().toLowerCase();
          bv = (b[0].children[cellIdx].innerText || '').trim().toLowerCase();
          if (av < bv) return -1 * dir;
          if (av > bv) return 1 * dir;
          return 0;
        });
        // Re-append in the new order. detail rows stay collapsed
        // because the click-to-expand state is independent of order.
        tbody.innerHTML = '';
        var previousRun = null;
        var previousDay = null;
        pairs.forEach(function(p) {
          var runKey = p[0].dataset.run;
          if (runKey !== previousRun) {
            var heading = RUN_HEADINGS[RUN_ORDER.get(runKey)];
            if (heading) tbody.appendChild(heading);
            previousRun = runKey;
          }
          var dayKey = dayGroup(p[0]);
          if (dayKey !== previousDay) {
            var dayHeading = DAY_HEADINGS.get(dayKey);
            if (dayHeading) tbody.appendChild(dayHeading);
            previousDay = dayKey;
          }
          tbody.appendChild(p[0]);
          if (p[1]) tbody.appendChild(p[1]);
        });
      });
    });

  // ---- Expand / collapse all ----------------------------------------------
  // Expand only touches rows the active filter left visible, so
  // "filter to one tester → Expand all" doesn't open the whole folder.
  function setDetailRow(row, open) {
    var detail = document.querySelector(
      'tr[data-detail="' + row.dataset.idx + '"]');
    if (!detail) return;
    if (open) ensureDetail(detail);
    detail.classList.toggle('hidden', !open);
    if (row.hasAttribute('aria-expanded')) {
      row.setAttribute('aria-expanded', String(open));
    }
  }
  var expandAllBtn = document.getElementById('argus-expand-all');
  var collapseAllBtn = document.getElementById('argus-collapse-all');
  if (expandAllBtn) {
    expandAllBtn.addEventListener('click', function() {
      collapsedRuns.clear();
      applyFilters();
      document.querySelectorAll('tr.argus-row').forEach(function(row) {
        if (row.style.display !== 'none') setDetailRow(row, true);
      });
    });
  }
  if (collapseAllBtn) {
    collapseAllBtn.addEventListener('click', function() {
      document.querySelectorAll('tr.argus-row').forEach(function(row) {
        setDetailRow(row, false);
      });
    });
  }

  // ---- Theme toggle ---------------------------------------------------------
  // The <head> bootstrap already applied any stored choice before first
  // paint; this wires the header button. Effective theme = explicit
  // data-theme attribute, else the OS preference. The stored key is
  // global (not folder-scoped like bookmarks) — an operator's theme
  // choice should follow them across folders.
  var themeToggle = document.getElementById('argus-theme-toggle');
  if (themeToggle) {
    var effectiveTheme = function() {
      var attr = document.documentElement.getAttribute('data-theme');
      if (attr === 'dark' || attr === 'light') return attr;
      try {
        if (window.matchMedia
            && window.matchMedia('(prefers-color-scheme: dark)').matches) {
          return 'dark';
        }
      } catch (e) { /* matchMedia missing — assume light */ }
      return 'light';
    };
    var renderThemeToggle = function() {
      var dark = effectiveTheme() === 'dark';
      themeToggle.setAttribute('aria-pressed', String(dark));
      // Label names the theme the click switches TO.
      themeToggle.textContent = dark ? 'Light' : 'Dark';
    };
    themeToggle.addEventListener('click', function() {
      var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try {
        localStorage.setItem('argus.theme', next);
      } catch (e) { /* quota / disabled — choice becomes session-only */ }
      renderThemeToggle();
    });
    renderThemeToggle();
  }

  // ---- Sticky thead offset -------------------------------------------------
  // The filter bar is sticky at top:0 and the thead sticks directly
  // under it. The bar's height changes when its flex contents wrap at
  // narrow widths, so the offset is measured, not hardcoded: a
  // ResizeObserver keeps --argus-filters-h current on <html>.
  var filtersBarEl = document.getElementById('argus-filters');
  if (filtersBarEl && 'ResizeObserver' in window) {
    var setFiltersHeight = function() {
      document.documentElement.style.setProperty(
        '--argus-filters-h', filtersBarEl.offsetHeight + 'px');
    };
    new ResizeObserver(setFiltersHeight).observe(filtersBarEl);
    setFiltersHeight();
  }

  // ---- Report-specific coverage and tester controls ----
  function openCoverageFromNav(e) {
    e.preventDefault();
    var b = document.getElementById('argus-coverage-open');
    if (b) b.click();
  }
  ['nav-coverage', 'coverage-link'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('click', openCoverageFromNav);
  });
  document.querySelectorAll('.argus-tester-table .cs-expand').forEach(function(b) {
    var tr = b.closest('tr');
    b.addEventListener('click', function() {
      var open = tr.classList.toggle('is-open');
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  });
  // Tester column: sticks to the top while it fits the viewport. When it is taller,
  // follow the scroll direction instead of adding a scrollbar: scrolling down parks
  // its bottom edge at the viewport bottom, scrolling up parks its top edge at the
  // top, and in between it moves with the page from wherever it was.
  var rightCol = document.querySelector('.right-column');
  var STICK_TOP = 60, STICK_GAP = 16, colMode = 'top', lastY = window.pageYOffset;
  function colStatic() { return !rightCol || getComputedStyle(rightCol).position === 'static'; }
  function stickTop() {
    rightCol.style.position = 'sticky'; rightCol.style.top = STICK_TOP + 'px'; colMode = 'top';
  }
  function stickBottom() {
    rightCol.style.position = 'sticky';
    rightCol.style.top = (window.innerHeight - STICK_GAP - rightCol.offsetHeight) + 'px';
    colMode = 'bottom';
  }
  function releaseCol() {  // keep the current visual spot; scroll with the page from here
    var offset = rightCol.getBoundingClientRect().top - rightCol.parentElement.getBoundingClientRect().top;
    rightCol.style.position = 'relative'; rightCol.style.top = Math.max(0, offset) + 'px'; colMode = 'free';
  }
  function trackCol() {
    if (colStatic()) return;
    var y = window.pageYOffset, down = y > lastY; lastY = y;
    if (rightCol.offsetHeight <= window.innerHeight - STICK_TOP - STICK_GAP) {
      if (colMode !== 'top') stickTop();
      return;
    }
    var r = rightCol.getBoundingClientRect();
    if (down) {
      if (colMode === 'top') releaseCol();
      else if (colMode === 'free' && r.bottom <= window.innerHeight - STICK_GAP) stickBottom();
    } else {
      if (colMode === 'bottom') releaseCol();
      else if (colMode === 'free' && r.top >= STICK_TOP) stickTop();
    }
  }
  function refreshCol() {  // height or viewport changed: re-apply the current mode
    if (colStatic()) return;
    if (colMode === 'bottom') stickBottom(); else if (colMode === 'top') stickTop();
    trackCol();
  }
  if (rightCol) {
    window.addEventListener('scroll', trackCol, { passive: true });
    window.addEventListener('resize', refreshCol);
    if (window.ResizeObserver) new ResizeObserver(refreshCol).observe(rightCol);
    trackCol();
  }
})();
