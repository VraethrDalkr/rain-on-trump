// Renders the view model's sky (model.js → vm.sky) into #sky. Idempotent:
// particle elements are only added or removed when their counts change, so a
// refresh with the same weather never restarts the animations.

// Stars: a jittered 10×10 grid (2 per cell) plus extras, so the whole sky is
// covered evenly. Seeded, so the same sky comes back on every refresh.
const STAR_GRID = 10;
const STARS_PER_CELL = 2;
const EXTRA_STARS = 30;
const STAR_SEED = 20261104;

/** Small deterministic PRNG (mulberry32). */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function star(rand, left, top) {
  const el = document.createElement('i');
  const r = rand();
  if (r > 0.95) el.className = 'bright';
  else if (r > 0.85) el.className = 'large';
  el.style.left = `${left.toFixed(2)}%`;
  el.style.top = `${top.toFixed(2)}%`;
  el.style.animationDuration = `${(2 + rand() * 3).toFixed(2)}s`;
  el.style.animationDelay = `${(-rand() * 5).toFixed(2)}s`;
  return el;
}

function buildStars(container) {
  if (container.childElementCount) return;
  const rand = rng(STAR_SEED);
  const cell = 100 / STAR_GRID;
  const frag = document.createDocumentFragment();
  for (let col = 0; col < STAR_GRID; col++) {
    for (let row = 0; row < STAR_GRID; row++) {
      for (let k = 0; k < STARS_PER_CELL; k++) {
        frag.appendChild(star(rand, col * cell + rand() * cell, row * cell + rand() * cell));
      }
    }
  }
  for (let k = 0; k < EXTRA_STARS; k++) frag.appendChild(star(rand, rand() * 100, rand() * 100));
  container.appendChild(frag);
}

// Spread start times per element (golden-ratio steps) so particles never line
// up in rows; the CSS nth-child delays alone repeat across columns.
function setCount(container, n, spreadS = 0) {
  const kids = container.children;
  while (kids.length > n) container.lastElementChild.remove();
  if (kids.length < n) {
    const frag = document.createDocumentFragment();
    for (let i = kids.length; i < n; i++) {
      const el = document.createElement('i');
      if (spreadS) el.style.animationDelay = `${(-((i * 0.618034) % 1) * spreadS).toFixed(2)}s`;
      frag.appendChild(el);
    }
    container.appendChild(frag);
  }
}

// The attribute, not the .hidden property: SVG elements (the bolt) have no
// such property, so assigning it would silently do nothing.
function show(el, on) {
  el.toggleAttribute('hidden', !on);
}

/**
 * @param {HTMLElement} root  the #sky element
 * @param {object} sky        vm.sky from model.js
 */
export function renderSky(root, sky) {
  const q = (sel) => root.querySelector(sel);
  const fog = sky.kind === 'fog';
  const gradient = fog ? 'fog' : `${sky.time}${sky.wet ? '-wet' : ''}`;
  root.className = `sky sky-${gradient} storm-${sky.storm}`;

  const stars = q('.stars');
  if (sky.stars) buildStars(stars);
  show(stars, sky.stars);
  // The moon lives in the layout (index.html .moon-slot), not in the sky layer
  show(document.querySelector('.moon'), sky.stars);
  show(q('.sun'), sky.sun);
  show(q('.clouds'), sky.wet && !fog);
  show(q('.fog'), fog);
  show(q('.flight'), sky.kind === 'flight');
  show(q('.flash'), sky.storm !== 'none');
  show(q('.bolt'), sky.storm !== 'none');

  q('.particles').className = `particles wind-${sky.wind}`;
  setCount(q('.rain.far'), sky.drops);
  setCount(q('.rain.near'), sky.nearDrops);
  setCount(q('.snow'), sky.flakes, 12);
}
