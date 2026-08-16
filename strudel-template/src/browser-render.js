/**
 * Browser-side Strudel renderer for Bench Marker.
 * Exposed as window.BenchStrudel with init() + renderToBase64Wav(code, opts).
 */
import {
  evalScope,
  silence,
  stack,
  getCps,
} from '@strudel/core';
import { transpiler } from '@strudel/transpiler';
import * as strudelMini from '@strudel/mini';
import * as strudelTonal from '@strudel/tonal';
import * as strudelCore from '@strudel/core';
import {
  samples,
  registerSynthSounds,
  registerZZFXSounds,
  initAudio,
  getAudioContext,
  setAudioContext,
  setSuperdoughAudioController,
  resetGlobalEffects,
  superdough,
  webaudioRepl,
  webaudioOutput,
} from '@strudel/webaudio';
import { SuperdoughAudioController } from 'superdough/superdoughoutput.mjs';

let ready = false;
let initError = null;
let replApi = null;

function audioBufferToWav(buffer) {
  const numChannels = buffer.numberOfChannels;
  const sampleRate = buffer.sampleRate;
  const format = 1; // PCM
  const bitDepth = 16;
  const bytesPerSample = bitDepth / 8;
  const blockAlign = numChannels * bytesPerSample;

  let samplesData;
  if (numChannels === 2) {
    const left = buffer.getChannelData(0);
    const right = buffer.getChannelData(1);
    samplesData = new Float32Array(left.length * 2);
    for (let i = 0, j = 0; i < left.length; i++, j += 2) {
      samplesData[j] = left[i];
      samplesData[j + 1] = right[i];
    }
  } else {
    samplesData = buffer.getChannelData(0);
  }

  const dataLength = samplesData.length * bytesPerSample;
  const arrayBuffer = new ArrayBuffer(44 + dataLength);
  const view = new DataView(arrayBuffer);

  const writeString = (offset, str) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  };

  writeString(0, 'RIFF');
  view.setUint32(4, 36 + dataLength, true);
  writeString(8, 'WAVE');
  writeString(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, format, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitDepth, true);
  writeString(36, 'data');
  view.setUint32(40, dataLength, true);

  let offset = 44;
  for (let i = 0; i < samplesData.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samplesData[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return arrayBuffer;
}

function bufferToBase64(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  const chunk = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function initEngine() {
  if (ready) return { ok: true };
  if (initError) return { ok: false, error: initError };

  try {
    await evalScope(
      Promise.resolve(strudelCore),
      Promise.resolve(strudelMini),
      Promise.resolve(strudelTonal),
      import('@strudel/webaudio'),
    );

    globalThis.stack = stack;
    globalThis.silence = silence;

    await registerSynthSounds();
    await registerZZFXSounds();

    // Built-in drum/instrument banks (CDN). Failure is non-fatal for synth-only songs.
    const sampleLoads = [
      samples('github:tidalcycles/dirt-samples'),
      samples('https://strudel.b-cdn.net/uzu-drumkit.json', 'https://strudel.b-cdn.net/uzu-drumkit/', {
        prebake: true,
      }),
      samples('https://strudel.b-cdn.net/piano.json', 'https://strudel.b-cdn.net/piano/', {
        prebake: true,
      }),
    ];
    const results = await Promise.allSettled(sampleLoads);
    const sampleErrors = results
      .filter((r) => r.status === 'rejected')
      .map((r) => String(r.reason?.message || r.reason));
    if (sampleErrors.length) {
      console.warn('[bench-strudel] sample preload warnings:', sampleErrors);
    }

    // Warm a real AudioContext so OfflineAudioContext swap works later
    await initAudio({ disableWorklets: false });

    const audioContext = getAudioContext();
    replApi = webaudioRepl({
      transpiler,
      defaultOutput: webaudioOutput,
      getTime: () => audioContext.currentTime,
      onEvalError: (err) => console.warn('[bench-strudel] eval error', err),
    });

    ready = true;
    return { ok: true, sampleWarnings: sampleErrors };
  } catch (err) {
    initError = String(err?.message || err);
    console.error('[bench-strudel] init failed', err);
    return { ok: false, error: initError };
  }
}

/**
 * Evaluate Strudel user code and render offline audio → base64 WAV.
 *
 * Duration: prefer opts.durationSec (default 16s). Cycles are derived from the
 * pattern's cps so setcpm(120) still yields a full-length clip.
 */
async function renderToBase64Wav(code, opts = {}) {
  const sampleRate = Number(opts.sampleRate ?? 44100);
  const maxPolyphony = Number(opts.maxPolyphony ?? 128);
  const forcedCps = opts.cps != null ? Number(opts.cps) : null;
  const targetDurationSec = Number(opts.durationSec ?? 16);
  const forcedCycles = opts.cycles != null ? Number(opts.cycles) : null;

  if (!code || !String(code).trim()) {
    return { ok: false, error: 'Empty Strudel code' };
  }

  const init = await initEngine();
  if (!init.ok) return init;
  if (!replApi) return { ok: false, error: 'REPL not initialised' };

  // Evaluate without autoplay — pattern is stored on repl state
  let pattern;
  try {
    // hush + evaluate; autostart=false so we don't schedule live audio
    const result = await replApi.evaluate(String(code), false, true);
    pattern = result || replApi.state?.pattern;
  } catch (err) {
    return { ok: false, error: `Evaluate failed: ${err?.message || err}` };
  }

  if (!pattern || typeof pattern.queryArc !== 'function') {
    return {
      ok: false,
      error: 'Code did not produce a playable pattern. Use `$:` lines (e.g. `$: s("bd sd")`) or return a Pattern.',
    };
  }

  // Prefer cps from setcpm/setcps in the user code
  let cps = forcedCps;
  if (cps == null || !Number.isFinite(cps) || cps <= 0) {
    try {
      const fromScheduler = Number(replApi.scheduler?.cps);
      if (fromScheduler > 0) cps = fromScheduler;
    } catch {
      /* ignore */
    }
  }
  if (cps == null || !Number.isFinite(cps) || cps <= 0) {
    try {
      const g = Number(getCps?.());
      if (g > 0) cps = g;
    } catch {
      /* ignore */
    }
  }
  if (cps == null || !Number.isFinite(cps) || cps <= 0) {
    cps = 0.5;
  }

  // Stop any live scheduler before offline render
  try {
    replApi.stop?.();
  } catch {
    /* ignore */
  }

  let cycles;
  if (forcedCycles != null && Number.isFinite(forcedCycles) && forcedCycles > 0) {
    cycles = forcedCycles;
  } else {
    const target = Number.isFinite(targetDurationSec) && targetDurationSec > 0 ? targetDurationSec : 16;
    cycles = Math.max(4, Math.ceil(target * cps));
  }

  const begin = 0;
  const end = begin + cycles;
  const durationSec = (end - begin) / cps;
  if (durationSec <= 0 || durationSec > 90) {
    return {
      ok: false,
      error: `Invalid render duration ${durationSec.toFixed(2)}s (cycles=${cycles}, cps=${cps}). Cap is 90s.`,
    };
  }

  try {
    const live = getAudioContext();
    if (live && typeof live.close === 'function' && live.state !== 'closed') {
      await live.close();
    }
  } catch {
    /* ignore */
  }

  const frameCount = Math.max(1, Math.floor(durationSec * sampleRate));
  const offline = new OfflineAudioContext(2, frameCount, sampleRate);
  setAudioContext(offline);
  setSuperdoughAudioController(new SuperdoughAudioController(offline));
  await initAudio({ maxPolyphony, multiChannelOrbits: false });

  const haps = pattern
    .queryArc(begin, end, { _cps: cps })
    .sort((a, b) => a.whole.begin.valueOf() - b.whole.begin.valueOf());

  let hapCount = 0;
  for (const hap of haps) {
    if (!hap.hasOnset()) continue;
    try {
      hap.ensureObjectValue();
      await superdough(
        hap.value,
        (hap.whole.begin.valueOf() - begin) / cps,
        hap.duration / cps,
        cps,
        (hap.whole?.begin.valueOf() - begin) / cps,
      );
      hapCount += 1;
    } catch (err) {
      console.warn('[bench-strudel] hap error', err);
    }
  }

  if (hapCount === 0) {
    try {
      setAudioContext(null);
      setSuperdoughAudioController(null);
      resetGlobalEffects();
    } catch {
      /* ignore */
    }
    // Re-init live context for subsequent runs on same page
    try {
      await initAudio({});
      ready = false; // force repl rebuild next time if needed
      initError = null;
    } catch {
      /* ignore */
    }
    return {
      ok: false,
      error: 'Pattern produced zero notes in the render window. Check sounds, cps, and pattern length.',
      meta: { cycles, cps, durationSec, hapCount: 0 },
    };
  }

  let renderedBuffer;
  try {
    renderedBuffer = await offline.startRendering();
  } catch (err) {
    return { ok: false, error: `Offline render failed: ${err?.message || err}` };
  } finally {
    try {
      setAudioContext(null);
      setSuperdoughAudioController(null);
      resetGlobalEffects();
    } catch {
      /* ignore */
    }
  }

  let peak = 0;
  for (let ch = 0; ch < renderedBuffer.numberOfChannels; ch++) {
    const data = renderedBuffer.getChannelData(ch);
    for (let i = 0; i < data.length; i++) {
      const a = Math.abs(data[i]);
      if (a > peak) peak = a;
    }
  }
  if (peak < 1e-5) {
    return {
      ok: false,
      error: 'Rendered audio is silent (peak ~0). Sounds may have failed to load or pattern is empty.',
      meta: { cycles, cps, durationSec, hapCount, peak },
    };
  }

  const wav = audioBufferToWav(renderedBuffer);
  const b64 = bufferToBase64(wav);

  // Page is single-use per render process; mark dirty so next evaluate re-inits if reused
  ready = false;
  replApi = null;
  initError = null;

  return {
    ok: true,
    wavBase64: b64,
    meta: {
      cycles,
      cps,
      durationSec,
      hapCount,
      peak,
      sampleRate,
      bytes: wav.byteLength,
    },
  };
}

const api = { init: initEngine, renderToBase64Wav };
if (typeof window !== 'undefined') {
  window.BenchStrudel = api;
}
export default api;
