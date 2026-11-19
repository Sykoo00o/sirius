// Section screenshots + box geometry, per the clone-website skill.
// Usage: node capture/shots.js <url> <outDir> <sectionsFile> <width...>
const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const [url, outDir, mapFile, ...widths] = process.argv.slice(2);
  const sections = JSON.parse(fs.readFileSync(mapFile, 'utf8'));
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch();
  for (const width of widths.map(Number)) {
    const context = await browser.newContext({ viewport: { width, height: width < 700 ? 844 : 900 }, deviceScaleFactor: 1, reducedMotion: 'reduce' });
    const page = await context.newPage();
    // A capture run must not write funnel events. Without this, `visitor_key` is empty, every
    // capture shows up as a distinct visitor with no key at all, and `/dev/responses` fills
    // with rows from a laptop rather than from a launch.
    if (process.env.CAPTURE_NO_TRACK === '1') {
      await page.route('**/*', (route) => {
        const headers = { ...route.request().headers(), cookie: 'rl_vid=; path=/' };
        route.continue({ headers });
      });
    }
    await page.goto(url, { waitUntil: 'load' });
    await page.evaluate(async () => {
      await document.fonts.ready;
      for (let y = 0; y < document.documentElement.scrollHeight; y += 600) {
        scrollTo(0, y);
        await new Promise((done) => setTimeout(done, 150));
      }
      scrollTo(0, 0);
    });
    await page.waitForTimeout(500);
    // Freeze the moving things the source uses: cookie overlays and floating chat bubbles
    // would otherwise cover a section shot on one side of the comparison and not the other.
    await page.evaluate(() => {
      for (const sel of ['#onetrust-consent-sdk', '[class*=chat-widget]', '[id*=intercom-container]', '[class*=cookie-]']) {
        document.querySelectorAll(sel).forEach((el) => { el.style.display = 'none'; });
      }
    });
    await page.screenshot({ path: `${outDir}/page-${width}.png`, fullPage: true, animations: 'disabled' });
    const boxes = {};
    for (const [name, selector] of Object.entries(sections)) {
      const block = page.locator(selector).first();
      if (!(await block.count())) { boxes[name] = { missing: true }; continue; }
      boxes[name] = await block.evaluate((el) => ({ top: Math.round(el.getBoundingClientRect().top + scrollY), height: Math.round(el.offsetHeight) }));
      try {
        await block.screenshot({ path: `${outDir}/${name}-${width}.png`, animations: 'disabled' });
      } catch (error) {
        boxes[name].shotError = String(error.message).slice(0, 80);
      }
    }
    fs.writeFileSync(`${outDir}/boxes-${width}.json`, JSON.stringify(boxes, null, 2));
    console.log(`${outDir} @${width}:`, Object.entries(boxes).map(([k, v]) => `${k}:${v.height ?? 'x'}`).join(' '));
    await context.close();
  }
  await browser.close();
})();
