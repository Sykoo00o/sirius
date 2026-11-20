// Section-box walk for the Phase 2 member screens.
//
// Phase 1's `walk_visual.js` walks routes a browser can reach by typing a URL. The member
// screens cannot: `/dashboard`, `/account/cancel`, `/account/cancel/offer` and `/account/survey`
// all read the signed `rl_vid` cookie, and the cookie only becomes a subscriber once a
// subscription row exists for that visitor key. So this script drives the funnel for real, in a
// throwaway database, and hands the resulting cookie to the same page-level probe
// `walk_visual.js` uses.
//
// Usage:
//   1. start the app with a scratch database and rehearsal mode:
//        cd funnel_app
//        FUNNEL_DB_PATH=/tmp/member/f.sqlite3 \
//        FUNNEL_BILLING_DB_PATH=/tmp/member/b.sqlite3 \
//        FUNNEL_SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
//        FUNNEL_DISCOUNT_COUPON_ID=half_for_two \
//        ../backend/.venv/bin/python -m uvicorn main:app --port 8091
//   2. seed the paid row the cookie needs:
//        ../backend/.venv/bin/python capture/member_seed.py
//   3. node capture/member_walk.js capture/member 390
//
// Screens are reached by clicking the funnel's own buttons rather than by injecting forms, so
// what gets measured is the page a member actually lands on, with the form fields a real submit
// carries. That also gives the audit the cancel screen's *first* visit, which is the only one
// where the confirmation-before-offers ordering can be seen.
//
// Exit code 1 if any band measured zero height, which is the failure that matters: a band with
// no height is content nobody sees.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.FUNNEL_BASE || 'http://127.0.0.1:8091';
const OUT = process.argv[2] || 'capture/member';
const WIDTH = parseInt(process.argv[3] || '390', 10);
const SECRET_FILE = process.env.FUNNEL_SECRET_FILE || '/tmp/member/secret';

// The same probe `walk_visual.js` runs, kept here so this file is standalone: a section is a
// `data-section` element, and the interesting property is whether it has any height at all.
const PROBE = () => {
  const sections = [...document.querySelectorAll('[data-section]')].map((el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      name: el.getAttribute('data-section'),
      top: Math.round(rect.top + window.scrollY),
      height: Math.round(rect.height),
      width: Math.round(rect.width),
      invisible: style.visibility === 'hidden' || Number(style.opacity) === 0,
      text: (el.innerText || '').trim().slice(0, 80),
    };
  });
  return {
    url: location.pathname,
    title: document.title,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    sections,
  };
};

