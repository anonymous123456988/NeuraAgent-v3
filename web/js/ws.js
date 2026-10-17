/* NEURA WebSocket 客户端 */
const WS = (() => {
  let ws = null, handlers = {}, alive = false;

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { alive = true; fire("open"); };
    ws.onclose = () => {
      alive = false; fire("close");
      setTimeout(connect, 1500);
    };
    ws.onerror = () => { fire("error"); };
    ws.onmessage = (ev) => {
      try { fire("message", JSON.parse(ev.data)); }
      catch (e) { console.warn("bad ws msg", e); }
    };
  }
  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }
  function on(type, fn) {
    (handlers[type] = handlers[type] || []).push(fn);
  }
  function fire(type, data) {
    (handlers[type] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } });
  }
  return { connect, send, on, isOpen: () => alive };
})();
