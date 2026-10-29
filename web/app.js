/* 会员购自动抢票 · 前端逻辑 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const STATE = {
    project: null,
    mode: 'timed',
    running: null,
    offsetMs: 0,
    serverRtt: 0,
    buyers: [],
    busy: false,
  };

  /* ---------------- 基础工具 ---------------- */

  let toastTimer = null;
  function toast(msg, kind = '') {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast show ' + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = 'toast ' + kind; }, 2600);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
    if (data && data.ok === false) throw new Error(data.error || '请求失败');
    return data || {};
  }

  const post = (path, body) => api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });

  function pad(n) { return String(n).padStart(2, '0'); }

  function fmtLocal(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function fmtClock(ms) {
    if (!ms) return '—';
    const d = new Date(ms);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function fmtCountdown(sec) {
    sec = Math.max(0, Math.floor(sec));
    const d = Math.floor(sec / 86400);
    const h = Math.floor(sec % 86400 / 3600);
    const m = Math.floor(sec % 3600 / 60);
    const s = sec % 60;
    if (d > 0) return d + '天 ' + pad(h) + ':' + pad(m) + ':' + pad(s);
    return pad(h) + ':' + pad(m) + ':' + pad(s);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  /* ---------------- 账号 ---------------- */

  function renderAccount(data) {
    const online = !!data.logged_in;
    $('account-offline').classList.toggle('hidden', online);
    $('account-online').classList.toggle('hidden', !online);
    const chip = $('chip-account');
    if (online) {
      const u = data.user || {};
      chip.textContent = '已登录 · ' + (u.uname || '账号');
      chip.className = 'chip ok';
      $('user-name').textContent = u.uname || '已登录';
      $('user-mid').textContent = 'UID ' + (u.mid || '—') + (u.level ? ' · LV' + u.level : '');
      if (u.face) $('user-face').src = u.face;
    } else {
      chip.textContent = '未登录';
      chip.className = 'chip warn';
    }
  }

  async function refreshSession() {
    try {
      const d = await api('/api/session');
      renderAccount(d);
      return d;
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function doLogin() {
    const raw = $('cookie-input').value.trim();
    if (!raw) return toast('请先粘贴 Cookie', 'err');
    const btn = $('btn-cookie');
    btn.disabled = true;
    btn.textContent = '验证中…';
    try {
      const d = await post('/api/login/cookie', { cookie: raw });
      toast('登录成功：' + (d.user && d.user.uname || '账号'), 'ok');
      $('cookie-input').value = '';
      await refreshSession();
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '验证并登录';
    }
  }

  async function doLogout() {
    try {
      await post('/api/logout', {});
      STATE.buyers = [];
      $('buyer-select-wrap').classList.add('hidden');
      await refreshSession();
      toast('已退出登录');
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ---------------- 一键浏览器登录 ---------------- */

  let browserTimer = null;

  function renderBrowserProgress(st) {
    const box = $('browser-progress');
    const txt = $('browser-progress-text');
    const cancel = $('btn-browser-cancel');
    const start = $('btn-browser-login');
    const active = st && (st.state === 'launching' || st.state === 'waiting');

    if (!st || st.state === 'idle') {
      box.classList.add('hidden');
      cancel.style.display = 'none';
      start.disabled = false;
      start.textContent = '一键打开浏览器登录';
      return;
    }
    box.classList.remove('hidden', 'ok', 'err');
    if (st.state === 'logged_in') box.classList.add('ok');
    if (st.state === 'error') box.classList.add('err');
    start.disabled = !!active;
    start.textContent = active ? '等待登录中…' : '一键打开浏览器登录';
    cancel.style.display = active ? '' : 'none';
    txt.textContent = st.message || st.state;
  }

  function stopBrowserPoll() {
    if (browserTimer) { clearInterval(browserTimer); browserTimer = null; }
  }

  async function pollBrowser() {
    try {
      const d = await api('/api/login/browser/status');
      renderBrowserProgress(d.status);
      const st = d.status || {};
      if (st.state === 'logged_in') {
        stopBrowserPoll();
        toast('登录成功：' + ((st.user || {}).uname || '账号'), 'ok');
        await refreshSession();
        loadBuyers().catch(function () {});
      } else if (st.state === 'error') {
        stopBrowserPoll();
        toast(st.message || '浏览器登录失败', 'err');
      }
    } catch (e) { /* 单次轮询失败无所谓 */ }
  }

  async function doBrowserLogin() {
    const btn = $('btn-browser-login');
    btn.disabled = true;
    btn.textContent = '启动中…';
    try {
      const d = await post('/api/login/browser', {});
      renderBrowserProgress(d.status);
      browserTimer = setInterval(pollBrowser, 1200);
    } catch (e) {
      toast(e.message, 'err');
      btn.disabled = false;
      btn.textContent = '一键打开浏览器登录';
    }
  }

  async function doBrowserCancel() {
    try {
      const d = await post('/api/login/browser/cancel', {});
      renderBrowserProgress(d.status);
      stopBrowserPoll();
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ---------------- 解析 ---------------- */

  async function doParse() {
    const url = $('url-input').value.trim();
    if (!url) return toast('请输入会员购链接', 'err');
    const btn = $('btn-parse');
    btn.disabled = true; btn.textContent = '解析中…';
    try {
      const d = await post('/api/parse', { url });
      STATE.project = d.project;
      renderProject(d.project);
      toast('解析成功：' + (d.project.name || d.project.project_id), 'ok');
      syncTime();
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      btn.disabled = false; btn.textContent = '解析';
    }
  }

  function renderProject(p) {
    $('project-box').classList.remove('hidden');
    $('proj-name').textContent = p.name || ('项目 ' + p.project_id);
    const meta = [];
    meta.push('ID ' + p.project_id);
    if (p.sale_start) meta.push('开售 ' + fmtClock(p.sale_start));
    if (p.venue) meta.push(p.venue);
    else if (p.city) meta.push(p.city);
    meta.push('场次 ' + (p.screens || []).length + ' · 票档 ' + (p.skus || []).length);
    $('proj-meta').textContent = meta.join(' ｜ ');

    const sel = $('screen-select');
    sel.innerHTML = '';
    (p.screens || []).forEach((s) => {
      const bits = [];
      if (s.date) bits.push(s.date);
      if (s.tickets && s.tickets.length) bits.push(s.tickets.length + ' 档');
      if (s.sale_flag) bits.push(s.sale_flag);
      const label = (s.name || ('场次 ' + s.id)) + (bits.length ? '  · ' + bits.join(' / ') : '');
      const o = el('option', null, label);
      o.value = s.id;
      sel.appendChild(o);
    });
    if (!p.screens || !p.screens.length) {
      const o = el('option', null, '（未返回场次信息）');
      o.value = '';
      sel.appendChild(o);
    }
    renderSkus();
    sel.onchange = renderSkus;
    $('sku-select').onchange = renderSkuMeta;
  }

  function currentScreenId() {
    const v = $('screen-select').value;
    return v === '' ? null : v;
  }

  function renderSkus() {
    const p = STATE.project;
    if (!p) return;
    const sid = currentScreenId();
    const sel = $('sku-select');
    sel.innerHTML = '';
    let list = p.skus || [];
    if (sid != null) {
      const filtered = list.filter((k) => String(k.screen_id) === String(sid));
      if (filtered.length) list = filtered;
    }
    list.forEach((k) => {
      const bits = [];
      if (k.price) bits.push('¥' + (k.price / 100).toFixed(2));
      if (k.stock != null) bits.push('余 ' + k.stock);
      const o = el('option', null, (k.desc || ('票档 ' + k.id)) + (bits.length ? '  ' + bits.join(' · ') : ''));
      o.value = k.id;
      sel.appendChild(o);
    });
    if (!list.length) {
      const o = el('option', null, '（无可用票档）');
      o.value = '';
      sel.appendChild(o);
    }
    renderSkuMeta();
  }

  function renderSkuMeta() {
    const p = STATE.project;
    if (!p) return;
    const id = $('sku-select').value;
    const sku = (p.skus || []).find((k) => String(k.id) === String(id));
    const box = $('sku-meta');
    box.innerHTML = '';
    if (!sku) return;
    const add = (text, cls) => box.appendChild(el('span', cls || '', text));
    add('票档 ID ' + sku.id);
    add(sku.price_text || ('¥' + (sku.price / 100).toFixed(2)));
    if (sku.stock != null) add('余 ' + sku.stock, sku.stock > 0 ? 'hot' : 'cold');
    if (sku.sale_start) add('本档开售 ' + fmtClock(sku.sale_start));
    if (sku.limit) add('限购 ' + sku.limit + ' 张');
    if (sku.sale_flag) add(sku.sale_flag);
    if (p.sale_start) add('项目开售 ' + fmtClock(p.sale_start));
    autofillTarget(sku);
  }

  /** 选中票档后，若用户没手动改过开抢时间，就用该票档真实开售时间填进去。 */
  function autofillTarget(sku) {
    const input = $('target-time');
    if (!input || input.dataset.touched === '1') return;
    if (!sku || !sku.sale_start) return;
    input.value = fmtLocal(new Date(sku.sale_start));
    const hint = $('target-hint');
    if (hint) {
      const past = sku.sale_start < Date.now();
      hint.textContent = past
        ? '该票档已开售，可改为「立刻开抢」'
        : '已按票档开售时间自动填入，可手动修改';
      hint.className = 'hint' + (past ? ' warn' : '');
    }
  }

  /* ---------------- 实名 ---------------- */

  async function loadBuyers() {
    if (!STATE.project) return toast('请先解析链接', 'err');
    const btn = $('btn-buyers');
    btn.disabled = true; btn.textContent = '读取中…';
    try {
      const d = await api('/api/buyers?project_id=' + encodeURIComponent(STATE.project.project_id));
      STATE.buyers = d.list || [];
      const sel = $('buyer-select');
      sel.innerHTML = '';
      if (!STATE.buyers.length) {
        const o = el('option', null, '（账号里没有实名观演人，请手动填写）');
        o.value = '';
        sel.appendChild(o);
      }
      STATE.buyers.forEach((b) => {
        const o = el('option', null, b.name + '  ' + (b.personal_id_masked || ''));
        o.value = b.id;
        sel.appendChild(o);
      });
      $('buyer-select-wrap').classList.remove('hidden');
      applyBuyer(sel.value);
      toast('已读取 ' + STATE.buyers.length + ' 位观演人', 'ok');
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      btn.disabled = false; btn.textContent = '从账号同步实名';
    }
  }

  function applyBuyer(id) {
    const b = STATE.buyers.find((x) => String(x.id) === String(id));
    if (!b) return;
    $('buyer-name').value = b.name || '';
    $('buyer-tel').value = b.tel || '';
    $('buyer-id').value = b.personal_id || '';
  }

  /* ---------------- 模式 ---------------- */

  function switchMode(mode) {
    STATE.mode = mode;
    document.querySelectorAll('#mode-tabs button').forEach((b) => {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
    document.querySelectorAll('.pane').forEach((p) => {
      p.classList.toggle('hidden', p.dataset.pane !== mode);
    });
    document.querySelectorAll('.monitor-only').forEach((n) => {
      n.classList.toggle('hidden', mode !== 'monitor');
    });
    const maxInput = $('opt-max');
    if (mode === 'monitor') maxInput.value = 1800;
    else if (mode === 'now') maxInput.value = 120;
    else if (!maxInput.dataset.touched) maxInput.value = 300;
  }

  /* ---------------- 任务 ---------------- */

  function collectCfg() {
    const p = STATE.project;
    if (!p) throw new Error('请先解析链接');
    const skuId = $('sku-select').value;
    if (!skuId) throw new Error('请选择票档');
    const screenId = $('screen-select').value || (p.screens[0] && p.screens[0].id) || 0;

    const name = $('buyer-name').value.trim();
    const tel = $('buyer-tel').value.trim();
    const pid = $('buyer-id').value.trim().toUpperCase();
    if (!pid) throw new Error('请填写身份证号');

    const cfg = {
      mode: STATE.mode,
      project_id: p.project_id,
      screen_id: Number(screenId) || 0,
      sku_id: Number(skuId),
      count: Math.max(1, Number($('buyer-count').value) || 1),
      buyer: {
        id: Number($('buyer-select').value || 0),
        name,
        tel,
        personal_id: pid,
        id_type: 0,
      },
      deliver: $('deliver').value,
      concurrency: Math.max(1, Math.min(5, Number($('opt-concurrency').value) || 2)),
      retry_ms: Math.max(0, Number($('opt-retry').value) || 0),
      lead_ms: Math.max(0, Number($('opt-lead').value) || 0),
      max_seconds: Math.max(10, Number($('opt-max').value) || 300),
      monitor_ms: Math.max(200, Number($('opt-monitor').value) || 800),
    };

    if (STATE.mode === 'timed') {
      const v = $('target-time').value;
      if (!v) throw new Error('请选择开抢时间');
      const ts = new Date(v).getTime();
      if (!ts || Number.isNaN(ts)) throw new Error('开抢时间格式不正确');
      if (ts < Date.now() - 30000) throw new Error('开抢时间已经过去了');
      cfg.target_ms = ts;
    }
    return cfg;
  }

  async function startTask() {
    if (STATE.busy) return;
    let cfg;
    try { cfg = collectCfg(); } catch (e) { return toast(e.message, 'err'); }

    STATE.busy = true;
    const btn = $('btn-start');
    btn.disabled = true; btn.textContent = '启动中…';
    try {
      const d = await post('/api/task', { cfg });
      STATE.running = d.task;
      renderRun(d.task);
      toast('任务已启动', 'ok');
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      STATE.busy = false;
      btn.disabled = false; btn.textContent = '开始抢票';
    }
  }

  async function stopTask() {
    if (!STATE.running) return;
    try {
      await post('/api/task/stop', { id: STATE.running.id });
      toast('已发送停止指令');
    } catch (e) { toast(e.message, 'err'); }
  }

  const STATUS_CLASS = {
    idle: 'idle', pending: 'waiting', preparing: 'preparing', waiting: 'waiting',
    firing: 'firing', monitoring: 'monitoring', success: 'success',
    failed: 'failed', stopped: 'stopped',
  };

  function renderRun(task) {
    if (!task) {
      $('run-badge').className = 'badge idle';
      $('run-badge').textContent = '空闲';
      $('run-detail').textContent = '尚未开始';
      $('btn-start').classList.remove('hidden');
      $('btn-stop').classList.add('hidden');
      $('countdown').textContent = '--:--:--';
      return;
    }
    const st = task.status || 'idle';
    const badge = $('run-badge');
    badge.className = 'badge ' + (STATUS_CLASS[st] || 'idle');
    badge.textContent = task.status_text || st;
    $('run-detail').textContent = task.detail || '';
    $('run-meta').textContent = '开抢时间 ' + (task.fire_at ? fmtClock(task.fire_at) : '立即') +
      (task.attempts ? ' ｜ 已尝试 ' + task.attempts + ' 次' : '');

    const active = ['pending', 'preparing', 'waiting', 'firing', 'monitoring'].includes(st);
    $('btn-start').classList.toggle('hidden', active);
    $('btn-stop').classList.toggle('hidden', !active);

    const box = $('result');
    if (st === 'success' && task.order_id) {
      box.className = 'result ok';
      box.classList.remove('hidden');
      box.innerHTML = '抢票成功！订单号 <b>' + escapeHtml(task.order_id) + '</b><br>' +
        (task.pay_url ? '支付入口：<a href="' + escapeHtml(task.pay_url) + '" target="_blank" rel="noopener">' + escapeHtml(task.pay_url) + '</a><br>' : '') +
        '请在订单页 15 分钟内完成支付。';
    } else if (st === 'failed') {
      box.className = 'result err';
      box.classList.remove('hidden');
      box.textContent = '失败：' + (task.detail || '未知原因');
    } else if (st === 'stopped') {
      box.className = 'result err';
      box.classList.remove('hidden');
      box.textContent = '任务已停止';
    } else {
      box.classList.add('hidden');
    }
  }

  /* ---------------- 倒计时 ---------------- */

  function tick() {
    const t = STATE.running;
    const cd = $('countdown');
    const bar = $('progress-bar');
    if (!t || !['pending', 'preparing', 'waiting'].includes(t.status)) {
      if (t && t.status === 'firing') { cd.textContent = '开抢中'; cd.classList.add('hot'); bar.style.width = '100%'; }
      else if (t && t.status === 'monitoring') { cd.textContent = '蹲票中'; cd.classList.remove('hot'); }
      else if (t && t.status === 'success') { cd.textContent = '成功'; cd.classList.remove('hot'); bar.style.width = '100%'; }
      else { cd.textContent = '--:--:--'; cd.classList.remove('hot'); bar.style.width = '0%'; }
      return;
    }
    const remain = (t.fire_at - Date.now()) / 1000;
    cd.textContent = remain > 0 ? fmtCountdown(remain) : '00:00:00';
    cd.classList.toggle('hot', remain <= 10);
    const total = Math.max(1, (t.fire_at - t.created_at) / 1000);
    const pct = Math.max(0, Math.min(100, 100 - (remain / total) * 100));
    bar.style.width = pct.toFixed(1) + '%';
  }

  /* ---------------- 日志 ---------------- */

  function appendLog(ev) {
    const box = $('log');
    const line = el('div', 'line');
    const d = new Date(ev.ts || Date.now());
    const ts = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    line.appendChild(el('span', 't', ts));
    line.appendChild(el('span', ev.level || 'info', ev.msg || ''));
    box.appendChild(line);
    while (box.childElementCount > 500) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  /* ---------------- SSE ---------------- */

  function connectSSE() {
    const es = new EventSource('/api/events');
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (data.type === 'log') {
        appendLog(data);
      } else if (data.type === 'task') {
        if (STATE.running && data.task === STATE.running.id) {
          STATE.running = Object.assign({}, STATE.running, data);
          renderRun(STATE.running);
        } else {
          api('/api/tasks').then((d) => {
            const t = (d.tasks || [])[0];
            if (t && t.id === data.task) { STATE.running = t; renderRun(t); }
          }).catch(() => {});
        }
      }
    };
    es.onerror = () => { /* EventSource 会自动重连 */ };
  }

  /* ---------------- 对时 ---------------- */

  async function syncTime() {
    try {
      const d = await api('/api/ntp');
      const s = d.sync || {};
      STATE.offsetMs = s.offset_ms || 0;
      STATE.serverRtt = s.rtt_ms || 0;
      const chip = $('chip-ntp');
      chip.textContent = '对时 ' + (STATE.offsetMs >= 0 ? '+' : '') + STATE.offsetMs.toFixed(0) + 'ms / RTT ' + STATE.serverRtt.toFixed(0) + 'ms';
      chip.className = 'chip ' + (Math.abs(STATE.offsetMs) < 800 ? 'ok' : 'warn');
    } catch (e) { /* 静默 */ }
  }

  function clockTick() {
    const d = new Date();
    $('chip-clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  /* ---------------- 初始化 ---------------- */

  async function boot() {
    $('target-time').value = fmtLocal(new Date(Date.now() + 5 * 60 * 1000));

    $('btn-cookie').onclick = doLogin;
    $('btn-logout').onclick = doLogout;
    $('btn-browser-login').onclick = doBrowserLogin;
    $('btn-browser-cancel').onclick = doBrowserCancel;
    $('btn-parse').onclick = doParse;
    $('btn-buyers').onclick = loadBuyers;
    $('btn-start').onclick = startTask;
    $('btn-stop').onclick = stopTask;

    $('url-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') doParse(); });

    $('buyer-select').onchange = (e) => applyBuyer(e.target.value);
    $('buyer-id').addEventListener('input', () => { $('buyer-select').value = ''; });

    document.querySelectorAll('#mode-tabs button').forEach((b) => {
      b.onclick = () => switchMode(b.dataset.mode);
    });

    document.querySelectorAll('[data-plus]').forEach((b) => {
      b.onclick = () => {
        const sec = Number(b.dataset.plus);
        const t = $('target-time');
        t.value = fmtLocal(new Date(Date.now() + sec * 1000));
        t.dataset.touched = '1';
      };
    });

    $('target-time').addEventListener('input', (e) => { e.target.dataset.touched = '1'; });

    $('opt-max').addEventListener('input', (e) => { e.target.dataset.touched = '1'; });

    switchMode('timed');
    renderRun(null);

    try {
      const d = await api('/api/login/browser/status');
      renderBrowserProgress(d.status);
      if (d.status && (d.status.state === 'launching' || d.status.state === 'waiting')) {
        browserTimer = setInterval(pollBrowser, 1200);
      }
    } catch (e) { /* ignore */ }

    try {
      await refreshSession();
      const tasks = await api('/api/tasks');
      if (tasks.tasks && tasks.tasks.length) {
        const t = tasks.tasks[0];
        STATE.running = t;
        renderRun(t);
      }
    } catch (e) { /* ignore */ }

    connectSSE();
    syncTime();
    clockTick();
    setInterval(clockTick, 1000);
    setInterval(tick, 100);
    setInterval(() => { if (!STATE.running) syncTime(); }, 60000);

    appendLog({ ts: Date.now(), level: 'info', msg: '控制台就绪，按顺序完成 1 → 4 步后点击「开始抢票」' });
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
