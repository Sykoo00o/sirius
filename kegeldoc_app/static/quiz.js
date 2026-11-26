/* kegeldoc quiz: answer without a page load.
 *
 * Every quiz screen is still a real page at /onboarding/N, and every answer is still a real
 * POST that the server checks and saves before anything moves on. This script only changes how
 * the next screen arrives: instead of the browser following a redirect and repainting the whole
 * page, the answer is sent in the background the moment it is tapped (while the selected option
 * shows), the server replies with the next screen in the same response, and only the header and
 * question area are swapped in. The address bar and the Back button follow along.
 *
 * Anything unexpected (no fetch, a network error, a reply that is not a quiz screen) falls back
 * to the ordinary page-by-page quiz, which is what runs with JavaScript off.
 */
(function () {
  'use strict';

  var BEAT = 260;   // ms the tapped option stays visibly selected before the next screen
  var canSwap = !!(window.fetch && window.DOMParser && window.FormData && window.URLSearchParams &&
                   window.history && history.pushState);
  var busy = false;

  function quizForm() { return document.getElementById('quiz-form'); }
  function isQuizPath(path) { return /^\/onboarding\/\d+$/.test(path); }
  function reducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // ---- Swap a fetched screen into the page ------------------------------------------------
  function show(html, url, push) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var nextTop = doc.querySelector('.quiz-top');
    var nextMain = doc.querySelector('.quiz-main');
    var top = document.querySelector('.quiz-top');
    var main = document.querySelector('.quiz-main');
    if (!nextTop || !nextMain || !top || !main) { location.assign(url); return; }

    // The progress bar grows from where it was rather than jumping.
    var oldBar = top.querySelector('.progress i');
    var newBar = nextTop.querySelector('.progress i');
    var from = oldBar ? oldBar.style.width : '';
    var to = newBar ? newBar.style.width : '';
    if (newBar && from) newBar.style.width = from;

    if (push) history.pushState({ kd: 1 }, '', url);
    else if (url !== location.pathname) history.replaceState({ kd: 1 }, '', url);

    function apply() {
      top.replaceWith(document.adoptNode(nextTop));
      main.replaceWith(document.adoptNode(nextMain));
      document.title = doc.title;
      window.scrollTo(0, 0);
    }
    function settle() {
      if (newBar && to) {
        void newBar.offsetWidth;            // commit the start width, then animate to the target
        newBar.style.width = to;
      }
      var title = document.querySelector('.q-title, .fact-title');
      if (title) { title.setAttribute('tabindex', '-1'); title.focus({ preventScroll: true }); }
      busy = false;
    }
    if (document.startViewTransition && !reducedMotion()) {
      var t = document.startViewTransition(apply);
      t.updateCallbackDone.then(settle, settle);
    } else {
      apply();
      settle();
    }
  }

  // ---- Send an answer ---------------------------------------------------------------------
  function submitAnswer(form, beat) {
    if (busy) return;
    busy = true;
    var started = Date.now();
    var action = new URL(form.getAttribute('action'), location.href).pathname;
    fetch(action, {
      method: 'POST',
      body: new URLSearchParams(new FormData(form)),
      headers: { 'X-KD-Swap': '1' },
      credentials: 'same-origin'
    }).then(function (res) {
      var away = res.headers.get('X-KD-Location');
      if (away) { location.assign(away); return; }                 // e.g. the last answer
      if (res.redirected && !isQuizPath(new URL(res.url).pathname)) { location.assign(res.url); return; }
      if (!res.ok && res.status !== 422) throw new Error('status ' + res.status);
      var url = res.headers.get('X-KD-URL') || (res.redirected ? new URL(res.url).pathname : action);
      return res.text().then(function (html) {
        var wait = Math.max(0, beat - (Date.now() - started));
        setTimeout(function () { show(html, url, url !== location.pathname); }, wait);
      });
    }).catch(function () {
      busy = false;
      form.submit();                                               // plain page-by-page fallback
    });
  }

  // ---- Load a screen (Back link, browser Back/Forward) -------------------------------------
  function load(url, push) {
    if (busy) return;
    busy = true;
    fetch(url, { headers: { 'X-KD-Swap': '1' }, credentials: 'same-origin' }).then(function (res) {
      var path = new URL(res.url).pathname;
      if (!res.ok || !isQuizPath(path)) { location.assign(res.url); return; }
      return res.text().then(function (html) { show(html, path, push); });
    }).catch(function () { location.assign(url); });
  }

  // ---- Wiring (delegated, so it keeps working after every swap) ----------------------------
  // Tap an option: single choice and scale move on by themselves after the beat.
  document.addEventListener('change', function (e) {
    var input = e.target;
    var form = input && input.form;
    if (!form || form !== quizForm()) return;

    // Multi-select: "None of these" clears the others, and any other clears it.
    var exclusive = form.dataset.exclusive;
    if (form.dataset.kind === 'multi' && exclusive && input.type === 'checkbox' && input.checked) {
      form.querySelectorAll('input[type="checkbox"]').forEach(function (other) {
        if (other !== input && (input.value === exclusive || other.value === exclusive)) other.checked = false;
      });
    }

    if (input.dataset.autosubmit !== 'true' || document.getElementById('note')) return;
    if (canSwap) { submitAnswer(form, BEAT); return; }
    if (busy) return;
    busy = true;
    setTimeout(function () { form.requestSubmit ? form.requestSubmit() : form.submit(); }, BEAT);
  });

  // The Continue / Got it buttons. Capture phase, so it runs before base.html's page-fade hook.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!canSwap || form !== quizForm()) return;
    e.preventDefault();
    submitAnswer(form, 0);
  }, true);

  // The in-quiz Back arrow, when it points at another quiz screen.
  document.addEventListener('click', function (e) {
    if (!canSwap || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest('.quiz-top a[href]');
    if (!a || a.origin !== location.origin || !isQuizPath(a.pathname)) return;
    e.preventDefault();
    load(a.pathname, true);
  }, true);

  // Browser Back / Forward between swapped screens.
  if (canSwap) {
    history.replaceState({ kd: 1 }, '', location.href);
    window.addEventListener('popstate', function () {
      if (isQuizPath(location.pathname)) load(location.pathname, false);
      else location.reload();
    });
  }

  // Coming back to a page restored from the back/forward cache: let it take answers again.
  window.addEventListener('pageshow', function (e) { if (e.persisted) busy = false; });
})();
