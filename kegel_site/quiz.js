/* Floorwise quiz engine: questions -> analysing screen -> personal plan.
   No network calls; answers live in this tab (localStorage, best effort). */
(function () {
  'use strict';

  var PRICE = { trial: '$2.99', trialDays: 7, monthly: '$24.99' };
  var STORE_KEY = 'floorwise.quiz.v1';

  // ---------- icons (decorative) ----------
  var ICON = {
    men: '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><circle cx="24" cy="16" r="8" fill="#0b6f78"/><path d="M10 42c0-8 6-14 14-14s14 6 14 14" fill="#9fd3cb"/></svg>',
    women: '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><circle cx="24" cy="16" r="8" fill="#157a5a"/><path d="M16 13c0-6 4-9 8-9s8 3 8 9v6h-3v-6M19 19v-6" stroke="#157a5a" stroke-width="2"/><path d="M8 42l6-12c2-2 6-3 10-3s8 1 10 3l6 12z" fill="#f3c2b5"/></svg>'
  };

  var FREQ = [['never', 'Never'], ['sometimes', 'Sometimes'], ['often', 'Often'], ['most', 'Most of the time'], ['skip', "I'd rather not say"]];

  // ---------- question bank ----------
  // kind: single | multi | scale | fact
  var Q = {
    track: { kind: 'single', section: 'About you', title: 'Who is this plan for?', help: 'Pelvic floor anatomy differs, so the exercises and cues do too.',
      options: [['men', 'Men', 'Bladder, control and stamina'], ['women', 'Women', 'Leaks, recovery and comfort']], icons: true },
    age: { kind: 'single', section: 'About you', title: 'How old are you?', help: 'Muscle tone changes with age. It changes your starting holds, not whether this works for you.',
      options: [['18', '18–29'], ['30', '30–39'], ['40', '40–49'], ['50', '50–59'], ['60', '60 or over']] },

    goal_men: { kind: 'single', section: 'Your goal', title: "What's your main goal?", help: 'Pick the one that matters most. The plan adds a daily drill for it.',
      options: [['bladder', 'Better bladder control'], ['ejac', 'More control over when I finish'], ['erection', 'Firmer, longer-lasting erections'], ['general', 'Overall pelvic strength']] },
    dribble: { kind: 'single', section: 'What you notice', title: 'Do you notice a few drops after you have finished urinating?', help: 'Very common, and one of the easiest things for a pelvic floor routine to target.', options: FREQ },
    early: { kind: 'single', section: 'What you notice', title: 'How often do you finish sooner than you would like?', help: 'Answers stay on this device.', options: FREQ },
    erect: { kind: 'single', section: 'What you notice', title: 'How often is getting or keeping an erection harder than you would like?', help: 'Answers stay on this device.', options: FREQ },
    prostate: { kind: 'single', section: 'What you notice', title: 'Have you had prostate surgery or treatment?', help: 'Pelvic floor training is often part of recovery, but timing matters.',
      options: [['no', 'No'], ['old', 'Yes, more than 3 months ago'], ['recent', 'Yes, in the last 3 months']] },

    goal_women: { kind: 'single', section: 'Your goal', title: "What's your main goal?", help: 'Pick the one that matters most. The plan adds a daily drill for it.',
      options: [['leaks', 'Fewer leaks when I cough, laugh or move'], ['birth', 'Recovery after pregnancy and birth'], ['intimacy', 'More sensation and comfort during sex'], ['menopause', 'Support through menopause'], ['general', 'Overall pelvic strength']] },
    stress_leak: { kind: 'single', section: 'What you notice', title: 'Do you leak when you cough, sneeze, laugh or jump?', help: 'Even a little counts. This is the most common reason women start kegels.', options: FREQ },
    urge: { kind: 'single', section: 'What you notice', title: 'Do you get sudden urges where you need to go right now?', help: 'Urgency gets its own drill, separate from leaks on effort.', options: FREQ },
    birth: { kind: 'single', section: 'What you notice', title: 'Have you given birth?', help: 'Recent births start gentler, with breathing and connection first.',
      options: [['no', 'No'], ['old', 'Yes, more than a year ago'], ['recent', 'Yes, in the last year'], ['pregnant', "I'm pregnant now"]] },
    meno: { kind: 'single', section: 'What you notice', title: 'Where are you with menopause?', help: 'Hormone changes affect pelvic tissue, so endurance work gets more time.',
      options: [['pre', 'Not there yet'], ['peri', 'Perimenopause'], ['post', 'Post-menopause'], ['unsure', 'Not sure']] },

    fact_floor: { kind: 'fact', section: 'Good to know', eyebrow: 'Good to know', art: 'floor',
      title: 'Your pelvic floor is a sling of muscle, not a single switch',
      body: 'It holds up your bladder and bowel, closes the exits when you cough or lift, and has to let go fully when you go to the toilet. Training it means building strength, endurance and the ability to relax.' },
    tried: { kind: 'single', section: 'Your experience', title: 'Have you done kegels before?', help: 'Most people have tried. Few were ever shown how.',
      options: [['never', 'Never'], ['unsure', "Yes, but I'm not sure I did them right"], ['sometimes', 'Now and then'], ['regular', 'Yes, regularly']] },
    feel: { kind: 'single', section: 'Your experience', title: 'Try it now: gently squeeze as if holding in wind, then let go. Could you feel it?',
      help: 'Keep your belly, thighs and buttocks relaxed. You should feel a lift inward, not a push down.',
      options: [['yes', 'Yes, clearly'], ['some', 'A little'], ['no', "Not really"]] },
    strength: { kind: 'scale', section: 'Your experience', title: 'How strong did that squeeze feel?', help: 'A guess is fine. Your plan re-tests it on day 7.', low: 'Barely there', high: 'Very strong' },
    fact_relax: { kind: 'fact', section: 'Good to know', eyebrow: 'Good to know', art: 'relax',
      title: 'Letting go is half of every rep',
      body: 'A muscle that never fully relaxes gets tired and tight, not strong. Every hold in your plan is followed by a release at least as long as the squeeze.' },
    safety: { kind: 'multi', section: 'Safety check', title: 'Do any of these apply to you right now?', help: 'Select all that apply. This changes how your plan starts.',
      options: [['pain', 'Pelvic, genital or rectal pain'], ['sexpain', 'Pain during or after sex'], ['blood', 'Blood in urine'], ['bulge', 'A feeling of heaviness or bulging'], ['surgery', 'Pelvic surgery in the last 3 months'], ['none', 'None of these']], exclusive: 'none' },
    minutes: { kind: 'single', section: 'Your routine', title: 'How much time can you give it each day?', help: 'Short and daily beats long and occasional.',
      options: [['3', '3 minutes', 'One round'], ['5', '5 minutes', 'Recommended'], ['10', '10 minutes', 'Two rounds plus a drill']] },
    when: { kind: 'single', section: 'Your routine', title: 'When would a session fit best?', help: 'We anchor it to something you already do, so you remember.',
      options: [['morning', 'Right after I wake up'], ['commute', 'On my commute'], ['desk', 'At my desk'], ['evening', 'In bed at night']] },
    activity: { kind: 'scale', section: 'Your routine', title: 'How active are you day to day?', help: 'Running and lifting load the pelvic floor, so active plans add standing work sooner.', low: 'Mostly sitting', high: 'Very active' }
  };

  function flowFor(track) {
    var head = ['track', 'age'];
    var men = ['goal_men', 'dribble', 'early', 'erect', 'prostate'];
    var women = ['goal_women', 'stress_leak', 'urge', 'birth', 'meno'];
    var tail = ['fact_floor', 'tried', 'feel', 'strength', 'fact_relax', 'safety', 'minutes', 'when', 'activity'];
    return head.concat(track === 'women' ? women : men, tail);
  }

  // ---------- state ----------
  var state = load() || { answers: {}, i: 0 };
  var urlTrack = new URLSearchParams(location.search).get('track');
  if (urlTrack === 'men' || urlTrack === 'women') {
    if (state.answers.track !== urlTrack) state = { answers: { track: urlTrack }, i: 1 };
  }
  if (state.done) state.i = Math.min(state.i, flow().length);

  function flow() { return flowFor(state.answers.track || 'men'); }
  function save() { try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) {} }
  function load() { try { return JSON.parse(localStorage.getItem(STORE_KEY)); } catch (e) { return null; } }

  var app = document.getElementById('app');
  var bar = document.getElementById('bar');
  var count = document.getElementById('count');
  var backBtn = document.getElementById('back');
  var sectionLabel = document.getElementById('section-label');
  var barRow = document.getElementById('bar-row');

  backBtn.addEventListener('click', function () {
    if (state.i > 0) { state.i--; state.done = false; save(); render(); }
    else { location.href = 'index.html'; }
  });

  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  function setChrome(key) {
    var f = flow();
    var total = f.length;
    var pct = Math.round((state.i / total) * 100);
    bar.style.width = pct + '%';
    bar.parentNode.setAttribute('aria-valuenow', pct);
    count.textContent = Math.min(state.i + 1, total) + '/' + total;
    sectionLabel.textContent = key && Q[key] ? Q[key].section : '';
    barRow.style.display = '';
  }

  function next() { state.i++; save(); render(); window.scrollTo(0, 0); }

  // ---------- render ----------
  function render() {
    var f = flow();
    if (state.i >= f.length) { return state.done ? renderResult() : renderLoading(); }
    var key = f[state.i];
    var q = Q[key];
    setChrome(key);
    var html = '<div class="screen-in">';
    if (q.kind === 'fact') {
      html += '<div class="fact"><div class="art">' + art(q.art) + '</div><span class="eyebrow">' + esc(q.eyebrow) + '</span><h2>' + esc(q.title) + '</h2><p>' + esc(q.body) + '</p></div>' +
        '<div class="quiz-cta"><button class="btn-cta" type="button" id="go">Got it <span aria-hidden="true">&rarr;</span></button></div>';
      app.innerHTML = html + '</div>';
      document.getElementById('go').onclick = next;
      return;
    }
    html += '<h1 class="q-title">' + esc(q.title) + '</h1><p class="q-help">' + esc(q.help) + '</p>';
    var cur = state.answers[key];

    if (q.kind === 'scale') {
      html += '<div class="scale" role="radiogroup" aria-label="' + esc(q.title) + '">';
      for (var n = 1; n <= 5; n++) html += '<button type="button" class="option" role="radio" aria-checked="' + (cur === n) + '" data-v="' + n + '">' + n + '</button>';
      html += '</div><div class="scale-ends"><span>' + esc(q.low) + '</span><span>' + esc(q.high) + '</span></div>';
      app.innerHTML = html + '</div>';
      app.querySelectorAll('.option').forEach(function (b) {
        b.onclick = function () { pick(key, Number(b.dataset.v), b); };
      });
      return;
    }

    if (q.kind === 'single') {
      html += '<div class="options" role="radiogroup" aria-label="' + esc(q.title) + '">';
      q.options.forEach(function (o) {
        html += '<button type="button" class="option' + (q.icons ? ' tall' : '') + '" role="radio" aria-checked="' + (cur === o[0]) + '" data-v="' + esc(o[0]) + '">' +
          (q.icons ? '<span class="ico">' + ICON[o[0]] + '</span>' : '') +
          '<span>' + esc(o[1]) + (o[2] ? '<span class="sub">' + esc(o[2]) + '</span>' : '') + '</span></button>';
      });
      app.innerHTML = html + '</div></div>';
      app.querySelectorAll('.option').forEach(function (b) {
        b.onclick = function () { pick(key, b.dataset.v, b); };
      });
      return;
    }

    // multi
    var sel = Array.isArray(cur) ? cur.slice() : [];
    html += '<div class="options" role="group" aria-label="' + esc(q.title) + '">';
    q.options.forEach(function (o) {
      html += '<button type="button" class="option" role="checkbox" aria-checked="' + (sel.indexOf(o[0]) > -1) + '" data-v="' + esc(o[0]) + '"><span>' + esc(o[1]) + '</span><span class="box" aria-hidden="true"></span></button>';
    });
    html += '</div><div class="quiz-cta"><button class="btn-cta" type="button" id="go"' + (sel.length ? '' : ' disabled') + '>Continue <span aria-hidden="true">&rarr;</span></button></div>';
    app.innerHTML = html + '</div>';
    var go = document.getElementById('go');
    app.querySelectorAll('.option').forEach(function (b) {
      b.onclick = function () {
        var v = b.dataset.v, at = sel.indexOf(v);
        if (at > -1) sel.splice(at, 1);
        else if (v === q.exclusive) sel = [v];
        else { sel = sel.filter(function (x) { return x !== q.exclusive; }); sel.push(v); }
        app.querySelectorAll('.option').forEach(function (o) { o.setAttribute('aria-checked', sel.indexOf(o.dataset.v) > -1); });
        go.disabled = !sel.length;
      };
    });
    go.onclick = function () { state.answers[key] = sel; next(); };
  }

  // Single-choice: show the selection for a beat, then advance (reference pattern).
  var advancing = false;
  function pick(key, v, btn) {
    if (advancing) return;
    advancing = true;
    app.querySelectorAll('.option').forEach(function (o) { o.setAttribute('aria-checked', o === btn); });
    if (key === 'track' && state.answers.track !== v) state.answers = {};
    state.answers[key] = v;
    setTimeout(function () { advancing = false; next(); }, 260);
  }

  // ---------- analysing interstitial ----------
  function renderLoading() {
    bar.style.width = '100%';
    count.textContent = '';
    sectionLabel.textContent = 'Building your plan';
    var steps = ['Reading your answers', 'Setting your starting hold', 'Choosing your daily drill', 'Writing week 1'];
    app.innerHTML = '<div class="loading screen-in"><div class="pct" id="pct">0%</div><p class="muted">Putting your plan together</p><ul>' +
      steps.map(function (s) { return '<li><i></i>' + s + '</li>'; }).join('') + '</ul></div>';
    var pctEl = document.getElementById('pct');
    var lis = app.querySelectorAll('li');
    var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    // A timer, not requestAnimationFrame: rAF pauses in a hidden tab, which would park the
    // visitor on 0% forever if they switched away mid-load.
    var dur = reduce ? 300 : 3200, t0 = Date.now();
    var timer = setInterval(function () {
      var p = Math.min(1, (Date.now() - t0) / dur);
      var eased = 1 - Math.pow(1 - p, 2);
      pctEl.textContent = Math.round(eased * 100) + '%';
      lis.forEach(function (li, k) { if (eased >= (k + 1) / lis.length - 0.001) li.classList.add('done'); });
      if (p >= 1) {
        clearInterval(timer);
        setTimeout(function () { state.done = true; save(); render(); window.scrollTo(0, 0); }, 350);
      }
    }, 40);
  }

  // ---------- plan builder ----------
  function buildPlan(a) {
    var flags = Array.isArray(a.safety) ? a.safety.filter(function (x) { return x !== 'none'; }) : [];
    var pain = flags.indexOf('pain') > -1 || flags.indexOf('sexpain') > -1;
    var gentle = pain || a.birth === 'recent' || a.prostate === 'recent' || flags.indexOf('surgery') > -1;

    var score = { never: 0, unsure: 1, sometimes: 1, regular: 2 }[a.tried] || 0;
    score += { no: 0, some: 1, yes: 2 }[a.feel] || 0;
    score += (Number(a.strength) || 2) >= 4 ? 2 : (Number(a.strength) || 2) >= 3 ? 1 : 0;
    var level = gentle ? 0 : score >= 5 ? 2 : score >= 3 ? 1 : 0;
    var L = [
      { name: 'Foundation', hold: 3, rest: 6, reps: 8, quick: 5 },
      { name: 'Builder', hold: 5, rest: 5, reps: 10, quick: 10 },
      { name: 'Strong', hold: 8, rest: 8, reps: 10, quick: 15 }
    ][level];
    if (pain) { L = { name: 'Relax first', hold: 2, rest: 10, reps: 6, quick: 0 }; }

    var minutes = Number(a.minutes) || 5;
    var rounds = minutes >= 10 ? 2 : 1;

    var women = a.track === 'women';
    var goal = women ? a.goal_women : a.goal_men;
    var DRILL = {
      bladder: ['After-drip squeeze', 'After urinating, wait a moment, then give one strong squeeze and lift to clear the last drops.'],
      ejac: ['Control hold', 'One long hold at about half effort for as long as you can keep breathing easily, then a slow 5-second release.'],
      erection: ['Endurance set', 'Three holds of twice your normal length at about 70% effort, with a full relax between each.'],
      leaks: ['The knack', 'Squeeze and lift just before you cough, sneeze or pick something up. Practise it on three fake coughs.'],
      birth: ['Breath connection', 'Breathe in and let the floor drop; breathe out and gently lift. Ten slow breaths, no forcing.'],
      intimacy: ['Awareness squeeze', 'Slow squeeze, notice where you feel it, then a deliberate slow release. Five reps, eyes closed.'],
      menopause: ['Endurance set', 'Three holds of twice your normal length at about 70% effort, with a full relax between each.'],
      general: ['Elevator lift', 'Lift in three steps (a third, two thirds, full), then lower in three steps. Three reps.']
    };
    var drill = DRILL[goal] || DRILL.general;
    if (women && (a.urge === 'often' || a.urge === 'most')) drill = ['Urge pause', 'When an urge hits: stand still, five quick squeezes, slow breath, then walk calmly to the toilet.'];
    if (pain) drill = ['Down-training breath', 'Lying down, knees bent: breathe in deeply and let the floor soften and drop. Eight breaths, before anything else.'];

    var setTxt = rounds + ' round' + (rounds > 1 ? 's' : '') + ': ' + L.reps + ' slow holds (' + L.hold + 's squeeze, ' + L.rest + 's full relax)' + (L.quick ? ' + ' + L.quick + ' quick squeezes' : '');
    var WHEN = { morning: 'right after you wake up', commute: 'on your commute', desk: 'at your desk', evening: 'in bed before sleep' }[a.when] || 'at a time you pick';
    var pos = (Number(a.activity) || 3) >= 4 ? 'standing' : 'sitting';

    var days = [
      ['Mon', 'Find the muscle', 4, ['Lie on your back, knees bent.', 'Squeeze as if holding in wind, lift, let go. 5 gentle tries.', 'Notice: belly, thighs and buttocks stay soft.']],
      ['Tue', 'Foundation set, lying down', minutes, ['Lie down, knees bent.', setTxt, 'Relax completely between every rep.']],
      ['Wed', 'Foundation set + daily drill', minutes, ['Lying down.', setTxt, drill[0] + ': ' + drill[1]]],
      ['Thu', 'Relax and reset', 3, ['Lying down, slow breathing.', 'Breathe in, let the floor drop; breathe out, soften further. 10 breaths.', 'Light day: no holds.']],
      ['Fri', 'Sitting set + daily drill', minutes, ['Sit upright, feet flat.', setTxt, drill[0] + ': ' + drill[1]]],
      ['Sat', (pos === 'standing' ? 'Standing' : 'Sitting') + ' set + daily drill', minutes, [(pos === 'standing' ? 'Stand tall, knees soft.' : 'Sit upright, feet flat.'), setTxt, drill[0] + ': ' + drill[1]]],
      ['Sun', 'Check-in', 4, ['Lie down. Time your longest comfortable hold.', 'Rate the squeeze 1–5 again.', 'This sets where week 2 starts.']]
    ];
    if (pain) days = days.map(function (d) { return [d[0], d[0] === 'Thu' || d[0] === 'Sun' ? d[1] : 'Relax-first session', Math.min(d[2], 5), ['Lying down, knees bent.', drill[0] + ': ' + drill[1], 'Only if comfortable: ' + L.reps + ' very light 2s squeezes, 10s release.']]; });

    var focusName = { bladder: 'Bladder control', ejac: 'Ejaculation control', erection: 'Erection support', leaks: 'Fewer leaks', birth: 'Post-birth recovery', intimacy: 'Sensation & comfort', menopause: 'Menopause support', general: 'Overall strength' }[goal] || 'Overall strength';

    var safetyNote = null;
    if (flags.length || a.birth === 'pregnant' || a.birth === 'recent' || a.prostate === 'recent') {
      safetyNote = 'Based on your answers, please check with a doctor or pelvic health physiotherapist before you start, and stop any exercise that causes pain.' +
        (flags.indexOf('blood') > -1 ? ' Blood in urine should always be checked by a doctor.' : '') +
        (a.birth === 'pregnant' ? ' Pelvic floor exercise is commonly recommended in pregnancy, but your midwife or doctor should confirm what suits you.' : '');
    }

    return { women: women, level: L, focus: focusName, minutes: pain ? Math.min(minutes, 5) : minutes, when: WHEN, drill: drill, days: days, safety: safetyNote, gentle: gentle || pain };
  }

  // ---------- result ----------
  function renderResult() {
    barRow.style.display = 'none';
    sectionLabel.textContent = 'Your plan';
    var a = state.answers;
    var p = buildPlan(a);
    var who = p.women ? 'women' : 'men';
    var h = '<div class="screen-in">' +
      '<section class="result-head"><div class="glow a" style="left:-140px;top:-120px;width:320px;height:320px" aria-hidden="true"></div><div class="glow b" style="right:-140px;top:40px;width:300px;height:300px" aria-hidden="true"></div>' +
      '<span class="eyebrow" style="position:relative">Your plan is ready</span>' +
      '<h1>A ' + p.minutes + '-minute daily kegel plan for <span class="grad-text">' + esc(p.focus.toLowerCase()) + '</span></h1>' +
      '<p class="muted" style="margin-top:12px">Built for ' + who + ' from your ' + Object.keys(a).length + ' answers. Do it ' + esc(p.when) + '.</p>' +
      '<dl class="kv"><div><dt>Focus</dt><dd>' + esc(p.focus) + '</dd></div><div><dt>Starting level</dt><dd>' + esc(p.level.name) + '</dd></div>' +
      '<div><dt>Starting hold</dt><dd>' + p.level.hold + ' seconds</dd></div><div><dt>Daily drill</dt><dd>' + esc(p.drill[0]) + '</dd></div></dl></section>';

    if (p.safety) h += '<div class="safety"><b>Before you start</b>' + esc(p.safety) + '</div>';

    h += '<h2 class="sec-title">Week 1, free to read</h2><p class="muted" style="margin-top:4px;font-size:14px">Tap a day to see every rep.</p>';
    p.days.forEach(function (d, k) {
      h += '<details class="day"' + (k < 2 ? ' open' : '') + '><summary><span class="d">' + d[0] + '</span><span class="t">' + esc(d[1]) + '<span class="m" style="display:block">' + d[2] + ' min</span></span><span class="caret" aria-hidden="true">&#9662;</span></summary><ol>' +
        d[3].map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ol></details>';
    });

    h += '<h2 class="sec-title">Weeks 2 to 4</h2>' +
      '<div class="locked"><b>Week 2 · Longer holds.</b> Starts from your day-7 hold time and adds about a second a week.</div>' +
      '<div class="locked"><b>Week 3 · Real-life positions.</b> Standing and moving sets, with your drill built into daily moments.</div>' +
      '<div class="locked"><b>Week 4 · Make it automatic.</b> Fewer reminders, a maintenance routine, and a re-test.</div>';

    h += '<section class="price-card" aria-labelledby="price-h"><h2 id="price-h">Unlock weeks 2 to 4 and the guided timer</h2>' +
      '<p class="price-line">' + PRICE.trial + ' today for ' + PRICE.trialDays + ' days, then ' + PRICE.monthly + '/month until you cancel</p>' +
      '<ul><li>Your trial turns into a monthly subscription at ' + PRICE.monthly + ' unless you cancel before day ' + PRICE.trialDays + '.</li>' +
      '<li>Cancel in one click from your account. No calls, no emails.</li>' +
      '<li>Week 1 above is yours either way.</li></ul>' +
      '<button class="btn-cta" type="button" id="buy">Start my 7-day trial</button>' +
      '<small>Rehearsal build: no card is collected and nothing is charged.</small></section>';

    h += '<p class="note-small">General exercise guidance, not medical advice. Results vary with your starting point and how consistently you practise.</p>' +
      '<p class="note-small"><button type="button" class="icon-btn" id="restart" style="width:auto;padding:0 14px;border-radius:99px;font-weight:600;font-size:13px">Start the quiz again</button></p></div>';
    app.innerHTML = h;
    document.getElementById('buy').onclick = function () { toast('Rehearsal mode: no payment is taken on this site yet.'); };
    document.getElementById('restart').onclick = function () { state = { answers: {}, i: 0 }; save(); history.replaceState(null, '', 'quiz.html'); render(); window.scrollTo(0, 0); };
  }

  function toast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.remove('show'); }, 3200);
  }

  function art(kind) {
    if (kind === 'relax') {
      return '<svg viewBox="0 0 180 180" aria-hidden="true"><circle cx="90" cy="90" r="80" fill="#f2f8f7"/><path d="M30 100 Q60 60 90 100 T150 100" fill="none" stroke="#0b6f78" stroke-width="8" stroke-linecap="round"/><path d="M30 125 Q60 110 90 125 T150 125" fill="none" stroke="#9fd3cb" stroke-width="8" stroke-linecap="round"/></svg>';
    }
    return '<svg viewBox="0 0 180 180" aria-hidden="true"><circle cx="90" cy="90" r="80" fill="#f2f8f7"/><path d="M38 70 Q90 150 142 70" fill="none" stroke="#0b6f78" stroke-width="12" stroke-linecap="round"/><path d="M52 66 Q90 128 128 66" fill="none" stroke="#9fd3cb" stroke-width="6" stroke-linecap="round"/><circle cx="90" cy="62" r="16" fill="#e8735a" opacity=".85"/></svg>';
  }

  render();
})();
