// List top-level blocks in page order, so capture/sections.*.json can name them.
// node capture/outline.js <url> <out.json>
const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const [url, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })).newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.evaluate(async () => {
    await document.fonts.ready;
    for (let y = 0; y < document.documentElement.scrollHeight; y += 600) { scrollTo(0, y); await new Promise((d) => setTimeout(d, 120)); }
    scrollTo(0, 0);
  });
  await page.waitForTimeout(600);
  const blocks = await page.evaluate(() => {
    const label = (el) => {
      const h = el.querySelector('h1,h2,h3');
      if (h) return (h.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 70);
      const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
      return t.slice(0, 70);
    };
    const sel = (el) => {
      if (el.id) return `#${el.id}`;
      const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter((c) => !/^(is-|has-|wp-|elementor|css|sc-)/.test(c)).slice(0, 2) : [];
      return el.tagName.toLowerCase() + (cls.length ? `.${cls.join('.')}` : '');
    };
    // Candidate section roots: landmarks, semantic sections, and any direct child of a
    // content wrapper that clears 200px tall. Anchoring on structure rather than on a
    // framework's class names, since this site is WordPress and those names are noise here.
    const seen = new Set();
    const out = [];
    const push = (el, depth) => {
      const box = el.getBoundingClientRect();
      if (box.height < 140 || box.width < 200) return;
      const top = Math.round(box.top + scrollY);
      const key = `${sel(el)}@${top}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ selector: sel(el), top, height: Math.round(box.height), depth, heading: label(el) });
    };
    document.querySelectorAll('header,footer,nav,section,main,article,aside,[class*="section"],[class*="Section"],[id]').forEach((el) => push(el, 0));
    out.sort((a, b) => a.top - b.top || b.height - a.height);
    // Keep blocks that start below the previous kept block, so the list reads as a page.
    const kept = [];
    for (const b of out) {
      const last = kept[kept.length - 1];
      if (last && b.top < last.top + Math.min(80, last.height)) continue;
      if (kept.some((k) => k.top === b.top && Math.abs(k.height - b.height) < 4 && k.selector.split('.').length >= b.selector.split('.').length)) continue;
      kept.push(b);
    }
    return kept.slice(0, 60);
  });
  fs.writeFileSync(out, JSON.stringify(blocks, null, 2));
  for (const b of blocks) console.log(`${String(b.top).padStart(6)}  ${String(b.height).padStart(5)}  ${b.selector.padEnd(46)} ${b.heading}`);
  await browser.close();
})();
