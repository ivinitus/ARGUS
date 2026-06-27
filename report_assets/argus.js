
(function() {
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

  // ---- Coverage gap tabs (R15a / R15b / R15c) -----------------------------
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

  // ---- Row click toggles the paired detail row ----------------------------
  document.querySelectorAll('tr.argus-row').forEach(function(row) {
    row.addEventListener('click', function(e) {
      // Don't toggle if the user clicked a thumbnail link inside the
      // detail row (target.closest fails for cells outside this row).
      if (e.target.closest('a, button, input, select')) return;
      var idx = row.dataset.idx;
      var detail = document.querySelector('tr[data-detail="' + idx + '"]');
      if (detail) detail.classList.toggle('hidden');
    });
  });

  // ---- Filtering ----------------------------------------------------------
  var search = document.getElementById('argus-search');
  var verdict = document.getElementById('argus-verdict');
  var tester = document.getElementById('argus-tester');
  var source = document.getElementById('argus-source');
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
      (tester && tester.value) ||
      (source && source.value) ||
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
    // URL persistence — only write params that are non-default to keep
    // the URL short. URLSearchParams gives stable encoding (spaces as +).
    try {
      var p = new URLSearchParams();
      if (search && search.value) p.set('q', search.value);
      if (verdict && verdict.value) p.set('verdict', verdict.value);
      if (tester && tester.value) p.set('tester', tester.value);
      if (source && source.value) p.set('source', source.value);
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

  function applyFilters() {
    if (!search) return;
    var q = search.value.toLowerCase();
    var v = verdict.value;
    var t = tester.value;
    var src = source.value;
    var act = action ? action.value : '';
    var cat = category ? category.value : '';
    var hi = highOnly.checked;
    var fl = flaggedOnly.checked;
    var visible = 0;
    document.querySelectorAll('tr.argus-row').forEach(function(row) {
      var idx = row.dataset.idx;
      var detail = document.querySelector('tr[data-detail="' + idx + '"]');
      var show = true;
      if (q && (row.dataset.search || '').indexOf(q) < 0) show = false;
      if (v && row.dataset.verdict !== v) show = false;
      if (t && row.dataset.tester !== t) show = false;
      if (act && (row.dataset.actions || '').split('|').indexOf(act) < 0) {
        show = false;
      }
      if (cat && (row.dataset.categories || '').split('|').indexOf(cat) < 0) {
        show = false;
      }
      if (hi && row.dataset.high !== 'true') show = false;
      if (fl && row.dataset.flagged !== 'true') show = false;
      if (bookmarkedOnly && bookmarkedOnly.checked) {
        // Look the row's key out of its bookmark button. Cheap because
        // we already render one button per row.
        var bm = row.querySelector('button.argus-bookmark');
        if (!bm || !bookmarks.has(bm.dataset.key)) show = false;
      }
      // Issue-label filter: rows store their issue labels in
      // data-issues as a "|"-joined lowercased list. The chip's label
      // is also lowercased on click, so substring match suffices.
      if (activeIssue) {
        var issues = (row.dataset.issues || '');
        if (issues.indexOf(activeIssue) < 0) show = false;
      }
      // Source filter — same options the legacy report used:
      //   has-model / has-ocr / has-rule  → must have at least one
      //   only-model                      → has model AND not OCR
      //   only-ocr                        → has OCR AND not model
      if (src === 'has-model' && row.dataset.hasModel !== 'true') show = false;
      if (src === 'has-ocr' && row.dataset.hasOcr !== 'true') show = false;
      if (src === 'has-rule' && row.dataset.hasRule !== 'true') show = false;
      if (src === 'only-model' &&
          (row.dataset.hasModel !== 'true' || row.dataset.hasOcr === 'true'))
        show = false;
      if (src === 'only-ocr' &&
          (row.dataset.hasOcr !== 'true' || row.dataset.hasModel === 'true'))
        show = false;
      row.style.display = show ? '' : 'none';
      if (detail) {
        detail.classList.add('hidden');
        detail.style.display = show ? '' : 'none';
      }
      if (show) visible++;
    });
    if (countEl) countEl.textContent = visible + ' of ' + totalRows;
    syncStateAfterFilter();
  }

  ['input', 'change'].forEach(function(e) {
    [search, verdict, tester, source, action, category, highOnly, flaggedOnly,
     bookmarkedOnly].forEach(function(el) {
      if (el) el.addEventListener(e, applyFilters);
    });
  });

  if (clearBtn) {
    clearBtn.addEventListener('click', function() {
      search.value = '';
      verdict.value = '';
      tester.value = '';
      source.value = '';
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

  // ---- Action queue chips/cards: click → filter by required action ------
  document.querySelectorAll('[data-action-filter]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      if (!action) return;
      var nextAction = btn.dataset.actionFilter;
      action.value = (action.value === nextAction) ? '' : nextAction;
      if (btn.dataset.tester && tester) {
        tester.value = btn.dataset.tester;
      }
      applyFilters();
      var table = document.getElementById('argus-table');
      if (table) {
        table.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    });
  });

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
  // Audits = reset all; Pass/Concerns/Fail = set verdict; Flagged = toggle
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
      // 'divergent' is informational only — there's no per-row
      // divergence filter today (the tile shows the rollup count).
      // Clicking does nothing; the tooltip carries the meaning.
      // Skip applyFilters when no real filter changed so the user
      // doesn't see ghost-flicker on the rows.
      if (action === 'divergent') return;
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
      if (tester && p.has('tester')) tester.value = p.get('tester');
      if (source && p.has('source')) source.value = p.get('source');
      if (action && p.has('action')) action.value = p.get('action');
      if (category && p.has('category')) category.value = p.get('category');
      if (highOnly && p.get('high') === '1') highOnly.checked = true;
      if (flaggedOnly && p.get('flagged') === '1') flaggedOnly.checked = true;
      if (bookmarkedOnly && p.get('bookmarked') === '1') {
        bookmarkedOnly.checked = true;
      }
    } catch (err) { /* ignore — fall through to defaults */ }
    applyFilters();
  })();

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
        // Remove sort indicators on all headers, then mark the active one.
        document.querySelectorAll('#argus-table thead th.argus-sortable')
          .forEach(function(h) {
            h.classList.remove('argus-sorted-asc', 'argus-sorted-desc');
          });
        th.classList.add(dir > 0 ? 'argus-sorted-asc' : 'argus-sorted-desc');
        // Pair audit-rows with their following detail-row; sort the pairs.
        var pairs = [];
        var rows = Array.from(tbody.children);
        for (var i = 0; i < rows.length; i += 2) {
          pairs.push([rows[i], rows[i + 1]]);
        }
        pairs.sort(function(a, b) {
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
          // Text-based columns. Pull from the visible cell content so
          // the sort matches what the user sees.
          var cellIdx = { key: 0, tester: 1, test_case: 3 }[key] || 0;
          av = (a[0].children[cellIdx].innerText || '').trim().toLowerCase();
          bv = (b[0].children[cellIdx].innerText || '').trim().toLowerCase();
          if (av < bv) return -1 * dir;
          if (av > bv) return 1 * dir;
          return 0;
        });
        // Re-append in the new order. detail rows stay collapsed
        // because the click-to-expand state is independent of order.
        tbody.innerHTML = '';
        pairs.forEach(function(p) {
          tbody.appendChild(p[0]);
          if (p[1]) tbody.appendChild(p[1]);
        });
      });
    });
})();
