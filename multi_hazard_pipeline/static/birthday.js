/* Decorative, non-interactive background. Soft repulsion keeps portraits clear of balloons. */
(() => {
  const key = 'workspace-theme-victor-v1', root = document.documentElement;
  const scene = document.getElementById('birthday-scene');
  const greeting = document.querySelector('.birthday-greeting');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let frame = 0, last = 0, elapsed = 0, paused = reduced.matches;
  let width = innerWidth, height = innerHeight;
  const portraits = [], flights = [];
  const scriptUrl = document.currentScript.src;
  const colors = ['#d9859c', '#b59ad9', '#e9bc6a', '#84bdb3'];
  const revealKey = 'victor-birthday-revealed-v2', launchKey = 'victor-birthday-launch-v2';
  let revealed = root.dataset.theme === 'birthday';
  function confetti() {
    if (reduced.matches) return;
    const burst = document.createElement('div'); burst.className = 'birthday-confetti';
    burst.setAttribute('aria-hidden', 'true'); document.body.append(burst);
    for (let i = 0; i < 28; i++) {
      const piece = document.createElement('i'), left = i % 2 === 0;
      piece.style.cssText = `left:${left ? 8 : 92}%;top:30%;background:${colors[i % colors.length]}`;
      burst.append(piece);
      const dx = (left ? 1 : -1) * (40 + Math.random() * Math.min(300, width * .4));
      const dy = 120 + Math.random() * 190, spin = Math.random() * 500 - 250;
      piece.animate([
        {transform: 'translate(0,0) rotate(0deg)', opacity: 0},
        {transform: `translate(${dx * .45}px,-${35 + Math.random() * 80}px) rotate(${spin * .4}deg)`, opacity: .85, offset: .25},
        {transform: `translate(${dx}px,${dy}px) rotate(${spin}deg)`, opacity: 0}
      ], {duration: 1800 + Math.random() * 900, delay: Math.random() * 180, easing: 'cubic-bezier(.2,.6,.4,1)', fill: 'both'});
    }
    setTimeout(() => burst.remove(), 3000);
  }
  function launch(broadcast = true) {
    if (revealed || window.WORKSPACE_THEME !== 'birthday') return;
    revealed = true; elapsed = 0;
    try {
      sessionStorage.setItem(revealKey, 'true');
      sessionStorage.setItem('victor-birthday-entry-v2', String(Date.now()));
      if (broadcast) localStorage.setItem(launchKey, String(Date.now()));
    } catch (_) { /* The surprise also works without browser storage. */ }
    root.dataset.theme = 'birthday';
    greeting.classList.add('is-visible');
    render(0); sync(); confetti();
  }
  document.addEventListener('click', () => {
    let restored = false;
    try { restored = localStorage.getItem(key) === 'original'; } catch (_) {}
    if (!restored) launch();
  }, {once: true, capture: true});
  for (let i = 0; i < 10; i++) {
    const el = document.createElement('img'); el.className = 'victor-floater';
    el.src = new URL('victor-cartoon.png', scriptUrl).href; el.alt = ''; el.draggable = false;
    scene.append(el);
    portraits.push({el, x: Math.random() * width, y: Math.random() * height, vx: (Math.random() - .5) * 15, vy: (Math.random() - .5) * 12, size: 48 + Math.random() * 30, phase: Math.random() * 6});
  }
  for (let i = 0; i < 3; i++) {
    const el = document.createElement('div'); el.className = 'birthday-flight';
    el.innerHTML = `<svg viewBox="0 0 330 220" aria-hidden="true"><g stroke="#ad8397" stroke-width="1" fill="none"><path d="M45 70Q35 125 72 158M110 43Q130 110 110 158M220 43Q200 110 220 158M285 70Q300 125 258 158"/></g>${[45,110,220,285].map((x,j) => `<g fill="${colors[(i+j)%4]}"><ellipse cx="${x}" cy="${j===1||j===2 ? 36 : 60}" rx="25" ry="33"/><path d="M${x-4} ${j===1||j===2 ? 71 : 95}l4 -8l4 8Z"/><ellipse cx="${x-9}" cy="${j===1||j===2 ? 25 : 49}" rx="5" ry="10" fill="#fff" opacity=".32"/></g>`).join('')}<path d="M12 156L38 166L12 198L53 194L53 159ZM318 156L292 166L318 198L277 194L277 159Z" fill="#bb7e9c"/><rect x="36" y="146" width="258" height="48" rx="5" fill="#fff5e7" stroke="#d6a8b9"/><text x="165" y="176" text-anchor="middle" fill="#714368" font-family="Georgia,serif" font-size="21">Happy Birthday Victor</text></svg>`;
    scene.append(el); flights.push({el, x: 0, y: 0, phase: i * 2.1});
  }
  function render(dt) {
    elapsed += dt;
    flights.forEach((f, i) => {
      const period = 48 + i * 8;
      const progress = (elapsed < 8 ? elapsed / 8 * .28 : .28 + (elapsed - 8) / period) % 1;
      f.x = width * (.2 + i * .3) - 165 + Math.sin(elapsed * .13 + f.phase) * Math.min(100, width * .12);
      f.y = height + 40 - progress * (height + 320);
      f.el.style.transform = `translate(${f.x}px,${f.y}px) rotate(${Math.sin(elapsed * .2 + f.phase)*4}deg)`;
    });
    portraits.forEach(p => {
      p.vx += Math.sin(elapsed * .25 + p.phase) * dt * 2;
      p.vy += Math.cos(elapsed * .2 + p.phase) * dt * 2;
      flights.forEach(f => {
        const dx = (p.x + p.size/2) - (f.x + 165), dy = (p.y + p.size/2) - (f.y + 110);
        const distance = Math.hypot(dx / 185, dy / 145);
        if (distance < 1.3) {
          const length = Math.hypot(dx, dy) || 1, force = (1.3 - distance) * 115;
          p.vx += (dx / length || 1) * force * dt; p.vy += dy / length * force * dt;
        }
      });
      const damping = Math.exp(-dt * .45); p.vx *= damping; p.vy *= damping;
      p.x += p.vx * dt; p.y += p.vy * dt;
      // Wrap outside the viewport for continuous free floating.
      if (p.x < -100) p.x = width + 20; if (p.x > width + 100) p.x = -80;
      if (p.y < -100) p.y = height + 20; if (p.y > height + 100) p.y = -80;
      p.el.style.width = `${p.size}px`;
      p.el.style.transform = `translate(${p.x}px,${p.y}px) rotate(${Math.sin(elapsed * .25 + p.phase)*10}deg)`;
    });
    if (flights.some(f => f.y < height - 180)) greeting.classList.add('is-visible');
  }
  function tick(now) {
    frame = 0;
    if (root.dataset.theme !== 'birthday' || paused || document.hidden) return;
    render(last ? Math.min((now-last)/1000, .05) : 0); last = now;
    frame = requestAnimationFrame(tick);
  }
  function sync() {
    cancelAnimationFrame(frame); frame = 0; last = 0;
    document.getElementById('birthday-motion').textContent = paused ? 'Play animation' : 'Pause animation';
    if (root.dataset.theme === 'birthday' && !paused && !document.hidden) frame = requestAnimationFrame(tick);
  }
  window.setWorkspaceTheme = theme => {
    if (theme === 'birthday' && !revealed) launch();
    root.dataset.theme = theme === 'birthday' ? 'birthday' : 'original';
    if (theme !== 'birthday') {
      revealed = false;
      document.querySelectorAll('.birthday-confetti').forEach(el => el.remove());
      try { sessionStorage.removeItem(revealKey); } catch (_) {}
    }
    try { localStorage.setItem(key, root.dataset.theme); } catch (_) { /* Storage may be disabled. */ }
    sync();
  };
  document.getElementById('birthday-restore').onclick = () => window.setWorkspaceTheme('original');
  document.getElementById('birthday-motion').onclick = () => { paused = !paused; sync(); };
  addEventListener('storage', e => {
    if (e.key === launchKey) launch(false);
    if (e.key === key) {
      if (e.newValue === 'birthday') launch(false);
      else { revealed = false; root.dataset.theme = 'original'; document.querySelectorAll('.birthday-confetti').forEach(el => el.remove()); try { sessionStorage.removeItem(revealKey); } catch (_) {} }
      sync();
    }
  });
  addEventListener('resize', () => { width = innerWidth; height = innerHeight; render(0); });
  document.addEventListener('visibilitychange', sync);
  reduced.addEventListener('change', e => { paused = e.matches; if (paused) greeting.classList.add('is-visible'); sync(); });
  render(0);
  if (paused || revealed) greeting.classList.add('is-visible');
  try {
    const entry = Number(sessionStorage.getItem('victor-birthday-entry-v2'));
    sessionStorage.removeItem('victor-birthday-entry-v2');
    if (revealed && Date.now() - entry < 2500) confetti();
  } catch (_) {}
  sync();
})();
