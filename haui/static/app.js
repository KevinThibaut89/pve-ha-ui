'use strict';

/* HA Manager — single-page UI. No framework, no build step. */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem('haui.' + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem('haui.' + key, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

const STATE_HELP = {
  started: 'Keep it running. If its node fails, HA restarts it elsewhere.',
  stopped: 'Keep it off, but still managed (it follows node failures and maintenance).',
  disabled: 'Stop it and leave it alone. Also the way out of the "error" state.',
  ignored: 'HA keeps the config but takes its hands off completely.',
};
const CRM_CLASS = {
  started: 'ok', stopped: '', disabled: '', ignored: '',
  migrate: 'info busy', relocate: 'info busy', request_stop: 'info busy', request_start: 'info busy',
  error: 'bad', fence: 'bad', recovery: 'warn busy', freeze: 'warn',
};
const CRM_LABEL = { request_stop: 'stopping', request_start: 'starting', migrate: 'migrating', relocate: 'relocating' };
const TASK_LABEL = {
  hamigrate: 'HA migrate', harelocate: 'HA relocate', hastart: 'HA start', hastop: 'HA stop',
  qmigrate: 'VM migrate', vzmigrate: 'CT migrate', haupdate: 'HA update',
};

const ui = {
  state: null,
  error: null,
  filter: store.get('filter', 'all'),
  node: '',
  q: '',
  groupByNode: store.get('groupByNode', false),
  sort: store.get('sort', { key: 'vmid', dir: 1 }),
  selected: new Set(),
  busy: new Set(),
  tab: 'overview',
  lastRows: '',
  timer: null,
  fetching: false,
};

/* ------------------------------------------------------------------ API */
class AuthError extends Error {}

async function api(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: { 'X-Requested-With': 'haui' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch('/api' + path, opts);
  } catch {
    throw new Error('Cannot reach the HA Manager server');
  }
  let payload = {};
  try { payload = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && !path.startsWith('/login')) throw new AuthError(payload.error || 'Please sign in');
  if (!res.ok) throw new Error(payload.error || `${res.status} ${res.statusText}`);
  return payload.data;
}

/* --------------------------------------------------------------- toasts */
function toast(msg, kind = 'ok', ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), ms);
}

/* --------------------------------------------------------------- dialog */
function openDialog({ title, body, okText = 'OK', danger = false, onOk, init }) {
  const dlg = $('#dlg');
  $('#dlg-title').textContent = title;
  $('#dlg-body').innerHTML = body;
  $('#dlg-error').textContent = '';
  const ok = $('#dlg-ok');
  ok.textContent = okText;
  ok.className = 'btn ' + (danger ? 'danger solid' : 'primary');
  ok.disabled = false;
  if (init) init($('#dlg-body'));

  return new Promise((resolve) => {
    const form = $('#dlg-form');
    const submit = async (e) => {
      if (e.submitter && e.submitter.value === 'cancel') return; // native close
      e.preventDefault();
      ok.disabled = true;
      try {
        const result = onOk ? await onOk($('#dlg-body')) : true;
        cleanup();
        dlg.close();
        resolve(result ?? true);
      } catch (err) {
        if (err instanceof AuthError) { cleanup(); dlg.close(); showLogin(err.message); resolve(false); return; }
        $('#dlg-error').textContent = err.message;
        ok.disabled = false;
      }
    };
    const closed = () => { cleanup(); resolve(false); };
    const cleanup = () => {
      form.removeEventListener('submit', submit);
      dlg.removeEventListener('close', closed);
    };
    form.addEventListener('submit', submit);
    dlg.addEventListener('close', closed);
    dlg.showModal();
    const first = $('#dlg-body input:not([type=hidden]):not(:disabled), #dlg-body select');
    (first || ok).focus();
  });
}

/* ---------------------------------------------------------------- login */
async function showLogin(message, info = false) {
  stopPolling();
  $('#app-view').hidden = true;
  $('#setup-view').hidden = true;
  $('#login-view').hidden = false;
  $('#login-step-password').hidden = false;
  $('#login-step-tfa').hidden = true;
  $('#login-error').textContent = message || '';
  $('#login-error').classList.toggle('info', info);
  try {
    const { realms, setup } = await api('GET', '/realms');
    if (setup) return showSetup();
    const sel = $('#login-form [name=realm]');
    const saved = store.get('realm', 'pam');
    sel.innerHTML = realms.map((r) => `<option value="${esc(r.realm)}" ${r.realm === saved ? 'selected' : ''}>${esc(r.comment)}</option>`).join('');
  } catch { /* keep the form usable */ }
  const user = $('#login-form [name=username]');
  user.value = user.value || store.get('user', '');
  (user.value ? $('#login-form [name=password]') : user).focus();
}

