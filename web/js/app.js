/* NEURA 主应用: 会话管理 + 聊天 + 事件路由 */
const App = (() => {
  const $ = (id) => document.getElementById(id);
  let sessions = [], currentSid = null, configInfo = {};
  const toolCards = new Map();  // tool event key -> card element

  const SUGGESTS = [
    "查看我的家目录文件",
    "今天天气怎么样",
    "帮我写一个计算器程序",
    "系统配置信息",
    "搜索 人工智能最新进展",
    "现在几点了",
  ];

  function init() {
    WS.on("open", () => {
      setStatus("connected");
      if (Ball) Ball.setStatus("connected");
    });
    WS.on("close", () => {
      setStatus("offline");
      if (Ball) Ball.setStatus("offline");
    });
    WS.on("message", route);

    $("btnNewSession").onclick = () => WS.send({ type: "new_session" });
    const btnCmd = $("btnCmdTerminal");
    if (btnCmd) btnCmd.onclick = () => Panel.openTerminal();
    $("btnSend").onclick = sendCurrent;
    const btnExt = $("btnExtUpload");
    if (btnExt) {
      btnExt.onclick = () => Panel.openExtManager();
    }
    const btnCollapse = $("btnCollapseChat");
    if (btnCollapse) btnCollapse.onclick = () => Ball.toggle();
    const input = $("chatInput");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) { stopTask(); return; }
        sendCurrent();
      }
    });
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 150) + "px";
    });
    $("inputMeta").innerHTML = "Enter 发送 · Shift+Enter 换行 · Ctrl+Enter 停止当前任务";
    renderSuggests();
    setupDesktopBridge();
    Ball.init();
    WS.connect();
  }

  function setStatus(text) {
    const pill = $("statusPill"), t = $("statusText");
    t.textContent = text;
    pill.classList.toggle("busy", ["thinking", "executing", "connected"].includes(text));
  }

  function route(msg) {
    switch (msg.type) {
      case "hello":
        sessions = msg.sessions || [];
        configInfo = msg.config || {};
        renderSessions();
        if (!currentSid && sessions.length) selectSession(sessions[0].id);
        else if (!sessions.length) WS.send({ type: "new_session" });
        updateModeLabel();
        break;
      case "session_created":
        sessions.unshift(msg.session);
        currentSid = msg.session.id;
        renderSessions(); renderChat(); renderSuggests();
        break;
      case "session_updated":
        const s = sessions.find((x) => x.id === msg.session.id);
        if (s) { Object.assign(s, msg.session); }
        renderSessions();
        break;
      case "session_deleted":
        sessions = sessions.filter((x) => x.id !== msg.session_id);
        if (currentSid === msg.session_id) {
          currentSid = null;
          if (sessions.length) selectSession(sessions[0].id);
          else WS.send({ type: "new_session" });
        }
        renderSessions();
        break;
      case "agent_status":
        setStatus(msg.status);
        break;
      case "message_delta":
        appendAssistantDelta(msg.text);
        break;
      case "message_done":
        if (msg.text) finalizeAssistant(msg.text, msg.tools || [], msg.blocked === true);
        else finishStreaming(null);  // 取消收尾: 只结束流式, 不新增气泡
        break;
      case "tool_event":
        renderToolEvent(msg);
        break;
      case "panel":
        Panel.open({ id: msg.window_id, title: msg.title, type: msg.window_type, content: msg.content, status: msg.status, admin: msg.admin });
        break;
      case "panel_update":
        Panel.update({ id: msg.window_id, content: msg.content, status: msg.status, append: msg.append });
        break;
      case "panel_close":
        Panel.close(msg.window_id);
        break;
      case "panel_control":
        Panel.control(msg);   // AI 窗口管理: close / close_all / move / focus / minimize / restore / maximize / layout
        break;
      case "admin_challenge":
        // 高危操作: 在控制板弹【系统登录密码验证窗口】(幽蓝科技风, 非系统弹窗)
        Panel.openPassword(msg.operation_id, msg.description, msg.tool, msg.args);
        break;
      case "admin_auth_result":
        Panel.passwordResult(msg.operation_id, msg.ok, msg.message);
        break;
      case "notify":
        toast(msg.message, msg.level || "info");
        break;
      case "download_ready":
        Panel.autoDownload(msg.url, msg.filename);
        toast("下载已触发: " + msg.filename, "success");
        break;
      case "command_stream":
        Panel.commandStream(msg.window_id, msg.line, msg.status);
        break;
      case "confirm_request":
        // 缺依赖等需要用户决定: 在控制板弹【是否安装】确认窗口
        Panel.openConfirm(msg.confirm_id, msg.title, msg.description, msg.package);
        break;
      case "tool_synthesized":
        Panel.open({
          id: "tool_" + msg.name,
          title: "小AI 新调用集 · " + msg.name,
          type: "code",
          content: { code: msg.code || "", path: msg.review || "动态合成" },
          status: "ok"
        });
        addToolCard(msg.name, "ok", { 动态注册: true }, null);
        toast("调用集「" + msg.name + "」已注册 · " + (msg.review || ""), "success");
        break;
      case "error":
        toast(msg.message, "error");
        break;
    }
  }

  /* ---------- 会话 ---------- */
  function renderSessions() {
    const bar = $("sessionBar");
    bar.innerHTML = "";
    sessions.forEach((s) => {
      const chip = document.createElement("div");
      chip.className = "session-chip" + (s.id === currentSid ? " active" : "");
      chip.innerHTML = `<span class="s-title">${esc(s.title || "新会话")}</span><span class="x" title="删除会话">✕</span>`;
      chip.querySelector(".s-title").onclick = () => selectSession(s.id);
      chip.querySelector(".x").onclick = (e) => {
        e.stopPropagation();
        WS.send({ type: "delete_session", session_id: s.id });
      };
      bar.appendChild(chip);
    });
  }

  function selectSession(sid) {
    currentSid = sid;
    renderSessions();
    renderChat();
  }

  function renderChat() {
    const log = $("chatLog");
    log.innerHTML = "";
    const s = sessions.find((x) => x.id === currentSid);
    if (!s) return;
    (s.history || []).forEach((m) => {
      if (m.role === "user") addBubble("user", m.content);
      else if (m.role === "assistant" && m.content) addBubble("assistant", m.content);
      else if (m.role === "assistant" && m.tool_calls) {
        (m.tool_calls).forEach((tc) => addToolCard(tc.function.name, "ok", null, null));
      }
    });
    log.scrollTop = log.scrollHeight;
  }

  /* ---------- 聊天 ---------- */
  let streaming = false, lastAssistant = null;

  function addBubble(role, text) {
    const log = $("chatLog");
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    el.innerHTML = `<div class="avatar">${role === "user" ? "U" : "◆"}</div><div class="bubble"></div>`;
    el.querySelector(".bubble").textContent = text || "";
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el.querySelector(".bubble");
  }

  function appendAssistantDelta(delta) {
    const log = $("chatLog");
    if (!streaming || !lastAssistant) {
      streaming = true;
      lastAssistant = document.createElement("div");
      lastAssistant.className = "msg assistant";
      lastAssistant.innerHTML = `<div class="avatar">◆</div><div class="bubble"></div>`;
      log.appendChild(lastAssistant);
    }
    const bubble = lastAssistant.querySelector(".bubble");
    bubble.textContent += delta;
    log.scrollTop = log.scrollHeight;
  }

  function finalizeAssistant(text, tools, blocked) {
    let el;
    if (streaming || lastAssistant) {
      el = lastAssistant;
      if (el) el.querySelector(".bubble").textContent = text;
      streaming = false; lastAssistant = null;
    } else {
      el = addBubble("assistant", text);
    }
    // 小AI 拦截的那一次消息: 气泡变红(仅此条, 其他消息样式不变)
    if (blocked && el) {
      el.classList.add("msg-blocked");
      const b = el.querySelector(".bubble");
      if (b) {
        b.classList.add("bubble-blocked");
        b.innerHTML = "\u26D4&nbsp;" + text;
      }
    }
    // 更新对应 tool 卡片状态为完成
  }

  function renderToolEvent(msg) {
    const log = $("chatLog");
    const key = msg.tool + "_" + (msg.status === "running" ? Date.now() : "card");
    if (msg.status === "running") {
      const el = document.createElement("div");
      el.className = "toolcard running";
      el.innerHTML = `<div class="tc-head"><span class="tc-icon">▸</span><b>${esc(msg.tool)}</b><span style="color:var(--text-dim)">执行中…</span></div>
        <div class="tc-args">${esc(JSON.stringify(msg.args || {}).slice(0, 180))}</div>`;
      log.appendChild(el);
      toolCards.set(msg.tool, el);
      log.scrollTop = log.scrollHeight;
    } else {
      const el = toolCards.get(msg.tool);
      if (el) {
        el.classList.remove("running");
        if (msg.status === "ok") {
          el.classList.add("ok");
          if (msg.admin) {
            el.classList.add("admin");
            el.querySelector(".tc-head").innerHTML = `<span class="tc-icon">▲</span><b>${esc(msg.tool)}</b><span style="color:var(--err)">成功 · 管理员操作</span>`;
          } else {
            el.querySelector(".tc-head").innerHTML = `<span class="tc-icon">▸</span><b>${esc(msg.tool)}</b><span style="color:var(--ok)">成功</span>`;
          }
        } else if (msg.status === "error") {
          el.classList.add("error");
          el.querySelector(".tc-head").innerHTML = `<span class="tc-icon">▦</span><b>${esc(msg.tool)}</b><span style="color:var(--err)">失败</span>`;
          const err = document.createElement("div");
          err.className = "tc-err";
          err.textContent = msg.error || "";
          el.appendChild(err);
        } else if (msg.status === "retry") {
          const r = document.createElement("div");
          r.className = "tc-args";
          r.style.color = "var(--warn)";
          r.textContent = "↻ 自动调整参数重试: " + JSON.stringify(msg.args || {}).slice(0, 160);
          el.appendChild(r);
        }
        toolCards.delete(msg.tool);
      } else {
        addToolCard(msg.tool, msg.status, msg.args, msg.error, msg.admin);
      }
    }
  }

  function addToolCard(tool, status, args, error, admin) {
    const log = $("chatLog");
    const el = document.createElement("div");
    el.className = `toolcard ${status || ""}${admin && status === "ok" ? " admin" : ""}`;
    const icon = admin && status === "ok" ? "⚠" : (status === "ok" ? "✓" : (status === "error" ? "✕" : "◌"));
    el.innerHTML = `<div class="tc-head"><span class="tc-icon">${icon}</span><b>${esc(tool)}</b>${
      admin && status === "ok" ? '<span style="color:var(--err)">成功 · 管理员操作</span>' : ""}</div>
      ${args ? `<div class="tc-args">${esc(JSON.stringify(args).slice(0, 180))}</div>` : ""}
      ${error ? `<div class="tc-err">${esc(error)}</div>` : ""}`;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  function sendCurrent() {
    const input = $("chatInput");
    const text = input.value.trim();
    if (!text || !currentSid) return;
    addBubble("user", text);
    WS.send({ type: "user_message", session_id: currentSid, text });
    input.value = "";
    input.style.height = "auto";
    // 注意: 发送指令绝不发送 cancel——否则每条消息都会误取消上一任务并堆积"已取消当前任务"提示
  }

  function stopTask() {
    if (!currentSid) return;
    // 本地立即收尾流式气泡(标记已停止), 避免取消后半截气泡卡在"生成中"
    finishStreaming("(已停止)");
    WS.send({ type: "cancel", session_id: currentSid });
  }

  function finishStreaming(mark) {
    if (streaming && lastAssistant) {
      const bubble = lastAssistant.querySelector(".bubble");
      if (mark) bubble.textContent = (bubble.textContent || "") + " " + mark;
      streaming = false;
      lastAssistant = null;
    }
  }

  function renderSuggests() {
    const wrap = $("suggestChips");
    wrap.innerHTML = "";
    SUGGESTS.forEach((s) => {
      const chip = document.createElement("div");
      chip.className = "suggest-chip";
      chip.textContent = s;
      chip.onclick = () => {
        $("chatInput").value = s;
        sendCurrent();
      };
      wrap.appendChild(chip);
    });
  }

  function updateModeLabel() {
    const label = $("modeLabel");
    const bi = configInfo.use_builtin_model;
    const ds = configInfo.use_deepseek_api;
    const modeTxt = bi ? "内置模型" : "DeepSeek API";
    const dsTxt = ds ? "ON" : "OFF";
    if (!bi) label.textContent = `${modeTxt} · ${configInfo.api_model} · 内置模型:OFF · DeepSeek:ON · Key:${configInfo.api_key_masked}`;
    else if (configInfo.builtin_backend === "ollama") label.textContent = `${modeTxt} · Ollama/${configInfo.builtin_model || "?"} · DeepSeek:${dsTxt}`;
    else if (configInfo.builtin_backend === "openai_compatible") label.textContent = `${modeTxt} · 本地大模型(${configInfo.builtin_model || "?"}) · DeepSeek:${dsTxt}`;
    else if (configInfo.builtin_backend === "nlm") label.textContent = `${modeTxt} · 独家大模型 NeuraLM · DeepSeek:${dsTxt}`;
    else label.textContent = `${modeTxt} · NeuraBrain 引擎 · DeepSeek:${dsTxt}`;
  }

  function toast(msg, level = "info") {
    const wrap = $("toastWrap");
    // 同文本去重: 连续相同提示只刷新计时并上浮, 不再叠加多个重复气泡
    const existing = Array.from(wrap.children).find(
      (el) => el.dataset.msg === msg && el.dataset.level === level
    );
    if (existing) { scheduleToast(existing); return; }
    const t = document.createElement("div");
    t.className = "toast " + level;
    t.dataset.msg = msg;
    t.dataset.level = level;
    t.textContent = msg;
    wrap.appendChild(t);
    scheduleToast(t);
  }

  function scheduleToast(t) {
    t.style.transition = "none";
    t.style.opacity = "1";
    clearTimeout(t._timer);
    t._timer = setTimeout(() => {
      t.style.transition = "opacity .3s";
      t.style.opacity = "0";
      setTimeout(() => t.remove(), 320);
    }, 2600);
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  return { init, currentSid: () => currentSid };
})();

