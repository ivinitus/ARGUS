(function() {
  'use strict';

  var FRESHNESS_CLASSES = [
    'fresh-now',
    'fresh-recent',
    'fresh-day',
    'fresh-stale'
  ];

  function ageSeconds(timestampMs) {
    return Math.max(0, Math.floor((Date.now() - timestampMs) / 1000));
  }

  function relativeAge(seconds) {
    if (seconds < 60) return 'now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
  }

  function freshnessClass(seconds) {
    if (seconds < 600) return 'fresh-now';
    if (seconds < 14400) return 'fresh-recent';
    if (seconds < 172800) return 'fresh-day';
    return 'fresh-stale';
  }

  function updateRelativeTimes() {
    document.querySelectorAll('[data-argus-relative-time]').forEach(
      function(element) {
        var timestampMs = Number(element.dataset.timestampMs);
        if (!Number.isFinite(timestampMs)) return;

        var seconds = ageSeconds(timestampMs);
        var label = relativeAge(seconds);
        element.textContent = label;

        if (element.classList.contains('argus-folder-time-rel')) {
          FRESHNESS_CLASSES.forEach(function(className) {
            element.classList.remove(className);
          });
          element.classList.add(freshnessClass(seconds));

          var row = element.closest('[data-argus-row-label]');
          if (row) {
            row.setAttribute(
              'aria-label',
              row.dataset.argusRowLabel + ', updated ' + label
            );
          }
        }
      }
    );
  }

  updateRelativeTimes();
  window.setInterval(updateRelativeTimes, 30000);
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) updateRelativeTimes();
  });

  // ---- Report-name search -------------------------------------------------
  // This control intentionally has its own ID and implementation. The audit
  // report page has a richer `argus-search` filter; sharing that ID here would
  // make the audit script try to filter folder rows as execution rows.
  var search = document.getElementById('argus-report-search');
  var rows = Array.prototype.slice.call(
    document.querySelectorAll('tr.argus-folder-row')
  );
  var sections = Array.prototype.slice.call(
    document.querySelectorAll('[data-argus-report-section]')
  );
  var resultCount = document.getElementById('argus-report-search-count');
  var noResults = document.getElementById('argus-report-no-results');
  var searchableNames = rows.map(function(row) {
    return (row.dataset.argusReportSearch || '').toLocaleLowerCase();
  });

  function queryFromUrl() {
    try {
      return new URL(location.href).searchParams.get('q') || '';
    } catch (error) {
      return '';
    }
  }

  function syncSearchUrl(query) {
    try {
      var url = new URL(location.href);
      if (query) url.searchParams.set('q', query);
      else url.searchParams.delete('q');
      history.replaceState(
        null,
        '',
        url.pathname + url.search + url.hash
      );
    } catch (error) {
      // URL and history APIs are unavailable only in obsolete browsers.
    }
  }

  function applyReportSearch(syncUrl) {
    if (!search) return;
    var rawQuery = search.value.trim();
    var query = rawQuery.toLocaleLowerCase();
    var visible = 0;
    rows.forEach(function(row, index) {
      var matched = !query || searchableNames[index].indexOf(query) >= 0;
      row.hidden = !matched;
      if (matched) visible += 1;
    });
    sections.forEach(function(section) {
      var sectionRows = Array.prototype.slice.call(
        section.querySelectorAll('tr.argus-folder-row')
      );
      var sectionVisible = sectionRows.filter(function(row) {
        return !row.hidden;
      }).length;
      section.hidden = sectionVisible === 0;
      var count = section.querySelector('[data-argus-section-count]');
      if (count) count.textContent = String(sectionVisible);
    });
    if (resultCount) {
      resultCount.textContent =
        visible + ' of ' + rows.length +
        (rows.length === 1 ? ' report' : ' reports');
    }
    if (noResults) noResults.hidden = visible !== 0;
    if (syncUrl) syncSearchUrl(rawQuery);
  }

  if (search) {
    search.value = queryFromUrl();
    search.addEventListener('input', function() {
      applyReportSearch(true);
    });
    document.addEventListener('keydown', function(event) {
      var target = event.target;
      var editing = target && (
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT' ||
        target.isContentEditable
      );
      if (event.key === '/' && !editing) {
        event.preventDefault();
        search.focus();
        search.select();
      } else if (
        event.key === 'Escape' &&
        document.activeElement === search &&
        search.value
      ) {
        event.preventDefault();
        search.value = '';
        applyReportSearch(true);
      }
    });
    window.addEventListener('popstate', function() {
      search.value = queryFromUrl();
      applyReportSearch(false);
    });
    applyReportSearch(false);
  }

  // ---- Local report prefetch ----------------------------------------------
  // Published reports remain no-store, so only rows explicitly marked by the
  // local server participate. The newest eligible visible row is selected by
  // timestamp rather than by section order.
  var prefetched = new Set();
  function prefetchReport(href) {
    if (!href || prefetched.has(href)) return;
    prefetched.add(href);
    var link = document.createElement('link');
    link.rel = 'prefetch';
    link.as = 'document';
    link.href = href;
    document.head.appendChild(link);
  }
  var prefetchRows = rows.filter(function(row) {
    return row.dataset.argusPrefetch === 'true';
  });
  prefetchRows.forEach(function(row) {
    var anchor = row.querySelector('a.argus-folder-name');
    if (!anchor) return;
    row.addEventListener('mouseenter', function() {
      prefetchReport(anchor.href);
    }, { passive: true });
    row.addEventListener('focusin', function() {
      prefetchReport(anchor.href);
    });
  });
  var newest = prefetchRows
    .filter(function(row) { return !row.hidden; })
    .reduce(function(current, row) {
      if (!current) return row;
      return Number(row.dataset.argusReportTimestampMs) >
        Number(current.dataset.argusReportTimestampMs) ? row : current;
    }, null);
  if (newest) {
    var newestAnchor = newest.querySelector('a.argus-folder-name');
    if (newestAnchor) prefetchReport(newestAnchor.href);
  }
})();
