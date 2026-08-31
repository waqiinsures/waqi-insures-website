(()=>{
  const root=document.querySelector('[data-rq-slider]');
  if(!root)return;
  const API='https://quotes.waqi-insures.com/api/recent-quotes';
  const track=root.querySelector('.rq-track');
  const dots=root.querySelector('.rq-dots');
  let cards=[];
  let i=0,startX=0;

  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const plural=(n,one,many)=>`${n} ${n===1?one:many}`;
  const personIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.2"/><path d="M5.5 19c.8-4 3.1-6 6.5-6s5.7 2 6.5 6"/></svg>';
  const licenceIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="14" rx="2.5"/><circle cx="8.5" cy="11" r="2.1"/><path d="M6 16c.6-1.8 1.5-2.7 2.5-2.7s1.9.9 2.5 2.7M14 10h4M14 14h4"/></svg>';
  const carIcon='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 15.5h14l-1.2-4.2a2 2 0 0 0-1.9-1.5H8.1a2 2 0 0 0-1.9 1.5L5 15.5Z"/><path d="M3.8 15.5v2.2c0 .8.6 1.4 1.4 1.4h1.3v-1.8h11v1.8h1.3c.8 0 1.4-.6 1.4-1.4v-2.2M7.2 13h9.6"/><circle cx="7.2" cy="16" r="1"/><circle cx="16.8" cy="16" r="1"/></svg>';

  function riskHTML(q){
    const r=[];
    if(+q.tickets)r.push(`<span>${esc(plural(+q.tickets,'Ticket','Tickets'))}</span>`);
    if(+q.suspensions)r.push(`<span>${esc(plural(+q.suspensions,'Suspension','Suspensions'))}</span>`);
    if(+q.nonpayment_cancellations)r.push(`<span>${esc(plural(+q.nonpayment_cancellations,'Non-Payment Cancellation','Non-Payment Cancellations'))}</span>`);
    if(+q.at_fault_accidents)r.push(`<span>${esc(plural(+q.at_fault_accidents,'At-Fault Accident','At-Fault Accidents'))}</span>`);
    return r.length ? `<div class="rq-risk">${r.join('')}</div>` : `<div class="rq-risk rq-risk-clean"><span class="rq-clean-record"><span class="rq-clean-check" aria-hidden="true">✓</span><strong>Clean Record</strong></span></div>`;
  }

  function cardHTML(q){
    const low=+q.lowest_monthly||0,avg=+q.average_monthly||0;
    const save=Math.max(0,+q.savings_monthly || (avg-low));
    const pct=Math.max(0,+q.savings_percent || (avg?Math.round(save/avg*100):0));
    const yearly=Math.max(0,+q.savings_yearly || save*12);
    return `<article class="rq-card"><div class="rq-card-grid"><div><span class="rq-label">RECENT AUTO QUOTE</span><h2 class="rq-vehicle">${esc(q.vehicle||'Auto Insurance')}</h2><div class="rq-location">⌖ ${esc(q.location||'Ontario')}</div><span class="rq-coverage">${esc(q.coverage||'COVERAGE')}</span><div class="rq-profile"><div class="rq-profile-item"><span class="rq-profile-icon">${personIcon}</span><span><b>AGE</b><strong>${esc(q.age||'—')}</strong></span></div><div class="rq-profile-item"><span class="rq-profile-icon">${licenceIcon}</span><span><b>LICENSED</b><strong>${esc(q.licensed_years||'—')} YEARS</strong></span></div><div class="rq-profile-item rq-type"><span class="rq-profile-icon">${carIcon}</span><span><b>CAR TYPE</b><strong>${esc(q.car_type||'AUTO')}</strong></span></div></div>${riskHTML(q)}<div class="rq-visual" aria-hidden="true"><div class="rq-contour rq-contour-a"></div><div class="rq-contour rq-contour-b"></div><div class="rq-contour rq-contour-c"></div><div class="rq-visual-mark"><img src="assets/images/waqi-gold-symbol.png" alt=""/></div></div></div><div class="rq-price-panel"><div class="rq-price-row"><span class="rq-price-title">LOWEST MONTHLY QUOTE</span><span class="rq-price primary" data-count="${low}">$${low}/mo</span></div><div class="rq-price-row"><span class="rq-price-title">AVERAGE QUOTE</span><span class="rq-price" data-count="${avg}">$${avg}/mo</span></div><div class="rq-price-row"><span class="rq-price-title">POTENTIAL SAVINGS</span><span class="rq-price rq-save" data-count="${save}">$${save}/mo</span><span class="rq-save-note">${pct}% less · $${yearly.toLocaleString()}/year</span></div></div></div></article>`;
  }

  function animate(card){
    if(!card)return;
    cards.forEach(c=>c.classList.remove('is-active'));
    card.classList.add('is-active');
    card.querySelectorAll('[data-count]').forEach(el=>{const end=+el.dataset.count,start=performance.now(),dur=520;function f(t){const p=Math.min(1,(t-start)/dur),v=Math.round(end*(1-Math.pow(1-p,3)));el.textContent='$'+v+'/mo';if(p<1)requestAnimationFrame(f)}requestAnimationFrame(f)});
  }
  function go(n){if(!cards.length)return;i=(n+cards.length)%cards.length;track.style.transform=`translateX(-${i*100}%)`;[...dots.children].forEach((d,x)=>d.classList.toggle('active',x===i));animate(cards[i]);}
  function bind(){
    cards=[...root.querySelectorAll('.rq-card')]; dots.innerHTML=''; i=0;
    cards.forEach((_,n)=>{const b=document.createElement('button');b.className='rq-dot'+(n===0?' active':'');b.setAttribute('aria-label',`Show quote ${n+1}`);b.onclick=()=>go(n);dots.appendChild(b)});
    root.querySelector('.rq-prev').onclick=()=>go(i-1); root.querySelector('.rq-next').onclick=()=>go(i+1); track.style.transform='translateX(0)'; animate(cards[0]);
  }
  root.querySelector('.rq-viewport').addEventListener('touchstart',e=>startX=e.touches[0].clientX,{passive:true});
  root.querySelector('.rq-viewport').addEventListener('touchend',e=>{const d=e.changedTouches[0].clientX-startX;if(Math.abs(d)>45)go(i+(d<0?1:-1))},{passive:true});
  bind();

  fetch(API,{mode:'cors',cache:'no-store'}).then(r=>{if(!r.ok)throw new Error('feed');return r.json()}).then(items=>{
    if(!Array.isArray(items)||!items.length)return; // current preview cards remain until first broker-published quote exists
    track.innerHTML=items.slice(0,10).map(cardHTML).join('');
    bind();
  }).catch(()=>{}); // fail-safe: a temporary API outage never breaks the page
})();
