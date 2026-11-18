// The About pane: what LakeBand does and how it scored, like the info panes of the other tools on this site.
(function () {
  const pane = document.getElementById('about');
  const open = document.getElementById('about-open');
  const close = document.getElementById('about-close');
  if (!pane || !open || !close) return;
  const show = (on) => {
    pane.hidden = !on;
    (on ? close : open).focus();
  };
  open.addEventListener('click', () => show(true));
  close.addEventListener('click', () => show(false));
  pane.addEventListener('click', (e) => { if (e.target === pane) show(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pane.hidden) show(false); });
})();
