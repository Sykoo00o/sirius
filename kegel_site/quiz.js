/* kegeldoc quiz engine: questions -> analysing screen -> personal plan.
   No network calls; answers live in this tab (localStorage, best effort).

   Screen count is matched to the reference quiz (~30 screens before its consent wall):
   39 for men, 35-36 for women, depending on branches. Facts cite real, published sources;
   there are no invented percentages and no reviews. */
(function () {
  'use strict';

  var PRICE = { trial: '$2.99', trialDays: 7, monthly: '$24.99' };
  var STORE_KEY = 'kegeldoc.quiz.v2';

  // ---------- icons (decorative) ----------
  var ICON = {
    men: '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><circle cx="24" cy="16" r="8" fill="#0b6f78"/><path d="M10 42c0-8 6-14 14-14s14 6 14 14" fill="#9fd3cb"/></svg>',
    women: '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><circle cx="24" cy="16" r="8" fill="#157a5a"/><path d="M16 13c0-6 4-9 8-9s8 3 8 9v6h-3v-6M19 19v-6" stroke="#157a5a" stroke-width="2"/><path d="M8 42l6-12c2-2 6-3 10-3s8 1 10 3l6 12z" fill="#f3c2b5"/></svg>'
  };

  var SKIP = ['skip', "I'd rather not say"];
  var FREQ = [['never', 'Never'], ['rarely', 'Rarely'], ['sometimes', 'Sometimes'], ['often', 'Often'], ['most', 'Most of the time'], SKIP];

  // ---------- question bank ----------
  // kind: single | multi | scale | fact | milestone. `show(a)` makes a screen conditional.
  var Q = {
    // --- about you ---
    track: { kind: 'single', section: 'About you', title: 'Who is this plan for?', help: 'Pelvic floor anatomy differs, so the exercises and cues do too.',
      options: [['men', 'Men', 'Erections, control, bladder'], ['women', 'Women', 'Leaks, recovery, sensation']], icons: true },
    age: { kind: 'single', section: 'About you', title: 'How old are you?', help: 'Muscle tone changes with age. It changes your starting holds, not whether this works for you.',
      options: [['18', '18–29'], ['30', '30–39'], ['40', '40–49'], ['50', '50–59'], ['60', '60 or over']] },

    // --- men: goal and erections ---
    goal_men: { kind: 'single', section: 'Your goal', title: "What's your main goal?", help: 'Pick the one that matters most. The plan adds a daily drill for it.',
      options: [['erection', 'Firmer, longer-lasting erections'], ['ejac', 'More control over when I finish'], ['both', 'Both of those'], ['bladder', 'Better bladder control'], ['general', 'Overall pelvic strength']] },
    erect_type: { kind: 'single', section: 'Erections', title: 'Which best describes what you notice?', help: 'Answers stay on this device.',
      options: [['getting', 'Getting an erection is hard'], ['keeping', 'Keeping it is hard'], ['both', 'Both'], ['none', "Neither, I'm training ahead"], SKIP] },
    erect_freq: { kind: 'single', section: 'Erections', title: 'How often does it happen?', help: 'Think about the last three months.', options: FREQ,
      show: function (a) { return a.erect_type !== 'none'; } },
    erect_since: { kind: 'single', section: 'Erections', title: 'How long has it been going on?', help: 'Gradual changes and sudden ones point to different things.',
      options: [['lt1', 'Less than a year'], ['1to5', '1 to 5 years'], ['gt5', 'More than 5 years'], SKIP],
      show: function (a) { return a.erect_type !== 'none'; } },
    morning: { kind: 'single', section: 'Erections', title: 'How often do you wake up with an erection?', help: 'Morning erections are a rough sign that blood flow and nerves are working.', options: FREQ.slice(0, 5).concat([SKIP]) },
    fact_men_erect: { kind: 'fact', section: 'Good to know', eyebrow: 'Study fact', art: 'floor',
      title: 'The muscles at the base of the penis help hold an erection',
      body: 'Two pelvic floor muscles wrap the root of the penis. When they contract they help keep blood in place. Training them has been tested as a first-line approach for erection problems.',
      src: 'Dorey et al., British Journal of General Practice, 2004: randomised trial of pelvic floor exercise for erectile dysfunction.' },
    firm_sex: { kind: 'scale', section: 'Erections', title: 'How firm are your erections during sex?', help: 'Rate from 1 to 5.', low: 'Not firm', high: 'Very firm' },
    sat: { kind: 'scale', section: 'Your sex life', title: 'How satisfied are you with your sex life in the last 3 months?', help: 'Rate from 1 to 5.', low: 'Not satisfied', high: 'Very satisfied' },
    worry: { kind: 'scale', section: 'Your sex life', title: 'How much does this worry you?', help: 'Worry itself can make erections harder, so it shapes the relaxation work.', low: "I don't worry", high: 'A lot' },
    relation: { kind: 'scale', section: 'Your sex life', title: 'How much does it affect your relationship?', help: 'Skip the thought if you are single: answer 1.', low: 'Not at all', high: 'A lot' },
    confidence_m: { kind: 'single', section: 'Your sex life', title: 'Does it affect how confident you feel?', help: 'A common, honest answer is yes.',
      options: [['yes', 'Yes'], ['bit', 'A little'], ['no', 'No'], SKIP] },

    // --- men: ejaculation ---
    sex_length: { kind: 'single', section: 'Control', title: 'How long does sex usually last for you?', help: 'From penetration to finishing, roughly.',
      options: [['lt2', 'Less than 2 minutes'], ['2to7', '2 to 7 minutes'], ['7to15', '7 to 15 minutes'], ['gt15', 'More than 15 minutes'], SKIP] },
    early: { kind: 'single', section: 'Control', title: 'How often do you finish sooner than you would like?', help: 'Answers stay on this device.', options: FREQ },
    control_want: { kind: 'single', section: 'Control', title: 'Would you like more control over when you finish?', help: 'If yes, the plan adds a slow-release drill.',
      options: [['yes', "Yes, I'd like that"], ['maybe', 'Maybe'], ['no', "It's not a concern"]] },
    fact_men_ejac: { kind: 'fact', section: 'Good to know', eyebrow: 'Study fact', art: 'relax',
      title: 'The same muscles are involved in ejaculation',
      body: 'Learning to feel, hold and deliberately relax the pelvic floor has been studied as a way to build control over timing, with sessions much like the ones in your plan.',
      src: 'Pastore et al., Therapeutic Advances in Urology, 2014: pelvic floor rehabilitation for lifelong premature ejaculation.' },

    // --- men: bladder, history ---
    dribble: { kind: 'single', section: 'Bladder', title: 'Do you notice a few drops after you have finished urinating?', help: 'Very common, and one of the easiest things for a pelvic floor routine to target.', options: FREQ },
    urge_m: { kind: 'single', section: 'Bladder', title: 'Do you get sudden urges where you need to go right now?', help: 'Urgency gets its own drill.', options: FREQ },
    prostate: { kind: 'single', section: 'Health', title: 'Have you had prostate surgery or treatment?', help: 'Pelvic floor training is often part of recovery, but timing matters.',
      options: [['no', 'No'], ['old', 'Yes, more than 3 months ago'], ['recent', 'Yes, in the last 3 months']] },
    libido_m: { kind: 'scale', section: 'Your sex life', title: 'How would you rate your sex drive in the last 3 months?', help: 'Rate from 1 to 5.', low: 'Low', high: 'High' },
    meds: { kind: 'single', section: 'Health', title: 'Do you take medication to help with erections?', help: 'This changes nothing about your exercises. It is here so the plan talks to you honestly.',
      options: [['always', 'Yes, every time'], ['sometimes', 'Sometimes'], ['no', 'No'], SKIP] },
    fact_men_meds: { kind: 'fact', section: 'Good to know', eyebrow: 'Good to know', art: 'floor',
      title: 'Exercise works alongside treatment, not instead of it',
      body: 'If you take medication, keep taking it as prescribed. Pelvic floor training is something you add. Any change to medication is a conversation with your doctor, not with a quiz.',
      show: function (a) { return a.meds === 'always' || a.meds === 'sometimes'; } },

    // --- women: goal and bladder ---
    goal_women: { kind: 'single', section: 'Your goal', title: "What's your main goal?", help: 'Pick the one that matters most. The plan adds a daily drill for it.',
      options: [['leaks', 'Fewer leaks when I cough, laugh or move'], ['intimacy', 'More sensation and comfort during sex'], ['birth', 'Recovery after pregnancy and birth'], ['menopause', 'Support through menopause'], ['general', 'Overall pelvic strength']] },
    leak_effort: { kind: 'single', section: 'Bladder', title: 'Do you leak when you cough, sneeze or laugh?', help: 'Even a little counts. This is the most common reason women start kegels.', options: FREQ },
    leak_exercise: { kind: 'single', section: 'Bladder', title: 'Do you leak when you run, jump or lift something?', help: 'Impact and heavy lifting load the pelvic floor most.', options: FREQ.concat([['na', "I don't do these"]]) },
    urge: { kind: 'single', section: 'Bladder', title: 'Do you get sudden urges where you need to go right now?', help: 'Urgency gets its own drill, separate from leaks on effort.', options: FREQ },
    night: { kind: 'single', section: 'Bladder', title: 'How many times do you usually get up at night to urinate?', help: 'Once is common. More often is worth knowing about.',
      options: [['0', 'Not at all'], ['1', 'Once'], ['2', 'Twice'], ['3', '3 times or more']] },
    leak_amount: { kind: 'single', section: 'Bladder', title: 'When you do leak, how much is it?', help: 'Sets how much effort-timing practice goes into week 1.',
      options: [['none', "I don't leak"], ['drops', 'A few drops'], ['small', 'A small amount'], ['more', 'Enough to change clothes'], SKIP] },
    pads: { kind: 'single', section: 'Bladder', title: 'Do you wear pads or liners because of leaks?', help: 'A practical measure of how much this shapes your day.',
      options: [['never', 'Never'], ['sometimes', 'Sometimes, just in case'], ['daily', 'Every day'], SKIP] },
    fact_women_leaks: { kind: 'fact', section: 'Good to know', eyebrow: 'Good to know', art: 'relax',
      title: 'Two kinds of leaks, two different drills',
      body: 'Leaks on a cough or a jump happen when the floor is not quick or strong enough under sudden pressure. Leaks with a sudden urge are about a bladder that squeezes too early. Your plan trains each one differently.' },

    // --- women: life stage ---
    births: { kind: 'single', section: 'Life stage', title: 'Have you given birth?', help: 'Pregnancy and birth stretch the pelvic floor. Recent births start gentler.',
      options: [['none', 'No'], ['one', 'Yes, once'], ['two', 'Yes, twice'], ['three', 'Three times or more'], ['pregnant', "I'm pregnant now"]] },
    birth_when: { kind: 'single', section: 'Life stage', title: 'When was your most recent birth?', help: 'Most guidance suggests waiting for your postnatal check before harder exercise.',
      options: [['under6w', 'Less than 6 weeks ago'], ['under1y', '6 weeks to a year ago'], ['over1y', 'More than a year ago']],
      show: function (a) { return ['one', 'two', 'three'].indexOf(a.births) > -1; } },
    meno: { kind: 'single', section: 'Life stage', title: 'Where are you with menopause?', help: 'Hormone changes affect pelvic tissue, so endurance work gets more time.',
      options: [['pre', 'Not there yet'], ['peri', 'Perimenopause'], ['post', 'Post-menopause'], ['unsure', 'Not sure']] },
    heaviness: { kind: 'single', section: 'Life stage', title: 'Do you ever feel heaviness, dragging or a bulge down below?', help: 'This can be a sign of prolapse, which a physiotherapist should check.', options: FREQ },

    // --- women: sex ---
    sensation: { kind: 'scale', section: 'Your sex life', title: 'How much sensation do you feel during sex?', help: 'Rate from 1 to 5.', low: 'Very little', high: 'A lot' },
    sat_w: { kind: 'scale', section: 'Your sex life', title: 'How satisfied are you with your sex life in the last 3 months?', help: 'Rate from 1 to 5.', low: 'Not satisfied', high: 'Very satisfied' },
    sexpain_w: { kind: 'single', section: 'Your sex life', title: 'Do you feel pain during or after sex?', help: 'Pain changes the plan: relaxation comes before strength.', options: FREQ },
    confidence_w: { kind: 'scale', section: 'Your sex life', title: 'How much do these symptoms affect your confidence?', help: 'Rate from 1 to 5.', low: 'Not at all', high: 'A lot' },
    libido_w: { kind: 'scale', section: 'Your sex life', title: 'How would you rate your sex drive in the last 3 months?', help: 'Rate from 1 to 5.', low: 'Low', high: 'High' },
    fact_women_first: { kind: 'fact', section: 'Good to know', eyebrow: 'Study fact', art: 'floor',
      title: 'Pelvic floor training is a recommended first step for leaks',
      body: 'UK clinical guidance recommends at least three months of pelvic floor muscle training as the first treatment for leaks on effort, and a large review found women who trained were more likely to report being cured or improved than women who did not.',
      src: 'NICE guideline NG123 (2019); Dumoulin et al., Cochrane review, 2018.' },

    // --- both tracks ---
    milestone: { kind: 'milestone', section: 'Your plan so far' },
    tried: { kind: 'single', section: 'Your experience', title: 'Have you done kegels before?', help: 'Most people have tried. Few were ever shown how.',
      options: [['never', 'Never'], ['unsure', "Yes, but I'm not sure I did them right"], ['sometimes', 'Now and then'], ['regular', 'Yes, regularly']] },
    fact_find: { kind: 'fact', section: 'Good to know', eyebrow: 'How to', art: 'floor',
      title: 'Finding the right muscles',
      body: 'Imagine stopping yourself passing wind and, at the same time, stopping the flow of urine. That squeeze-and-lift is a kegel. Use the image only: stopping urine mid-flow as a regular exercise is not recommended.' },
    feel: { kind: 'single', section: 'Your experience', title: 'Try it now: gently squeeze and lift, then let go. Could you feel it?',
      help: 'Keep your belly, thighs and buttocks relaxed. You should feel a lift inward, not a push down.',
      options: [['yes', 'Yes, clearly'], ['some', 'A little'], ['no', 'Not really']] },
    strength: { kind: 'scale', section: 'Your experience', title: 'How strong did that squeeze feel?', help: 'A guess is fine. Your plan re-tests it on day 7.', low: 'Barely there', high: 'Very strong' },
    hold: { kind: 'single', section: 'Your experience', title: 'Squeeze again and hold it, counting slowly. When did it fade?', help: 'This sets your starting hold length.',
      options: [['2', 'Within 2 seconds'], ['5', 'At 3 to 5 seconds'], ['9', 'At 6 to 9 seconds'], ['10', 'I held 10 seconds or more'], ['0', "I couldn't feel it"]] },
    fact_relax: { kind: 'fact', section: 'Good to know', eyebrow: 'Good to know', art: 'relax',
      title: 'Letting go is half of every rep',
      body: 'A muscle that never fully relaxes gets tired and tight, not strong. Every hold in your plan is followed by a release at least as long as the squeeze.' },
    safety: { kind: 'multi', section: 'Safety check', title: 'Do any of these apply to you right now?', help: 'Select all that apply. This changes how your plan starts.',
      options: [['pain', 'Pelvic, genital or rectal pain'], ['blood', 'Blood in urine'], ['burning', 'Burning when urinating'], ['surgery', 'Pelvic surgery in the last 3 months'], ['none', 'None of these']], exclusive: 'none' },
    alcohol: { kind: 'single', section: 'Lifestyle', title: 'How often do you drink alcohol?', help: 'Alcohol affects both the bladder and erections.',
      options: [['daily', 'Every day'], ['weekly_plus', 'Several times a week'], ['weekly', 'About once a week'], ['rarely', 'Once a month or less'], ['never', "I don't drink"]] },
    smoke: { kind: 'single', section: 'Lifestyle', title: 'Do you smoke or vape?', help: 'Nicotine narrows blood vessels; a smoker’s cough loads the pelvic floor.',
      options: [['yes', 'Yes'], ['sometimes', 'Sometimes'], ['quit', 'I quit'], ['no', 'No']] },
    diet: { kind: 'scale', section: 'Lifestyle', title: 'How healthy is your diet?', help: 'Straining with constipation is one of the biggest everyday loads on the pelvic floor.', low: 'Not healthy', high: 'Very healthy' },
    activity: { kind: 'scale', section: 'Lifestyle', title: 'How active are you day to day?', help: 'Running and lifting load the pelvic floor, so active plans add standing work sooner.', low: 'Mostly sitting', high: 'Very active' },
    sitting: { kind: 'single', section: 'Lifestyle', title: 'How many hours a day do you spend sitting?', help: 'Long sitting is the easiest place to hide a set.',
      options: [['lt4', 'Less than 4 hours'], ['4to8', '4 to 8 hours'], ['gt8', 'More than 8 hours']] },
    stress: { kind: 'scale', section: 'Lifestyle', title: 'How would you rate your daily stress?', help: 'Stress often shows up as a tight, tired pelvic floor.', low: 'Low', high: 'High' },
    minutes: { kind: 'single', section: 'Your routine', title: 'How much time can you give it each day?', help: 'Short and daily beats long and occasional.',
      options: [['3', '3 minutes', 'One round'], ['5', '5 minutes', 'Recommended'], ['10', '10 minutes', 'Two rounds plus a drill']] },
    when: { kind: 'single', section: 'Your routine', title: 'When would a session fit best?', help: 'We anchor it to something you already do, so you remember.',
      options: [['morning', 'Right after I wake up'], ['commute', 'On my commute'], ['desk', 'At my desk'], ['evening', 'In bed at night']] }
  };

  var MEN = ['goal_men', 'erect_type', 'erect_freq', 'erect_since', 'morning', 'fact_men_erect', 'firm_sex', 'sat', 'worry', 'relation', 'confidence_m',
    'sex_length', 'early', 'control_want', 'fact_men_ejac', 'dribble', 'urge_m', 'prostate', 'libido_m', 'meds', 'fact_men_meds', 'milestone'];
  var WOMEN = ['goal_women', 'leak_effort', 'leak_exercise', 'urge', 'night', 'leak_amount', 'pads', 'fact_women_leaks', 'births', 'birth_when', 'meno', 'heaviness',
    'sensation', 'sat_w', 'sexpain_w', 'confidence_w', 'libido_w', 'fact_women_first', 'milestone'];
  var TAIL = ['tried', 'fact_find', 'feel', 'strength', 'hold', 'fact_relax', 'safety', 'alcohol', 'smoke', 'diet', 'activity', 'sitting', 'stress', 'minutes', 'when'];

  function flowFor(a) {
    var all = ['track', 'age'].concat(a.track === 'women' ? WOMEN : MEN, TAIL);
    return all.filter(function (k) { return !Q[k].show || Q[k].show(a); });
  }

  // ---------- state ----------
  var state = load() || { answers: {}, i: 0 };
  var urlTrack = new URLSearchParams(location.search).get('track');
  if (urlTrack === 'men' || urlTrack === 'women') {
    if (state.answers.track !== urlTrack) state = { answers: { track: urlTrack }, i: 1 };
  }

  function flow() { return flowFor(state.answers); }
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
    var total = flow().length;
    var pct = Math.round((state.i / total) * 100);
    bar.style.width = pct + '%';
    bar.parentNode.setAttribute('aria-valuenow', pct);
    count.textContent = Math.min(state.i + 1, total) + '/' + total;
    sectionLabel.textContent = key && Q[key] ? Q[key].section : '';
    barRow.style.display = '';
  }

  function next() { state.i++; save(); render(); window.scrollTo(0, 0); }

  function fmtDate(d) { return d.toLocaleDateString(undefined, { month: 'long', day: 'numeric' }); }
  function addDays(n) { var d = new Date(); d.setDate(d.getDate() + n); return d; }

  // ---------- render ----------
  function render() {
    var f = flow();
    if (state.i >= f.length) { return state.done ? renderResult() : renderLoading(); }
    var key = f[state.i];
    var q = Q[key];
    setChrome(key);
    var html = '<div class="screen-in">';
    var cta = '<div class="quiz-cta"><button class="btn-cta" type="button" id="go">Got it <span aria-hidden="true">&rarr;</span></button></div>';

    if (q.kind === 'fact') {
      html += '<div class="fact"><div class="art">' + art(q.art) + '</div><span class="eyebrow">' + esc(q.eyebrow) + '</span><h2>' + esc(q.title) + '</h2><p>' + esc(q.body) + '</p>' +
        (q.src ? '<p class="src"><b>Source:</b> ' + esc(q.src) + '</p>' : '') + '</div>' + cta;
      app.innerHTML = html + '</div>';
      document.getElementById('go').onclick = next;
      return;
    }

    if (q.kind === 'milestone') {
      var a = state.answers, women = a.track === 'women';
      var goal = GOAL_NAME[women ? a.goal_women : a.goal_men] || 'Overall strength';
      html += '<div class="fact milestone"><span class="eyebrow">Your plan so far</span>' +
        '<h2>A 4-week plan for <span class="grad-text">' + esc(goal.toLowerCase()) + '</span></h2>' +
        '<ol class="timeline">' +
        '<li><b>' + esc(fmtDate(addDays(0))) + '</b><span>Week 1 starts: find the muscle, foundation holds</span></li>' +
        '<li><b>' + esc(fmtDate(addDays(7))) + '</b><span>Week 2: longer holds, built from your day-7 check-in</span></li>' +
        '<li><b>' + esc(fmtDate(addDays(14))) + '</b><span>Week 3: standing and moving sets</span></li>' +
        '<li><b>' + esc(fmtDate(addDays(27))) + '</b><span>Re-test: compare your hold time to day 1</span></li></ol>' +
        '<dl class="kv"><div><dt>Each day</dt><dd>3–10 min</dd></div><div><dt>Equipment</dt><dd>None</dd></div>' +
        '<div><dt>Where</dt><dd>Anywhere</dd></div><div><dt>Who can tell</dt><dd>Nobody</dd></div></dl>' +
        '<p class="src">These are plan dates, not a promised result. Progress varies from person to person.</p></div>' +
        '<div class="quiz-cta"><button class="btn-cta" type="button" id="go">Continue <span aria-hidden="true">&rarr;</span></button></div>';
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
    var steps = ['Reading your answers', 'Setting your starting hold', 'Choosing your daily drill', 'Adding your lifestyle notes', 'Writing week 1'];
    app.innerHTML = '<div class="loading screen-in"><div class="pct" id="pct">0%</div><p class="muted">Putting your plan together</p><ul>' +
      steps.map(function (s) { return '<li><i></i>' + s + '</li>'; }).join('') + '</ul></div>';
    var pctEl = document.getElementById('pct');
    var lis = app.querySelectorAll('li');
    var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    // A timer, not requestAnimationFrame: rAF pauses in a hidden tab, which would park the
    // visitor on 0% forever if they switched away mid-load.
    var dur = reduce ? 300 : 3600, t0 = Date.now();
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
  var GOAL_NAME = { bladder: 'Bladder control', ejac: 'Ejaculation control', erection: 'Erection support', both: 'Erections and control',
    leaks: 'Fewer leaks', birth: 'Post-birth recovery', intimacy: 'Sensation & comfort', menopause: 'Menopause support', general: 'Overall strength' };
  var OFTEN = ['often', 'most'];

  function buildPlan(a) {
    var flags = Array.isArray(a.safety) ? a.safety.filter(function (x) { return x !== 'none'; }) : [];
    var women = a.track === 'women';
    var pain = flags.indexOf('pain') > -1 || OFTEN.indexOf(a.sexpain_w) > -1;
    var recentBirth = a.birth_when === 'under6w' || a.birth_when === 'under1y';
    var gentle = pain || recentBirth || a.prostate === 'recent' || flags.indexOf('surgery') > -1;

    var score = { never: 0, unsure: 1, sometimes: 1, regular: 2 }[a.tried] || 0;
    score += { no: 0, some: 1, yes: 2 }[a.feel] || 0;
    var st = Number(a.strength) || 2;
    score += st >= 4 ? 2 : st >= 3 ? 1 : 0;
    score += { '0': 0, '2': 0, '5': 1, '9': 2, '10': 3 }[a.hold] || 0;
    var level = gentle ? 0 : score >= 6 ? 2 : score >= 3 ? 1 : 0;
    var L = [
      { name: 'Foundation', hold: 3, rest: 6, reps: 8, quick: 5 },
      { name: 'Builder', hold: 5, rest: 5, reps: 10, quick: 10 },
      { name: 'Strong', hold: 8, rest: 8, reps: 10, quick: 15 }
    ][level];
    if (pain) { L = { name: 'Relax first', hold: 2, rest: 10, reps: 6, quick: 0 }; }

    var minutes = Number(a.minutes) || 5;
    var rounds = minutes >= 10 ? 2 : 1;

    var goal = women ? a.goal_women : a.goal_men;
    var DRILL = {
      bladder: ['After-drip squeeze', 'After urinating, wait a moment, then give one strong squeeze and lift to clear the last drops.'],
      ejac: ['Control hold', 'One long hold at about half effort for as long as you can keep breathing easily, then a slow 5-second release.'],
      erection: ['Endurance set', 'Three holds of twice your normal length at about 70% effort, with a full relax between each.'],
      both: ['Control + endurance', 'Two long holds at 70% effort, then one half-effort hold released over a slow count of five.'],
      leaks: ['The knack', 'Squeeze and lift just before you cough, sneeze or pick something up. Practise it on three fake coughs.'],
      birth: ['Breath connection', 'Breathe in and let the floor drop; breathe out and gently lift. Ten slow breaths, no forcing.'],
      intimacy: ['Awareness squeeze', 'Slow squeeze, notice where you feel it, then a deliberate slow release. Five reps, eyes closed.'],
      menopause: ['Endurance set', 'Three holds of twice your normal length at about 70% effort, with a full relax between each.'],
      general: ['Elevator lift', 'Lift in three steps (a third, two thirds, full), then lower in three steps. Three reps.']
    };
    var drill = DRILL[goal] || DRILL.general;
    if (OFTEN.indexOf(women ? a.urge : a.urge_m) > -1 && goal !== 'erection' && goal !== 'ejac' && goal !== 'both') {
      drill = ['Urge pause', 'When an urge hits: stand still, five quick squeezes, slow breath, then walk calmly to the toilet.'];
    }
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

    // Lifestyle notes: general, well-established, and phrased as observations, not cures.
    var tips = [];
    if (a.smoke === 'yes' || a.smoke === 'sometimes') tips.push('Smoking narrows blood vessels, including the ones erections and pelvic tissue rely on, and a smoker’s cough loads the pelvic floor. Cutting down helps more than any exercise can.');
    if (a.alcohol === 'daily' || a.alcohol === 'weekly_plus') tips.push('Alcohol irritates the bladder and can blunt erections. Notice how you feel on your lighter days.');
    if ((Number(a.diet) || 3) <= 2) tips.push('Straining on the toilet is a big everyday load on the pelvic floor. Fibre and water keep things moving.');
    if (a.sitting === 'gt8') tips.push('You sit a lot, so your sitting set doubles as a movement break. Stand up after it.');
    if ((Number(a.stress) || 3) >= 4) tips.push('High stress often shows up as a tight pelvic floor. Your Thursday relax day matters as much as the holds.');
    if (women && (a.night === '2' || a.night === '3')) tips.push('Getting up twice or more a night is worth mentioning to your doctor, especially if it is new.');
    if (!women && (a.morning === 'never' || a.morning === 'rarely') && a.erect_since === 'lt1') tips.push('Erection changes that are new can be an early sign of heart or blood-vessel problems. A routine check-up is worth booking.');

    var safetyNote = null;
    var needsCheck = flags.length || a.births === 'pregnant' || a.birth_when === 'under6w' || a.prostate === 'recent' || OFTEN.indexOf(a.heaviness) > -1 || pain;
    if (needsCheck) {
      safetyNote = 'Based on your answers, please check with a doctor or pelvic health physiotherapist before you start, and stop any exercise that causes pain.' +
        (flags.indexOf('blood') > -1 ? ' Blood in urine should always be checked by a doctor.' : '') +
        (flags.indexOf('burning') > -1 ? ' Burning when you urinate can be an infection, which needs treating first.' : '') +
        (OFTEN.indexOf(a.heaviness) > -1 ? ' A feeling of heaviness or bulging is worth an examination.' : '') +
        (a.births === 'pregnant' ? ' Pelvic floor exercise is commonly recommended in pregnancy, but your midwife or doctor should confirm what suits you.' : '');
    }

    return { women: women, level: L, focus: GOAL_NAME[goal] || 'Overall strength', minutes: pain ? Math.min(minutes, 5) : minutes,
      when: WHEN, drill: drill, days: days, safety: safetyNote, tips: tips };
  }

  // ---------- result ----------
  function renderResult() {
    barRow.style.display = 'none';
    sectionLabel.textContent = 'Your plan';
    var a = state.answers;
    var p = buildPlan(a);
    var who = p.women ? 'women' : 'men';
    var answered = flow().filter(function (k) { return Q[k].kind !== 'fact' && Q[k].kind !== 'milestone'; }).length;
    var h = '<div class="screen-in">' +
      '<section class="result-head"><div class="glow a" style="left:-140px;top:-120px;width:320px;height:320px" aria-hidden="true"></div><div class="glow b" style="right:-140px;top:40px;width:300px;height:300px" aria-hidden="true"></div>' +
      '<span class="eyebrow" style="position:relative">Your plan is ready</span>' +
      '<h1>A ' + p.minutes + '-minute daily kegel plan for <span class="grad-text">' + esc(p.focus.toLowerCase()) + '</span></h1>' +
      '<p class="muted" style="margin-top:12px">Built for ' + who + ' from your ' + answered + ' answers. Do it ' + esc(p.when) + '.</p>' +
      '<dl class="kv"><div><dt>Focus</dt><dd>' + esc(p.focus) + '</dd></div><div><dt>Starting level</dt><dd>' + esc(p.level.name) + '</dd></div>' +
      '<div><dt>Starting hold</dt><dd>' + p.level.hold + ' seconds</dd></div><div><dt>Daily drill</dt><dd>' + esc(p.drill[0]) + '</dd></div></dl></section>';

    if (p.safety) h += '<div class="safety"><b>Before you start</b>' + esc(p.safety) + '</div>';

    h += '<h2 class="sec-title">Week 1, free to read</h2><p class="muted" style="margin-top:4px;font-size:14px">Tap a day to see every rep.</p>';
    p.days.forEach(function (d, k) {
      h += '<details class="day"' + (k < 2 ? ' open' : '') + '><summary><span class="d">' + d[0] + '</span><span class="t">' + esc(d[1]) + '<span class="m" style="display:block">' + d[2] + ' min</span></span><span class="caret" aria-hidden="true">&#9662;</span></summary><ol>' +
        d[3].map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ol></details>';
    });

    if (p.tips.length) {
      h += '<h2 class="sec-title">Notes from your answers</h2><ul class="tips">' + p.tips.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul>';
    }

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

  if (state.i > flow().length) state.i = flow().length;
  render();
})();
