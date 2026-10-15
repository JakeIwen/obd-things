/**
 * Synthesised warning chime (Web Audio; no audio files, nothing fetched).
 *
 * Browsers start audio only after a user gesture, so the context is created and resumed on the
 * first tap or key press, and resumed again on later ones if the system suspended it. Until the
 * screen has been touched once after loading, a chime is silently skipped.
 */

let context = null;

function audioContext() {
  if (context) return context;
  const Ctor = typeof window !== "undefined" ? window.AudioContext || window.webkitAudioContext : null;
  if (!Ctor) return null;
  try {
    context = new Ctor();
  } catch {
    context = null;
  }
  return context;
}

function unlock() {
  const ctx = audioContext();
  if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
}

/** Install the gesture listeners that allow audio; call once at startup. */
export function enableChimeAudio() {
  for (const type of ["pointerdown", "keydown"]) document.addEventListener(type, unlock, { capture: true, passive: true });
}

/** One note: a sine with a soft octave overtone and a bell-like decay. */
function note(ctx, frequency, start, duration) {
  const gain = ctx.createGain();
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(0.5, start + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  gain.connect(ctx.destination);
  for (const [multiple, level] of [[1, 1], [2, 0.25]]) {
    const osc = ctx.createOscillator();
    const partial = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = frequency * multiple;
    partial.gain.value = level;
    osc.connect(partial).connect(gain);
    osc.start(start);
    osc.stop(start + duration + 0.05);
  }
}

/** High-low warning chime, played twice (about 1.4 s). Returns false when audio is not allowed yet. */
export function playWarningChime() {
  const ctx = audioContext();
  if (!ctx || ctx.state !== "running") return false;
  const t = ctx.currentTime + 0.02;
  for (const offset of [0, 0.7]) {
    note(ctx, 1046.5, t + offset, 0.5);
    note(ctx, 784, t + offset + 0.25, 0.6);
  }
  return true;
}
