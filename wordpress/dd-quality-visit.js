/* Dice Dungeon: "QualityVisit" pixel event for Meta ad optimisation (10 Nov 2026 ad review).
 * Install as a WPCode "JavaScript Snippet", Auto Insert, Site Wide Footer. Skips logged-in WordPress users.
 * Fires once per page view when a visitor either
 *   - clicks a booking / buy button (Amelia "amelia-button", or a link/button saying book, varaa, osta, buy), or
 *   - stays 45 s with the tab visible and scrolls at least 40 % of the page.
 * Uses fbq only if the Meta pixel is loaded, so it respects the cookie banner (no consent, no pixel, no event).
 * Also sends a GA4 event "quality_visit" if gtag is present.
 */
(function () {
  if (document.body && document.body.classList.contains('logged-in')) return;
  var fired = false, start = Date.now(), maxScroll = 0;
  function fire(reason) {
    if (fired) return;
    fired = true;
    try {
      if (window.fbq) window.fbq('trackCustom', 'QualityVisit', { reason: reason, page: location.pathname });
      if (window.gtag) window.gtag('event', 'quality_visit', { reason: reason });
    } catch (e) {}
  }
  window.addEventListener('scroll', function () {
    var h = document.documentElement;
    var s = (h.scrollTop + window.innerHeight) / Math.max(h.scrollHeight, 1);
    if (s > maxScroll) maxScroll = s;
  }, { passive: true });
  var t = setInterval(function () {
    if (fired) return clearInterval(t);
    if (Date.now() - start >= 45000 && maxScroll >= 0.4 && document.visibilityState === 'visible') fire('engaged');
  }, 5000);
  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('a, button') : null;
    if (!el) return;
    var txt = (el.innerText || el.textContent || '').toLowerCase();
    if (el.id === 'amelia-button' || el.closest('#amelia-button') || /book|varaa|osta|buy|lahjakortti|gift/.test(txt)) fire('book_click');
  }, true);
})();
