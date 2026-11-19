// Run the skill's survey function in a page and save what it returns.
// node capture/survey.js <url> <out.json>
const fs = require('node:fs');
const { chromium } = require('playwright');

const SURVEY = () => {
  const tally = (map, key) => key && map.set(key, (map.get(key) || 0) + 1);
  const seen = { text: new Map(), fill: new Map(), family: new Map(), type: new Map(), space: new Map(), radius: new Map(), shadow: new Map(), maxWidth: new Map(), borderWidth: new Map() };
  const assets = [];
  for (const el of document.body.querySelectorAll('*')) {
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    const cs = getComputedStyle(el);
    if ([...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) {
      tally(seen.text, cs.color);
      tally(seen.family, cs.fontFamily);
      tally(seen.type, `${cs.fontSize} / ${cs.lineHeight} / ${cs.fontWeight}`);
    }
    for (const gap of [cs.paddingTop, cs.paddingLeft, cs.rowGap, cs.columnGap]) if (/^[1-9]/.test(gap)) tally(seen.space, gap);
    if (cs.backgroundColor !== 'rgba(0, 0, 0, 0)') tally(seen.fill, cs.backgroundColor);
    if (cs.borderTopLeftRadius !== '0px') tally(seen.radius, cs.borderTopLeftRadius);
    if (cs.boxShadow !== 'none') tally(seen.shadow, cs.boxShadow);
    if (cs.maxWidth !== 'none') tally(seen.maxWidth, cs.maxWidth);
    if (cs.borderTopWidth !== '0px') tally(seen.borderWidth, `${cs.borderTopWidth} ${cs.borderTopStyle} ${cs.borderTopColor}`);
    for (const m of cs.backgroundImage.matchAll(/url\("([^"]+)"\)/g)) assets.push({ kind: 'background', url: m[1] });
  }
  for (const img of document.images) assets.push({ kind: 'img', url: img.currentSrc, alt: img.alt, natural: `${img.naturalWidth}x${img.naturalHeight}`, shown: `${img.clientWidth}x${img.clientHeight}` });
  for (const media of document.querySelectorAll('video, audio')) assets.push({ kind: media.tagName.toLowerCase(), url: media.currentSrc, poster: media.poster });
  for (const link of document.querySelectorAll('link[rel~="icon"]')) assets.push({ kind: 'icon', url: link.href });
  const variables = {}, breakpoints = new Set(), unreadable = [];
  const walk = (rules) => {
    for (const rule of rules) {
      if (rule instanceof CSSMediaRule) for (const m of rule.conditionText.matchAll(/width[^)]*?([\d.]+(?:px|em|rem))/g)) breakpoints.add(m[1]);
      if (rule instanceof CSSStyleRule && /(^|,)\s*(:root|html|body)\s*(,|$)/.test(rule.selectorText)) {
        for (const name of rule.style) if (name.startsWith('--')) variables[name] = rule.style.getPropertyValue(name).trim();
      }
      if (rule.cssRules) walk(rule.cssRules);
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules); } catch { unreadable.push(sheet.href); }
  }
  const ranked = Object.fromEntries(Object.entries(seen).map(([k, map]) => [k, [...map].sort((a, b) => b[1] - a[1]).slice(0, 14)]));
  const fonts = [...document.fonts].filter((f) => f.status === 'loaded').map((f) => `${f.family} ${f.weight} ${f.style}`);
  return { url: location.href, title: document.title, variables, breakpoints: [...breakpoints], unreadable, fonts, inlineSvg: document.querySelectorAll('svg').length, ...ranked, assets };
};

(async () => {
  const [url, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })).newPage();
  await page.goto(url, { waitUntil: 'load' });
  await page.evaluate(async () => {
    await document.fonts.ready;
    for (let y = 0; y < document.documentElement.scrollHeight; y += 600) { scrollTo(0, y); await new Promise((d) => setTimeout(d, 120)); }
    scrollTo(0, 0);
  });
  await page.waitForTimeout(800);
  const result = await page.evaluate(SURVEY);
  fs.writeFileSync(out, JSON.stringify(result, null, 2));
  console.log(`${out}: ${result.assets.length} assets, ${Object.keys(result.variables).length} vars, ${result.breakpoints.length} breakpoints`);
  await browser.close();
})();
