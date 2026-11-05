// Location sheet: where we think he is, how sure we are, how we know, and
// links (radar, map, source). Opened by the location card.

import { createSheet } from './dialog.js';

const $ = (id) => document.getElementById(id);

let vm = null;
let sheet = null;

function setLink(id, href) {
  const a = $(id);
  if (href) a.href = href;
  else a.removeAttribute('href');
}

function renderTimeline(steps) {
  $('timelineSection').hidden = steps.length === 0;
  $('timeline').replaceChildren(
    ...steps.map((s) => {
      const li = document.createElement('li');
      li.dataset.current = String(s.current);
      const text = document.createElement('div');
      const title = document.createElement('span');
      title.className = 'timeline-title';
      title.textContent = s.title;
      const meta = document.createElement('span');
      meta.className = 'timeline-meta';
      meta.textContent = s.meta;
      text.append(title, meta);
      li.append(text);
      return li;
    }),
  );
}

/** Paint the sheet from the latest view model. */
function render() {
  if (!vm) return;
  $('sheetTitle').textContent = vm.location.title;
  $('sheetSub').textContent = vm.location.detail || vm.location.meta || '';
  const conf = vm.location.confidence;
  $('confidencePanel').hidden = conf === null || conf === undefined;
  if (conf !== null && conf !== undefined) {
    $('confidenceValue').textContent = `${conf}%`;
    $('confidenceBar').style.width = `${Math.max(0, Math.min(100, conf))}%`;
    $('confidenceNote').textContent = vm.location.note || '';
  }
  renderTimeline(vm.timeline);
  setLink('linkRadar', vm.links.radar);
  setLink('linkMap', vm.links.map);
  setLink('linkSource', vm.links.source);
  $('linkSource').textContent = vm.links.sourceLabel;
}

export function initSheet() {
  const card = $('locationCard');
  sheet = createSheet({
    el: $('sheet'),
    backdrop: $('sheetBackdrop'),
    heading: $('sheetTitle'),
    closeBtn: $('sheetClose'),
    onOpen: () => {
      render();
      card.setAttribute('aria-expanded', 'true');
    },
    onClose: () => card.setAttribute('aria-expanded', 'false'),
  });
  card.setAttribute('aria-expanded', 'false');
  // Nothing to show before the first answer (the card says "Finding him…")
  card.addEventListener('click', (e) => vm && sheet.open(e.currentTarget));
  document.addEventListener('vm-change', (e) => {
    vm = e.detail;
    if (sheet.isOpen()) render();
  });
}
