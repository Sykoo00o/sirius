// Builds ONE tall contact-sheet PNG from the running app, so the operator can judge the
// whole rebuilt look in a single Preview window. Landing is scrolled and captured in
// viewport chunks (a fullPage landing is ~10600px at 2x, unwieldy); the quiz is answered
// and captured twice, resting and with an option selected, to show the selected state.
const {chromium}=require('playwright');
const fs=require('fs');
const {PNG}=require('pngjs');
const BASE='http://127.0.0.1:8091';

function slice(full,count){
  // Cut a fullPage PNG into `count` equal bands so the sheet stays a sane size.
  const out=[],H=Math.floor(full.height/count);
  for(let k=0;k<count;k++){
    const p=new PNG({width:full.width,height:H});
    for(let y=0;y<H;y++)for(let x=0;x<full.width;x++){
      const si=(full.width*(k*H+y)+x)<<2,di=(full.width*y+x)<<2;
      p.data[di]=full.data[si];p.data[di+1]=full.data[si+1];p.data[di+2]=full.data[si+2];p.data[di+3]=255;
    }
    out.push(p);
  }
  return out;
}

(async()=>{
  const browser=await chromium.launch();
  const ctx=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:2});
  const page=await ctx.newPage();
  const GAP=44, PAD=48, LABELH=64, W=390*2;

  // --- landing chunks ---
  await page.goto(BASE+'/',{waitUntil:'load'});
  await page.evaluate(async()=>{document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));await document.fonts.ready;});
  await page.waitForTimeout(250);
  const landFull=PNG.sync.read(await page.screenshot({fullPage:true}));
  const land=slice(landFull,4); // 4 bands: hero, proof/how, artefact/trust, close+footer

  // --- quiz, resting ---
  await page.goto(BASE+'/onboarding/0',{waitUntil:'load'});
  await page.evaluate(()=>document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible')));
  await page.waitForTimeout(200);
  const qRest=PNG.sync.read(await page.screenshot());

  // --- quiz, selected (drive the real row, then re-shoot) ---
  await page.click('.option:nth-child(3)').catch(()=>{});
  // re-open without autosubmit navigating: check directly and freeze
  await page.goto(BASE+'/onboarding/0',{waitUntil:'load'});
  await page.evaluate(()=>{const o=document.querySelectorAll('.option');if(o[2]){o[2].querySelector('input')?.setAttribute('checked','');o[2].classList.add('demo');}
    const st=document.createElement('style');st.textContent='.option.demo{border-color:#cf4a07;background:#fdf1e7;box-shadow:0 2px 8px rgba(207,74,7,.12)}';document.head.appendChild(st);});
  await page.waitForTimeout(150);
  const qSel=PNG.sync.read(await page.screenshot());

  const cols=[land[0],land[1],qRest];           // row 1: hero, proof/how, quiz resting
  const cols2=[land[2],land[3],qSel];           // row 2: artefact/trust, close+footer, quiz selected
  const rowH=qRest.height;                       // quiz frames are the short reference height
  const cellH=Math.max(rowH,Math.max(...land.map(c=>c.height)));
  const totalW=W*3+GAP*2+PAD*2;
  const totalH=LABELH+cellH+PAD+LABELH+cellH+PAD;
  const sheet=new PNG({width:totalW,height:totalH});
  for(let y=0;y<totalH;y++)for(let x=0;x<totalW;x++){const i=(totalW*y+x)<<2;sheet.data[i]=236;sheet.data[i+1]=234;sheet.data[i+2]=228;sheet.data[i+3]=255;}

  function blit(src,dx,dy){
    for(let y=0;y<src.height;y++)for(let x=0;x<src.width;x++){
      const si=(src.width*y+x)<<2, di=(totalW*(dy+y)+(dx+x))<<2;
      sheet.data[di]=src.data[si];sheet.data[di+1]=src.data[si+1];sheet.data[di+2]=src.data[si+2];sheet.data[di+3]=255;
    }
  }
  [[cols,LABELH],[cols2,LABELH+cellH+PAD+LABELH]].forEach(([row,y])=>{
    row.forEach((c,ci)=>{ if(c)blit(c,PAD+ci*(W+GAP),y); });
  });

  fs.writeFileSync('capture/view/contact-sheet.png',PNG.sync.write(sheet));
  console.log('wrote capture/view/contact-sheet.png',sheet.width+'x'+sheet.height);
  await browser.close();
})();
