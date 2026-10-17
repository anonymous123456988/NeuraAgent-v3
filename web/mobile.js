/* ============ NEURA Mobile · 手机版主逻辑 ============ */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const LS_KEY = "neura_mobile_server";

  let ws = null, sid = null, connected = false, busy = false;
  let voiceRec = null, listening = false, wakeListening = false;
  let pendingOp = null;   // admin_challenge operation_id
  let lastCmd = "";

  /* ---------------- 工具 ---------------- */
  function toast(msg, level) {
    const w = $("toastWrap");
    const t = document.createElement("div");
    t.className = "toast" + (level === "error" ? " error" : "");
    t.textContent = msg;
    w.appendChild(t);
    setTimeout(() => { t.remove(); }, 2800);
  }
  function vibrate(ms) { try { navigator.vibrate && navigator.vibrate(ms); } catch (e) { } }
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
  }
  function addMsg(text, cls) {
    const list = $("msgList");
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.textContent = text;
    list.appendChild(d);
    list.scrollTop = list.scrollHeight;
    return d;
  }
  function showDialog() { $("dialogWrap").classList.add("show"); $("dialogBackdrop").classList.add("show"); }
  function hideDialog() { $("dialogWrap").classList.remove("show"); $("dialogBackdrop").classList.remove("show"); }
  function isDialogOpen() { return $("dialogWrap").classList.contains("show"); }

  /* ---------------- 状态条 ---------------- */
  function setStatus(on, text) {
    $("statusDot").className = on ? "on" : "off";
    $("statusText").textContent = text || (on ? "已连接" : "未连接");
  }
  function setBusy(on) {
    busy = on;
    $("bgBar").classList.toggle("show", on);
    if (on && isDialogOpen()) { hideDialog(); }        // AI 执行 -> 收起对话框后台运行
  }

  /* ---------------- WebSocket ---------------- */
  function serverBase() {
    let v = (localStorage.getItem(LS_KEY) || "").trim();
    if (v && !/^https?:\/\//i.test(v)) v = "http://" + v;
    return v.replace(/\/+$/, "");
  }
  function apiBase() {
    // 浏览器(同源)直接用相对路径; Capacitor WebView(https://localhost)用服务器地址
    const sb = serverBase();
    if (!sb) return "";
    if (location.origin && (location.origin + "").includes(sb.replace(/^https?:\/\//, ""))) return "";
    return sb;
  }
  function connect() {
    const base = serverBase();
    if (!base) { setStatus(false, "未连接 · 点击右上角设置服务器"); return; }
    setStatus(false, "连接 " + base + " …");
    try { if (ws) ws.close(); } catch (e) { }
    ws = new WebSocket(base.replace(/^http/, "ws") + "/ws");
    ws.onopen = () => {
      connected = true;
      setStatus(true, "已连接 " + base + " · NEURA");
      ws.send(JSON.stringify({ type: "new_session" }));
    };
    ws.onclose = () => { connected = false; setStatus(false, "连接断开 · 点击右上角重连"); };
    ws.onerror = () => { setStatus(false, "无法连接 · 检查电脑端已运行 run.py"); };
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  }

  function handle(m) {
    const t = m.type || "";
    if (t === "session_created") { sid = m.session && m.session.id; }
    else if (t === "hello") { if (!sid && m.sessions && m.sessions[0]) sid = m.sessions[0].id; }
    else if (t === "message_delta") {
      if (isDialogOpen()) {
        const list = $("msgList");
        let last = list.lastElementChild;
        if (!last || !last.classList.contains("ai")) { last = addMsg("", "ai"); }
        last.textContent = (m.text || "");
        list.scrollTop = list.scrollHeight;
      }
    }
    else if (t === "message_done") {
      setBusy(false);
      if (!isDialogOpen()) { showDialog(); addMsg("", "sys"); }
      addMsg((m.text || "").trim() || "完成", "ai");
      if (m.blocked) addMsg("⚠ 该操作被小AI 拦截", "blocked");
      vibrate(30);
    }
    else if (t === "agent_status") {
      if (m.status === "executing") setBusy(true);
      else if (m.status === "thinking") { /* 保持 */ }
    }
    else if (t === "tool_event") {
      if (m.status === "running") {
        setBusy(true);
        $("bgBarText").textContent = "NEURA 正在执行: " + (m.tool || "") + " …";
        if (isDialogOpen()) { hideDialog(); }
      }
    }
    else if (t === "admin_challenge") {
      setBusy(false);
      pendingOp = m.operation_id;
      $("authTitle").textContent = "⚠ 高危操作 · " + (m.tool || "");
      $("authDesc").textContent = (m.description || "该操作需要系统登录密码确认") + "\n工具: " + (m.tool || "");
      $("authInput").value = "";
      $("authMask").classList.add("show");
      setTimeout(() => { try { $("authInput").focus(); } catch (e) { } }, 120);
    }
    else if (t === "admin_auth_result") {
      $("authMask").classList.remove("show");
      toast(m.message || (m.ok ? "已确认" : "验证失败"), m.ok ? "info" : "error");
    }
    else if (t === "notify") { toast(m.message || ""); }
    else if (t === "error") { toast(m.message || "出错", "error"); }
  }

  /* ---------------- 发送 ---------------- */
  function sendText(text) {
    text = (text || "").trim();
    if (!text || !connected) { if (!connected) toast("未连接服务", "error"); return; }
    if (!sid) { ws.send(JSON.stringify({ type: "new_session" })); }
    addMsg(text, "me");
    lastCmd = text;
    ws.send(JSON.stringify({ type: "user_message", session_id: sid, text: text }));
    setBusy(true);
    if (isDialogOpen()) { setTimeout(() => hideDialog(), 300); }   // 执行时后台化
  }

  /* ---------------- 语音 ---------------- */
  function speechSupported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }
  function startVoice(onResult, onEnd) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { toast("当前浏览器不支持语音(需 https 或本机访问)", "error"); return null; }
    const rec = new SR();
    rec.lang = "zh-CN";
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 3;
    let buf = "";
    rec.onresult = (e) => {
      let full = "";
      for (let i = e.resultIndex; i < e.results.length; i++) full += e.results[i][0].transcript;
      buf = full;
      if (onResult) onResult(buf);
    };
    rec.onerror = () => { try { rec.stop(); } catch (e) { } if (onEnd) onEnd(); };
    rec.onend = () => { if (onEnd) onEnd(); };
    try { rec.start(); } catch (e) { }
    return rec;
  }
  function matchWake(text) {
    const t = (text || "").toLowerCase().replace(/[，。,.！!？?\s]/g, "");
    const words = ["嘿neura", "heineura", "嘿你拉", "嘿内拉", "内拉", "neura", "嘿nira", "嘿尼拉"];
    return words.some((w) => t.includes(w));
  }
  function startWakeListening() {
    if (wakeListening) return;
    if (!speechSupported()) { toast("语音唤醒不可用: 浏览器需支持 Web Speech(建议安装为应用/HTTPS)", "error"); return; }
    wakeListening = true;
    $("wakeBtn").classList.add("listening");
    voiceRec = startVoice((txt) => {
      if (matchWake(txt) && !isDialogOpen()) {
        showDialog();
        vibrate([40, 60, 40]);
        addMsg("🔊 已唤醒 · 请说出指令或输入", "sys");
        $("wakeBtn").classList.remove("listening");
        stopWake();
      }
    }, () => { });
  }
  function stopWake() {
    wakeListening = false;
    $("wakeBtn").classList.remove("listening");
    if (voiceRec) { try { voiceRec.stop(); } catch (e) { } voiceRec = null; }
  }

  /* 按住说话/点按说话 -> 直接发送指令 */
  function toggleChatVoice() {
    if (listening) {
      listening = false;
      $("btnVoice").classList.remove("listening");
      if (voiceRec) { try { voiceRec.stop(); } catch (e) { } voiceRec = null; }
      return;
    }
    if (!speechSupported()) { toast("语音输入不可用(浏览器限制)", "error"); return; }
    listening = true;
    $("btnVoice").classList.add("listening");
    if (!isDialogOpen()) showDialog();
    toast("聆听中…再次点击结束并发送", "info");
    voiceRec = startVoice((txt) => {
      if (isDialogOpen()) { /* 实时回显到输入框 */ $("chatInput").value = txt; }
    }, () => { });
    setTimeout(() => {
      if (listening) {
        const txt = $("chatInput").value.trim();
        listening = false;
        $("btnVoice").classList.remove("listening");
        if (voiceRec) { try { voiceRec.stop(); } catch (e) { } voiceRec = null; }
        if (txt) sendText(txt);
      }
    }, 8000);
  }

  /* ---------------- 扩展管理 ---------------- */
  function openSheet(title, html) {
    $("sheetTitle").textContent = title;
    $("sheetBody").innerHTML = html;
    $("sheet").classList.add("show");
  }
  function renderExtManager() {
    fetch(apiBase() + "/api/extensions").then((r) => r.json()).then((j) => {
      const exts = (j && j.extensions) || [];
      let h = "";
      if (!exts.length) h = '<div class="ext-empty">暂无扩展 · 点击下方上传扩展包(zip: manifest.json + usage.md + src/main.py)</div>';
      exts.forEach((x) => {
        const en = !!x.enabled;
        h += `<div class="ext-row">
          <div style="flex:1">
            <div class="e-name">${esc(x.name)} <span class="e-title">${esc(x.title || "")}</span></div>
            <div style="font-size:11px;color:#8a95a5;margin-top:2px">${esc((x.purpose || "").slice(0, 42))}</div>
          </div>
          <span class="e-state ${en ? "on" : "off"}">${en ? "已启用" : "已禁用"}</span>
          <button class="mini-btn" data-ext="toggle" data-name="${esc(x.name)}" data-en="${en ? "0" : "1"}">${en ? "禁用" : "启用"}</button>
          <button class="mini-btn" data-ext="detail" data-name="${esc(x.name)}">详情</button>
          <button class="mini-btn danger" data-ext="del" data-name="${esc(x.name)}">删除</button>
        </div>`;
      });
      h += `<div style="display:flex;gap:8px;margin-top:14px">
        <label class="mini-btn primary" style="flex:1;text-align:center;padding:11px" for="extUpload">⬒ 上传扩展包</label>
        <input id="extUpload" type="file" accept=".zip">
        <button class="mini-btn" style="padding:11px" data-ext="refresh">↻</button>
      </div>`;
      openSheet("⬒ 扩展管理", h);
    }).catch(() => toast("读取扩展失败", "error"));
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ext]");
    if (!b) return;
    const act = b.getAttribute("data-ext");
    const name = b.getAttribute("data-name") || "";
    if (act === "toggle") {
      const en = b.getAttribute("data-en") === "1";
      fetch(apiBase() + "/api/extensions/" + encodeURIComponent(name) + "/toggle", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: en })
      }).then((r) => r.json()).then((j) => {
        toast(j.ok ? (en ? "已启用 " : "已禁用 ") + name : "操作失败: " + (j.error || ""), j.ok ? "info" : "error");
        renderExtManager();
      });
    } else if (act === "del") {
      if (!confirm("确定删除扩展 " + name + " 吗?")) return;
      fetch(apiBase() + "/api/extensions/" + encodeURIComponent(name), { method: "DELETE" })
        .then((r) => r.json()).then((j) => {
          toast(j.ok ? "已删除 " + name : "删除失败", j.ok ? "info" : "error");
          renderExtManager();
        });
    } else if (act === "detail") {
      fetch(apiBase() + "/api/extensions/" + encodeURIComponent(name)).then((r) => r.json()).then((j) => {
        if (!j || !j.ok) { toast("读取失败", "error"); return; }
        addMsg("【扩展详情 " + name + "】 v" + (j.version || "1.0") + (j.enabled ? " · 已启用" : " · 已禁用") +
          "\n用途: " + (j.purpose || "") + "\n\n使用方法:\n" + (j.usage || "无"), "sys");
        $("sheet").classList.remove("show");
        showDialog();
      });
    } else if (act === "refresh") { renderExtManager(); }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id !== "extUpload") return;
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    toast("正在上传扩展包…", "info");
    fetch(apiBase() + "/api/extensions/upload", { method: "POST", body: f }).then((r) => r.json()).then((j) => {
      toast(j.ok ? ("扩展 " + (j.title || j.name) + " 已安装并启用") : ("安装失败: " + (j.error || "")), j.ok ? "info" : "error");
      renderExtManager();
    });
  });

  /* ---------------- 模型设置 ---------------- */
  const MODELS = [
    ["use_builtin_model", "内置独家大模型 NeuraLM", "离线 · 零外部依赖 · 自研引擎"],
    ["use_deepseek_api", "DeepSeek API", "deepseek-chat · 需配置 api_key"],
    ["use_openai_api", "OpenAI API", "OpenAI 兼容接口 · 需配置 key/base_url"],
    ["use_doubao_api", "豆包 API", "火山方舟 · 需配置 key/model"],
    ["use_yuanbao_api", "元宝 API", "腾讯元宝 · 需配置 key"],
    ["use_custom_api", "自定义 API", "任意 OpenAI 兼容端点"],
  ];
  function renderModelSettings() {
    fetch(apiBase() + "/api/settings/model").then((r) => r.json()).then((j) => {
      if (!j || !j.ok) { toast("读取模型配置失败", "error"); return; }
      const sw = j.switches || {};
      let h = '<div style="font-size:12px;color:#8a95a5;margin-bottom:6px">当前: ' + esc(j.mode || "") + " · 选择后立即切换</div>";
      MODELS.forEach(([key, name, sub]) => {
        const on = !!sw[key];
        h += `<div class="model-item${on ? " on" : ""}" data-model="${key}">
          <span class="rad"></span>
          <div style="flex:1"><div class="m-name">${esc(name)}</div><div class="m-sub">${esc(sub)}</div></div>
        </div>`;
      });
      openSheet("⚙ 设置 AI 模型", h);
    }).catch(() => toast("读取模型配置失败", "error"));
  }
  document.addEventListener("click", (e) => {
    const it = e.target.closest("[data-model]");
    if (!it) return;
    const primary = it.getAttribute("data-model");
    if (it.classList.contains("on")) return;
    toast("正在切换模型…", "info");
    fetch(apiBase() + "/api/settings/model", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ primary: primary })
    }).then((r) => r.json()).then((j) => {
      if (j && j.ok) {
        toast(j.message || "已切换", "info");
        renderModelSettings();
      } else { toast((j && j.error) || "切换失败", "error"); }
    });
  });

  /* ---------------- UI 事件 ---------------- */
  function initUI() {
    // 唤醒按钮
    $("wakeBtn").addEventListener("click", () => {
      if (wakeListening) { stopWake(); toast("语音唤醒已关闭", "info"); }
      else { startWakeListening(); toast("语音唤醒已开启 · 说 “嘿 Neura”", "info"); }
    });
    // 点外部关闭对话框
    $("dialogBackdrop").addEventListener("click", () => { hideDialog(); });
    // 对话框
    $("btnClose").addEventListener("click", () => { hideDialog(); });
    $("btnSend").addEventListener("click", () => { const t = $("chatInput").value; $("chatInput").value = ""; sendText(t); });
    $("chatInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { const t = $("chatInput").value; $("chatInput").value = ""; sendText(t); }
    });
    $("chatInput").addEventListener("focus", () => { if (!isDialogOpen()) showDialog(); });
    $("btnVoice").addEventListener("click", toggleChatVoice);
    // 扩展 / 模型 / 连接
    $("btnExt").addEventListener("click", renderExtManager);
    $("btnModel").addEventListener("click", renderModelSettings);
    $("btnSheetClose").addEventListener("click", () => $("sheet").classList.remove("show"));
    $("btnLink").addEventListener("click", openLink);
    $("btnLinkNo").addEventListener("click", () => $("linkMask").classList.remove("show"));
    $("btnLinkOk").addEventListener("click", saveLink);
    $("linkInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveLink(); });
    // 密码确认
    $("btnAuthNo").addEventListener("click", cancelAuth);
    $("btnAuthOk").addEventListener("click", submitAuth);
    $("authInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submitAuth(); });
  }
  function openLink() {
    $("linkInput").value = serverBase().replace(/^https?:\/\//, "");
    $("linkMask").classList.add("show");
  }
  function saveLink() {
    let v = ($("linkInput").value || "").trim();
    if (!v) { toast("请输入地址", "error"); return; }
    if (!/^https?:\/\//i.test(v)) v = "http://" + v;
    localStorage.setItem(LS_KEY, v.replace(/\/+$/, ""));
    $("linkMask").classList.remove("show");
    connect();
  }
  function submitAuth() {
    if (!pendingOp) return;
    const pwd = $("authInput").value;
    if (!connected) { toast("未连接", "error"); return; }
    ws.send(JSON.stringify({ type: "admin_auth", operation_id: pendingOp, password: pwd }));
    pendingOp = null;
  }
  function cancelAuth() {
    if (pendingOp && connected) {
      ws.send(JSON.stringify({ type: "admin_cancel", operation_id: pendingOp }));
    }
    pendingOp = null;
    $("authMask").classList.remove("show");
  }

  /* ---------------- 启动 ---------------- */
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  initUI();
  if (serverBase()) {
    connect();
    setInterval(() => { if (!connected && serverBase()) { try { connect(); } catch (e) { } } }, 12000);
  }
})();
