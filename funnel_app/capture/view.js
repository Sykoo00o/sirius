// One-shot full-page shots of the running funnel, for the operator to open in Preview.
// Full page (fullPage:true), so every band is in frame without scrolling. The funnel's
// entrance reveals are observer-gated, so they are forced visible before capture (this is
// a review artifact, not a metric-affecting run, so CAPTURE_NO_TRACK is not needed).
const {chromium}=require('playwright');
const BASE=process.env.FUNNEL_BASE||'http://127.0.0.1:8091';
const SHOTS=[
  ['landing','/'],
  ['quiz-q1','/onboarding/0'],
  ['quiz-q8','/onboarding/7'],
  ['preview','/preview'],
  ['checkout','/checkout'],
];
(async()=>{
  const browser=await chromium.launch();
  const ctx=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:2});
  const page=await ctx.newPage();
  // seed a completed session so /preview and /checkout render populated, not empty
  await page.goto(BASE+'/',{waitUntil:'load'});
  for(const path of SHOTS){
    await page.goto(BASE+path[1],{waitUntil:'load'});
    await page.evaluate(async()=>{
      document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));
      await document.fonts.ready;
    });
    await page.waitForTimeout(250);
    await page.screenshot({path:`capture/view/${path[0]}.png`,fullPage:true});
    console.log('shot',path[0]);
  }
  await browser.close();
})();
