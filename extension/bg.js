/* POI 划词收词 —— 后台（唯一发网络请求的地方）。

   === 本地服务地址：改端口就改这一行 ===================================== */
const API_BASE = "http://127.0.0.1:8000";
/* ======================================================================= */

// 请求超时：服务没起来时连接会立刻被拒（ECONNREFUSED），这个上限是给
// "端口被别的进程占着、但不吐数据"那种半死不活的情况兜底的。
const TIMEOUT_MS = 4000;

/* 为什么网络请求非得在这儿、不能写在 content.js 里：
   Chrome 85 起内容脚本的 fetch 走**页面的 origin**，被同源策略挡死
   （实测：直接在内容脚本里 fetch 本地服务 → "Failed to fetch"）。
   后台脚本用扩展自己的身份发请求（host_permissions 授权），才连得上 127.0.0.1。
   顺带也把攻击面收窄了：网页脚本碰不到这条链路。

   跨浏览器：Chrome 用 background.service_worker，Firefox 用 background.scripts，
   manifest 两个键都写了，各取所需；代码本身只用 chrome.* 回调式 API（Firefox 兼容）。 */

// Firefox 也提供回调式的 chrome.* 别名，统一用它，跨浏览器一套代码
const api = typeof chrome !== "undefined" ? chrome : browser;

async function callJSON(path, init) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(API_BASE + path, Object.assign({ signal: ctl.signal }, init));
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (!resp.ok) {
      const detail = data && data.detail ? data.detail : "HTTP " + resp.status;
      return { ok: false, status: resp.status, error: String(detail) };
    }
    return { ok: true, data: data };
  } catch (e) {
    // fetch 抛异常 = 连不上（服务没起 / 被防火墙挡 / 超时）。
    // 这一档单独标出来，前端好显示"本地服务未启动"而不是红字堆栈。
    return { ok: false, offline: true, error: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}

function handle(msg) {
  if (!msg || typeof msg !== "object") {
    return Promise.resolve({ ok: false, error: "空消息" });
  }
  if (msg.type === "lookup") {
    return callJSON("/lookup?surface=" + encodeURIComponent(msg.surface || ""), {
      method: "GET"
    });
  }
  if (msg.type === "collect") {
    return callJSON("/collect/web", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        surface: msg.surface || "",
        sentence: msg.sentence || null,
        url: msg.url || null,
        title: msg.title || null
      })
    });
  }
  return Promise.resolve({ ok: false, error: "未知消息类型: " + msg.type });
}

// 回调式应答 + return true：Chrome MV3 不认 listener 返回的 Promise，
// Firefox 认；两边都支持 sendResponse，所以统一走这条路。
api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handle(msg).then(sendResponse, (e) =>
    sendResponse({ ok: false, error: String((e && e.message) || e) })
  );
  return true;
});
