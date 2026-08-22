document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',e=>{const t=document.querySelector(a.getAttribute('href'));if(t){e.preventDefault();t.scrollIntoView({behavior:'smooth'})}}));

(function(){
  document.body.classList.add('page-entering');
  requestAnimationFrame(function(){
    requestAnimationFrame(function(){
      document.body.classList.remove('page-entering');
    });
  });

  document.addEventListener('click', function(e){
    var a=e.target.closest('a');
    if(!a) return;
    var href=a.getAttribute('href');
    if(!href || href.startsWith('#') || a.target==='_blank' || a.hasAttribute('download')) return;
    if(href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('sms:') || href.startsWith('https://wa.me/')) return;

    var url;
    try { url=new URL(a.href, window.location.href); } catch(err){ return; }
    if(url.origin!==window.location.origin) return;

    e.preventDefault();
    document.body.classList.add('page-leaving');
    setTimeout(function(){ window.location.href=url.href; }, 180);
  });

  window.addEventListener('pageshow', function(){
    document.body.classList.remove('page-leaving','page-entering');
  });
})();

/* ONE connector routine only:
   1 -> top-centre of first card
   35 -> short vertical line to top-centre of middle card
   5 -> top-centre of third card */
(function(){
  function drawWhyLines(){
    var section = document.querySelector('#why');
    if(!section) return;

    var container = section.querySelector('.container');
    var svg = section.querySelector('.why-connectors');
    if(!container || !svg) return;

    if(window.matchMedia('(max-width: 900px)').matches){
      svg.innerHTML = '';
      return;
    }

    var c = container.getBoundingClientRect();
    var grid = section.querySelector('.why-grid');
    if(!grid) return;

    var gridRect = grid.getBoundingClientRect();
    var height = Math.max(1, gridRect.top - c.top + 8);

    svg.setAttribute('viewBox', '0 0 ' + c.width + ' ' + height);
    svg.setAttribute('width', c.width);
    svg.setAttribute('height', height);
    svg.innerHTML = '';

    ['one','providers','languages'].forEach(function(key){
      var num = section.querySelector('.num-anchor[data-target="'+key+'"]');
      var card = section.querySelector('.why-card[data-rail="'+key+'"]');
      if(!num || !card) return;

      var n = num.getBoundingClientRect();
      var r = card.getBoundingClientRect();

      var x1 = (n.left + n.width/2) - c.left;
      var y1 = (n.bottom - c.top) + 7;
      var x2 = (r.left + r.width/2) - c.left;
      var y2 = (r.top - c.top) - 10;

      // Middle connector must be perfectly vertical.
      if(key === 'providers'){
        x2 = x1;
      }

      var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', x1);
      line.setAttribute('y1', y1);
      line.setAttribute('x2', x2);
      line.setAttribute('y2', y2);
      svg.appendChild(line);
    });
  }

  function redraw(){
    requestAnimationFrame(function(){
      requestAnimationFrame(drawWhyLines);
    });
  }

  document.addEventListener('DOMContentLoaded', redraw);
  window.addEventListener('load', redraw);
  window.addEventListener('resize', redraw);
})();