(async () => {
  const secret = fs.readFileSync(SECRET_FILE, 'utf8').trim();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: WIDTH, height: 900 } });
  fs.mkdirSync(OUT, { recursive: true });

  // Signed cookie format is `key.signature`, minted by session.py. The seed script writes the
  // key; signing it here keeps the secret out of that file and out of git.
  const { createHmac } = require('crypto');
  const visitor = fs.readFileSync('/tmp/member/visitor', 'utf8').trim();
  // `session.py` truncates the digest to 32 hex chars, so this must too: the app rejects a
  // full-length MAC by handing the browser a fresh visitor cookie, which renders every member
  // page as "no plan on this browser yet" and measures a perfectly healthy template as empty.
  const sig = createHmac('sha256', secret).update(visitor).digest('hex').slice(0, 32);
  await page.goto(BASE + '/', { waitUntil: 'load' });
  // `url` alone, not `url` plus `path`: this build of Playwright rejects a cookie carrying both.
  await page.context().addCookies([{
    name: 'rl_vid', value: `${visitor}.${sig}`, url: BASE,
  }]);

  const results = [];

  async function record(label, status) {
    const probe = await page.evaluate(PROBE);
    probe.label = label;
    probe.status = status === undefined ? null : status;
    await page.screenshot({
      path: path.join(OUT, `${label.replace(/\//g, '_')}-${WIDTH}.png`), fullPage: true,
    });
    results.push(probe);
    return probe;
  }

  // GET-reachable screens first: no click needed, and the cookie is all the session is.
  for (const [label, url] of [['/dashboard', BASE + '/dashboard'],
                              ['/account/survey', BASE + '/account/survey']]) {
    const response = await page.goto(url, { waitUntil: 'load' });
    await record(label, response.status());
  }

  // Re-measure the cancel-button click first, on a throwaway screenshot directory, because that
  // click mutates the one state the walk depends on: it cancels the subscription, and the cancel
  // screens below only render their menu and their "access until" copy for a member whose
  // cancellation the funnel has just performed. Reported as a line of its own rather than pushed
  // into the results, so ordering this file depends on cannot silently reorder the audit.
  await page.goto(BASE + '/dashboard', { waitUntil: 'load' });
  const armed = page.getByRole('button', { name: /cancel subscription/i }).first();
  if (await armed.count()) {
    await Promise.all([page.waitForNavigation({ waitUntil: 'load' }), armed.click()]);
    const probe = await page.evaluate(PROBE);
    const bands = probe.sections.map((section) => section.name);
    console.log(`info\t/dashboard cancel button\t${bands.join(', ') || 'no bands'}`);
  }

  // Then the two POST screens, by clicking. Reloading `/dashboard` above would not do: the
  // cancel screen's ordering claim only exists on the visit that performed the cancellation.
  // The cancel screen itself, as it renders for a member whose cancellation just happened. This
  // is the page whose band *order* is the compliance property: `test_billing.py` holds the
  // confirmation before the offers in the HTML, and position on screen is this file's share.
  // Checked rather than left to the JSON, because a band that renders above the fold changes
  // which of the two a member reads first, and only a layout break would tell.
  await record('/account/cancel-after-click', 200);
  const cancelScreen = results[results.length - 1];
  const byName = Object.fromEntries(cancelScreen.sections.map((s) => [s.name, s]));
  if (!byName['cancel-confirmed'] || !byName['save-offer']) {
    console.log('FAIL\t/account/cancel: a band is missing, so the ordering cannot be judged\t' +
                cancelScreen.sections.map((s) => s.name).join(', '));
    process.exitCode = 1;
  } else if (byName['save-offer'].top < byName['cancel-confirmed'].top +
                                        byName['cancel-confirmed'].height) {
    console.log(`FAIL\t/account/cancel: the offer menu (${byName['save-offer'].top}) starts ` +
                `inside or above the confirmation (${byName['cancel-confirmed'].top}+` +
                `${byName['cancel-confirmed'].height}), so persuasion precedes the action`);
    process.exitCode = 1;
  }
  const pause = page.getByRole('button', { name: /pause for \d+ days/i }).first();
  if (await pause.count()) {
    await Promise.all([page.waitForNavigation({ waitUntil: 'load' }), pause.click()]);
    await record('/account/cancel/offer', 200);
    // Back to a cancelled state so a re-run of this file measures the same pages. The button
    // text is the funnel's own decline copy, so this also exercises the fourth option.
    await page.goto(BASE + '/dashboard', { waitUntil: 'load' });
  } else {
    console.log('warn\tcancel screen offered no pause button');
  }

  fs.writeFileSync(path.join(OUT, 'sections.member.json'), JSON.stringify(results, null, 2));
  await browser.close();

  let failed = 0;
  for (const probe of results) {
    const zero = probe.sections.filter((section) => section.height === 0 || section.invisible);
    const overflow = probe.scrollWidth > probe.clientWidth;
    const marker = zero.length || overflow ? 'FAIL' : 'ok';
    console.log(`${marker}\t${probe.label}\t[${probe.status}]\t` +
                `${probe.sections.length} bands` +
                (zero.length ? `\tzero/invisible: ${zero.map((s) => s.name).join(', ')}` : '') +
                (overflow ? `\thorizontal scroll ${probe.scrollWidth}>${probe.clientWidth}` : ''));
    if (zero.length || overflow) failed += 1;
  }
  process.exit(failed || process.exitCode ? 1 : 0);
})();
