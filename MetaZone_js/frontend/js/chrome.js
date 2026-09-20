// Global chrome shared across every page, matching the original app's
// persistent top bar.
//
// v0.8.4: the bottom status bar (done/failed/pending pills + ExifTool/
// Drag&Drop line) was removed from the UI per Hasib's explicit request.
// The live counts it displayed are still tracked here (liveCounts) in
// case a future page wants them, and get_status_bar() is still called
// once at startup so btnStopAll's initial visibility stays correct if
// a batch was already running -- but nothing renders the removed pills
// or ExifTool text anymore.

const btnStopAll = document.getElementById('btnStopAll');

let liveCounts = { done: 0, failed: 0, pending: 0 };

// Real counts, driven by the same card_update events the Meta
// Generator grid already listens to -- kept for any page that wants
// them later, not rendered anywhere right now.
BackendEvents.on('card_update', (p) => {
  if (p.result.status === 'done') liveCounts.done++;
  else if (p.result.status === 'failed') liveCounts.failed++;
  if (liveCounts.pending > 0) liveCounts.pending--;
});

BackendEvents.on('task_progress', (p) => {
  const remaining = Math.max((p.total || 0) - (p.done || 0), 0);
  if (remaining >= 0 && p.total) liveCounts.pending = remaining;
  btnStopAll.style.display = 'inline-block';
});

BackendEvents.on('task_completed', () => { btnStopAll.style.display = 'none'; });
BackendEvents.on('embed_completed', () => { btnStopAll.style.display = 'none'; });

async function loadStatusBar() {
  const res = await pywebview.api.get_status_bar();
  if (!res.ok) return;
  btnStopAll.style.display = res.meta_running ? 'inline-block' : 'none';
}

btnStopAll.addEventListener('click', async () => {
  await pywebview.api.stop_generation();
  btnStopAll.style.display = 'none';
});

document.getElementById('btnRefresh').addEventListener('click', () => {
  loadStatusBar();
  // v0.9.7.2 (Problem C / section 9): this used to ONLY touch the
  // status bar / whichever page's own refresh happened to be visible
  // -- it never refreshed API key/provider state at all, so it could
  // not have kept the left-panel API info box in sync no matter how
  // many times it was pressed. refreshKeySummary() is cheap (one
  // get_active_keys_summary() call, same one the box already made at
  // startup) and safe to call unconditionally regardless of which
  // page is showing. If the Settings/API Manager page happens to be
  // the one currently visible, also re-run its own loadProviders() so
  // both surfaces reflect the same refreshed state at the same time,
  // per the "must remain synchronized" requirement -- this does NOT
  // touch media cards or re-run any generation/import work, so it
  // carries none of the perf risk a blanket "refresh everything"
  // button would.
  if (typeof refreshKeySummary === 'function') refreshKeySummary();
  const visible = document.querySelector('.page:not([hidden])');
  if (visible && visible.id === 'page-dashboard' && typeof loadDashboard === 'function') loadDashboard();
  if (visible && visible.id === 'page-settings' && typeof loadProviders === 'function') loadProviders();
});

loadStatusBar();
