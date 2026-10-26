(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const ACTIVE = new Set(["submitting", "waiting_reply", "confirming", "generating", "downloading"]);
  const FINAL = new Set(["success", "failed", "canceled"]);
  const FILTERS = {
    all: () => true,
    active: (t) => ACTIVE.has(t.status) || t.status === "queued",
    retry: (t) => t.status === "retry_wait",
    success: (t) => t.status === "success",
    failed: (t) => t.status === "failed" || t.status === "canceled",
  };

  const state = {
    tasks: new Map(),
    global: {},
    settings: {},
    filter: "all",
    openLogs: new Set(),
    openHist: new Set(),
    folder: null, // last folder scan result
    autoPrompt: "", // text we auto-filled from prompt.txt
  };

  // ------------------------------------------------------------ helpers
  async function api(path, options = {}) {
    const opts = { method: "GET", headers: {}, ...options };
    if (opts.body !== undefined && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["content-type"] = "application/json";
    }
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (_) { /* empty body */ }
    if (!resp.ok) throw new Error((data && data.detail) || `请求失败（${resp.status}）`);
    return data;
  }

  let toastTimer = null;
  function toast(text, isError = false) {
    const el = $("#toast");
    $("span", el).textContent = text;
    $("use", el).setAttribute("href", isError ? "#i-alert" : "#i-check");
    el.className = "toast show" + (isError ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast" + (isError ? " err" : ""); }, isError ? 5600 : 2800);
  }

  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  const icon = (name, cls = "ic") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }
  const fmtClock = (ts) => new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  const fmtHM = (ts) => new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  function fmtSize(bytes) {
    if (!bytes) return "";
    return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
  }
  const basename = (p) => String(p || "").replace(/\/+$/, "").split("/").pop() || p;
  const now = () => Date.now() / 1000;

  async function withBusy(button, fn) {
    if (button) button.disabled = true;
    try { return await fn(); }
    catch (e) { toast(e.message, true); }
    finally { if (button) button.disabled = false; }
  }

  // ------------------------------------------------------------ global state & banners
  const coolingDown = (g) => Boolean(g.cooldown_until && g.cooldown_until > now());

  function renderGlobal() {
    const g = state.global || {};
    const pill = $("#conn-status");
    let cls = "", label = "Chrome 未启动";
    if (g.browser === "starting") { cls = "warn"; label = "正在启动 Chrome"; }
    else if (g.browser === "error") { cls = "err"; label = "Chrome 启动失败"; }
    else if (g.browser === "ready") {
      if (g.login === "logged_in") { cls = "ok"; label = "已登录豆包"; }
      else if (g.login === "logged_out") { cls = "err"; label = "豆包已下线"; }
      else { cls = "warn"; label = "Chrome 已就绪"; }
    }
    if (g.captcha) { cls = "warn"; label = "等待安全验证"; }
    pill.className = "status-pill " + cls;
    $("span", pill).textContent = label;

    if (g.fixed) {
      $("#fx-model").textContent = g.fixed.model;
      $("#fx-ratio").textContent = g.fixed.ratio;
      $("#fx-duration").textContent = `${g.fixed.duration}s`;
    }
    const pauseBtn = $("#btn-pause");
    $("span", pauseBtn).textContent = g.paused ? "继续队列" : "暂停队列";
    $("use", pauseBtn).setAttribute("href", g.paused ? "#i-play" : "#i-pause");

    const parts = [];
    if (g.login === "logged_out") parts.push(loginCard(g));
    if (g.captcha) parts.push(captchaCard(g));
    if (g.browser === "error") parts.push(alertBar("err", "alert", `<b>Chrome 启动失败：</b>${esc(g.browser_error)}`));
    if (coolingDown(g)) {
      const at = fmtHM(g.cooldown_until);
      const msg = g.cooldown_kind === "rate_limit"
        ? `<b>豆包提示操作太频繁。</b>为避免被下线，暂停提交到 ${at}，之后自动继续。`
        : `<b>豆包创作额度已用完。</b>新任务暂停提交，${at} 自动再试（额度恢复前会定时重试）。`;
      parts.push(alertBar("warn", "clock", msg, `<button class="btn sm" data-act="quota-now">${icon("refresh")}立即重试</button>`));
    }
    if (g.paused) {
      parts.push(alertBar("info", "pause", `<b>队列已暂停。</b>${esc(g.pause_reason || "")}`, `<button class="btn primary sm" data-act="resume">${icon("play")}继续队列</button>`));
    }
    const html = parts.join("");
    const box = $("#banners");
    if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
    renderLoginLine();
  }

  function alertBar(kind, ic, html, actions = "") {
    return `<div class="alert ${kind}">${icon(ic)}<p>${html}</p>${actions}</div>`;
  }

  function loginCard(g) {
    const qr = g.qr_version
      ? `<img alt="豆包登录二维码" src="/api/login/qr.png?v=${g.qr_version}">`
      : `<div class="qr-wait"><span class="spinner"></span>${esc(g.qr_error || "正在打开豆包登录二维码…")}</div>`;
    const qrState = g.qr_text ? `<div class="qr-state">${icon("info")}${esc(g.qr_text.replace(/\s+/g, " "))}</div>` : "";
    const extra = g.qr_version && g.qr_error ? `<p class="reason">${esc(g.qr_error)}</p>` : "";
    return `<div class="login-card">
      <div class="qr-frame">${qr}<i class="corner c1"></i><i class="corner c2"></i><i class="corner c3"></i><i class="corner c4"></i></div>
      <div class="login-info">
        <h3>${icon("alert")}豆包已下线，请扫码重新登录</h3>
        <p class="reason">${esc(g.login_reason || "检测到豆包退出了登录")}</p>
        <ul class="steps-list">
          <li><span>1</span>打开手机上的「豆包」App（或飞书 App）</li>
          <li><span>2</span>扫描左侧二维码，在手机上确认登录</li>
          <li><span>3</span>登录成功后任务自动继续，不用回来点任何按钮</li>
        </ul>
        ${qrState}${extra}
        <div class="login-actions">
          <button class="btn sm" data-act="qr">${icon("refresh")}刷新二维码</button>
          <button class="btn sm" data-act="import-login">${icon("window")}从日常 Chrome 导入登录</button>
          <button class="btn ghost sm" data-act="show-chrome">${icon("external")}在 Chrome 中查看</button>
        </div>
      </div>
    </div>`;
  }

  function captchaCard(g) {
    return `<div class="captcha-card">
      <div>
        <h3>${icon("shield")}豆包需要安全验证</h3>
        <p>已暂停所有操作，并把自动化 Chrome 窗口切到前台。请在 Chrome 中<b>手动完成验证码</b>，完成后任务会自动继续。</p>
        <button class="btn primary sm" data-act="show-chrome">${icon("window")}切换到 Chrome</button>
      </div>
      <img class="captcha-shot" id="captcha-shot" alt="验证码截图（点击放大）" data-act="zoom-captcha" src="/api/captcha.png?v=${g.captcha_version || 0}">
    </div>`;
  }

  // ------------------------------------------------------------ task rendering
  const STEPS = ["提交", "确认", "生成", "下载"];
  const KIND_STEP = { images: 0, submit: 0, internal: 0, no_reply: 1, no_start: 1, continue: 1, policy: 1, quota: 1, rate_limit: 1, generation: 2, timeout: 2, monitor: 2, monitor_transient: 2, download: 3 };

  function stepState(task) {
    switch (task.status) {
      case "submitting": return { index: 0, mode: "current" };
      case "waiting_reply": case "confirming": return { index: 1, mode: "current" };
      case "generating": return { index: 2, mode: "current" };
      case "downloading": return { index: 3, mode: "current" };
      case "success": return { index: 4, mode: "done" };
      case "retry_wait": return { index: KIND_STEP[(task.error || {}).kind] ?? 1, mode: "hold" };
      case "failed": return { index: KIND_STEP[(task.error || {}).kind] ?? 1, mode: "bad" };
      default: return { index: -1, mode: "none" };
    }
  }

  function stepperHtml(task) {
    const s = stepState(task);
    return `<div class="stepper">${STEPS.map((label, i) => {
      let cls = "", dot = "";
      if (s.mode === "done" || i < s.index) { cls = "done"; dot = icon("check"); }
      else if (i === s.index) {
        cls = s.mode === "bad" ? "bad" : s.mode === "hold" ? "hold" : "current";
        dot = s.mode === "bad" ? icon("x") : s.mode === "hold" ? icon("retry") : "";
      }
      const lineDone = s.mode === "done" || i < s.index;
      const line = i < STEPS.length - 1 ? `<div class="step-line${lineDone ? " done" : ""}"></div>` : "";
      return `<div class="step ${cls}"><span class="dot">${dot}</span><span class="lbl">${label}</span></div>${line}`;
    }).join("")}</div>`;
  }

  function pill(task) {
    let cls = "active";
    if (task.status === "success") cls = "success";
    else if (task.status === "failed") cls = "failed";
    else if (task.status === "canceled") cls = "muted";
    else if (task.status === "retry_wait") cls = "wait";
    else if (task.status === "queued") cls = state.global.paused ? "wait" : "";
    else if (/验证|登录/.test(task.stage || "")) cls = "wait";
    return `<span class="pill ${cls}">${esc(task.status_label)}</span>`;
  }

  function thumbUrl(folder, name) {
    return `/api/folder/thumb?folder=${encodeURIComponent(folder)}&name=${encodeURIComponent(name)}`;
  }

  function mediaHtml(task) {
    const files = (task.result && task.result.files) || [];
    if (task.status === "success" && files.length) {
      const f = files[0];
      const dur = f.duration ? `<span class="dur">${Math.round(f.duration)}s</span>` : "";
      return `<div class="media"><img alt="" src="/api/tasks/${task.id}/cover/0" data-fallback="/api/tasks/${task.id}/video/0">${dur}
        <button class="play-btn" data-act="play" data-index="0" aria-label="播放视频"><span>${icon("play")}</span></button></div>`;
    }
    if (task.status === "generating") {
      return `<div class="media gen"><div class="gen-info"><b data-pct>0%</b><span>生成中</span></div></div>`;
    }
    const first = (task.image_names || [])[0];
    const cls = task.status === "canceled" ? "gray" : ACTIVE.has(task.status) ? "dim" : "";
    const img = task.folder && first ? `<img alt="" src="${thumbUrl(task.folder, first)}" data-hide-on-error>` : "";
    const ph = `<div class="ph">${icon(first ? "image" : "film")}</div>`;
    const busy = ACTIVE.has(task.status) ? `<div class="busy"><span class="spinner"></span></div>` : "";
    return `<div class="media ${cls}">${ph}${img}${busy}</div>`;
  }

  function stageHtml(task) {
    let lead = "";
    if (ACTIVE.has(task.status)) lead = `<span class="spinner"></span>`;
    else if (task.status === "success") lead = `<span style="color:var(--ok);display:flex">${icon("check")}</span>`;
    else if (task.status === "failed") lead = `<span style="color:var(--err);display:flex">${icon("alert")}</span>`;
    else if (task.status === "retry_wait") lead = `<span style="color:var(--warn);display:flex">${icon("retry")}</span>`;
    else if (task.status === "queued") lead = `<span style="color:var(--muted);display:flex">${icon("clock")}</span>`;
    let html = `<div class="stage">${lead}<span>${esc(task.stage || task.status_label)}</span><span class="time" data-time></span></div>`;
    if (task.status === "generating") {
      html += `<div class="progress"><div data-bar style="width:0%"></div></div><div class="progress-note" data-eta></div>`;
    } else if (ACTIVE.has(task.status)) {
      html += `<div class="progress indeterminate"><div></div></div>`;
    }
    return html;
  }

  function timeText(task) {
    const t = now();
    if (task.status === "retry_wait" && task.next_retry_at) {
      const left = task.next_retry_at - t;
      return left > 0 ? `${fmtDuration(left)} 后重试` : "即将重试";
    }
    if (task.status === "generating" && task.generating_since) return `已生成 ${fmtDuration(t - task.generating_since)}`;
    if (ACTIVE.has(task.status) && task.submitted_at) return `已用 ${fmtDuration(t - task.submitted_at)}`;
    if (FINAL.has(task.status) && task.finished_at && task.submitted_at) return `耗时 ${fmtDuration(task.finished_at - task.submitted_at)}`;
    if (task.status === "queued") return `${fmtHM(task.created_at)} 加入`;
    return "";
  }

  function genProgress(task) {
    const eta = (task.eta_minutes || 15) * 60;
    const elapsed = Math.max(0, now() - (task.generating_since || now()));
    return { pct: Math.min(97, (elapsed / eta) * 100), over: elapsed > eta };
  }

  function updateTimers(el, task) {
    const time = $("[data-time]", el);
    if (time) time.textContent = timeText(task);
    if (task.status !== "generating") return;
    const p = genProgress(task);
    const bar = $("[data-bar]", el);
    if (bar) bar.style.width = p.pct.toFixed(1) + "%";
    const pct = $("[data-pct]", el);
    if (pct) pct.textContent = Math.floor(p.pct) + "%";
    const etaEl = $("[data-eta]", el);
    if (etaEl) {
      const base = task.eta_minutes ? `豆包预计约 ${task.eta_minutes} 分钟` : "通常需要 10–20 分钟";
      etaEl.textContent = p.over ? `${base}，已超过预计时间，仍在等待豆包返回…` : `${base}，完成后自动下载`;
    }
  }

  function filesHtml(task) {
    const files = (task.result && task.result.files) || [];
    if (!files.length) return "";
    return `<div class="files">${files.map((f, i) => {
      const tags = [
        f.width && f.height ? `${f.width}×${f.height}` : "",
        f.duration ? `${Number(f.duration).toFixed(1)} 秒` : "",
        fmtSize(f.size),
        f.watermark_free ? "无水印" : "带水印",
      ].filter(Boolean).join(" · ");
      return `<div class="file">
        <span class="f-icon">${icon("film")}</span>
        <div class="f-text"><b title="${esc(f.path)}">${esc(f.name)}</b><span>${esc(tags)}</span></div>
        <div class="f-btns">
          <button class="btn sm" data-act="play" data-index="${i}">${icon("play")}播放</button>
          <button class="btn sm" data-act="open" data-path="${esc(f.path)}">${icon("external")}打开</button>
          <button class="btn sm" data-act="reveal" data-path="${esc(f.path)}">${icon("folder")}在 Finder 中显示</button>
        </div>
      </div>`;
    }).join("")}</div>`;
  }

  function taskHtml(task) {
    const active = ACTIVE.has(task.status);
    const imgs = (task.image_names || []).length;
    const meta = [
      `<span>${icon("image")}${imgs ? `${imgs} 张参考图` : "无参考图"}</span>`,
      task.folder ? `<span title="${esc(task.folder)}">${icon("folder")}<span class="folder-name">${esc(basename(task.folder))}</span></span>` : "",
      task.continues ? `<span>${icon("chat")}已自动确认 ${task.continues} 次</span>` : "",
      task.retry_count ? `<span>${icon("retry")}已重试 ${task.retry_count} 次</span>` : "",
    ].filter(Boolean).join("");

    let body = `<div class="task-top"><div class="task-title" title="${esc(task.title)}">${esc(task.title)}</div>${pill(task)}</div>
      <div class="meta">${meta}</div>
      <div class="prompt-text" title="${esc(task.prompt)}">${esc(task.prompt)}</div>
      ${stepperHtml(task)}
      ${stageHtml(task)}`;

    const errText = (task.error && task.error.message) || "";
    if ((task.status === "failed" || task.status === "retry_wait") && errText) {
      const kind = task.status === "failed" ? "err" : "warn";
      body += `<div class="callout ${kind}">${icon("alert")}<span>${task.status === "retry_wait" ? "上次失败：" : ""}${esc(errText)}</span></div>`;
    }
    const replies = task.replies || [];
    const last = replies.length ? replies[replies.length - 1] : null;
    const dup = last && errText && errText.includes(String(last.text || "").slice(0, 24));
    if (last && task.status !== "success" && !dup) {
      body += `<div class="quote"><b>${icon("chat")}豆包最新回复</b>${esc(last.text)}</div>`;
    }
    if (task.status === "success") {
      body += filesHtml(task);
      const reply = (task.result && task.result.reply) || "";
      if (reply.replace(/\s+/g, "").length > 30) body += `<div class="quote"><b>${icon("chat")}豆包回复</b>${esc(reply)}</div>`;
    }

    const btns = [];
    if (task.status === "retry_wait") btns.push(`<button class="btn primary sm" data-act="retry">${icon("bolt")}立即重试</button>`);
    if (task.status === "failed" || task.status === "canceled") btns.push(`<button class="btn sm" data-act="retry">${icon("retry")}重试（新对话）</button>`);
    if ((task.status === "failed" || task.status === "canceled") && task.conversation_id) btns.push(`<button class="btn sm" data-act="recheck">${icon("refresh")}重新检查结果</button>`);
    if (task.conversation_url) btns.push(`<a class="btn ghost sm" href="${esc(task.conversation_url)}" target="_blank" rel="noopener">${icon("external")}豆包对话</a>`);
    if (task.status === "queued" || task.status === "retry_wait" || active) btns.push(`<button class="btn ghost sm danger" data-act="cancel">${icon("x")}取消</button>`);
    if (!active) btns.push(`<button class="btn ghost sm" data-act="delete">${icon("trash")}删除</button>`);
    body += `<div class="actions">${btns.join("")}</div>`;

    const events = task.events || [];
    const logs = [`<details class="log" data-log="${task.id}"${state.openLogs.has(task.id) ? " open" : ""}><summary>过程记录（${events.length}）</summary><ul class="timeline">${events.slice().reverse().map((e) => `<li class="${esc(e.level)}"><time>${fmtClock(e.t)}</time><span>${esc(e.text)}</span></li>`).join("")}</ul></details>`];
    const hist = task.history || [];
    if (hist.length) {
      logs.push(`<details class="log" data-hist="${task.id}"${state.openHist.has(task.id) ? " open" : ""}><summary>失败记录（${hist.length}）</summary><ul class="timeline">${hist.slice(-50).reverse().map((h) => `<li class="warn"><time>${fmtClock(h.t)}</time><span>第 ${h.attempt} 次：${esc(h.message)}</span></li>`).join("")}</ul></details>`);
    }
    body += `<div class="log-row">${logs.join("")}</div>`;

    return `${mediaHtml(task)}<div class="task-main">${body}</div>`;
  }

  function cardClass(task) {
    let cls = "task";
    if (ACTIVE.has(task.status)) cls += " is-active";
    if (task.status === "success") cls += " is-success";
    if (task.status === "failed") cls += " is-failed";
    return cls;
  }

  function placeCard(el, task) {
    // Keep cards ordered newest first.
    const list = $("#task-list");
    const after = [...state.tasks.values()]
      .filter((t) => t.id !== task.id && t.created_at < task.created_at)
      .sort((a, b) => b.created_at - a.created_at)
      .map((t) => document.getElementById("task-" + t.id))
      .find(Boolean);
    list.insertBefore(el, after || null);
  }

  function renderTask(task) {
    let el = document.getElementById("task-" + task.id);
    if (!el) {
      el = document.createElement("article");
      el.id = "task-" + task.id;
      el.dataset.id = task.id;
      placeCard(el, task);
    }
    el.className = cardClass(task);
    const html = taskHtml(task);
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
    el.hidden = !FILTERS[state.filter](task);
    updateTimers(el, task);
  }

  function upsertTask(task) {
    const old = state.tasks.get(task.id);
    if (old && old.updated_at > task.updated_at) return; // stale snapshot (e.g. a late HTTP response)
    state.tasks.set(task.id, task);
    renderTask(task);
    renderMeta();
  }

  function removeTask(id) {
    state.tasks.delete(id);
    const el = document.getElementById("task-" + id);
    if (el) el.remove();
    renderMeta();
  }

  function renderMeta() {
    const tasks = [...state.tasks.values()];
    $("#task-count").textContent = tasks.length;
    for (const btn of $$("#filters button")) {
      const f = btn.dataset.filter;
      $("span", btn).textContent = tasks.filter(FILTERS[f]).length;
      btn.classList.toggle("active", f === state.filter);
    }
    const visible = tasks.filter(FILTERS[state.filter]).length;
    $("#empty").hidden = visible > 0;
    if (!visible) {
      $("#empty-title").textContent = tasks.length ? "这个分类下没有任务" : "还没有任务";
      $("#empty-sub").textContent = tasks.length ? "切换到「全部」查看其他任务。" : "在左侧写好提示词、选择参考图文件夹，点「加入队列」即可开始。";
    }
  }

  function renderAll(tasks) {
    $("#task-list").innerHTML = "";
    state.tasks.clear();
    for (const t of tasks) state.tasks.set(t.id, t);
    for (const t of tasks) renderTask(t);
    renderMeta();
  }

  // Images that fail to load: covers fall back to a video frame, thumbnails disappear.
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement)) return;
    if (img.dataset.fallback) {
      const v = document.createElement("video");
      v.muted = true;
      v.preload = "metadata";
      v.playsInline = true;
      v.src = img.dataset.fallback + "#t=1";
      img.replaceWith(v);
    } else if (img.hasAttribute("data-hide-on-error")) {
      img.remove();
    }
  }, true);

  setInterval(() => {
    for (const task of state.tasks.values()) {
      if (!ACTIVE.has(task.status) && task.status !== "retry_wait") continue;
      const el = document.getElementById("task-" + task.id);
      if (el) updateTimers(el, task);
    }
    if (state.global.cooldown_until && !coolingDown(state.global)) { state.global.cooldown_until = 0; renderGlobal(); }
  }, 1000);

  // ------------------------------------------------------------ live events (subscribe first, then snapshot)
  let es = null;
  let buffered = null;

  function handleEvent(ev) {
    if (ev.type === "task") upsertTask(ev.task);
    else if (ev.type === "removed") removeTask(ev.id);
    else if (ev.type === "state") { state.global = ev.state; renderGlobal(); }
    else if (ev.type === "hello") { state.global = ev.state; renderGlobal(); }
    else if (ev.type === "captcha") {
      const img = $("#captcha-shot");
      if (img && ev.version % 2 === 0) img.src = `/api/captcha.png?v=${ev.version}`;
    }
  }

  async function snapshot() {
    const data = await api("/api/bootstrap");
    $("#version").textContent = `v${data.version}`;
    state.settings = data.settings;
    state.global = data.state;
    renderGlobal();
    renderAll(data.tasks);
  }

  function connect() {
    if (es) es.close();
    buffered = [];
    es = new EventSource("/api/events");
    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (_) { return; }
      if (buffered) buffered.push(ev); else handleEvent(ev);
    };
    es.onopen = async () => {
      try { await snapshot(); } catch (e) { toast(e.message, true); }
      const pending = buffered || [];
      buffered = null;
      pending.forEach(handleEvent);
    };
    es.onerror = () => {
      const p = $("#conn-status");
      p.className = "status-pill err";
      $("span", p).textContent = "与本地服务断开";
      es.close();
      es = null;
      setTimeout(connect, 3000);
    };
  }

  // ------------------------------------------------------------ composer
  for (const btn of $$(".seg button")) {
    btn.addEventListener("click", () => {
      for (const b of $$(".seg button")) {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      }
      $("#form-single").hidden = btn.dataset.mode !== "single";
      $("#form-batch").hidden = btn.dataset.mode !== "batch";
    });
  }

  const promptEl = $("#prompt");
  const updateCount = () => { $("#prompt-count").textContent = `${promptEl.value.trim().length} 字`; };
  promptEl.addEventListener("input", updateCount);

  function renderFolderZone() {
    const zone = $("#folder-zone");
    const data = state.folder;
    $("#img-count").textContent = data && !data.error ? `${data.count} / ${data.limit} 张` : "";
    if (!data) {
      zone.innerHTML = `<button type="button" class="dropzone" data-act="pick">
          <span class="dz-icon">${icon("folder-plus")}</span>
          <strong>选择参考图片文件夹</strong>
          <span>最多 10 张 · jpg / png / webp / heic 均可</span>
        </button>
        <div class="dz-foot"><button type="button" class="linkish" data-act="manual">或手动输入文件夹路径</button></div>`;
      return;
    }
    const head = `<div class="folder-head"><span class="fh-icon">${icon("folder")}</span>
        <div class="fh-text"><b>${esc(basename(data.folder))}</b><span title="${esc(data.folder)}">${esc(data.folder)}</span></div>
        <button type="button" class="btn sm" data-act="pick">更换</button>
        <button type="button" class="icon-btn" data-act="clear-folder" aria-label="清除文件夹">${icon("x")}</button>
      </div>`;
    if (data.error) {
      zone.innerHTML = `<div class="folder-card">${head}<div class="folder-body"><div class="note err">${icon("alert")}<span>${esc(data.error)}</span></div></div></div>`;
      return;
    }
    const thumbs = data.images.slice(0, 10).map((name, i) =>
      `<figure title="${esc(name)}"><img loading="lazy" src="${thumbUrl(data.folder, name)}" alt="" data-hide-on-error><span class="idx">${i + 1}</span></figure>`).join("");
    const pct = Math.min(100, (data.count / data.limit) * 100);
    const notes = [];
    if (data.too_many) notes.push(`<div class="note err">${icon("alert")}<span>超过 ${data.limit} 张，豆包不接受。请减少到 ${data.limit} 张以内。</span></div>`);
    if (!data.count) notes.push(`<div class="note warn">${icon("info")}<span>文件夹里没有可用图片，将只用提示词生成。</span></div>`);
    if (data.prompt) notes.push(`<div class="note ok">${icon("check")}<span>已读取文件夹里的 prompt.txt</span></div>`);
    if (data.skipped.length) notes.push(`<div class="note muted">${icon("info")}<span>已忽略：${esc(data.skipped.slice(0, 5).join("、"))}${data.skipped.length > 5 ? " 等" : ""}</span></div>`);
    zone.innerHTML = `<div class="folder-card">${head}
      <div class="folder-body">
        ${data.count ? `<div class="thumbs">${thumbs}</div>` : ""}
        <div class="meter${data.too_many ? " over" : ""}"><div class="bar"><div style="width:${pct}%"></div></div><span>${data.count} / ${data.limit}</span></div>
        ${notes.join("")}
      </div>
    </div>`;
  }

  // Replace the prompt with the folder's prompt.txt unless the user typed their own.
  function applyAutoPrompt(text) {
    const current = promptEl.value.trim();
    if (!current || current === state.autoPrompt.trim()) {
      promptEl.value = text;
      updateCount();
    }
    state.autoPrompt = text;
  }

  let scanSeq = 0;
  async function scanFolder() {
    const folder = $("#folder").value.trim();
    const seq = ++scanSeq;
    if (!folder) {
      applyAutoPrompt("");
      state.folder = null;
      renderFolderZone();
      return;
    }
    try {
      const data = await api("/api/folder/scan", { method: "POST", body: { folder } });
      if (seq !== scanSeq) return;
      state.folder = data;
      applyAutoPrompt(data.prompt || "");
    } catch (e) {
      if (seq !== scanSeq) return;
      state.folder = { folder, error: e.message };
      applyAutoPrompt("");
    }
    renderFolderZone();
  }

  let scanTimer = null;
  $("#folder").addEventListener("input", () => { clearTimeout(scanTimer); scanTimer = setTimeout(scanFolder, 350); });

  async function pickInto(target) {
    const data = await api("/api/folder/pick", { method: "POST" });
    if (data.folder) { target.value = data.folder; return true; }
    return false;
  }

  $("#folder-zone").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "pick") withBusy(btn, async () => { if (await pickInto($("#folder"))) await scanFolder(); });
    else if (act === "manual") { $("#manual-path").hidden = false; $("#folder").focus(); }
    else if (act === "clear-folder") { $("#folder").value = ""; scanFolder(); }
  });
  $("#btn-pick-batch").addEventListener("click", (e) => withBusy(e.currentTarget, () => pickInto($("#batch-parent"))));

  $("#form-single").addEventListener("submit", (e) => {
    e.preventDefault();
    withBusy($("#btn-add"), async () => {
      if (state.folder && state.folder.too_many) throw new Error(`参考图超过 ${state.folder.limit} 张，请先减少。`);
      const task = await api("/api/tasks", { method: "POST", body: { prompt: promptEl.value, folder: $("#folder").value } });
      upsertTask(task);
      promptEl.value = "";
      state.autoPrompt = "";
      updateCount();
      $("#folder").value = "";
      state.folder = null;
      renderFolderZone();
      if (!FILTERS[state.filter](task)) { state.filter = "all"; applyFilter(); }
      toast("已加入队列");
    });
  });

  $("#form-batch").addEventListener("submit", (e) => {
    e.preventDefault();
    withBusy($("#btn-add-batch"), async () => {
      const data = await api("/api/tasks/batch", { method: "POST", body: { parent: $("#batch-parent").value, default_prompt: $("#batch-prompt").value } });
      data.added.forEach(upsertTask);
      let msg = `已加入 ${data.added.length} 个任务`;
      if (data.skipped.length) msg += `，跳过 ${data.skipped.length} 个：${data.skipped.map((s) => `${s.folder}（${s.reason}）`).join("；")}`;
      toast(msg, data.skipped.length > 0 && !data.added.length);
    });
  });

  // ------------------------------------------------------------ board actions
  function applyFilter() {
    for (const t of state.tasks.values()) {
      const el = document.getElementById("task-" + t.id);
      if (el) el.hidden = !FILTERS[state.filter](t);
    }
    renderMeta();
  }
  $("#filters").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    state.filter = btn.dataset.filter;
    applyFilter();
  });

  function openPlayer(task, index) {
    const f = ((task.result && task.result.files) || [])[index];
    if (!f) return;
    const video = $("#player-video");
    video.src = `/api/tasks/${task.id}/video/${index}`;
    $("#player-title").textContent = task.title;
    $("#player-meta").innerHTML = `<span>${esc(f.name)}</span><span class="row"><button class="btn sm" data-act="open" data-path="${esc(f.path)}">${icon("external")}打开</button><button class="btn sm" data-act="reveal" data-path="${esc(f.path)}">${icon("folder")}在 Finder 中显示</button></span>`;
    $("#player").showModal();
    video.play().catch(() => {});
  }

  const openPath = (btn) => api("/api/open", { method: "POST", body: { path: btn.dataset.path, reveal: btn.dataset.act === "reveal" } });

  $("#task-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const card = btn.closest(".task");
    const id = card && card.dataset.id;
    const task = state.tasks.get(id);
    const act = btn.dataset.act;
    if (act === "play") { if (task) openPlayer(task, Number(btn.dataset.index || 0)); return; }
    withBusy(btn, async () => {
      if (act === "cancel") upsertTask(await api(`/api/tasks/${id}/cancel`, { method: "POST" }));
      else if (act === "retry") upsertTask(await api(`/api/tasks/${id}/retry`, { method: "POST" }));
      else if (act === "recheck") upsertTask(await api(`/api/tasks/${id}/recheck`, { method: "POST" }));
      else if (act === "delete") {
        if (!confirm("从列表中删除这个任务？已下载的视频文件不会被删除。")) return;
        await api(`/api/tasks/${id}`, { method: "DELETE" });
        removeTask(id);
      } else if (act === "open" || act === "reveal") await openPath(btn);
    });
  });

  $("#task-list").addEventListener("toggle", (e) => {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement)) return;
    if (d.dataset.log) { if (d.open) state.openLogs.add(d.dataset.log); else state.openLogs.delete(d.dataset.log); }
    else if (d.dataset.hist) { if (d.open) state.openHist.add(d.dataset.hist); else state.openHist.delete(d.dataset.hist); }
    else return;
    const card = d.closest(".task");
    if (card) card._html = null; // the next render bakes the open state into the markup
  }, true);

  $("#player").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn && (btn.dataset.act === "open" || btn.dataset.act === "reveal")) withBusy(btn, () => openPath(btn));
  });
  $("#player").addEventListener("close", () => {
    const v = $("#player-video");
    v.pause();
    v.removeAttribute("src");
    v.load();
  });

  for (const dlg of $$("dialog")) {
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg || e.target.closest("[data-close]")) dlg.close();
    });
  }

  $("#banners").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "zoom-captcha") { $("#viewer-img").src = btn.src; $("#viewer").showModal(); return; }
    withBusy(btn, async () => {
      if (act === "show-chrome") await api("/api/browser/show", { method: "POST" });
      else if (act === "qr") {
        const res = await api("/api/login/qr", { method: "POST" });
        state.global = res; renderGlobal();
        toast(res.already_logged_in ? "当前已登录豆包，无需扫码" : "正在刷新二维码…");
      }
      else if (act === "quota-now") { state.global = await api("/api/quota/clear", { method: "POST" }); renderGlobal(); }
      else if (act === "resume") { state.global = await api("/api/queue/pause", { method: "POST", body: { paused: false } }); renderGlobal(); }
      else if (act === "import-login") await importLogin();
    });
  });

  $("#btn-pause").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    state.global = await api("/api/queue/pause", { method: "POST", body: { paused: !state.global.paused } });
    renderGlobal();
  }));

  $("#btn-clear").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const data = await api("/api/tasks/clear-finished", { method: "POST" });
    toast(data.removed ? `已清除 ${data.removed} 个已结束的任务（视频文件保留）` : "没有已结束的任务");
  }));

  $("#btn-show-chrome").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    state.global = await api("/api/browser/show", { method: "POST" });
    renderGlobal();
    api("/api/login/check", { method: "POST" }).then((s) => { state.global = s; renderGlobal(); }).catch(() => {});
  }));

  $("#conn-status").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    state.global = await api("/api/login/check", { method: "POST" });
    renderGlobal();
    const login = state.global.login;
    toast(login === "logged_in" ? "豆包登录正常" : login === "logged_out" ? "豆包未登录，请按页面顶部提示登录" : "已检查");
  }));

  $("#btn-open-output").addEventListener("click", (e) => withBusy(e.currentTarget, () => api("/api/open-output", { method: "POST" })));

  $("#btn-quit").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const busy = [...state.tasks.values()].some((t) => ACTIVE.has(t.status));
    const msg = busy
      ? "还有任务在进行中。退出后，已提交给豆包的生成会在下次启动时继续跟踪。确定退出？"
      : "退出豆包视频批量生成？自动化 Chrome 窗口也会一起关闭。";
    if (!confirm(msg)) return;
    await api("/api/shutdown", { method: "POST" });
    if (es) { es.onerror = null; es.close(); }
    document.body.innerHTML = `<div class="empty" style="padding-top:22vh"><h3>已退出</h3><p>可以关闭这个页面了。下次使用时重新打开 App 或双击 start.command。</p></div>`;
  }));

  // ------------------------------------------------------------ settings
  let profileTouched = false;
  async function loadProfiles() {
    const sel = $("#profile-select");
    profileTouched = false;
    try {
      const data = await api("/api/chrome/profiles");
      const saved = (state.settings && state.settings.chrome_profile) || "";
      const opts = [`<option value=""${saved ? "" : " selected"}>自动选择（已登录豆包的 Chrome 用户）</option>`];
      for (const p of data.profiles) {
        const status = p.has_session ? "已登录豆包" : p.has_doubao ? "登录已过期" : "未登录豆包";
        opts.push(`<option value="${esc(p.directory)}"${p.directory === saved ? " selected" : ""}>${esc(p.name)}（${esc(p.directory)}）· ${status}</option>`);
      }
      sel.innerHTML = opts.join("");
    } catch (e) {
      sel.innerHTML = `<option value="">${esc(e.message)}</option>`;
    }
  }
  $("#profile-select").addEventListener("change", () => { profileTouched = true; });

  function renderLoginLine() {
    const el = $("#login-line");
    if (!el) return;
    const login = (state.global || {}).login;
    if (login === "logged_in") el.innerHTML = `<span style="color:var(--ok);display:flex">${icon("check")}</span>自动化 Chrome 已登录豆包`;
    else if (login === "logged_out") el.innerHTML = `<span style="color:var(--err);display:flex">${icon("alert")}</span>自动化 Chrome 未登录（主页面顶部会显示登录二维码）`;
    else el.innerHTML = `<span style="color:var(--muted);display:flex">${icon("info")}</span>尚未检查（添加任务或点「显示 Chrome」后自动启动）`;
  }

  async function importLogin() {
    const sel = $("#profile-select");
    const profile = sel && sel.value ? sel.value : "";
    toast("正在导入登录，macOS 可能请求钥匙串授权…");
    const data = await api("/api/login/import", { method: "POST", body: { profile } });
    state.global = data.state;
    renderGlobal();
    const ok = data.state.login === "logged_in";
    toast(ok ? `已导入 ${data.imported} 个豆包 Cookie，登录成功` : "日常 Chrome 的豆包登录也已失效，请扫描页面顶部的二维码登录", !ok);
  }

  const SETTING_FIELDS = [
    ["output_dir", "#s-output", "text"],
    ["continue_text", "#s-continue", "text"],
    ["max_auto_continue", "#s-max-continue", "num"],
    ["reply_timeout_minutes", "#s-reply-timeout", "num"],
    ["generation_timeout_minutes", "#s-gen-timeout", "num"],
    ["max_retries", "#s-max-retries", "num"],
    ["retry_delay_minutes", "#s-retry-delay", "num"],
    ["retry_max_delay_minutes", "#s-retry-max", "num"],
    ["quota_wait_minutes", "#s-quota-wait", "num"],
    ["max_parallel", "#s-parallel", "num"],
    ["submit_gap_seconds", "#s-submit-gap", "num"],
    ["auto_retry", "#s-auto-retry", "bool"],
    ["auto_import_login", "#s-auto-import", "bool"],
    ["notify", "#s-notify", "bool"],
  ];

  $("#btn-settings").addEventListener("click", async () => {
    try { state.settings = await api("/api/settings"); } catch (e) { toast(e.message, true); return; }
    for (const [key, sel, kind] of SETTING_FIELDS) {
      const el = $(sel);
      if (kind === "bool") el.checked = Boolean(state.settings[key]);
      else el.value = state.settings[key] ?? "";
    }
    renderLoginLine();
    $("#settings").showModal();
    loadProfiles();
  });

  $("#btn-import").addEventListener("click", (e) => withBusy(e.currentTarget, importLogin));
  $("#btn-qr-login").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
    const res = await api("/api/login/qr", { method: "POST" });
    state.global = res;
    renderGlobal();
    if (res.already_logged_in) { toast("当前已登录豆包，无需扫码"); return; }
    $("#settings").close();
    toast("已打开扫码登录，请看页面顶部的二维码");
  }));

  $("#settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    withBusy($("#btn-save-settings"), async () => {
      const body = {};
      for (const [key, sel, kind] of SETTING_FIELDS) {
        const el = $(sel);
        if (kind === "bool") body[key] = el.checked;
        else if (kind === "num") {
          const v = el.value.trim();
          if (v !== "" && Number.isFinite(Number(v))) body[key] = Number(v);
        } else {
          const v = el.value.trim();
          if (v) body[key] = v;
        }
      }
      if (profileTouched) body.chrome_profile = $("#profile-select").value;
      state.settings = await api("/api/settings", { method: "PUT", body });
      $("#settings").close();
      toast("设置已保存");
    });
  });

  // ------------------------------------------------------------ boot
  renderFolderZone();
  updateCount();
  renderMeta();
  connect();
})();