async function onLogin(e) {
  e.preventDefault();
  const f = e.target;
  const btn = $('button[type=submit]', f);
  $('#login-error').textContent = '';
  $('#login-error').classList.remove('info');
  btn.disabled = true;
  try {
    let res;
    if (!$('#login-step-tfa').hidden) {
      res = await api('POST', '/login/tfa', { code: f.code.value, recovery: f.recovery.checked });
    } else {
      res = await api('POST', '/login', { username: f.username.value, password: f.password.value, realm: f.realm.value });
      store.set('user', f.username.value);
      store.set('realm', f.realm.value);
    }
    if (res.need_tfa) {
      $('#login-step-password').hidden = true;
      $('#login-step-tfa').hidden = false;
      f.code.value = '';
      f.code.focus();
      return;
    }
    f.password.value = '';
    showApp();
  } catch (err) {
    $('#login-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- polling */
function showApp() {
  $('#login-view').hidden = true;
  $('#setup-view').hidden = true;
  $('#app-view').hidden = false;
  refresh();
  startPolling();
}
function startPolling() {
  stopPolling();
  ui.timer = setInterval(() => { if (!document.hidden) refresh(); }, 5000);
}
function stopPolling() {
  clearInterval(ui.timer);
  ui.timer = null;
}

async function refresh() {
  if (ui.fetching) return;
  ui.fetching = true;
  try {
    ui.state = await api('GET', '/state');
    ui.error = null;
    ui.loadedAt = Date.now();
  } catch (err) {
    if (err instanceof AuthError) { ui.fetching = false; showLogin(err.message); return; }
    ui.error = err.message;
  }
  ui.fetching = false;
  render();
}
/* After an action: refresh now and a little later, so transitions show up. */
function settle() {
  refresh();
  setTimeout(refresh, 1500);
  setTimeout(refresh, 4000);
}

/* --------------------------------------------------------------- render */
function render() {
  const s = ui.state;
  if (!s) {
    if (ui.error) setBanner('bad', `Can't load the cluster: ${ui.error}`);
    return;
  }
  renderHeader(s);
  renderBanner(s);
  renderSummary(s);
  renderNodes(s);
  renderFilters(s);
  renderGuests(s);
  renderGroups(s);
  renderActivity(s);
  $('#groups-count').textContent = s.groups.length || '';
  const running = s.tasks.filter((t) => t.status === 'running').length;
  $('#activity-count').textContent = running ? `${running} running` : '';
}

function renderHeader(s) {
  const m = s.manager;
  const q = s.cluster.quorate
    ? '<span class="pill ok">Quorate</span>'
    : '<span class="pill bad">No quorum</span>';
  const master = m.master
    ? `<span class="pill ${m.status === 'active' ? 'ok' : 'warn'}" title="HA manager (CRM) master">Manager on ${esc(m.master)}</span>`
    : '<span class="pill warn">No HA manager</span>';
  $('#cluster-line').innerHTML = `<span class="name">${esc(s.cluster.name || 'Proxmox')}</span>${q}${master}`;
  $('#me').textContent = s.me.user + (s.me.can_manage ? '' : ' (read-only)');
  tickUpdated();
}

function tickUpdated() {
  if (!ui.loadedAt) return;
  const sec = Math.round((Date.now() - ui.loadedAt) / 1000);
  $('#updated').textContent = ui.error ? 'Connection problem' : sec < 3 ? 'Live' : `Updated ${sec}s ago`;
}

function setBanner(kind, html) {
  const b = $('#banner');
  b.className = 'banner ' + kind;
  b.innerHTML = html;
  b.hidden = !html;
}

function renderBanner(s) {
  if (ui.error) return setBanner('bad', `Can't reach the cluster right now (${esc(ui.error)}). Showing the last known state.`);
  if (!s.cluster.quorate) return setBanner('bad', 'The cluster has lost quorum. HA cannot recover guests and changes are blocked until quorum returns.');
  const problems = s.guests.filter((g) => g.ha && g.ha.problem);
  if (problems.length) {
    return setBanner('bad', `${plural(problems.length, 'HA guest')} need${problems.length === 1 ? 's' : ''} attention: ${problems.slice(0, 4).map((g) => esc(g.name)).join(', ')}${problems.length > 4 ? '…' : ''}. To clear an <b>error</b>, set its wanted state to <b>disabled</b>, fix the cause, then set <b>started</b>.`);
  }
  if (s.orphans.length) return setBanner('warn', `HA config refers to guests that no longer exist: ${s.orphans.map(esc).join(', ')}.`);
  if (!s.me.can_manage) return setBanner('info', 'Read-only: your Proxmox user needs the Sys.Console privilege on / to change HA.');
  setBanner('', '');
}

function renderSummary(s) {
  const sum = s.summary;
  const stat = (label, value, cls = '', filter = '') => {
    const tag = filter ? 'button' : 'div';
    return `<${tag} class="stat ${cls}" ${filter ? `data-goto="${filter}"` : ''}><div class="label">${label}</div><div class="value">${value}</div></${tag}>`;
  };
  const allUp = sum.nodes_online === sum.nodes_total;
  $('#summary').innerHTML = [
    stat('Nodes online', `${sum.nodes_online}<small> / ${sum.nodes_total}</small>`, allUp ? 'ok' : 'bad'),
    stat('Protected by HA', `${sum.ha}<small> / ${sum.guests} guests</small>`, '', 'ha'),
    stat('Problems', sum.ha_problems, sum.ha_problems ? 'bad' : 'ok', 'problems'),
    stat('Moving now', sum.ha_moving, sum.ha_moving ? 'warn' : ''),
    stat('In maintenance', sum.maintenance.length ? esc(sum.maintenance.join(', ')) : '0', sum.maintenance.length ? 'warn' : ''),
  ].join('');
}

function meter(label, used, total, text) {
  const p = pct(used, total);
  const cls = p >= 90 ? 'crit' : p >= 75 ? 'hot' : '';
  return `<div class="meter" title="${esc(text)}"><span>${label}</span><div class="track"><div class="fill ${cls}" data-w="${p}"></div></div><span class="pct">${p}%</span></div>`;
}
const gib = (b) => (b / 1024 ** 3).toFixed(b >= 100 * 1024 ** 3 ? 0 : 1) + ' GiB';

function renderNodes(s) {
  const canMaint = s.me.can_manage && s.features.maintenance && s.cluster.quorate;
  $('#nodes').innerHTML = s.nodes.map((n) => {
    const state = !n.online ? ['bad', 'Offline'] : n.maintenance ? ['warn', 'Maintenance'] : n.lrm === 'active' ? ['ok', 'Active'] : ['', n.lrm === 'idle' ? 'Idle' : n.lrm];
    const busy = ui.busy.has('node:' + n.name);
    const title = !s.features.maintenance ? 'Maintenance needs the SSH key set up (see README)'
      : !s.me.can_manage ? 'Read-only user' : '';
    return `<div class="node ${!n.online ? 'offline' : ''} ${n.maintenance ? 'maint' : ''}">
      <div class="node-head">
        <div class="node-name"><span class="dot ${state[0] === 'ok' || state[0] === '' ? '' : state[0]}"></span>${esc(n.name)}</div>
        <span class="pill ${state[0]}" title="Local resource manager (LRM): ${esc(n.lrm)}">${esc(state[1])}</span>
      </div>
      <div class="node-meta"><span><b>${n.ha_guests}</b> HA</span><span><b>${n.running}</b>/${n.guests} running</span>${n.ip ? `<span>${esc(n.ip)}</span>` : ''}</div>
      ${n.online ? meter('CPU', n.cpu, 1, `${n.maxcpu} cores`) + meter('RAM', n.mem, n.maxmem, `${gib(n.mem)} of ${gib(n.maxmem)}`) : '<div class="muted small">No data while offline. HA will fence and recover its guests.</div>'}
      <div class="node-foot">
        <button class="btn small" data-act="maint" data-node="${esc(n.name)}" ${canMaint && n.online && !busy ? '' : 'disabled'} title="${esc(title)}">
          <svg viewBox="0 0 24 24" width="15" height="15"><use href="#i-wrench"/></svg>${busy ? 'Working…' : n.maintenance ? 'End maintenance' : 'Maintenance'}
        </button>
        <button class="btn small ghost" data-act="show-node" data-node="${esc(n.name)}">Guests</button>
      </div>
    </div>`;
  }).join('');
  // Widths via CSSOM: the CSP forbids inline style attributes.
  for (const el of $$('#nodes .fill[data-w]')) el.style.width = el.dataset.w + '%';
}

function renderFilters(s) {
  const counts = {
    all: s.guests.length,
    ha: s.guests.filter((g) => g.ha).length,
    noha: s.guests.filter((g) => !g.ha).length,
    problems: s.guests.filter((g) => g.ha && g.ha.problem).length,
  };
  for (const b of $$('#filters button')) {
    const f = b.dataset.filter;
    b.setAttribute('aria-pressed', String(f === ui.filter));
    const label = { all: 'All', ha: 'In HA', noha: 'Not in HA', problems: 'Problems' }[f];
    b.innerHTML = `${label}<span class="n">${counts[f]}</span>`;
  }
  const sel = $('#node-filter');
  const opts = '<option value="">All nodes</option>' + s.nodes.map((n) => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join('');
  if (sel.dataset.opts !== opts) { sel.innerHTML = opts; sel.dataset.opts = opts; }
  sel.value = ui.node;
  $('#group-by-node').checked = ui.groupByNode;
  for (const th of $$('th.sortable')) {
    if (th.dataset.sort === ui.sort.key) th.setAttribute('aria-sort', ui.sort.dir > 0 ? 'ascending' : 'descending');
    else th.removeAttribute('aria-sort');
  }
}

function visibleGuests(s) {
  const q = ui.q.trim().toLowerCase();
  let list = s.guests.filter((g) => {
    if (ui.filter === 'ha' && !g.ha) return false;
    if (ui.filter === 'noha' && g.ha) return false;
    if (ui.filter === 'problems' && !(g.ha && g.ha.problem)) return false;
    if (ui.node && g.node !== ui.node) return false;
    if (!q) return true;
    const hay = [g.vmid, g.name, g.node, g.sid, g.type, g.status, g.ha?.group, g.ha?.crm_state, ...g.tags].join(' ').toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  });
  const { key, dir } = ui.sort;
  const val = (g) => (key === 'crm' ? (g.ha ? g.ha.crm_state : '~') : g[key]);
  list.sort((a, b) => {
    const x = val(a), y = val(b);
    const c = typeof x === 'number' ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
    return c * dir || a.vmid - b.vmid;
  });
  if (ui.groupByNode) list.sort((a, b) => a.node.localeCompare(b.node, undefined, { numeric: true }));
  return list;
}

function guestRow(g, s) {
  const can = s.me.can_manage && s.cluster.quorate;
  const busy = ui.busy.has(g.sid);
  const dis = can && !busy ? '' : 'disabled';
  const sel = ui.selected.has(g.sid);
  const power = `<span class="pill ${g.status === 'running' ? 'ok' : ''}">${esc(g.status)}</span>`;
  const tags = g.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join('');
  let state = '<span class="dash">—</span>', group = state, status = state, act = '';
  if (g.ha) {
    state = `<select data-act="state" ${dis} title="${esc(STATE_HELP[g.ha.state] || '')}" aria-label="Wanted HA state">${
      ['started', 'stopped', 'disabled', 'ignored'].map((v) => `<option ${v === g.ha.state ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
    group = `<select data-act="group" ${dis} aria-label="HA group"><option value="">(none)</option>${
      s.groups.map((gr) => `<option ${gr.group === g.ha.group ? 'selected' : ''}>${esc(gr.group)}</option>`).join('')}</select>`;
    const crm = g.ha.crm_state;
    const tip = crm === 'error' ? 'To recover: set wanted state to disabled, fix the cause, then set started.' : '';
    status = `<span class="pill ${CRM_CLASS[crm] ?? 'warn'}" title="${esc(tip)}">${esc(CRM_LABEL[crm] || crm)}</span>`;
    const movable = !['disabled', 'ignored'].includes(g.ha.state) && !g.ha.problem && !g.ha.moving;
    act = `<button class="icon-btn" data-act="move" title="Move to another node" aria-label="Move ${esc(g.name)}" ${can && movable && !busy ? '' : 'disabled'}><svg viewBox="0 0 24 24"><use href="#i-move"/></svg><span class="m-label">Move</span></button>`;
  }
  const sub = `on <b>${esc(g.node)}</b> · ${esc(g.status)}${g.ha ? ' · HA ' + esc(CRM_LABEL[g.ha.crm_state] || g.ha.crm_state) : ''}`;
  return `<tr data-sid="${esc(g.sid)}" class="${sel ? 'selected' : ''}">
    <td class="c-sel"><input type="checkbox" data-act="select" ${sel ? 'checked' : ''} aria-label="Select ${esc(g.name)}"></td>
    <td class="c-id">${g.vmid}</td>
    <td class="c-name"><div class="gname">${esc(g.name)}<span class="kind">${g.type.toUpperCase()}</span>${tags}</div><div class="sub m-only">${sub}</div></td>
    <td class="c-node">${esc(g.node)}</td>
    <td class="c-power">${power}</td>
    <td class="c-ha"><label class="switch" title="${g.ha ? 'Protected by HA — click to remove' : 'Not protected — click to add to HA'}"><input type="checkbox" data-act="ha" ${g.ha ? 'checked' : ''} ${dis} aria-label="HA for ${esc(g.name)}"><span></span></label></td>
    <td class="c-state">${state}</td>
    <td class="c-group">${group}</td>
    <td class="c-status">${status}</td>
    <td class="c-act">${act}</td>
  </tr>`;
}

function renderGuests(s) {
  const list = visibleGuests(s);
  let html = '';
  let lastNode = null;
  for (const g of list) {
    if (ui.groupByNode && g.node !== lastNode) {
      lastNode = g.node;
      const n = s.nodes.find((x) => x.name === g.node);
      html += `<tr class="group-row"><td colspan="10">${esc(g.node)}${n && n.maintenance ? ' · maintenance' : ''}${n && !n.online ? ' · offline' : ''}</td></tr>`;
    }
    html += guestRow(g, s);
  }
  const tbody = $('#guest-rows');
  const active = document.activeElement;
  const interacting = active && tbody.contains(active) && active.tagName === 'SELECT';
  if (html !== ui.lastRows && !interacting) {
    tbody.innerHTML = html;
    ui.lastRows = html;
  }
  $('#empty').hidden = list.length > 0;
  const visible = list.map((g) => g.sid);
  const all = $('#select-all');
  const nSel = visible.filter((sid) => ui.selected.has(sid)).length;
  all.checked = nSel > 0 && nSel === visible.length;
  all.indeterminate = nSel > 0 && nSel < visible.length;
  all.disabled = !s.me.can_manage;
  renderBulk();
}

function renderBulk() {
  // Drop selections that no longer exist.
  const known = new Set((ui.state?.guests || []).map((g) => g.sid));
  for (const sid of ui.selected) if (!known.has(sid)) ui.selected.delete(sid);
  const n = ui.selected.size;
  $('#bulkbar').hidden = n === 0;
  $('#bulk-count').textContent = `${n} selected`;
}

function renderGroups(s) {
  const can = s.me.can_manage && s.cluster.quorate;
  $('#new-group-btn').disabled = !can;
  if (!s.groups.length) {
    $('#groups').innerHTML = '<p class="muted">No groups yet. Without a group, HA may run a guest on any node.</p>';
    return;
  }
  $('#groups').innerHTML = s.groups.map((g) => {
    const nodes = Object.entries(g.nodes).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const flags = [
      g.restricted ? '<span class="pill warn plain" title="Guests may only run on these nodes, even if all of them are down">Restricted</span>' : '<span class="pill plain" title="If all listed nodes are down, guests may run anywhere">Unrestricted</span>',
      g.nofailback ? '<span class="pill plain" title="Guests stay where they are when a higher-priority node comes back">No failback</span>' : '<span class="pill plain" title="Guests move back to the highest-priority node when it returns">Failback</span>',
    ].join('');
    return `<div class="group-card">
      <h3><span>${esc(g.group)}</span><span class="muted small">${plural(g.members, 'guest')}</span></h3>
      ${g.comment ? `<div class="muted small">${esc(g.comment)}</div>` : ''}
      <div class="group-nodes">${nodes.map(([n, p]) => `<span class="pill accent plain">${esc(n)}${p ? ` <span class="prio">· ${p}</span>` : ''}</span>`).join('')}</div>
      <div class="flags">${flags}</div>
      <div class="actions">
        <button class="btn small" data-act="edit-group" data-group="${esc(g.group)}" ${can ? '' : 'disabled'}>Edit</button>
        <button class="btn small danger ghost" data-act="delete-group" data-group="${esc(g.group)}" ${can && !g.members ? '' : 'disabled'} title="${g.members ? 'Move its guests to another group first' : ''}">Delete</button>
      </div>
    </div>`;
  }).join('');
}

function renderActivity(s) {
  if (!s.tasks.length) {
    $('#activity').innerHTML = '<p class="muted">No recent HA tasks.</p>';
    return;
  }
  const names = Object.fromEntries(s.guests.map((g) => [String(g.vmid), g.name]));
  $('#activity').innerHTML = '<div class="task-list">' + s.tasks.map((t) => {
    const when = t.starttime ? new Date(t.starttime * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
    const st = t.status === 'running' ? '<span class="pill info busy">running</span>'
      : t.status === 'OK' ? '<span class="pill ok">OK</span>'
      : `<span class="pill bad" title="${esc(t.status)}">failed</span>`;
    return `<div class="task"><span class="when">${esc(when)}</span>
      <span class="what">${esc(TASK_LABEL[t.type] || t.type)} ${esc(t.id || '')}${names[t.id] ? ` (${esc(names[t.id])})` : ''} <span class="who">on ${esc(t.node)} · ${esc(t.user || '')}</span></span>${st}</div>`;
  }).join('') + '</div>';
}

/* -------------------------------------------------------------- actions */
const guestBySid = (sid) => ui.state.guests.find((g) => g.sid === sid);

async function withBusy(key, fn) {
  ui.busy.add(key);
  render();
  try {
    return await fn();
  } catch (err) {
    if (err instanceof AuthError) return showLogin(err.message);
    toast(err.message, 'bad', 7000);
  } finally {
    ui.busy.delete(key);
    settle();
  }
}

function addToHA(g) {
  const state = g.status === 'running' ? 'started' : 'stopped';
  return withBusy(g.sid, async () => {
    await api('POST', '/ha/resources', { sid: g.sid, state });
    toast(`${g.name} is now protected by HA`);
  });
}

async function removeFromHA(g) {
  const ok = await openDialog({
    title: `Remove ${g.name} from HA?`,
    body: `<p>${esc(g.name)} (${esc(g.sid)}) keeps running on <b>${esc(g.node)}</b>, but it will <b>not</b> be restarted or moved if that node fails.</p>`,
    okText: 'Remove from HA',
    danger: true,
  });
  if (!ok) return render();
  return withBusy(g.sid, async () => {
    await api('DELETE', `/ha/resources/${g.sid}`);
    toast(`${g.name} removed from HA`);
  });
}

async function setState(g, state) {
  if (state === g.ha.state) return;
  const stopping = g.status === 'running' && (state === 'stopped' || state === 'disabled');
  if (stopping) {
    const ok = await openDialog({
      title: `Set ${g.name} to "${state}"?`,
      body: `<p>${esc(STATE_HELP[state])}</p><div class="note warn">This shuts down ${esc(g.name)} on ${esc(g.node)}.</div>`,
      okText: 'Shut down',
      danger: true,
    });
    if (!ok) { ui.lastRows = ''; return render(); }
  }
  return withBusy(g.sid, async () => {
    await api('PUT', `/ha/resources/${g.sid}`, { state });
    toast(`${g.name}: wanted state → ${state}`);
  });
}

function setGroup(g, group) {
  return withBusy(g.sid, async () => {
    await api('PUT', `/ha/resources/${g.sid}`, { group });
    toast(group ? `${g.name} → group ${group}` : `${g.name} no longer in a group`);
  });
}

async function moveGuest(g) {
  const s = ui.state;
  const group = s.groups.find((x) => x.group === g.ha.group);
  const targets = s.nodes.filter((n) => n.name !== g.node);
  const nodeOpts = targets.map((n) => {
    const off = !n.online || n.maintenance;
    const outside = group && group.restricted && !(n.name in group.nodes);
    const top = group ? Math.max(...Object.values(group.nodes)) : null;
    const preferred = group && n.name in group.nodes && group.nodes[n.name] === top ? ` · preferred by "${group.group}"` : '';
    const why = !n.online ? 'offline' : n.maintenance ? 'in maintenance' : outside ? 'outside its restricted group: HA would move it straight back'
      : `${pct(n.cpu, 1)}% CPU · ${pct(n.mem, n.maxmem)}% RAM${preferred}`;
    return `<label class="${off ? 'disabled' : ''}"><input type="radio" name="node" value="${esc(n.name)}" ${off ? 'disabled' : ''} >${esc(n.name)}<small>${esc(why)}</small></label>`;
  }).join('');
  const live = g.type === 'vm'
    ? 'Live migration, no downtime (needs shared or replicated storage).'
    : 'Containers restart during the move: a short downtime.';
  await openDialog({
    title: `Move ${g.name}`,
    body: `<p class="muted">Currently on <b>${esc(g.node)}</b>.</p>
      <div class="field-label">Target node</div>
      <div class="radio-cards">${nodeOpts}</div>
      <div class="field-label">How</div>
      <div class="radio-cards">
        <label><input type="radio" name="mode" value="migrate" checked>Migrate<small>${live}</small></label>
        <label><input type="radio" name="mode" value="relocate">Relocate<small>Stop, move, then start again. Use when migration isn't possible.</small></label>
      </div>`,
    okText: 'Move',
    init(body) {
      const first = $('input[name=node]:not(:disabled)', body);
      if (first) first.checked = true;
    },
    async onOk(body) {
      const node = $('input[name=node]:checked', body)?.value;
      const mode = $('input[name=mode]:checked', body).value;
      if (!node) throw new Error('Pick a node to move to');
      ui.busy.add(g.sid);
      try {
        await api('POST', `/ha/resources/${g.sid}/move`, { node, mode });
      } finally {
        ui.busy.delete(g.sid);
      }
      toast(`Moving ${g.name} to ${node}…`, 'info');
      settle();
    },
  });
}

async function toggleMaintenance(name) {
  const s = ui.state;
  const n = s.nodes.find((x) => x.name === name);
  const enable = !n.maintenance;
  const onNode = s.guests.filter((g) => g.node === name);
  const haGuests = onNode.filter((g) => g.ha && g.ha.state !== 'ignored');
  const otherRunning = onNode.filter((g) => !g.ha && g.status === 'running');
  const usable = s.nodes.filter((x) => x.online && !x.maintenance && x.name !== name);
  const body = enable
    ? `<p>HA will move its <b>${plural(haGuests.length, 'HA guest')}</b> to other nodes and keep new ones away until you end maintenance.</p>
       ${otherRunning.length ? `<div class="note warn">${plural(otherRunning.length, 'running guest')} not in HA will <b>not</b> be moved: ${otherRunning.slice(0, 6).map((g) => esc(g.name)).join(', ')}${otherRunning.length > 6 ? ' and more' : ''}. Shut them down or migrate them yourself.</div>` : ''}
       ${usable.length ? '' : '<div class="note warn">No other node is available to take the guests.</div>'}
       <p class="muted small">Watch the "Moving now" counter drop to 0 before rebooting the node.</p>`
    : `<p>${esc(name)} accepts guests again. HA guests that were moved away during maintenance migrate back.</p>`;
  const ok = await openDialog({
    title: enable ? `Put ${name} into maintenance?` : `End maintenance on ${name}?`,
    body,
    okText: enable ? 'Start maintenance' : 'End maintenance',
    danger: enable,
  });
  if (!ok) return;
  return withBusy('node:' + name, async () => {
    await api('POST', `/nodes/${encodeURIComponent(name)}/maintenance`, { enable });
    toast(enable ? `${name} is entering maintenance — guests are moving` : `${name} is out of maintenance`, 'info');
  });
}

/* ----------------------------------------------------------------- bulk */
async function runBulk(label, sids, fn) {
  let done = 0;
  const failed = [];
  sids.forEach((sid) => ui.busy.add(sid));
  render();
  for (const sid of sids) {
    const g = guestBySid(sid);
    try {
      await fn(g);
      done++;
    } catch (err) {
      if (err instanceof AuthError) { showLogin(err.message); return; }
      failed.push(`${g ? g.name : sid}: ${err.message}`);
    } finally {
      ui.busy.delete(sid);
    }
  }
  ui.selected.clear();
  if (done) toast(`${label}: ${done} done`);
  if (failed.length) toast(`${failed.length} failed — ${failed[0]}`, 'bad', 9000);
  settle();
}

async function onBulk(action) {
  const sids = [...ui.selected];
  const guests = sids.map(guestBySid).filter(Boolean);
  const inHA = guests.filter((g) => g.ha);
  const notHA = guests.filter((g) => !g.ha);
  const s = ui.state;
  if (action === 'clear') { ui.selected.clear(); ui.lastRows = ''; return render(); }
  if (action === 'add') {
    if (!notHA.length) return toast('All selected guests are already in HA', 'info');
    return runBulk('Added to HA', notHA.map((g) => g.sid), (g) =>
      api('POST', '/ha/resources', { sid: g.sid, state: g.status === 'running' ? 'started' : 'stopped' }));
  }
  if (!inHA.length) return toast('None of the selected guests are in HA', 'info');
  if (action === 'remove') {
    const ok = await openDialog({
      title: `Remove ${plural(inHA.length, 'guest')} from HA?`,
      body: `<p>They keep running, but won't be restarted or moved when a node fails.</p><p class="muted small">${inHA.map((g) => esc(g.name)).join(', ')}</p>`,
      okText: 'Remove from HA',
      danger: true,
    });
    if (ok) runBulk('Removed from HA', inHA.map((g) => g.sid), (g) => api('DELETE', `/ha/resources/${g.sid}`));
  }
  if (action === 'state') {
    const choice = await openDialog({
      title: `Set wanted state for ${plural(inHA.length, 'guest')}`,
      body: `<div class="radio-cards">${['started', 'stopped', 'disabled', 'ignored'].map((v, i) =>
        `<label><input type="radio" name="state" value="${v}" ${i === 0 ? 'checked' : ''}>${v}<small>${esc(STATE_HELP[v])}</small></label>`).join('')}</div>`,
      okText: 'Apply',
      onOk: (body) => $('input[name=state]:checked', body).value,
    });
    if (choice) runBulk(`State → ${choice}`, inHA.map((g) => g.sid), (g) => api('PUT', `/ha/resources/${g.sid}`, { state: choice }));
  }
  if (action === 'group') {
    const choice = await openDialog({
      title: `Set group for ${plural(inHA.length, 'guest')}`,
      body: `<label>Group<select name="group"><option value="">(none)</option>${s.groups.map((g) => `<option>${esc(g.group)}</option>`).join('')}</select></label>`,
      okText: 'Apply',
      onOk: (body) => ({ group: $('select', body).value }),
    });
    if (choice) runBulk(choice.group ? `Group → ${choice.group}` : 'Group cleared', inHA.map((g) => g.sid), (g) => api('PUT', `/ha/resources/${g.sid}`, { group: choice.group }));
  }
}

/* --------------------------------------------------------------- groups */
async function editGroup(name) {
  const s = ui.state;
  const existing = name ? s.groups.find((g) => g.group === name) : null;
  const prio = existing ? { ...existing.nodes } : {};
  const rows = s.nodes.map((n) => {
    const on = n.name in prio;
    const p = prio[n.name] || 0;
    return `<div class="row" data-node="${esc(n.name)}">
      <label class="check"><input type="checkbox" ${on ? 'checked' : ''}> ${esc(n.name)}</label>
      <span class="stepper" title="Priority: higher runs first"><button type="button" data-d="-1" aria-label="Lower priority">−</button><output>${p}</output><button type="button" data-d="1" aria-label="Higher priority">+</button></span>
    </div>`;
  }).join('');
  await openDialog({
    title: existing ? `Edit group ${name}` : 'New HA group',
    body: `${existing ? '' : '<label>Name<input name="group" required pattern="[A-Za-z][A-Za-z0-9_\\-]+" placeholder="e.g. databases" autocomplete="off"></label>'}
      <div><div class="small muted picker-label">Nodes and priority (higher = preferred)</div><div class="node-picker">${rows}</div></div>
      <label class="check"><input type="checkbox" name="restricted" ${existing?.restricted ? 'checked' : ''}> Restricted: only ever run on these nodes</label>
      <label class="check"><input type="checkbox" name="nofailback" ${existing?.nofailback ? 'checked' : ''}> No failback: don't move back when a preferred node returns</label>
      <label>Comment<input name="comment" value="${esc(existing?.comment || '')}" autocomplete="off"></label>`,
    okText: existing ? 'Save' : 'Create group',
    init(body) {
      body.addEventListener('click', (e) => {
        const b = e.target.closest('.stepper button');
        if (!b) return;
        const out = $('output', b.parentElement);
        out.textContent = Math.max(0, Math.min(1000, Number(out.textContent) + Number(b.dataset.d)));
        $('input[type=checkbox]', b.closest('.row')).checked = true;
      });
    },
    async onOk(body) {
      const nodes = {};
      for (const row of $$('.row', body)) {
        if ($('input', row).checked) nodes[row.dataset.node] = Number($('output', row).textContent);
      }
      const payload = {
        nodes,
        restricted: $('[name=restricted]', body).checked,
        nofailback: $('[name=nofailback]', body).checked,
        comment: $('[name=comment]', body).value.trim(),
      };
      if (existing) {
        await api('PUT', `/ha/groups/${encodeURIComponent(name)}`, payload);
      } else {
        payload.group = $('[name=group]', body).value.trim();
        await api('POST', '/ha/groups', payload);
      }
      toast(existing ? `Group ${name} saved` : `Group ${payload.group} created`);
      settle();
    },
  });
}

async function deleteGroup(name) {
  await openDialog({
    title: `Delete group ${name}?`,
    body: '<p>No guests use it. This only removes the placement rule.</p>',
    okText: 'Delete group',
    danger: true,
    async onOk() {
      await api('DELETE', `/ha/groups/${encodeURIComponent(name)}`);
      toast(`Group ${name} deleted`);
      settle();
    },
  });
}

/* ------------------------------------------------------------- settings */
const cfg = { info: null, draft: null, results: [], el: null, setup: false, busy: false };

const TLS_MODES = {
  verify: ['Verify certificates (recommended)', (i) => i.ca_file
    ? "Trusts your cluster's own CA (installed with HA Manager) and public CAs such as Let's Encrypt."
    : "Trusts public CAs only. Proxmox's default certificates are self-signed, so use Pin unless you installed trusted certificates."],
  pin: ['Pin certificates', () => 'Trust exactly the certificates listed below. Use Test connection, then Trust on each node.'],
  insecure: ["Don't verify (not recommended)", () => 'Accepts any certificate: someone on the network path could read Proxmox passwords.'],
};

async function loadSettings(el, setup) {
  cfg.el = el;
  cfg.setup = setup;
  el.innerHTML = '<p class="muted">Loading…</p>';
  try {
    cfg.info = await api('GET', '/settings');
  } catch (err) {
    if (err instanceof AuthError) return showLogin(err.message);
    el.innerHTML = `<p class="form-error">${esc(err.message)}</p>`;
    return;
  }
  const i = cfg.info;
  cfg.draft = { hosts: i.hosts.length ? [...i.hosts] : [''], tls: i.tls, fingerprints: [...i.fingerprints] };
  cfg.results = [];
  renderSettings();
  if (setup) $('.host-input', el)?.focus();
}

function hostResult(r) {
  if (!r) return '';
  if (r.ok) return '<div class="host-result"><span class="pill ok">Proxmox VE answers</span></div>';
  const pinned = cfg.draft.fingerprints.includes(r.fingerprint);
  const trust = r.untrusted && r.fingerprint && !pinned && cfg.info.can_edit
    ? `<button type="button" class="btn small" data-sact="trust" data-fp="${esc(r.fingerprint)}">Trust this certificate</button>` : '';
  return `<div class="host-result"><span class="pill bad">${r.untrusted ? 'Certificate not trusted' : 'Failed'}</span>
    <span class="err">${esc(r.error || '')}</span>
    ${r.untrusted && r.fingerprint ? `<div class="fp-line"><span class="muted">SHA-256</span> <code class="fp">${esc(r.fingerprint)}</code>${trust}</div>` : ''}</div>`;
}

function renderSettings() {
  const { info, draft, results } = cfg;
  const can = info.can_edit && !cfg.busy;
  const dis = can ? '' : 'disabled';
  const known = (ui.state?.nodes || []).map((n) => n.ip).filter(Boolean);
  const missing = cfg.setup ? [] : known.filter((ip) => !draft.hosts.includes(ip));
  const hostRows = draft.hosts.map((h, i) => `
    <div class="host-row" data-i="${i}">
      <div class="host-line">
        <input class="host-input" value="${esc(h)}" placeholder="192.168.2.57" autocomplete="off" spellcheck="false" aria-label="Node ${i + 1} address" ${dis}>
        <button type="button" class="icon-btn" data-sact="remove" title="Remove" aria-label="Remove node ${i + 1}" ${can && draft.hosts.length > 1 ? '' : 'disabled'}>✕</button>
      </div>
      ${hostResult(results[i])}
    </div>`).join('');
  const modes = Object.entries(TLS_MODES).map(([k, [label, help]]) => `
    <label><input type="radio" name="tls" value="${k}" ${draft.tls === k ? 'checked' : ''} ${dis}>${label}<small>${esc(help(info))}</small></label>`).join('');
  const fps = draft.tls !== 'pin' ? '' : draft.fingerprints.length
    ? `<div class="fp-list">${draft.fingerprints.map((f, i) => `<div class="fp-line"><code class="fp">${esc(f)}</code><button type="button" class="icon-btn" data-sact="unpin" data-i="${i}" aria-label="Remove fingerprint" ${dis}>✕</button></div>`).join('')}</div>`
    : '<div class="note warn">No certificates pinned yet. Click <b>Test connection</b>, then <b>Trust</b> on each node.</div>';
  const source = cfg.setup ? '' : info.source === 'ui'
    ? 'Saved from this page (/var/lib/haui/settings.json).'
    : 'From the config file. Saving here overrides it.';

  cfg.el.innerHTML = `<div class="settings">
    ${info.can_edit ? '' : '<div class="note">Only Proxmox administrators (<code>Sys.Modify</code> on <code>/</code>) can change these settings.</div>'}
    <section class="panel">
      <h3>Proxmox nodes</h3>
      <p class="muted small">IP address or hostname of each cluster node (API port 8006 unless you add <code>:port</code>). List several: when one is down, the next one is used.</p>
      <div class="host-rows">${hostRows}</div>
      ${can ? `<div class="row-actions">
        <button type="button" class="btn small" data-sact="add">Add node</button>
        ${missing.length ? `<button type="button" class="btn small ghost" data-sact="add-cluster">Add all cluster nodes (${missing.length})</button>` : ''}
      </div>` : ''}
    </section>
    <section class="panel">
      <h3>Certificate check</h3>
      <div class="radio-cards">${modes}</div>
      ${fps}
    </section>
    <p class="form-error" id="settings-error" role="alert"></p>
    ${info.can_edit ? `<div class="settings-actions">
      <span class="muted small">${esc(source)}</span>
      <button type="button" class="btn" data-sact="test" ${dis}>${cfg.busy === 'test' ? 'Testing…' : 'Test connection'}</button>
      <button type="button" class="btn primary" data-sact="save" ${dis}>${cfg.busy === 'save' ? 'Saving…' : cfg.setup ? 'Connect' : 'Save'}</button>
    </div>` : ''}
  </div>`;
}

function settingsPayload() {
  return { hosts: cfg.draft.hosts.map((h) => h.trim()).filter(Boolean), tls: cfg.draft.tls, fingerprints: cfg.draft.fingerprints };
}

async function settingsCall(kind) {
  cfg.busy = kind;
  renderSettings();
  let error = '';
  try {
    const res = await api(kind === 'save' ? 'PUT' : 'POST', kind === 'save' ? '/settings' : '/settings/test', settingsPayload());
    cfg.draft.hosts = res.hosts;          // normalized + de-duplicated, aligned with results
    cfg.results = res.results;
    if (kind === 'save') {
      cfg.busy = false;
      if (cfg.setup || res.relogin) {
        showLogin(cfg.setup ? 'Connected. Sign in with your Proxmox account.' : 'Saved. Sign in to the cluster again.', true);
        return;
      }
      toast('Cluster connection saved');
      await loadSettings(cfg.el, false);
      cfg.results = res.results;
      renderSettings();
      settle();
      return;
    }
    const ok = res.results.filter((r) => r.ok).length;
    toast(`${ok} of ${res.results.length} node${res.results.length === 1 ? '' : 's'} reachable`, ok ? 'ok' : 'bad');
  } catch (err) {
    if (err instanceof AuthError) { cfg.busy = false; return showLogin(err.message); }
    error = err.message;
  }
  cfg.busy = false;
  renderSettings();
  $('#settings-error', cfg.el).textContent = error;
}

function wireSettings(el) {
  el.addEventListener('input', (e) => {
    const input = e.target.closest('.host-input');
    if (!input) return;
    const i = Number(input.closest('.host-row').dataset.i);
    cfg.draft.hosts[i] = input.value;
    const res = input.closest('.host-row').querySelector('.host-result');
    if (res) res.remove();
    cfg.results[i] = null;
  });
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.closest('.host-input')) { e.preventDefault(); settingsCall('test'); }
  });
  el.addEventListener('change', (e) => {
    if (e.target.name === 'tls') { cfg.draft.tls = e.target.value; renderSettings(); }
  });
  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-sact]');
    if (!b || b.disabled) return;
    const d = cfg.draft;
    switch (b.dataset.sact) {
      case 'add':
        d.hosts.push('');
        renderSettings();
        $$('.host-input', el).pop().focus();
        break;
      case 'add-cluster':
        for (const n of ui.state.nodes) if (n.ip && !d.hosts.includes(n.ip)) d.hosts.push(n.ip);
        d.hosts = d.hosts.filter((h) => h.trim());
        cfg.results = [];
        renderSettings();
        break;
      case 'remove': {
        const i = Number(b.closest('.host-row').dataset.i);
        d.hosts.splice(i, 1);
        cfg.results.splice(i, 1);
        renderSettings();
        break;
      }
      case 'trust':
        if (!d.fingerprints.includes(b.dataset.fp)) d.fingerprints.push(b.dataset.fp);
        d.tls = 'pin';
        renderSettings();
        toast('Certificate pinned. Test again, then save.', 'info');
        break;
      case 'unpin':
        d.fingerprints.splice(Number(b.dataset.i), 1);
        renderSettings();
        break;
      case 'test':
      case 'save':
        settingsCall(b.dataset.sact);
        break;
    }
  });
}

