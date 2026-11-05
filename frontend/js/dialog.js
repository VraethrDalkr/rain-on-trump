// Shared modal behaviour: keep Tab inside an open dialog.

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Keydown handler body: wraps Tab / Shift+Tab around the dialog's visible
 * focusable elements. `start` is an extra element (e.g. a heading focused on
 * open) that counts as the beginning for Shift+Tab.
 */
export function trapTab(container, e, start = null) {
  if (e.key !== 'Tab') return;
  const items = [...container.querySelectorAll(FOCUSABLE)].filter((el) => !el.closest('[hidden]'));
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || active === start || !container.contains(active))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (active === last || !container.contains(active))) {
    e.preventDefault();
    first.focus();
  }
}

const SWIPE_ZONE_PX = 60;
const SWIPE_CLOSE_PX = 80;

/**
 * Bottom-sheet behaviour shared by the location and settings sheets: one
 * backdrop, focus moves to `heading` on open and back to the trigger on close,
 * Tab stays inside, Escape / backdrop tap / swipe down from the handle close it.
 *
 * @param {{el: HTMLElement, backdrop: HTMLElement, heading: HTMLElement,
 *          closeBtn: HTMLElement, onOpen?: () => void, onClose?: () => void}} o
 */
export function createSheet({ el, backdrop, heading, closeBtn, onOpen, onClose }) {
  let returnFocus = null;
  let closing = null;
  const isOpen = () => !el.hidden && !closing;
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function onAnimEnd(e) {
    if (e.target === el) finishClose();
  }

  function finishClose() {
    el.removeEventListener('animationend', onAnimEnd);
    clearTimeout(closing);
    closing = null;
    el.classList.remove('closing');
    backdrop.classList.remove('closing');
    el.hidden = true;
    backdrop.hidden = true;
    el.style.transform = '';
  }

  function close() {
    if (!isOpen()) return;
    if (onClose) onClose();
    if (returnFocus && returnFocus.focus) returnFocus.focus();
    returnFocus = null;
    if (reduced()) {
      finishClose();
      return;
    }
    // Slide down from wherever it is (a swipe leaves an inline transform),
    // then hide. The timer is the fallback if animationend never fires.
    el.classList.add('closing');
    backdrop.classList.add('closing');
    el.addEventListener('animationend', onAnimEnd);
    closing = setTimeout(finishClose, 400);
  }

  function open(trigger) {
    if (closing) finishClose();
    if (isOpen()) return;
    returnFocus = trigger || document.activeElement;
    if (onOpen) onOpen();
    backdrop.hidden = false;
    el.hidden = false;
    el.scrollTop = 0;
    heading.focus();
  }

  closeBtn.addEventListener('click', close);
  backdrop.addEventListener('click', () => isOpen() && close());
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    } else {
      trapTab(el, e, heading);
    }
  });

  let startY = null;
  let dy = 0;
  el.addEventListener('touchstart', (e) => {
    // Only from the handle area, so the content can still scroll
    if (e.touches[0].clientY - el.getBoundingClientRect().top < SWIPE_ZONE_PX) {
      startY = e.touches[0].clientY;
    }
  }, { passive: true });
  el.addEventListener('touchmove', (e) => {
    if (startY === null) return;
    dy = e.touches[0].clientY - startY;
    if (dy > 0) el.style.transform = `translateY(${dy}px)`;
  }, { passive: true });
  el.addEventListener('touchend', () => {
    if (startY === null) return;
    if (dy > SWIPE_CLOSE_PX) close(); // slides on from the dragged position
    else el.style.transform = '';
    startY = null;
    dy = 0;
  });

  return { open, close, isOpen };
}
