/* ============================================================
   NEURA 控制板 · 电影级丝滑窗口管理器
   - 窗口打开/关闭/最小化: 弹性缩放+滑入滑出动画(非生硬开关)
   - 拖拽: Pointer Events + transform 跟手(零重排), 拖拽中微放大+光晕
   - 双击标题栏最大化/还原; 右下角手柄可自由缩放
   - 触摸屏同样支持(pointer 统一处理)
   - 窗口坞 chips 动画化; 聚焦窗口呼吸光 + 顶部光条
   ============================================================ */
const Panel = (() => {
  const canvas = () => document.getElementById("panelCanvas");
  const dock = () => document.getElementById("panelDock");
  const windows = new Map();     // id -> {el, body, statusEl, minimized, maxed, title, type, x, y, w, h}
  let zTop = 100, seq = 0;

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  /* ---------- 打开 ---------- */
  const closedFor = new Set();   // v3.16: 已关闭窗口ID, 关闭后禁止被 panel_update 自动重建
  function open({ id, title, type, content, status, admin }) {
    // v3.24: 同名同类型窗口复用(避免文件预览/目录浏览重复开窗)
    for (const [oid, rec] of windows) {
      if (rec.type === type && rec.title === title && !rec.minimized) {
        try { rec.el.remove(); } catch (e) { /* 忽略 */ }
        windows.delete(oid);
        closedFor.add(oid);
      }
    }
    if (windows.has(id)) { closedFor.delete(id); update({ id, title, type, content, status, admin }); focus(id); return id; }
    if (windows.size >= 8) {
      const first = windows.keys().next().value;
      close(first);
    }
    const el = document.createElement("div");
    el.className = "pwin" + (admin ? " pwin-admin" : "");
    if (admin) el.setAttribute("data-admin", "1");
    const minW = type === "weather" ? 340 : 250, minH = type === "process" ? 180 : 140;
    const w = Math.min(Math.max(minW, 320), canvas().clientWidth - 20);
    const h = Math.min(Math.max(minH, 200), canvas().clientHeight - 40);
    const x = Math.min(14 + (seq % 5) * 26, Math.max(0, canvas().clientWidth - w - 10));
    const y = Math.min(12 + (seq % 5) * 22, Math.max(0, canvas().clientHeight - h - 30));
    seq++;
    el.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px;z-index:${zTop++}`;
    el.innerHTML = `
      <div class="pwin-head">
        <span class="pwin-dot"></span>
        <span class="pwin-title">${esc(title)}</span>
        <div class="pwin-btns">
          <button class="b-min" title="最小化">—</button>
          <button class="b-max" title="最大化">□</button>
          <button class="b-close" title="关闭">✕</button>
        </div>
      </div>
      <span class="pwin-c tl"></span><span class="pwin-c tr"></span>
      <span class="pwin-c bl"></span><span class="pwin-c br"></span>
      <div class="pwin-body"></div>
      <div class="pwin-resize" title="拖拽缩放"></div>
      <div class="pwin-status"></div>
      <div class="pwin-hud">NEURA · HOLO-WINDOW</div>`;
    canvas().appendChild(el);
    const rec = { el, body: el.querySelector(".pwin-body"), statusEl: el.querySelector(".pwin-status"),
                  minimized: false, maxed: false, title, type, x, y, w, h };
    closedFor.delete(id);
    windows.set(id, rec);
    wireDrag(el, rec);
    el.querySelector(".b-close").onclick = (e) => { e.stopPropagation(); close(id); };
    el.querySelector(".b-min").onclick = (e) => { e.stopPropagation(); toggleMin(id); };
    el.querySelector(".b-max").onclick = (e) => { e.stopPropagation(); toggleMax(id); };
    el.addEventListener("pointerdown", () => focus(id));
    el.querySelector(".pwin-head").addEventListener("dblclick", () => toggleMax(id));
    render(el, type, content);
    if (type === "command") wireTerminal(rec);
    setStatus(id, status);
    renderDock();
    // 丝滑入场动画
    el.classList.add("win-in");
    el.addEventListener("animationend", () => el.classList.remove("win-in"), { once: true });
    return id;
  }

  /* ---------- 最小化 / 还原 ---------- */
  function toggleMin(id) {
    const rec = windows.get(id);
    if (!rec) return;
    rec.minimized = !rec.minimized;
    if (rec.minimized) {
      rec.el.classList.add("win-min");
      rec.el.addEventListener("animationend", () => {
        if (rec.minimized) rec.el.style.display = "none";
      }, { once: true });
    } else {
      rec.el.style.display = "flex";
      rec.el.classList.add("win-restore");
      rec.el.addEventListener("animationend", () => rec.el.classList.remove("win-restore"), { once: true });
      focus(id);
    }
    renderDock();
  }

  function toggleMax(id) {
    const rec = windows.get(id);
    if (!rec) return;
    rec.maxed = !rec.maxed;
    rec.el.classList.toggle("maxed", rec.maxed);
    if (!rec.maxed) {
      rec.el.style.left = rec.x + "px"; rec.el.style.top = rec.y + "px";
      rec.el.style.width = rec.w + "px"; rec.el.style.height = rec.h + "px";
    }
    focus(id);
  }

  /* AI 窗口管理(panel_control 事件): 关闭/全部关闭/滑动/聚焦/最小化/还原; 未指定ID作用于聚焦窗口 */
  function control(cmd) {
    const action = cmd.action, id = cmd.window_id;
    const focused = () => { for (const [i, r] of windows) if (r.el.classList.contains("focused")) return i; return null; };
    const target = id || focused() || (windows.keys().next().value);
    if (action === "close" && id) close(id);
    else if (action === "close" && !id) clearAll();   /* 未指定窗口 -> 清空所有弹窗 */
    else if (action === "close_all") clearAll();
    else if (action === "focus") focus(target);
    else if (action === "minimize") { const r = windows.get(target); if (r && !r.minimized) toggleMin(target); }
    else if (action === "restore") { const r = windows.get(target); if (r && r.minimized) toggleMin(target); }
    else if (action === "maximize") { const r = windows.get(target); if (r && !r.maxed) toggleMax(target); }
    else if (action === "resize") {
      // 缩放窗口(像《极限审判》的 AI 一样自由调整): direction=in 放大一点 / out 缩小一点
      const rec = windows.get(target);
      if (!rec) return;
      const C = canvas();
      const ratio = cmd.direction === "in" ? 1.16 : 0.86;
      const nw = Math.max(220, Math.min(rec.w * ratio, C.clientWidth - 40));
      const nh = Math.max(140, Math.min(rec.h * ratio, C.clientHeight - 40));
      rec.w = nw; rec.h = nh;
      rec.el.style.width = nw + "px"; rec.el.style.height = nh + "px";
      rec.el.classList.add("win-restore");
      rec.el.addEventListener("animationend", () => rec.el.classList.remove("win-restore"), { once: true });
      focus(target);
    }
    else if (action === "layout") { layout(cmd.kind || "maximize_all"); }
    else if (action === "move") {
      const rec = windows.get(target);
      if (!rec) return;
      const C = canvas();
      const nx = Math.max(0, Math.min(cmd.x != null ? cmd.x : rec.x, C.clientWidth - 80));
      const ny = Math.max(0, Math.min(cmd.y != null ? cmd.y : rec.y, C.clientHeight - 30));
      rec.x = nx; rec.y = ny;
      rec.el.style.left = nx + "px"; rec.el.style.top = ny + "px";
      rec.el.classList.add("win-restore");
      rec.el.addEventListener("animationend", () => rec.el.classList.remove("win-restore"), { once: true });
      focus(target);
    }
  }

  /* ---------- 电影级拖拽: Pointer Events + transform(零重排, 极丝滑) ---------- */
  function wireDrag(el, rec) {
    const head = el.querySelector(".pwin-head");
    const rs = el.querySelector(".pwin-resize");
    const C = canvas();

    function begin(e, mode) {
      if (e.target.closest(".pwin-btns")) return;
      if (rec.maxed) return;
      el.classList.remove("win-in", "win-restore", "win-min", "win-out");
      el.classList.add("dragging");
      focusForDrag(idOf(el));
      const sx = e.clientX, sy = e.clientY;
      const ox = rec.x, oy = rec.y;
      const ow = rec.w, oh = rec.h;
      const target = mode === "move" ? head : rs;
      target.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const dx = ev.clientX - sx, dy = ev.clientY - sy;
        if (mode === "move") {
          el.style.transform = `translate(${dx}px, ${dy}px) scale(1.015)`;
        } else {
          const nw = Math.max(200, ow + dx), nh = Math.max(120, oh + dy);
          el.style.width = nw + "px"; el.style.height = nh + "px";
        }
      };
      const up = (ev) => {
        try { target.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
        el.classList.remove("dragging");
        if (mode === "move") {
          const dx = ev.clientX - sx, dy = ev.clientY - sy;
          rec.x = Math.max(0, Math.min(ox + dx, C.clientWidth - 80));
          rec.y = Math.max(0, Math.min(oy + dy, C.clientHeight - 30));
          el.style.transform = "";
          el.style.left = rec.x + "px"; el.style.top = rec.y + "px";
        } else {
          rec.w = Math.max(200, ow + (ev.clientX - sx));
          rec.h = Math.max(120, oh + (ev.clientY - sy));
        }
        target.removeEventListener("pointermove", move);
        target.removeEventListener("pointerup", up);
        target.removeEventListener("pointercancel", up);
      };
      target.addEventListener("pointermove", move);
      target.addEventListener("pointerup", up);
      target.addEventListener("pointercancel", up);
    }
    head.addEventListener("pointerdown", (e) => begin(e, "move"));
    rs.addEventListener("pointerdown", (e) => begin(e, "resize"));
  }

  function idOf(el) {
    for (const [id, rec] of windows) if (rec.el === el) return id;
    return null;
  }
  function focusForDrag(id) { if (id) focus(id); }

  /* ---------- 更新 / 状态 / 聚焦 / 关闭 ---------- */
  function update({ id, title, type, content, status, append, admin }) {
    const rec = windows.get(id);
    if (!rec) { if (closedFor.has(id)) return; return open({ id, title, type, content, status, admin }); }
    if (admin) {
      rec.el.classList.add("pwin-admin");
      rec.el.setAttribute("data-admin", "1");
    }
    if (title) { rec.title = title; rec.el.querySelector(".pwin-title").textContent = title; }
    if (type) rec.type = type;
    if (content !== undefined) {
      if (rec.type === "progress") updateProgress(rec, content);
      else if (append && rec.type === "process") appendProc(rec, content);
      else render(rec.el, rec.type, content, rec.body);
    }
    if (rec.type === "command") wireTerminal(rec);
    if (status) setStatus(id, status);
    focus(id);
  }

  function appendProc(rec, lines) {
    const wrap = rec.body.querySelector(".pbody-proc");
    if (!wrap) { render(rec.el, rec.type, lines, rec.body); return; }
    (Array.isArray(lines) ? lines : [lines]).forEach((ln) => {
      const div = document.createElement("div");
      div.className = `proc-line ${esc(ln.level || "out")}`;
      div.innerHTML = `<span class="t">${esc((ln.time || "").slice(11, 19))}</span><span>${esc(ln.text)}</span>`;
      wrap.appendChild(div);
      rec.body.scrollTop = rec.body.scrollHeight;
    });
  }

  function setStatus(id, status) {
    const rec = windows.get(id);
    if (!rec) return;
    rec.statusEl.className = "pwin-status " + (status || "");
  }

  function focus(id) {
    const rec = windows.get(id);
    if (!rec) return;
    windows.forEach((r) => r.el.classList.remove("focused"));
    rec.el.classList.add("focused");
    rec.el.style.zIndex = ++zTop;
    renderDock();
  }

  function close(id) {
    const rec = windows.get(id);
    if (!rec) return;
    closedFor.add(id);
    // 丝滑退场: 播放动画后移除(带兜底)
    rec.el.classList.remove("win-in", "win-restore");
    rec.el.classList.add("win-out");
    let done = false;
    const fin = () => {
      if (done) return; done = true;
      if (windows.has(id)) { rec.el.remove(); windows.delete(id); renderDock(); }
    };
    rec.el.addEventListener("animationend", fin, { once: true });
    setTimeout(fin, 320);
  }

  function renderDock() {
    dock().innerHTML = "";
    windows.forEach((rec, id) => {
      const chip = document.createElement("div");
      chip.className = "dock-chip" + (rec.el.classList.contains("focused") ? " active" : "");
      chip.innerHTML = `${esc(rec.title || id)}<span class="d-close">✕</span>`;
      chip.onclick = (e) => {
        if (e.target.classList.contains("d-close")) return close(id);
        if (rec.minimized) {
          rec.minimized = false;
          rec.el.style.display = "flex";
          rec.el.classList.add("win-restore");
          rec.el.addEventListener("animationend", () => rec.el.classList.remove("win-restore"), { once: true });
        }
        focus(id);
      };
      dock().appendChild(chip);
    });
  }

  /* ---------- 渲染器(内容动效) ---------- */
  function render(el, type, content, body) {
    const b = body || el.querySelector(".pwin-body");
    if (typeof content === "string") {
      try { content = JSON.parse(content); } catch (e) { /* 保持字符串 */ }
    }
    switch (type) {
      case "data": b.innerHTML = renderTable(content); break;
      case "code": b.innerHTML = renderCode(content); break;
      case "process": b.innerHTML = renderProc(content); break;
      case "files": b.innerHTML = renderFiles(content); break;
      case "weather": b.innerHTML = renderWeather(content); break;
      case "chart": b.innerHTML = renderChart(content); break;
      case "download": b.innerHTML = renderDownload(content); break;
      case "html_preview": b.innerHTML = renderHtmlPreview(content); break;
      case "appview": b.innerHTML = renderAppView(content); break;
      case "image": b.innerHTML = renderImage(content); break;
      case "password": b.innerHTML = renderPassword(content); break;
      case "progress": b.innerHTML = renderProgress(content); break;
      case "command": b.innerHTML = renderCommand(content); break;
      case "ext": b.innerHTML = renderExtManager(content); break;
      default: b.innerHTML = renderInfo(content);
    }
    if (type === "chart") drawChart(b.querySelector("canvas"), content);
  }

  /* 实时命令终端窗口: 输入命令 -> WS 执行 -> 流式行输出(电影级终端感) */
  function renderCommand(content) {
    const d = (content && typeof content === "object") ? content : {};
    return `<div class="pbody-cmd">
      <div class="cmd-out"></div>
      <div class="cmd-row">
        <span class="cmd-prompt">$</span>
        <input class="cmd-input" placeholder="输入命令后回车执行, 如: dir / ls -la / python3 -c 'print(1)'" autocomplete="off">
      </div>
    </div>`;
  }
  function wireTerminal(rec) {
    const input = rec.body.querySelector(".cmd-input");
    if (!input || input.dataset.wired) return;
    input.dataset.wired = "1";
    const run = () => {
      const cmd = input.value.trim();
      if (!cmd) return;
      input.value = "";
      const out = rec.body.querySelector(".cmd-out");
      out.insertAdjacentHTML("beforeend",
        `<div class="proc-line cmd"><span class="t">▶</span><span>$${esc(cmd)}</span></div>`);
      rec.body.scrollTop = rec.body.scrollHeight;
      WS.send({ type: "run_command_direct", session_id: App.currentSid(), command: cmd });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } });
  }
  function commandStream(win_id, line, status) {
    const rec = windows.get(win_id);
    if (!rec) { open({ id: win_id, title: "⌘ 实时命令终端", type: "command", content: {}, status: "running" }); return; }
    const out = rec.body.querySelector(".cmd-out");
    if (!out) { render(rec.el, "command", {}, rec.body); return; }
    out.insertAdjacentHTML("beforeend",
      `<div class="proc-line ${esc(status === "err" ? "err" : status === "ok" ? "ok" : "out")}"><span class="t">▸</span><span>${esc(line)}</span></div>`);
    rec.body.scrollTop = rec.body.scrollHeight;
    setStatus(win_id, status === "err" ? "error" : "ok");
    focus(win_id);
  }

  /* 依赖安装进度条窗口: 实时百分比 + 文本 */
  function renderProgress(content) {
    const d = (content && typeof content === "object") ? content : {};
    const pct = Math.max(0, Math.min(100, Number(d.percent) || 0));
    const txt = d.text || "安装中…";
    const id = "pb_" + Math.random().toString(36).slice(2, 8);
    return `<div class="pbody-progress">
      <div class="pg-row"><b style="color:var(--primary)">${esc(d.package || "依赖安装")}</b>
        <span class="pg-pct">${pct}%</span></div>
      <div class="pg-track"><div class="pg-fill" id="${id}" style="width:${pct}%"></div></div>
      <div class="pg-text">${esc(txt)}</div>
    </div>`;
  }
  function updateProgress(rec, content) {
    const d = (content && typeof content === "object") ? content : {};
    if (d.percent === undefined && d.text === undefined) return;
    let b = rec.body.querySelector(".pbody-progress");
    if (!b) { render(rec.el, "progress", d, rec.body); return; }
    const pct = Math.max(0, Math.min(100, Number(d.percent) || 0));
    const fill = b.querySelector(".pg-fill");
    const pctEl = b.querySelector(".pg-pct");
    const txtEl = b.querySelector(".pg-text");
    if (fill) fill.style.width = pct + "%";
    if (pctEl) pctEl.textContent = pct + "%";
    if (txtEl && d.text !== undefined) txtEl.textContent = d.text;
  }

  /* 缺依赖确认窗口: 是否安装(在控制板内弹窗询问, 非系统弹窗) */
  function renderExtManager(content) {
    const exts = (content && content.extensions) || [];
    let html = "";
    if (exts.length) {
      exts.forEach((x) => {
        const en = !!x.enabled;
        html += `<div class="ext-row${en ? "" : " ext-off"}">
          <div class="ext-main">
            <span class="ext-name">${esc(x.name)}</span>
            <span class="ext-title">${esc(x.title || "")}</span>
            <span class="ext-state ${en ? "on" : "off"}">${en ? "● 已启用" : "○ 已禁用"}</span>
          </div>
          <div class="ext-purpose">${esc(x.purpose || "")}</div>
          <div class="ext-ops">
            <button class="ext-btn" data-act="ext_detail" data-name="${esc(x.name)}">详情</button>
            <button class="ext-btn" data-act="ext_toggle" data-name="${esc(x.name)}" data-enable="${en ? "0" : "1"}">${en ? "禁用" : "启用"}</button>
            <button class="ext-btn danger" data-act="ext_uninstall" data-name="${esc(x.name)}">卸载</button>
          </div></div>`;
      });
    } else {
      html = `<div style="padding:16px;color:var(--text-dim);font-size:12px">${
        (content && content.error) ? ("无法连接服务器: " + esc(content.error)) :
        "暂无扩展 — 点击下方『⬒ 上传扩展包』安装 (zip 需含 manifest.json + usage.md + src/main.py)"}</div>`;
    }
    html += `<div class="ext-foot">
      <button class="ext-btn primary" data-act="ext_reload">↻ 刷新</button>
      <label class="ext-btn primary" for="extFileInput2">⬒ 上传扩展包</label>
      <input type="file" id="extFileInput2" accept=".zip" style="display:none"></div>`;
    return html;
  }

  function openConfirm(confirm_id, title, description, package) {
    const id = "confirm_" + confirm_id;
    open({ id, title: title || "是否需要安装？", type: "info", status: "warn",
           content: { "说明": description } });
    const rec = windows.get(id);
    if (!rec) return;
    rec.body.innerHTML = `<div class="pbody-confirm">
      <div class="cf-ico">◈</div>
      <div class="cf-desc">${esc(description || "")}</div>
      <div class="cf-pkg">依赖: <b style="color:var(--primary)">${esc(package || "")}</b></div>
      <div class="cf-ops">
        <button class="cf-btn yes">安装</button>
        <button class="cf-btn no">取消</button>
      </div>
    </div>`;
    const yesBtn = rec.body.querySelector(".cf-btn.yes");
    const noBtn = rec.body.querySelector(".cf-btn.no");
    yesBtn.onclick = () => { WS.send({ type: "confirm_response", confirm_id, yes: true }); close(id); };
    noBtn.onclick = () => { WS.send({ type: "confirm_response", confirm_id, yes: false }); close(id); };
    focus(id);
  }

  /* 高危操作验证窗口: 输入系统登录密码解锁(在控制板内, 非系统弹窗) */
  function renderPassword(content) {
    const d = (content && typeof content === "object") ? content : {};
    return `<div class="pbody-password">
      <div class="pw-warn">⚠ 高危操作 · 需要系统登录密码授权</div>
      <div class="pw-desc">${esc(d.description || d.reason || "执行该操作")}</div>
      <div class="pw-tool">调用集: ${esc(d.tool || "")}</div>
      <input class="pw-input" type="password" placeholder="输入系统登录密码" autocomplete="off">
      <div class="pw-err"></div>
      <div class="pw-ops">
        <button class="pw-btn ok" disabled>验证并执行</button>
        <button class="pw-btn cancel">取消</button>
      </div>
      <div class="pw-hint">密码仅用于本次系统验证, 不落盘、不记录</div>
    </div>`;
  }

  /* 高危操作: 打开密码验证窗口 */
  function openPassword(operation_id, description, tool, args) {
    const id = "auth_" + operation_id;
    const content = { description, tool, args };
    open({ id, title: "⚠ 高危操作验证", type: "password", content, status: "warn", admin: true });
    const el = windows.get(id);
    if (!el) return;
    const input = el.body.querySelector(".pw-input");
    const okBtn = el.body.querySelector(".pw-btn.ok");
    const errBox = el.body.querySelector(".pw-err");
    const cancelBtn = el.body.querySelector(".pw-btn.cancel");
    input.addEventListener("input", () => {
      okBtn.disabled = input.value.trim().length === 0;
      errBox.textContent = "";
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !okBtn.disabled) submit();
    });
    const submit = () => {
      const pw = input.value;
      if (!pw) return;
      okBtn.disabled = true;
      okBtn.textContent = "验证中…";
      WS.send({ type: "admin_auth", operation_id, password: pw });
    };
    okBtn.onclick = submit;
    cancelBtn.onclick = () => {
      WS.send({ type: "admin_cancel", operation_id });
      close(id);
    };
    setTimeout(() => { try { input.focus(); } catch (e) {} }, 120);
  }

  /* 密码验证结果: 通过 -> 绿色授权提示后自动关闭(后台正在执行); 失败 -> 红色提示可重试 */
  function passwordResult(operation_id, ok, message) {
    const id = "auth_" + operation_id;
    const rec = windows.get(id);
    if (!rec) return;
    const errBox = rec.body.querySelector(".pw-err");
    if (ok) {
      if (errBox) { errBox.textContent = ""; }
      rec.body.innerHTML = `<div class="pbody-info" style="color:#39ff9a">🔓 ${esc(message)}</div>`;
      setTimeout(() => close(id), 1400);
    } else if (errBox) {
      errBox.textContent = "✗ " + (message || "密码不正确, 请重试");
      const btn = rec.body.querySelector(".pw-btn.ok");
      if (btn) { btn.disabled = false; btn.textContent = "验证并执行"; }
    }
  }

  function renderInfo(content) {
    // 键值对: 仅转义键/值文本, HTML 结构本身不转义(否则整段 kv 会以源码显示)
    let s;
    if (content && typeof content === "object") {
      s = Object.entries(content).map(([k, v]) => {
        const val = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? "");
        return `<div class="kv-row"><b style="color:var(--primary)">${esc(k)}</b>: ${esc(val)}</div>`;
      }).join("\n");
    } else s = esc(String(content ?? ""));
    return `<div class="pbody-info">${s}</div>`;
  }

  /* 软件界面视图: 在控制板窗口中渲染软件的实时画面(截图)与操控状态 */
  function renderAppView(content) {
    const d = (content && typeof content === "object") ? content : {};
    let screen;
    if (d.image_base64) {
      screen = `<img src="data:${esc(d.mime || "image/png")};base64,${d.image_base64}" alt="software screen">`;
    } else {
      const steps = Array.isArray(d.steps) ? d.steps.map((s2) => `<div class="proc-line ok"><span class="t">▶</span><span>${esc(s2)}</span></div>`).join("") : "";
      screen = `<div class="av-placeholder">◈ ${esc(d.app || "软件")} 运行中
        <div class="proc-line" style="margin-top:8px">${steps || "操作已执行, 截图工具可用时将渲染软件界面"}</div></div>`;
    }
    return `<div class="pbody-appview">
      <div class="av-toolbar"><span>◈ ${esc(d.app || "软件")}</span><span>${esc(d.status || "running")}</span>${d.launched ? "<span>已启动</span>" : ""}${d.killed ? "<span>已结束</span>" : ""}</div>
      <div class="av-screen">${screen}</div>
    </div>`;
  }

  /* 图片预览: 可放大的图片渲染 */
  function renderImage(content) {
    const d = (content && typeof content === "object") ? content : {};
    if (d.image_base64) {
      return `<div class="pbody-image"><img src="data:${esc(d.mime || "image/png")};base64,${d.image_base64}" alt="${esc(d.path || "preview")}" title="双击最大化"></div>`;
    }
    return renderInfo(content);
  }

  /* AI 自主连续调整窗口: 一次指令串联动画调整所有窗口 */
  function layout(kind) {
    const list = [...windows.entries()];
    if (!list.length) return;
    const C = canvas();
    const kinds = kind || "maximize_all";
    if (kinds === "maximize_all") {
      list.forEach(([id], i) => {
        setTimeout(() => { const r = windows.get(id); if (r && !r.maxed) toggleMax(id); }, i * 120);
      });
    } else if (kinds === "grid") {
      const n = list.length;
      const cols = Math.ceil(Math.sqrt(n));
      const w = (C.clientWidth - 24) / cols;
      const h = (C.clientHeight - 24) / Math.ceil(n / cols);
      list.forEach(([id], i) => {
        const r = windows.get(id);
        if (!r) return;
        const nx = 8 + (i % cols) * w, ny = 8 + Math.floor(i / cols) * h;
        setTimeout(() => {
          r.x = nx; r.y = ny; r.w = w - 12; r.h = h - 12;
          r.el.style.left = nx + "px"; r.el.style.top = ny + "px";
          r.el.style.width = (w - 12) + "px"; r.el.style.height = (h - 12) + "px";
          r.el.classList.add("win-restore");
          r.el.addEventListener("animationend", () => r.el.classList.remove("win-restore"), { once: true });
          focus(id);
        }, i * 140);
      });
    }
  }

  /* 图片双击最大化 */
  document.addEventListener("dblclick", (e) => {
    if (e.target && e.target.tagName === "IMG" && e.target.closest(".pbody-image, .av-screen")) {
      const id = [...windows.entries()].find(([, r]) => r.el.contains(e.target))?.[0];
      if (id) toggleMax(id);
    }
  });

  function renderTable(content) {
    if (!content || typeof content !== "object") return `<div class="pbody-info">${esc(content)}</div>`;
    const rows = content.results || content.files || content.items || content.data;
    const headers = content.headers || (Array.isArray(rows) && rows[0] ? Object.keys(rows[0]) : []);
    if (!Array.isArray(rows)) return renderInfo(content);
    let html = `<table class="pbody-table"><thead><tr>`;
    headers.forEach((h) => { html += `<th>${esc(h)}</th>`; });
    html += `</tr></thead><tbody>`;
    rows.slice(0, 200).forEach((r, ri) => {
      html += `<tr style="animation-delay:${Math.min(ri * 16, 280)}ms">`;
      headers.forEach((h) => {
        let v = r[h];
        if (typeof v === "object") v = JSON.stringify(v);
        const isUrl = typeof v === "string" && v.startsWith("http");
        html += `<td>${isUrl ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(v)}</td>`;
      });
      html += "</tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function renderCode(content) {
    const code = typeof content === "string" ? content : (content.code || "");
    const path = content && content.path ? ` · ${esc(content.path)}` : "";
    const lines = String(code).split("\n");
    let html = `<div class="pbody-code">`;
    lines.forEach((l, i) => { html += `<div><span class="ln">${i + 1}</span>${esc(l) || " "}</div>`; });
    html += `</div>`;
    return `<div style="margin-bottom:6px;color:var(--text-dim);font-size:11px">${path}</div>` + html;
  }

  function renderProc(content) {
    const lines = Array.isArray(content) ? content : (content && content.lines ? content.lines : []);
    let html = `<div class="pbody-proc">`;
    lines.forEach((ln) => {
      const l = typeof ln === "string" ? { text: ln, level: "out" } : ln;
      html += `<div class="proc-line ${esc(l.level || "out")}"><span class="t">${esc((l.time || "").slice(11, 19))}</span><span>${esc(l.text)}</span></div>`;
    });
    return html + "</div>";
  }

  function renderFiles(content) {
    if (!content || !Array.isArray(content.files)) return renderInfo(content);
    let html = `<div class="pbody-files">`;
    html += `<div class="frow" style="color:var(--primary)"><span class="nm">${esc(content.path || "")}</span></div>`;
    content.files.forEach((f, i) => {
      const full = f.path || (content.path ? (content.path.endsWith("/") || content.path.endsWith("\\\\")) ? content.path + f.name : content.path + "/" + f.name : f.name);
      const act = f.type === "dir" ? "dir" : "file";
      html += `<div class="frow ${f.type === "dir" ? "dir" : ""}" data-act="${act}" data-path="${esc(full)}"
        title="${act === "dir" ? "点击进入目录" : "点击预览/打开"} (双击文件名, 单击选中)"
        style="animation-delay:${Math.min(i * 12, 240)}ms">
        <span class="nm">${f.type === "dir" ? "▣ " : ""}${esc(f.name)}</span>
        <span class="sz">${esc(f.size_h || "")}</span>
        <span class="mt">${esc(f.mtime || "")}</span></div>`;
    });
    if (content.truncated) html += `<div style="color:var(--warn)">… 条目过多已截断(前300)</div>`;
    if (content.note) html += `<div class="pbody-note">${esc(content.note)}</div>`;
    return html + "</div>";
  }

  function renderWeather(content) {
    if (!content || !content.current) return renderInfo(content);
    const c = content.current, days = content.daily || [];
    const cid = "wc_" + Math.random().toString(36).slice(2, 8);
    let html = `<div class="pbody-weather">
      <div class="weather-now">
        <b>城市</b><span>${esc(c.city || "")} ${esc(c.admin1 || "")} ${esc(c.country || "")}</span>
        <b>当前</b><span>${esc(c.condition || "")} · ${esc(c.temperature)}℃ (体感 ${esc(c.feels_like)}℃)</span>
        <b>湿度</b><span>${esc(c.humidity)}%</span>
        <b>风速</b><span>${esc(c.wind_speed)} km/h</span>
        <b>降水</b><span>${esc(c.precipitation)} mm</span>
      </div>
      <canvas class="wchart" id="${cid}"></canvas>
      <table class="pbody-table"><thead><tr><th>日期</th><th>天气</th><th>最低</th><th>最高</th><th>降水概率</th></tr></thead><tbody>`;
    days.forEach((d) => {
      html += `<tr><td>${esc(d.date)}</td><td>${esc(d.condition)}</td><td>${esc(d.min)}℃</td><td>${esc(d.max)}℃</td><td>${esc(d.precip_prob)}%</td></tr>`;
    });
    html += "</tbody></table></div>";
    setTimeout(() => {
      const cv = document.getElementById(cid);
      if (cv) drawChart(cv, content.chart);
    }, 30);
    return html;
  }

  function drawChart(canvas, chart) {
    if (!canvas || !chart || !chart.labels) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    const g = canvas.getContext("2d");
    g.scale(dpr, dpr);
    g.clearRect(0, 0, W, H);
    const kind = chart.kind || "line";
    const colors = ["#00d4ff", "#7aa2ff", "#34e0a1", "#ff9e64", "#e06cff"];
    const series = chart.series || [];
    // ---- 饼图: 圆环 + 扇形 + 图例 ----
    if (kind === "pie") {
      const total = series.reduce((a, s) => a + (Number(s.data && s.data[0]) || 0), 0);
      const cx = W / 2 - 40, cy = H / 2, R = Math.min(H / 2 - 12, 62);
      let ang = -Math.PI / 2;
      series.forEach((s, si) => {
        const v = Number(s.data && s.data[0]) || 0;
        if (total <= 0) return;
        const a2 = ang + (v / total) * Math.PI * 2;
        g.beginPath(); g.arc(cx, cy, R, ang, a2);
        g.arc(cx, cy, R * 0.55, a2, ang, true); g.closePath();
        g.fillStyle = colors[si % colors.length]; g.fill();
        g.strokeStyle = "rgba(2,10,30,0.9)"; g.lineWidth = 1.5; g.stroke();
        ang = a2;
      });
      g.font = "10px sans-serif"; g.fillStyle = "rgba(220,234,255,0.75)";
      let ly = 14;
      series.forEach((s, si) => {
        const v = Number(s.data && s.data[0]) || 0;
        const pct = total > 0 ? ((v / total) * 100).toFixed(1) : "0.0";
        g.fillStyle = colors[si % colors.length];
        g.fillRect(W - 128, ly - 8, 9, 9);
        g.fillStyle = "rgba(220,234,255,0.8)";
        g.fillText((s.name || "") + "  " + pct + "%", W - 114, ly);
        ly += 16;
      });
      return;
    }
    // ---- 折线/柱状 ----
    const pad = { l: 42, r: 12, t: 14, b: 22 };
    let all = [];
    series.forEach((s) => { all = all.concat(s.data.map(Number)); });
    if (!all.length) return;
    const rawMin = Math.min(...all), rawMax = Math.max(...all);
    const span = rawMax - rawMin || 1;
    const min = rawMin - span * 0.12, max = rawMax + span * 0.12;
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const stepX = iw / Math.max(chart.labels.length - 1, 1);
    const px = (i) => pad.l + stepX * i;
    const py = (v) => pad.t + ih - ((v - min) / (max - min || 1)) * ih;
    g.strokeStyle = "rgba(0,212,255,0.12)"; g.lineWidth = 1;
    g.font = "9px sans-serif"; g.fillStyle = "rgba(220,234,255,0.55)";
    for (let i = 0; i <= 4; i++) {
      const v = min + ((max - min) / 4) * i;
      g.beginPath(); g.moveTo(pad.l, py(v)); g.lineTo(W - pad.r, py(v)); g.stroke();
      g.fillText(v.toFixed(1), 2, py(v) + 3);
    }
    // x 轴稀疏标签(过多只显示首/中/尾)
    const N = chart.labels.length;
    const showIdx = N <= 7 ? Array.from({ length: N }, (_, i) => i) : [0, Math.floor(N / 2), N - 1];
    showIdx.forEach((i) => {
      if (i < 0 || i >= N) return;
      const lb = String(chart.labels[i]);
      g.fillText(lb.length > 8 ? lb.slice(0, 8) + "…" : lb, px(i) - (lb.length * 3), H - 8);
    });
    if (kind === "bar") {
      const bw = Math.min(stepX * 0.6, 34);
      series.forEach((s, si) => {
        s.data.forEach((v, i) => {
          const x0 = px(i) - bw / 2, y0 = py(Number(v)), y1 = py(min);
          const grad = g.createLinearGradient(0, y0, 0, y1);
          grad.addColorStop(0, colors[si % colors.length]);
          grad.addColorStop(1, "rgba(0,212,255,0.08)");
          g.fillStyle = grad;
          g.fillRect(x0, y0, bw, Math.max(y1 - y0, 1));
          g.strokeStyle = colors[si % colors.length]; g.lineWidth = 1;
          g.strokeRect(x0, y0, bw, Math.max(y1 - y0, 1));
        });
      });
      return;
    }
    // ---- 折线: 面积渐变 + 均线 ----
    series.forEach((s, si) => {
      g.strokeStyle = colors[si % colors.length]; g.lineWidth = 2; g.beginPath();
      s.data.forEach((v, i) => { const x = px(i), y = py(Number(v)); i === 0 ? g.moveTo(x, y) : g.lineTo(x, y); });
      g.stroke();
      // 面积渐变
      g.lineTo(px(s.data.length - 1), py(min)); g.lineTo(px(0), py(min)); g.closePath();
      const grad = g.createLinearGradient(0, pad.t, 0, pad.t + ih);
      grad.addColorStop(0, colors[si % colors.length] + "55");
      grad.addColorStop(1, colors[si % colors.length] + "00");
      g.fillStyle = grad; g.fill();
      // 端点发光
      const li = s.data.length - 1;
      g.beginPath(); g.arc(px(li), py(Number(s.data[li])), 3.4, 0, Math.PI * 2);
      g.fillStyle = colors[si % colors.length]; g.fill();
      g.shadowColor = colors[si % colors.length]; g.shadowBlur = 10;
      g.beginPath(); g.arc(px(li), py(Number(s.data[li])), 1.6, 0, Math.PI * 2); g.fill();
      g.shadowBlur = 0;
    });
    // 图例
    g.font = "9px sans-serif";
    let lx = pad.l, ly2 = 4;
    series.forEach((s, si) => {
      g.fillStyle = colors[si % colors.length];
      g.fillRect(lx, ly2, 8, 8);
      g.fillStyle = "rgba(220,234,255,0.8)";
      g.fillText(s.name || "", lx + 11, ly2 + 8);
      lx += 20 + (s.name || "").length * 9;
      if (lx > W - 60) { lx = pad.l; ly2 += 14; }
    });
    g.fillStyle = "rgba(220,234,255,0.6)";
    chart.labels.forEach((lb, i) => { g.fillText(lb, px(i) - 8, H - 6); });
  }

  function renderChart(content) {
    if (!content) return "<div class='pbody-note'>无数据</div>";
    const kind = content.kind || "line";
    const series = content.series || [];
    if (!series.length) return renderInfo(content);
    const cid = "ch_" + Math.random().toString(36).slice(2, 8);
    const labels = content.labels || (series[0] && series[0].points ? series[0].points.map((p) => String(p[0])) : []);
    const dataSeries = series.map((s) => ({
      name: s.name || "",
      data: s.points ? s.points.map((p) => Number(p[1])) : (s.data || []),
    }));
    const h = Math.max(150, labels.length > 6 ? 190 : 170);
    let html = "";
    if (content.title) html += `<div class="pbody-title">${esc(content.title)}</div>`;
    html += `<canvas class="wchart" id="${cid}" style="height:${h}px"></canvas>`;
    if (content.sub) html += `<div class="pbody-note">${esc(content.sub)}</div>`;
    if (content.advice) html += `<div class="pbody-note" style="color:var(--ok)">${esc(content.advice)}</div>`;
    setTimeout(() => {
      const cv = document.getElementById(cid);
      if (cv) drawChart(cv, { kind, labels, series: dataSeries });
    }, 30);
    return html;
  }

  function renderDownload(content) {
    const url = content && (content.url || content.download_url);
    const name = content && (content.filename || content.name);
    return `<div class="pbody-download">
      <div style="color:var(--text-dim)">${esc(name || "文件")} 已就绪</div>
      <button class="dl-btn" onclick="Panel.download('${esc(url)}','${esc(name || "download")}')">⬇ 下载 ${esc(content.multi ? "(ZIP打包)" : "")}</button>
    </div>`;
  }

  /* 浏览器渲染类代码(HTML)运行预览: 控制板内 iframe 渲染, 无需解释器执行 */
  function renderHtmlPreview(content) {
    const html = (typeof content === "string" ? content : (content && content.html)) || "";
    const path = content && content.path ? ` · ${esc(content.path)}` : "";
    // srcdoc 渲染 + sandbox(无 same-origin)隔离, 脚本可运行但无法访问宿主页面
    return `<div style="margin-bottom:6px;color:var(--text-dim);font-size:11px">运行预览${path}</div>
      <div class="pbody-html"><iframe sandbox="allow-scripts" srcdoc="${esc(html)}"></iframe></div>`;
  }

  return {
    open, update, close, focus, control,
    openPassword, passwordResult, openConfirm,
    commandStream,
    openExtManager() {
      fetch("/api/extensions").then((r) => r.json()).then((j) => {
        const exts = (j && j.extensions) || [];
        open({ id: "ext_manager", title: "⬒ 扩展管理", type: "ext",
               content: { extensions: exts }, status: "ok" });
      }).catch(() => {
        open({ id: "ext_manager", title: "⬒ 扩展管理", type: "ext",
               content: { extensions: [], error: "无法连接服务器" }, status: "error" });
      });
    },
    openTerminal() {
      open({ id: "cmd_terminal", title: "⌘ 实时命令终端", type: "command", content: {}, status: "running" });
    },
    download(url, name) {
      const a = document.createElement("a");
      a.href = url; a.download = name || "";
      document.body.appendChild(a); a.click(); a.remove();
      toast(`已开始下载: ${name}`, "success");
    },
    autoDownload(url, name) {
      Panel.download(url, name);
    },
    clearAll() {
      [...windows.keys()].forEach((id) => {
        const rec = windows.get(id);
        if (!rec) return;
        closedFor.add(id);
        rec.el.remove();
        windows.delete(id);
      });
      renderDock();
    }
  };

  /* 控制板用户点击交互: 文件/按钮等 -> 后端执行并刷新窗口 */
  document.addEventListener("click", (e) => {
    const t = e.target.closest ? e.target.closest("[data-act]") : null;
    if (!t) return;
    const act = t.getAttribute("data-act");
    const path = t.getAttribute("data-path") || "";
    if (!act) return;
    e.stopPropagation();
    if (act === "dir") {
      WS.send({ type: "panel_action", action: "list_dir", path });
      toast("正在打开目录: " + path.slice(-40), "info");
    } else if (act === "file") {
      WS.send({ type: "panel_action", action: "preview_file", path });
      toast("正在预览: " + path.slice(-40), "info");
    } else if (act === "refresh") {
      WS.send({ type: "panel_action", action: "refresh", path });
    } else if (act === "cmd") {
      const c = t.getAttribute("data-cmd") || "";
      WS.send({ type: "panel_action", action: "run_cmd", command: c });
    } else if (act === "ext_detail") {
      const name = t.getAttribute("data-name") || "";
      fetch("/api/extensions/" + encodeURIComponent(name)).then((r) => r.json()).then((j) => {
        if (!j || !j.ok) { toast("读取详情失败", "error"); return; }
        const usage = j.usage || "无使用方法";
        const info = "【" + (j.title || name) + "】 v" + (j.version || "1.0") +
          (j.enabled ? " · 已启用" : " · 已禁用") + "\n\n用途: " + (j.purpose || "") +
          "\n\n使用方法:\n" + usage;
        Panel.open({ id: "ext_detail_" + name, title: "扩展详情: " + name, type: "info",
                     content: { text: info }, status: "ok" });
      });
    } else if (act === "ext_toggle") {
      const name = t.getAttribute("data-name") || "";
      const enable = t.getAttribute("data-enable") === "1";
      fetch("/api/extensions/" + encodeURIComponent(name) + "/toggle", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: enable })
      }).then((r) => r.json()).then((j) => {
        toast((j.ok ? (enable ? "已启用" : "已禁用") : "操作失败") + ": " + name, j.ok ? "success" : "error");
        Panel.openExtManager();
      });
    } else if (act === "ext_uninstall") {
      const name = t.getAttribute("data-name") || "";
      if (!confirm("确定卸载扩展 " + name + " 吗?")) return;
      fetch("/api/extensions/" + encodeURIComponent(name), { method: "DELETE" })
        .then((r) => r.json()).then((j) => {
          toast(j.ok ? "已卸载: " + name : "卸载失败: " + (j.error || ""), j.ok ? "success" : "error");
          Panel.openExtManager();
        });
    } else if (act === "ext_reload") {
      Panel.openExtManager();
    }
  });

  /* 扩展上传(管理器内) */
  document.addEventListener("change", (e) => {
    const f = e.target && e.target.files && e.target.files[0];
    if (!f || e.target.id !== "extFileInput2") return;
    const fd = new FormData();
    fd.append("file", f);
    toast("正在上传扩展包: " + f.name, "info");
    fetch("/api/extensions/upload", { method: "POST", body: f })
      .then((r) => r.json()).then((j) => {
        toast(j.ok ? ("扩展 " + (j.title || j.name) + " 已安装并启用") : ("安装失败: " + (j.error || "")), j.ok ? "success" : "error");
        Panel.openExtManager();
      }).catch(() => { toast("上传失败", "error"); });
  });

  function toast(msg, level = "info") {
    const wrap = document.getElementById("toastWrap");
    const t = document.createElement("div");
    t.className = "toast " + level;
    t.textContent = msg;
    wrap.appendChild(t);
    setTimeout(() => { t.classList.add("toast-out"); setTimeout(() => t.remove(), 340); }, 2600);
  }
})();

window.Panel = Panel;
