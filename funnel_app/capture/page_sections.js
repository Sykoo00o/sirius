// Section shots for our own quiz and artefact, which have no marketing bands to name and
// are better addressed by the components the tests already assert on.
const fs=require('node:fs');const {chromium}=require('playwright');
const SECTIONS={
  quiz:{shell:'.quiz-shell',progress:'[role=progressbar]',heading:'h1',options:'.option',form:'#quiz-form'},
  preview:{header:'[data-section=preview-header]',days:'[data-section=preview-days]',metrics:'[data-section=preview-metrics]',ask:'[data-section=preview-ask]'},
  checkout:{form:'form',price:'[data-section=price-block]',fields:'fieldset',submit:'button[type=submit]'},
};
(async()=>{
 const [name,url,outDir,...widths]=process.argv.slice(2);
 const sections=SECTIONS[name];fs.mkdirSync(outDir,{recursive:true});
 const browser=await chromium.launch();
 for(const width of widths.map(Number)){
  const ctx=await browser.newContext({viewport:{width,height:width<700?844:900},deviceScaleFactor:1,reducedMotion:'reduce'});
  const page=await ctx.newPage();
  await page.route('**/*',r=>r.continue({headers:{...r.request().headers(),cookie:'rl_vid=; path=/'}}));
  await page.goto(url,{waitUntil:'load'});
  await page.evaluate(async()=>{await document.fonts.ready;for(let y=0;y<document.documentElement.scrollHeight;y+=600){scrollTo(0,y);await new Promise(d=>setTimeout(d,140));}scrollTo(0,0);});
  await page.waitForTimeout(400);
  await page.screenshot({path:`${outDir}/${name}-page-${width}.png`,fullPage:true,animations:'disabled'});
  const boxes={};
  for(const[n,sel]of Object.entries(sections)){
   const el=page.locator(sel).first();
   if(!(await el.count())){boxes[n]={missing:true};continue;}
   boxes[n]=await el.evaluate(e=>({top:Math.round(e.getBoundingClientRect().top+scrollY),height:Math.round(e.offsetHeight)}));
   try{await el.screenshot({path:`${outDir}/${name}-${n}-${width}.png`,animations:'disabled'});}catch(err){boxes[n].shotError=String(err.message).slice(0,60);}
  }
  fs.writeFileSync(`${outDir}/${name}-boxes-${width}.json`,JSON.stringify(boxes,null,2));
  console.log(`${name} @${width}:`,Object.entries(boxes).map(([k,v])=>`${k}:${v.height??'x'}`).join(' '));
  await ctx.close();
 }
 await browser.close();
})();
