/**
 * Character morph helpers: the "a secret turns into a placeholder" effect used
 * by the hero terminal and the See it work demo.
 *
 * Pure and deterministic: the scramble glyph for a position depends only on
 * the character index and the frame number, so a test (or a screenshot) sees
 * the same frame every time. No timers live here; callers step `frame`.
 */

const GLYPHS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789#%&*+=<>';

/** The scramble glyph at character `index` on `frame`. */
export function scrambleChar(index, frame) {
  return GLYPHS[(index * 31 + frame * 17 + 7) % GLYPHS.length];
}

/** `text` with every non-space character scrambled for `frame`. */
export function scramble(text, frame) {
  return Array.from(text, (ch, i) => (ch === ' ' ? ' ' : scrambleChar(i, frame))).join('');
}

/**
 * One frame of a morph from `from` to `to`, `frame` in 0..`total`.
 *
 * The length eases from `from.length` to `to.length`, and characters resolve
 * into `to` left to right; everything not yet resolved is scrambled. Frame
 * `total` is exactly `to`.
 */
export function morphText(from, to, frame, total) {
  const t = total <= 0 ? 1 : Math.min(1, Math.max(0, frame / total));
  const length = Math.round(from.length + (to.length - from.length) * t);
  const resolved = Math.floor(to.length * t);
  let out = '';
  for (let i = 0; i < length; i += 1) out += i < resolved ? to[i] : scrambleChar(i, frame);
  return out;
}
