const fs=require('node:fs');const {chromium}=require('playwright');
const ANSWERS=[['energy'],['comeback'],['3'],['30'],['evening'],['social'],['plant'],['dairy','nuts'],['dumbbells','bodyweight'],['minimal'],['under6'],['time']];
const NAMES=['goal','experience','days','minutes','timeslot','environment','diet','restrictions','equipment','kitchen','sleep','blocker'];
(async()=>{
 const out=process.argv[2]||'capture/rebuild';const width=+(process.argv[3]||390);
 const browser=await chromium.launch();
 const ctx=await browser.newContext({viewport:{width,height:width<700?844:900},deviceScaleFactor:1,reducedMotion:'reduce'});
 const page=await ctx.newPage();
 const errs=[];page.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,140));});
 page.on('pageerror',e=>errs.push('pageerror: '+String(e.message).slice(0,140)));
 await page.goto('http://127.0.0.1:8091/',{waitUntil:'load'});
 await page.evaluate(async()=>{await document.fonts.ready;for(let y=0;y<document.documentElement.scrollHeight;y+=600){scrollTo(0,y);await new Promise(d=>setTimeout(d,120));}scrollTo(0,0);});
 await page.waitForTimeout(400);
 await page.screenshot({path:`${out}/land-page-${width}.png`,fullPage:true,animations:'disabled'});
 for(let i=0;i<ANSWERS.length;i++){
  await page.goto(`http://127.0.0.1:8091/onboarding/${i}`,{waitUntil:'load'});
  await page.waitForTimeout(260);
  // Fill on a copy of the page so the multi steps do not navigate and the shots line up.
  await page.evaluate((vals)=>{vals.forEach(v=>{const el=document.getElementById('opt-'+v);if(el)el.checked=true;});
    document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));},ANSWERS[i]);
  await page.waitForTimeout(220);
  await page.screenshot({path:`${out}/q${String(i+1).padStart(2,0)}-${NAMES[i]}-${width}.png`,fullPage:true,animations:'disabled'});
 }
 for(const[path,name]of[['/summary','summary'],['/preview','preview'],['/checkout','checkout'],['/policy/refund','refund']]){
  await page.goto('http://127.0.0.1:8091'+path,{waitUntil:'load'});
  await page.evaluate(async()=>{document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));await document.fonts.ready;for(let y=0;y<document.documentElement.scrollHeight;y+=600){scrollTo(0,y);await new Promise(d=>setTimeout(d,110));}scrollTo(0,0);});
  await page.waitForTimeout(320);
  await page.screenshot({path:`${out}/${name}-page-${width}.png`,fullPage:true,animations:'disabled'});
 }
 console.log('console errors:',errs.length?errs:'none');
 await browser.close();
})();