/* ---------------------------------------------------------------
   NEURA 悬浮球(Ball): 对话框折叠为悬浮小球, 点击展开/再点缩回。
   桌面端(pywebview)下还负责无边框窗口拖动与置顶。
---------------------------------------------------------------- */
const Ball = (() => {
  let collapsed = false;

  function init() {
    const ball = document.getElementById("neuraBall");
    if (!ball) return;
    ball.addEventListener("click", toggle);
    makeDraggable(ball);
  }

  function toggle() {
    const chat = document.getElementById("chat");
    const ball = document.getElementById("neuraBall");
    if (!chat || !ball) return;
    collapsed = !collapsed;
    if (collapsed) {
      chat.classList.add("collapsed");
      ball.classList.remove("hidden");
      ball.classList.add("shown");
    } else {
      chat.classList.remove("collapsed");
      ball.classList.add("hidden");
      ball.classList.remove("shown");
      const input = document.getElementById("chatInput");
      if (input) input.focus();
    }
    const btn = document.getElementById("btnCollapseChat");
    if (btn) btn.textContent = collapsed ? "◉ 展开" : "◐ 悬浮球";
  }

  function setStatus(st) {
    const s = document.getElementById("ballStatus");
    if (!s) return;
    s.dataset.state = st || "idle";
  }

  // 悬浮球可拖动(指针事件)
  function makeDraggable(el) {
    if (!el) return;
    let sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
    el.addEventListener("pointerdown", (e) => {
      drag = true;
      sx = e.clientX; sy = e.clientY;
      ox = el.offsetLeft; oy = el.offsetTop;
      el.setPointerCapture && el.setPointerCapture(e.pointerId);
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag) return;
      el.style.left = Math.max(0, Math.min(window.innerWidth - 64, ox + (e.clientX - sx))) + "px";
      el.style.top = Math.max(0, Math.min(window.innerHeight - 64, oy + (e.clientY - sy))) + "px";
      el.style.transform = "none";
    });
    el.addEventListener("pointerup", () => { drag = false; });
    return el;
  }

  return { init, toggle, setStatus };
})();