function showSetup() {
  stopPolling();
  $('#app-view').hidden = true;
  $('#login-view').hidden = true;
  $('#setup-view').hidden = false;
  loadSettings($('#setup-form'), true);
}

/* ---------------------------------------------------------------- wiring */
function setTab(tab) {
  ui.tab = tab;
  for (const b of $$('.tabs [role=tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  for (const sec of $$('.tab')) sec.hidden = sec.id !== 'tab-' + tab;
  if (tab === 'settings') loadSettings($('#settings'), false);
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function wire() {
  applyTheme(store.get('theme', ''));
  $('#login-form').addEventListener('submit', onLogin);
  $('#logout-btn').addEventListener('click', async () => {
    try { await api('POST', '/logout'); } catch { /* ignore */ }
    ui.state = null;
    ui.lastRows = '';
    showLogin();
  });
  $('#theme-btn').addEventListener('click', () => {
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const cur = document.documentElement.dataset.theme || (dark ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    store.set('theme', next);
  });
  $$('.tabs [role=tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
  $('#settings-btn').addEventListener('click', () => setTab('settings'));
  wireSettings($('#settings'));
  wireSettings($('#setup-form'));

  const rerender = () => { ui.lastRows = ''; render(); };
  $('#search').addEventListener('input', (e) => { ui.q = e.target.value; rerender(); });
  $('#search').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.target.value = ''; ui.q = ''; rerender(); } });
  $('#filters').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    ui.filter = b.dataset.filter;
    store.set('filter', ui.filter);
    rerender();
  });
  $('#node-filter').addEventListener('change', (e) => { ui.node = e.target.value; rerender(); });
  $('#group-by-node').addEventListener('change', (e) => { ui.groupByNode = e.target.checked; store.set('groupByNode', ui.groupByNode); rerender(); });
  $$('th.sortable').forEach((th) => th.addEventListener('click', () => {
    const key = th.dataset.sort;
    ui.sort = { key, dir: ui.sort.key === key ? -ui.sort.dir : 1 };
    store.set('sort', ui.sort);
    rerender();
  }));
  $('#select-all').addEventListener('change', (e) => {
    const sids = visibleGuests(ui.state).map((g) => g.sid);
    sids.forEach((sid) => (e.target.checked ? ui.selected.add(sid) : ui.selected.delete(sid)));
    rerender();
  });

  $('#summary').addEventListener('click', (e) => {
    const b = e.target.closest('[data-goto]');
    if (!b) return;
    ui.filter = b.dataset.goto;
    ui.node = '';
    rerender();
    $('.guests-head').scrollIntoView({ behavior: 'smooth' });
  });

  $('#nodes').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    if (b.dataset.act === 'maint') toggleMaintenance(b.dataset.node);
    if (b.dataset.act === 'show-node') {
      ui.node = b.dataset.node;
      ui.filter = 'all';
      rerender();
      $('.guests-head').scrollIntoView({ behavior: 'smooth' });
    }
  });

  const rows = $('#guest-rows');
  rows.addEventListener('change', (e) => {
    const el = e.target;
    const tr = el.closest('tr[data-sid]');
    if (!tr) return;
    const g = guestBySid(tr.dataset.sid);
    if (!g) return;
    const act = el.dataset.act;
    if (act === 'select') {
      el.checked ? ui.selected.add(g.sid) : ui.selected.delete(g.sid);
      tr.classList.toggle('selected', el.checked);
      ui.lastRows = '';
      renderGuests(ui.state);
    } else if (act === 'ha') {
      el.checked = !!g.ha; // reflect reality until the server confirms
      g.ha ? removeFromHA(g) : addToHA(g);
    } else if (act === 'state') {
      el.blur();
      setState(g, el.value);
    } else if (act === 'group') {
      el.blur();
      setGroup(g, el.value);
    }
  });
  rows.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act=move]');
    if (!b) return;
    const g = guestBySid(b.closest('tr').dataset.sid);
    if (g) moveGuest(g);
  });

  $('#bulkbar').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-bulk]');
    if (b) onBulk(b.dataset.bulk);
  });
  $('#new-group-btn').addEventListener('click', () => editGroup(null));
  $('#groups').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    if (b.dataset.act === 'edit-group') editGroup(b.dataset.group);
    if (b.dataset.act === 'delete-group') deleteGroup(b.dataset.group);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName) && !$('#dlg').open) {
      e.preventDefault();
      setTab('overview');
      $('#search').focus();
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && ui.timer) refresh(); });
  setInterval(tickUpdated, 1000);
}

async function boot() {
  wire();
  try {
    await api('GET', '/session');
    showApp();
  } catch {
    showLogin();
  }
}

document.addEventListener('DOMContentLoaded', boot);
