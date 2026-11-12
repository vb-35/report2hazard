// Small shared navigation panel for both 3D renderers.
export function terrainControls(container, navigate) {
  const panel = document.createElement('details'); panel.className = 'terrain-navigation';
  const group = (label, actions) => `<div class="terrain-navigation-group"><span>${label}</span><div>${actions.map(([action,text,title])=>`<button type="button" data-action="${action}" aria-label="${title}" title="${title}">${text}</button>`).join('')}</div></div>`;
  panel.innerHTML = `<summary title="Expand or collapse 3D controls">3D controls <span aria-hidden="true">&#8984;</span></summary><div class="terrain-navigation-body">` +
    group('Rotate', [['rotate-left','&#8630;','Rotate left'],['rotate-right','&#8631;','Rotate right']]) +
    group('Tilt', [['tilt-down','&#8601;','View from above'],['tilt-up','&#8599;','View toward horizon']]) +
    group('Pan', [['pan-left','&#8592;','Pan left'],['pan-up','&#8593;','Pan up'],['pan-down','&#8595;','Pan down'],['pan-right','&#8594;','Pan right']]) +
    group('Zoom', [['zoom-in','+','Zoom in'],['zoom-out','&minus;','Zoom out']]) +
    group('View', [['north','N','Align to true north'],['reset','Reset','Reset view']]) +
    `<p>Left-drag: rotate & tilt<br>Right-drag: pan<br>Scroll: zoom</p></div>`;
  panel.addEventListener('click', e => { const action = e.target.closest('[data-action]')?.dataset.action; if (action) { e.preventDefault(); navigate(action); } });
  container.append(panel); return panel;
}

// Keep MapLibre's pinch and wheel controls; match OrbitControls for mouse drags.
export function terrainMouseControls(map) {
  map.dragPan.disable(); map.dragRotate.disable(); map.boxZoom.disable();
  const canvas = map.getCanvas(), listeners = new AbortController();
  let drag = null;
  const on = (name, handler) => canvas.addEventListener(name, handler, {signal:listeners.signal});
  on('contextmenu', e => e.preventDefault());
  on('pointerdown', e => {
    if (e.pointerType !== 'mouse' || ![0,2].includes(e.button)) return;
    e.preventDefault(); e.stopPropagation(); map.stop();
    drag = {id:e.pointerId,button:e.button,x:e.clientX,y:e.clientY};
    canvas.setPointerCapture(e.pointerId);
  });
  on('pointermove', e => {
    if (!drag || drag.id !== e.pointerId) return;
    e.preventDefault(); e.stopPropagation();
    const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
    drag.x=e.clientX; drag.y=e.clientY;
    if (drag.button === 2) map.panBy([-dx,-dy],{duration:0});
    else map.jumpTo({bearing:map.getBearing()+dx*.35,pitch:Math.max(0,Math.min(80,map.getPitch()-dy*.3))});
  });
  const stop = e => { if (drag?.id === e.pointerId) { drag=null; if(canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId); } };
  on('pointerup',stop); on('pointercancel',stop); on('lostpointercapture',()=>drag=null);
  return () => listeners.abort();
}
