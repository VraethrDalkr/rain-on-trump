// One-line status messages at the bottom of the screen.

let timer = null;

export function showToast(text, ms = 3000) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.dataset.show = 'true';
  clearTimeout(timer);
  timer = setTimeout(() => {
    el.dataset.show = 'false';
  }, ms);
}
