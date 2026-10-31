/* Runs before first paint: a hosted app never flashes a standalone canvas while the shell's
 * appearance message is on its way. */
try { if (window.parent !== window) document.documentElement.classList.add('wz-hosted'); }
catch (e) { document.documentElement.classList.add('wz-hosted'); }
