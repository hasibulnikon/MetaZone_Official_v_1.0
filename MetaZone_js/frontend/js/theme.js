// theme.js — shared, app-wide theme system.
//
// v0.9.8: reorganised into PRESETS + CUSTOMIZE.
//
//   Presets  (dark / light / neon): each one is a *complete, fixed*
//            palette -- background, panels, text, borders AND accent.
//            Nothing the user picked elsewhere can leak into them.
//   Customize (custom): the user's own background + accent colors,
//            stored under their own prefs keys, applied only while
//            Customize is the active theme.
//
// ROOT-CAUSE FIX (Neon accent lost after restart): before v0.9.8 there
// was ONE shared pref, `theme_accent_base`, and loadAndApply() applied
// the chosen mode's palette (Neon => cyan #00e5ff) and then
// unconditionally overwrote --accent with that shared value -- i.e.
// with whatever accent had last been saved under ANY mode. Live
// switching looked right (the click handler never re-applied the saved
// accent), so the bug only showed after a restart. Presets no longer
// read any per-user accent at all, so there is nothing to overwrite
// them; the legacy theme_accent_base / theme_bg_base* keys are simply
// ignored now (they may still sit in prefs.json, harmlessly).
//
// The prefs contract:
//   theme_mode          'dark' | 'light' | 'neon' | 'custom'   (same key
//                       as before, so existing saves keep working)
//   theme_custom_bg     '#rrggbb'  (Customize only)
//   theme_custom_accent '#rrggbb'  (Customize only)

const THEME_PALETTES = {
  dark: {
    '--bg1': '#1a1c22',
    '--bg2': '#22252c',
    '--nav-bg': '#16181d',
    '--text': '#e7e9ee',
    '--text-dim': '#9aa0ac',
    '--border': '#2e323b',
    '--accent': '#5b8cff',
  },
  light: {
    '--bg1': '#ffffff',
    '--bg2': '#f4f5f7',
    '--nav-bg': '#f7f7f9',
    '--text': '#1b1d22',
    '--text-dim': '#6a6f7b',
    '--border': '#dfe1e6',
    '--accent': '#5b8cff',
  },
  // Deep navy base + cyan/teal neon accent. The gradient/glow treatment
  // lives in the [data-theme="neon"] block in base.css.
  neon: {
    '--bg1': '#0a0e17',
    '--bg2': '#0f1524',
    '--nav-bg': '#080b12',
    '--text': '#e8f4ff',
    '--text-dim': '#7c92b3',
    '--border': '#1c2b45',
    '--accent': '#00e5ff',
  },
};

const THEME_PRESET_IDS = Object.keys(THEME_PALETTES);           // dark, light, neon
const THEME_MODES = [...THEME_PRESET_IDS, 'custom'];
const CUSTOM_FALLBACK = { bg: '#1a1c22', accent: '#5b8cff' };

const ThemeColor = (() => {
  // Accepts '#abc' / '#aabbcc' (with or without '#'), returns '#rrggbb'
  // lowercase, or null when it isn't a valid hex color.
  function normalizeHex(v) {
    if (typeof v !== 'string') return null;
    let s = v.trim().toLowerCase();
    if (s[0] === '#') s = s.slice(1);
    if (/^[0-9a-f]{3}$/.test(s)) s = s.split('').map(c => c + c).join('');
    return /^[0-9a-f]{6}$/.test(s) ? '#' + s : null;
  }
  function toRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function toHex(rgb) {
    return '#' + rgb.map(c => Math.max(0, Math.min(255, Math.round(c))).toString(16).padStart(2, '0')).join('');
  }
  // t=0 -> a, t=1 -> b
  function mix(a, b, t) {
    const ra = toRgb(a), rb = toRgb(b);
    return toHex(ra.map((c, i) => c + (rb[i] - c) * t));
  }
  // WCAG relative luminance, 0 (black) .. 1 (white)
  function luminance(hex) {
    const [r, g, b] = toRgb(hex).map(c => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  return { normalizeHex, mix, luminance };
})();

// Customize: the user picks ONE background color and ONE accent; every
// other shade the UI needs (panels, nav rail, borders, text) is derived
// from the background so a light custom background gets dark text and a
// dark one gets light text, instead of leaving the dark palette's
// near-white text on top of a white page.
function buildCustomPalette(bg, accent) {
  const light = ThemeColor.luminance(bg) > 0.4;
  const base = light ? THEME_PALETTES.light : THEME_PALETTES.dark;
  const palette = light
    ? {
        '--bg1': bg,
        '--bg2': ThemeColor.mix(bg, '#000000', 0.04),
        '--nav-bg': ThemeColor.mix(bg, '#000000', 0.025),
        '--border': ThemeColor.mix(bg, '#000000', 0.13),
      }
    : {
        '--bg1': bg,
        '--bg2': ThemeColor.mix(bg, '#ffffff', 0.05),
        '--nav-bg': ThemeColor.mix(bg, '#000000', 0.18),
        '--border': ThemeColor.mix(bg, '#ffffff', 0.12),
      };
  palette['--text'] = base['--text'];
  palette['--text-dim'] = base['--text-dim'];
  palette['--accent'] = accent;
  return { palette, tone: light ? 'light' : 'dark' };
}

const ThemeManager = (() => {
  let currentMode = 'dark';

  function paint(mode, palette, tone) {
    const root = document.documentElement;
    // Every palette defines the same 7 vars, so switching theme can
    // never leave a stale value from the previous one behind.
    Object.entries(palette).forEach(([k, v]) => root.style.setProperty(k, v));
    root.dataset.theme = mode;
    root.dataset.tone = tone;
    currentMode = mode;
  }

  function applyPreset(mode) {
    const id = THEME_PALETTES[mode] ? mode : 'dark';
    paint(id, THEME_PALETTES[id], id === 'light' ? 'light' : 'dark');
  }

  function applyCustom(bg, accent) {
    const b = ThemeColor.normalizeHex(bg) || CUSTOM_FALLBACK.bg;
    const a = ThemeColor.normalizeHex(accent) || CUSTOM_FALLBACK.accent;
    const { palette, tone } = buildCustomPalette(b, a);
    paint('custom', palette, tone);
  }

  // Single place that turns saved prefs into "what should be showing".
  function resolve(prefs) {
    prefs = prefs || {};
    const mode = THEME_MODES.includes(prefs.theme_mode) ? prefs.theme_mode : 'dark';
    return {
      mode,
      customBg: ThemeColor.normalizeHex(prefs.theme_custom_bg),
      customAccent: ThemeColor.normalizeHex(prefs.theme_custom_accent),
    };
  }

  function applyFromPrefs(prefs) {
    const r = resolve(prefs);
    if (r.mode === 'custom') applyCustom(r.customBg, r.customAccent);
    else applyPreset(r.mode);
    return r;
  }

  async function loadAndApply() {
    try {
      const res = await pywebview.api.get_prefs();
      if (!res.ok) return;
      applyFromPrefs(res.prefs);
    } catch (e) {
      applyPreset('dark');
    }
  }

  return { applyPreset, applyCustom, applyFromPrefs, resolve, loadAndApply, getMode: () => currentMode };
})();

onPywebviewReady(() => ThemeManager.loadAndApply());
