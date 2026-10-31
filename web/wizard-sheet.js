/* Wizard UI kit · the one pop-up (APP-DESIGN-RULES.md rule 4).
 * Use: <dialog class="wz-sheet"><h2>Title</h2>…</dialog>, then WzSheet.open(dialog).
 * Adds the grip and × if missing, and closes five ways: ×, Esc (native), tap outside, Back
 * and a downward swipe on the grip. Back: in an app that uses WizardApp, each open sheet is pushed on WizardApp.back
 * (the shell asks the app client first); window.__wizardCloseOverlay stays as the fallback for apps without it. No dependencies. */
(() => {
  'use strict';
  const open = [];
  function wire(d) {
    if (d.__wz) return d; d.__wz = true;
    if (!d.querySelector(':scope > .wz-sheet-grip')) d.insertAdjacentHTML('afterbegin', '<div class="wz-sheet-grip" aria-hidden="true"></div>');
    if (!d.querySelector(':scope > .wz-sheet-x')) d.querySelector(':scope > .wz-sheet-grip').insertAdjacentHTML('afterend', '<button type="button" class="wz-sheet-x" aria-label="Close">×</button>');
    d.addEventListener('click', e => {
      if (e.target.closest('.wz-sheet-x')) return close(d);
      if (e.target === d) { const r = d.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) close(d); }
    });
    d.addEventListener('close', () => { const i = open.indexOf(d); if (i >= 0) open.splice(i, 1); if (d.__wzBack) { d.__wzBack.release(); d.__wzBack = null; } });
    let y0 = null; const g = d.querySelector('.wz-sheet-grip');
    g.addEventListener('pointerdown', e => { y0 = e.clientY; g.setPointerCapture(e.pointerId); });
    g.addEventListener('pointermove', e => { if (y0 != null) d.style.transform = `translateY(${Math.max(0, e.clientY - y0)}px)`; });
    const end = e => { if (y0 == null) return; const dy = e.clientY - y0; y0 = null; d.style.transform = ''; if (dy > 70) close(d); };
    g.addEventListener('pointerup', end); g.addEventListener('pointercancel', end);
    return d;
  }
  function show(d) {
    wire(d); if (!d.open) d.showModal();
    if (!open.includes(d)) open.push(d);
    const B = window.WizardApp && window.WizardApp.back;
    if (!d.__wzBack && B && typeof B.push === 'function') { try { d.__wzBack = B.push(() => { close(d); return true; }); } catch (_) {} }
    return d;
  }
  function close(d) { if (d && d.open) d.close(); }
  const prev = window.__wizardCloseOverlay;
  window.__wizardCloseOverlay = () => { const d = open.pop(); if (d) { close(d); return true; } return typeof prev === 'function' ? !!prev() : false; };
  window.WzSheet = { open: show, close, wire };
})();
