// Post-rebuild audit. No pixel target in a reference build, so this checks the classes of
// defect that a pixel diff would otherwise have caught for us: content that renders but
// invisible, overflow, collapsed sections, broken contrast, focus lost.
const {chromium}=require('playwright');
const ANSWERS=[['energy'],['comeback'],['3'],['30'],['evening'],['social'],['plant'],['dairy','nuts'],['dumbbells','bodyweight'],['minimal'],['under6'],['time']];
const L=(c)=>{const m=c.match(/\d+/g);if(!m)return 0;const [r,g,b]=m.slice(0,3).map(v=>{let x=v/255;return x<=0.03928?x/12.92:((x+0.055)/1.055)**2.4;});return .2126*r+.7152*g+.0722*b;};
const CR=(a,b)=>{const[x,y]=[L(a),L(b)].sort((p,q)=>q-p);return (x+.05)/(y+.05);};
// Everything this needs is defined inside it, because page.evaluate serialises the function to
// a string and ships it: a reference to the node-scope `L`/`CR` above throws ReferenceError in
// the page, where those names were never defined.
const AUDIT=()=>{
 const lum=(c)=>{const m=c.match(/[\d.]+/g);if(!m)return 0;const [r,g,b]=m.slice(0,3).map(v=>{let x=v/255;return x<=0.03928?x/12.92:((x+0.055)/1.055)**2.4;});return .2126*r+.7152*g+.0722*b;};
 const cr=(a,b)=>{const[x,y]=[lum(a),lum(b)].sort((p,q)=>q-p);return (x+.05)/(y+.05);};
 const vis=(el)=>{let n=el;while(n&&n!==document.documentElement){const cs=getComputedStyle(n);
  if(cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity)<0.05)return false;n=n.parentElement;}return true;};
 const out={overflow:[],invisible:[],collapsed:[],lowContrast:[],tinyTargets:[],emptyTextNodes:[]};
 const vw=document.documentElement.clientWidth;
 for(const el of document.querySelectorAll('body *')){
  const b=el.getBoundingClientRect();
  const clipped=!!el.closest('[class*="overflow-hidden"],[class*="overflow-clip"]');
  const decorative=el.classList.contains('pointer-events-none');
  if(b.width&&b.right>vw+1&&!clipped&&!decorative)out.overflow.push(el.tagName+'.'+(typeof el.className==='string'?el.className.slice(0,28):'')+` right=${Math.round(b.right)}>${vw}`);
  const hasText=[...el.childNodes].some(n=>n.nodeType===3&&n.textContent.trim());
  // Two exclusions, both earned by producing real false positives on this funnel.
  // (1) `aria-hidden` glyph spans (the checkmarks, the arrows) are decoration: WCAG 1.4.3
  //     excludes text that is not communicated to users, and a screen reader never hears them.
  // (2) An element that declares no colour of its own is inheriting one. The arrow span inside
  //     every CTA takes `color: #fff` from `.btn-primary`, and comparing an inherited colour
  //     against the span's own `transparent` background is not a measurement, so each CTA on
  //     each page reported a 1.10:1 failure for a button that is fine.
  const isDecoration=el.getAttribute('aria-hidden')==='true';
  if(hasText&&vis(el)&&!isDecoration){
   const cs=getComputedStyle(el);
   if(parseFloat(cs.opacity)<0.05)out.invisible.push((el.innerText||'').slice(0,40));
   const f=cs.color;
   // Resolve the colour this text is actually sitting on. Three things defeat the naive read,
   // all of them hit here: `transparent` and alpha blends composite over an ancestor, so the
   // walk must continue past them; and a component whose fill is a *gradient painted by a
   // ::before layer* (`.btn-primary`) reports `backgroundColor: transparent` on the element
   // itself, which made every CTA on every page read as white-on-white at 1.10:1. So a
   // gradient on the element or any of its pseudo-elements counts as a deliberate fill, and
   // its darkest stop is the conservative backing colour.
   const STOP=/#([0-9a-f]{6})|rgba?\(([^)]+)\)/gi;
   const toRGB=(tok)=>{
     if(tok[0]==='#'){const h=tok.slice(1);return [0,1,2].map(i=>parseInt(h.substr(i*2,2),16));}
     const m=tok.match(/[\d.]+/g);return m?m.slice(0,3).map(Number):null;};
   const mixed=(stops,below)=>{
     // Composite the darkest gradient stop over whatever is behind it, at a weight that is
     // conservative rather than exact: gradient stops here are opaque brand colours.
     const s=stops.map(toRGB).filter(Boolean).sort((a,b)=>(a[0]+a[1]+a[2])-(b[0]+b[1]+b[2]))[0];
     if(!s)return below;const base=toRGB(below)||[255,255,255];
     return `rgb(${s.map((v,i)=>Math.round(v*0.85+base[i]*0.15)).join(', ')})`;};
   const darkStops=(str)=>{const stops=[...(str||'').matchAll(STOP)].map(m=>m[0]);
     return stops.length?stops:null;};
   let bgc='rgb(255, 255, 255)',n=el;
   outer:
   while(n){
     const style=getComputedStyle(n);
     for(const pseudo of ['', '::before', '::after']){
       const ps=pseudo?getComputedStyle(n,pseudo):style;
       const c=ps.backgroundColor;
       if(!/rgba\(0, 0, 0, 0\)|transparent/.test(c)&&/^rgb/.test(c)){
         const a=c.match(/[\d.]+/g);
         if(!a||a.length<4||+a[3]>0.85){bgc=c;break outer;}}
       const grad=darkStops(ps.backgroundImage);
       if(grad&&(!pseudo||ps.content!=='none')){
         // Resolve the rest of the stack first, then blend, so the ratio is against real paint.
         let below='rgb(255, 255, 255)',m2=n.parentElement;
         while(m2){const c2=getComputedStyle(m2).backgroundColor;
           if(!/rgba\(0, 0, 0, 0\)|transparent/.test(c2)&&/^rgb/.test(c2)){const a2=c2.match(/[\d.]+/g);
             if(!a2||a2.length<4||+a2[3]>0.85){below=c2;break;}}m2=m2.parentElement;}
         bgc=mixed(grad,below);break outer;}}
     n=n.parentElement;}
   {
    // Gradient-clipped text is a special case and it reports a lie: `-webkit-text-fill-color:
    // transparent` makes `color` irrelevant, so the computed value (`rgb(207,74,7)` here) is
    // compared against an element background that is also transparent, and the tool printed
    // 1.22:1 for a headline whose darkest stop is 4.12:1 on its band. The honest question for
    // clipped text is the worst stop in the image against what is behind it.
    const fillTransparent=/^(transparent|rgba\(0, 0, 0, 0\))$/.test(cs.webkitTextFillColor||'');
    let effectiveForeground=f;
    if(fillTransparent&&/gradient/.test(cs.backgroundImage||''))effectiveForeground='darkest-stop';
    const r=effectiveForeground==='darkest-stop'
      ? (()=>{const stops=[...(cs.backgroundImage||'').matchAll(/#[0-9a-f]{6}/gi)].map(m=>m[0]);
          const dark=stops.map(s=>({s,l:lum(s)})).sort((a,b)=>a.l-b.l)[0];
          return dark?cr(dark.s,bgc):99;})()
      : cr(f,bgc);
    const big=parseFloat(cs.fontSize)>=24||(parseFloat(cs.fontSize)>=18.66&&parseInt(cs.fontWeight)>=700);
    if(r<(big?3:4.5))out.lowContrast.push(`${(el.innerText||'').slice(0,34)} | ${cs.fontSize}/${cs.fontWeight} ${r.toFixed(2)}:1 ${el.tagName}.${(typeof el.className==='string'?el.className.slice(0,26):'')}`);
   }
  }
  // Target size, measured on whatever the visitor actually hits rather than on whatever box
  // the markup happens to give us. Two exclusions, and one relocation:
  // (1) A radio or checkbox is not the target. Its `label[for]` is, and here that row is a
  //     64px `.option`, so flagging the 20px control box reports a defect on a target that is
  //     comfortably legal. Measured on the row *instead* of skipped, so a row that really does
  //     collapse to 20px still reports.
  // (2) A skip link is `sr-only` until focus, which is precisely when nobody is aiming at it.
  //     WCAG's target-size criterion is about pointer input; only its focus-expanded box would
  //     be worth measuring, and that box is the full-height button it becomes.
  const row=el.matches('input:not([type=hidden])')&&el.closest('label');
  if(row&&!row.dataset.sized){
    row.dataset.sized='1';
    const rb=row.getBoundingClientRect();
    if(vis(row)&&(rb.width<44||rb.height<44))
      out.tinyTargets.push(`LABEL ${Math.round(rb.width)}x${Math.round(rb.height)} wrapping ${el.type}`);}
  const skipLink=el.matches('a[href^="#"]')&&el.classList.contains('sr-only')&&!el.matches(':focus');
  if(vis(el)&&!row&&!skipLink&&(b.width<24&&b.height<24)&&(el.matches('input,button,a[href],label[for]')))
   out.tinyTargets.push(`${el.tagName}.${(typeof el.className==='string'?el.className.slice(0,26):'')} ${Math.round(b.width)}x${Math.round(b.height)} ${(el.innerText||el.type||'').slice(0,20)}`);
 }
 // every labelled control must resolve to a real control
 out.deadLabels=[...document.querySelectorAll('label[for]')].filter(l=>!document.getElementById(l.getAttribute('for'))).map(l=>l.getAttribute('for'));
 // a heading that renders with no text is the blank-render bug wearing a different hat
 out.emptyHeadings=[...document.querySelectorAll('h1,h2,h3')].filter(h=>!(h.innerText||'').trim()).map(h=>h.tagName+'.'+(typeof h.className==='string'?h.className.slice(0,24):''));
 out.documentScroll=document.documentElement.scrollWidth>document.documentElement.clientWidth+1 ? `scrollWidth ${document.documentElement.scrollWidth} > viewport ${document.documentElement.clientWidth}` : null;
 return out;};
(async()=>{
 const browser=await chromium.launch();
 const problems=[];
 for(const width of [390,768,1440]){
  const ctx=await browser.newContext({viewport:{width,height:width<700?844:900},deviceScaleFactor:1});
  const page=await ctx.newPage();
  const errs=[];page.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,90));});
  page.on('pageerror',e=>errs.push('pageerror '+String(e.message).slice(0,90)));
  const pages=[];
  await page.goto('http://127.0.0.1:8091/',{waitUntil:'load'});
  await page.evaluate(async()=>{document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));await document.fonts.ready;for(let y=0;y<document.documentElement.scrollHeight;y+=600){scrollTo(0,y);await new Promise(d=>setTimeout(d,110));}scrollTo(0,0);});
  await page.waitForTimeout(250);
  pages.push(['/',await page.evaluate(AUDIT)]);
  for(let i=0;i<12;i++){
   await page.goto(`http://127.0.0.1:8091/onboarding/${i}`,{waitUntil:'load'});
   await page.evaluate((v)=>{v.forEach(x=>{const el=document.getElementById('opt-'+x);if(el)el.checked=true;});document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));},ANSWERS[i]);
   await page.waitForTimeout(120);
   pages.push([`/onboarding/${i}`,await page.evaluate(AUDIT)]);
  }
  for(const path of ['/summary','/preview','/checkout','/policy/refund','/policy/terms','/policy/privacy','/health']){
   await page.goto('http://127.0.0.1:8091'+path,{waitUntil:'load'});
   await page.evaluate(async()=>{document.querySelectorAll('.reveal').forEach(e=>e.classList.add('is-visible'));await document.fonts.ready;});
   await page.waitForTimeout(220);
   pages.push([path,await page.evaluate(AUDIT)]);
  }
  for(const[path,a]of pages){
   if(a.documentScroll) problems.push(`@${width} ${path} horizontal scroll: ${a.documentScroll}`);
   for(const[k,list]of Object.entries(a)) if(list&&list.length) problems.push(`@${width} ${path} ${k}: ${list.slice(0,3).join(' || ')}`);
   if(errs.length){problems.push(`@${width} ${path} console: ${errs.slice(0,2).join(' || ')}`);errs.length=0;}
  }
  await ctx.close();
 }
 // keyboard walk on a quiz step
 const kbctx=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:1});
 const kb=await kbctx.newPage();
 await kb.goto('http://127.0.0.1:8091/onboarding/7',{waitUntil:'load'});
 await kb.keyboard.press('Tab');
 const ring=await kb.evaluate(()=>{const el=document.activeElement;const cs=getComputedStyle(el);return{tag:el.tagName,radius:cs.outlineRadius,width:cs.outlineWidth,style:cs.outlineStyle};});
 console.log('keyboard focus after Tab:',JSON.stringify(ring));
 await kbctx.close();
 console.log('\n=== AUDIT ===');
 if(!problems.length)console.log('clean: no overflow, no invisible text, no contrast fails, no dead labels, no empty headings, no console errors');
 else problems.forEach(p=>console.log('-',p));
 await browser.close();
})();