/* ---------------------------------------------------------------
   桌面端桥(DesktopBridge): pywebview js_api —— 无边框 HUD 窗口拖动、
   置顶、最小化、系统信息一键查询。Web 浏览器下自动跳过。
---------------------------------------------------------------- */
function setupDesktopBridge() {
  const api = (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
  if (!api) return;                     // Web 模式: 跳过桌面专属能力
  // 无边框窗口拖动: 顶部标题栏指针拖动 -> js_api.move_window
  const header = document.querySelector("header");
  if (header) {
    let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    header.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button") || e.target.closest(".session-chip")) return;
      dragging = true; sx = e.clientX; sy = e.clientY;
    });
    header.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - sx, dy = e.clientY - sy;
      api.move_window(Math.round(dx), Math.round(dy)).then(() => {
        sx = e.clientX; sy = e.clientY;
      }).catch(() => {});
    });
    header.addEventListener("pointerup", () => { dragging = false; });
  }
  // 控制板工具区: 加"系统监控"与"窗口置顶"桌面按钮
  const tools = document.getElementById("panelTools") || document.querySelector(".panel-tools");
  if (tools && !document.getElementById("btnDesktopSys")) {
    const b1 = document.createElement("button");
    b1.id = "btnDesktopSys"; b1.className = "ptool"; b1.title = "桌面系统信息(实时)";
    b1.textContent = "◉ 系统";
    b1.onclick = async () => {
      try {
        const info = await api.get_system_info();
        const lines = [
          `CPU 使用率: ${info.cpu_percent ?? "?"}% (${info.cpu_cores ?? "?"} 核)`,
          `内存: ${info.memory_used_gb ?? "?"} / ${info.memory_total_gb ?? "?"} GB (${info.memory_percent ?? "?"}%)`,
          `磁盘: 剩余 ${info.disk_free_gb ?? "?"} / ${info.disk_total_gb ?? "?"} GB (${info.disk_percent ?? "?"}%)`,
          `网络: 上行 ${info.net_sent_mb ?? "?"} MB / 下行 ${info.net_recv_mb ?? "?"} MB`,
        ];
        if (info.battery_percent != null) lines.push(`电池: ${info.battery_percent}% ${info.battery_power ? "(充电中)" : ""}`);
        Panel.open({
          id: "desktop_sys", title: "桌面系统监控", type: "process",
          content: { lines: [info.os || "", `时间: ${info.time}`].concat(lines) },
          status: "ok"
        });
      } catch (e) {
        Panel.open({ id: "desktop_sys", title: "桌面系统监控", type: "process",
          content: { lines: ["桌面系统信息暂不可用: " + e] }, status: "error" });
      }
    };
    const b2 = document.createElement("button");
    b2.id = "btnDesktopTop"; b2.className = "ptool"; b2.title = "窗口置顶/取消";
    b2.textContent = "⛶ 置顶";
    b2.onclick = async () => {
      try {
        const on = await api.set_window_top(true);
        toast(on ? "窗口已置顶" : "置顶不可用", on ? "success" : "error");
      } catch (e) { toast("置顶不可用", "error"); }
    };
    tools.appendChild(b1);
    tools.appendChild(b2);
  }
}

document.addEventListener("DOMContentLoaded", App.init);
