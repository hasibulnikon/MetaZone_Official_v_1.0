// appearance.js — Settings > Theme page.
//
// v0.9.8: presets (Dark / Light / Neon) + Customize. Presets are fixed
// palettes and never touch the user's own colors; Customize shows the
// background + accent pickers and stores its own colors separately
// (theme_custom_bg / theme_custom_accent). See theme.js for why.
//
// Everything here saves as it applies -- there's no separate "Apply
// Theme" button any more, so what's showing is always what's saved.
const BG_PRESETS_CUSTOM = ['#000000', '#1c1c1c', '#4a4a4a', '#0a0e17', '#111a2e', '#ffffff', '#f5f3ee', '#e7e9ec'];
const ACCENT_PRESETS = ['#4caf7d', '#e5686b', '#a259e6', '#e6599e', '#8b6fe6', '#e6a24c', '#5b8cff', '#00bfa5'];

const themeModeRow = document.getElementById('themeModeRow');
const customizeSection = document.getElementById('customizeSection');
const bgSwatches = document.getElementById('bgSwatches');
const accentSwatches = document.getElementById('accentSwatches');
const bgHexInput = document.getElementById('bgHexInput');
const accentHexInput = document.getElementById('accentHexInput');
const bgColorPicker = document.getElementById('bgColorPicker');
const accentColorPicker = document.getElementById('accentColorPicker');

// The Customize colors currently in effect (always valid #rrggbb).
const customState = { bg: CUSTOM_FALLBACK.bg, accent: CUSTOM_FALLBACK.accent };

function setThemeModeButtons(mode) {
  themeModeRow.querySelectorAll('.theme-mode-btn').forEach(btn => {
    const on = btn.dataset.mode === mode;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  customizeSection.hidden = mode !== 'custom';
}

function syncCustomInputs() {
  bgHexInput.value = customState.bg;
  accentHexInput.value = customState.accent;
  bgColorPicker.value = customState.bg;
  accentColorPicker.value = customState.accent;
}

function applyCustomLive() {
  ThemeManager.applyCustom(customState.bg, customState.accent);
}

let saveTimer = null;
function persistCustom() {
  // Debounced: the native color picker fires 'input' continuously while
  // dragging, and each save is a full prefs.json write.
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    pywebview.api.save_prefs({
      theme_mode: 'custom',
      theme_custom_bg: customState.bg,
      theme_custom_accent: customState.accent,
    });
  }, 250);
}

function setCustomColor(kind, hex, { save = true } = {}) {
  const norm = ThemeColor.normalizeHex(hex);
  if (!norm) { syncCustomInputs(); return; }   // invalid text -> snap back to the last good value
  customState[kind] = norm;
  syncCustomInputs();
  applyCustomLive();
  if (save) persistCustom();
}

function buildSwatches(container, colors, kind) {
  container.innerHTML = '';
  colors.forEach(hex => {
    const dot = document.createElement('button');
    dot.type = 'button';
    dot.className = 'swatch';
    dot.style.background = hex;
    dot.title = hex;
    dot.addEventListener('click', () => setCustomColor(kind, hex));
    container.appendChild(dot);
  });
}
buildSwatches(bgSwatches, BG_PRESETS_CUSTOM, 'bg');
buildSwatches(accentSwatches, ACCENT_PRESETS, 'accent');

bgHexInput.addEventListener('change', () => setCustomColor('bg', bgHexInput.value));
accentHexInput.addEventListener('change', () => setCustomColor('accent', accentHexInput.value));
// 'input' = live preview while dragging in the native picker (not saved
// yet); 'change' = the user let go / closed it, so persist.
bgColorPicker.addEventListener('input', () => setCustomColor('bg', bgColorPicker.value, { save: false }));
bgColorPicker.addEventListener('change', () => setCustomColor('bg', bgColorPicker.value));
accentColorPicker.addEventListener('input', () => setCustomColor('accent', accentColorPicker.value, { save: false }));
accentColorPicker.addEventListener('change', () => setCustomColor('accent', accentColorPicker.value));

themeModeRow.querySelectorAll('.theme-mode-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const mode = btn.dataset.mode;
    if (mode === 'custom') {
      // First time in Customize (nothing saved yet): start from what's
      // on screen right now so the look doesn't jump when it's chosen.
      const res = await pywebview.api.get_prefs();
      const r = ThemeManager.resolve(res && res.ok ? res.prefs : {});
      const cs = getComputedStyle(document.documentElement);
      customState.bg = r.customBg || ThemeColor.normalizeHex(cs.getPropertyValue('--bg1')) || CUSTOM_FALLBACK.bg;
      customState.accent = r.customAccent || ThemeColor.normalizeHex(cs.getPropertyValue('--accent')) || CUSTOM_FALLBACK.accent;
      syncCustomInputs();
      applyCustomLive();
      setThemeModeButtons('custom');
      clearTimeout(saveTimer);
      await pywebview.api.save_prefs({
        theme_mode: 'custom',
        theme_custom_bg: customState.bg,
        theme_custom_accent: customState.accent,
      });
      return;
    }
    // Presets are self-contained: apply the fixed palette and remember
    // only the mode. The user's Customize colors are left untouched, so
    // they're still there next time Customize is picked.
    clearTimeout(saveTimer);
    ThemeManager.applyPreset(mode);
    setThemeModeButtons(mode);
    await pywebview.api.save_prefs({ theme_mode: mode });
  });
});

onPywebviewReady(async () => {
  const res = await pywebview.api.get_prefs();
  if (res && res.ok) {
    const r = ThemeManager.resolve(res.prefs);
    customState.bg = r.customBg || CUSTOM_FALLBACK.bg;
    customState.accent = r.customAccent || CUSTOM_FALLBACK.accent;
    syncCustomInputs();
    setThemeModeButtons(r.mode);
  } else {
    syncCustomInputs();
    setThemeModeButtons('dark');
  }
});
