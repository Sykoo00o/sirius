// Walk simple.life's own quiz, and record the settled computed style of one question
// screen in its resting, hovered and selected states. This is a reference build, so the
// output is a pattern brief (geometry, rhythm, state deltas), not a pixel target.
// node capture/quiz_walk.js
const fs = require('node:fs');
const { chromium } = require('playwright');

// Waits for running CSS transitions to finish, then reads computed values. A fixed sleep
// is not a safe wait in a headless browser: a 0.2s transition still reports mid-way values
// 400ms later, which would record a state that no visitor ever sees.
const PROBE = (selector) => new Promise(async (resolve) => {
  const frame = () => new Promise((next) => requestAnimationFrame(next));
  const running = () => document.getAnimations().filter((a) => a instanceof CSSTransition && a.playState === 'running');
  await frame(); await frame();
  while (running().length) { await Promise.allSettled(running().map((a) => a.finished)).then(frame); }
  const el = document.querySelector(selector);
  if (!el) return resolve(null);
  const cs = getComputedStyle(el);
  const box = el.getBoundingClientRect();
  const watch = ['color', 'backgroundColor', 'backgroundImage', 'borderTopWidth', 'borderTopStyle', 'borderTopColor',
    'borderRadius', 'boxShadow', 'transform', 'opacity', 'paddingTop', 'paddingLeft', 'fontSize', 'lineHeight',
    'fontWeight', 'textAlign', 'transitionProperty', 'transitionDuration', 'transitionTimingFunction', 'rowGap', 'gap'];
  const out = { width: Math.round(box.width), height: Math.round(box.height), top: Math.round(box.top) };
  for (const name of watch) out[name] = cs[name];
  // The label sits inside the option, so its type comes from the child that owns text.
  const label = [...el.querySelectorAll('*')].find((n) => [...n.childNodes].some((c) => c.nodeType === 3 && c.textContent.trim()));
  if (label) {
    const lcs = getComputedStyle(label);
    out.label = { text: label.innerText.trim().slice(0, 40), fontSize: lcs.fontSize, lineHeight: lcs.lineHeight, fontWeight: lcs.fontWeight, color: lcs.color };
  }
  const icon = el.querySelector('svg,img,i,[class*=icon]');
  if (icon) {
    const ics = getComputedStyle(icon);
    const ib = icon.getBoundingClientRect();
    out.icon = { width: Math.round(ib.width), height: Math.round(ib.height), radius: ics.borderRadius, bg: ics.backgroundColor, tag: icon.tagName.toLowerCase() };
  }
  resolve(out);
});

const SHELL = () => {
  const bar = document.querySelector('[class*=progress],[role=progressbar],[class*=Progress]');
  const back = document.querySelector('[class*=back],[aria-label*=back i],[aria-label*=Back]');
  const title = document.querySelector('h1,h2,[class*=title]');
  const read = (el) => {
    if (!el) return null;
    const cs = getComputedStyle(el); const box = el.getBoundingClientRect();
    return { selector: el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/)[0] : el.tagName.toLowerCase(),
      width: Math.round(box.width), height: Math.round(box.height), top: Math.round(box.top),
      bg: cs.backgroundColor, color: cs.color, radius: cs.borderTopLeftRadius, fontSize: cs.fontSize,
      fontWeight: cs.fontWeight, lineHeight: cs.lineHeight, textAlign: cs.textAlign,
      padding: cs.paddingTop + ' ' + cs.paddingRight + ' ' + cs.paddingBottom + ' ' + cs.paddingLeft,
      position: cs.position, text: (el.innerText || el.getAttribute('aria-valuenow') || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      inlineStyle: el.getAttribute('style') || '' };
  };
  const step = document.querySelector('[class*=select__option],[class*=option]');
  const group = step ? step.parentElement : null;
  const gcs = group ? getComputedStyle(group) : null;
  const view = document.querySelector('[class*=onboarding-view]');
  return {
    viewport: { w: innerWidth, h: innerHeight },
    page: view ? { bg: getComputedStyle(view).backgroundColor, padding: getComputedStyle(view).padding } : null,
    progress: read(bar), back: read(back), title: read(title),
    group: group ? { gap: gcs.rowGap, padding: gcs.paddingTop + ' ' + gcs.paddingLeft, display: gcs.display, width: Math.round(group.getBoundingClientRect().width) } : null,
    scrollHeight: document.documentElement.scrollHeight,
  };
};

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, reducedMotion: 'reduce' })).newPage();
  await page.goto('https://simple.life/survey/', { waitUntil: 'load' });
  // Get past the cookie banner and the "analyzing your profile" interstitial.
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(1500);
    const ok = page.locator('button:has-text("Ok")').first();
    if (await ok.count()) { await ok.click().catch(() => {}); }
    if (/How old are you/i.test(await page.evaluate(() => document.body.innerText))) break;
  }
  const out = { url: page.url(), capturedAt: new Date().toISOString(), steps: [] };
  for (let stepNumber = 0; stepNumber < 4; stepNumber++) {
    const question = await page.evaluate(() => {
      const t = document.querySelector('h1,h2,[class*=title]');
      return t ? (t.innerText || '').trim().replace(/\s+/g, ' ') : null;
    });
    const first = await page.evaluate(() => {
      const el = document.querySelector('[class*=select__option]');
      if (!el) return null;
      const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).join('.') : '';
      el.setAttribute('data-probe', '1');
      const sibling = el.nextElementSibling;
      if (sibling) sibling.setAttribute('data-probe-hover', '1');
      return '[data-probe]';
    });
    if (!first) break;
    const record = { path: new URL(page.url()).pathname, question, shell: await page.evaluate(SHELL) };
    record.resting = await page.evaluate(PROBE, '[data-probe]');
    await page.hover('[data-probe-hover]').catch(() => {});
    record.hover = await page.evaluate(PROBE, '[data-probe-hover]');
    await page.tap('[data-probe]').catch(async () => { await page.click('[data-probe]').catch(() => {}); });
    record.selected = await page.evaluate(PROBE, '[data-probe]');
    // Screenshots of the step before the auto-advance carries us away.
    await page.screenshot({ path: `capture/source/quiz-${stepNumber}-${record.path.split('/').pop()}.png`, animations: 'disabled' });
    await page.waitForTimeout(1200);
    out.steps.push(record);
    // If the step did not advance on its own, press the continue control.
    const cont = page.locator('button:has-text("OK"), button:has-text("Continue"), button:has-text("Next")').first();
    if (await cont.count()) { await cont.click().catch(() => {}); await page.waitForTimeout(1200); }
  }
  fs.writeFileSync('capture/quiz.states.json', JSON.stringify(out, null, 2));
  console.log(`captured ${out.steps.length} question screens -> capture/quiz.states.json`);
  await browser.close();
})();
