// ShangKey URL Reporter - 把活动标签页 URL 上报给本机输入法。
//
// 安装（Chrome/Edge）：
//   1. 地址栏打开 chrome://extensions（Edge: edge://extensions）
//   2. 打开右上角/左侧"开发者模式"
//   3. "加载已解压的扩展程序" -> 选择 runtime/browser-extension 目录
//
// 生效条件：ShangKey IME 正在运行（它监听 127.0.0.1:61823）。
// IME 没运行时 fetch 失败会静默忽略，无任何影响。
//
// 两部分职责：
//   1. 活动标签页 URL -> /url   （按网页自动切换中英文）
//   2. 输入框光标屏幕坐标 -> /caret（候选窗跟随光标；Chromium 不给
//      Windows 暴露插入符，只能靠 content script 的 JS 算出来）

const ENDPOINT = "http://127.0.0.1:61823/url";
const CARET_ENDPOINT = "http://127.0.0.1:61823/caret";
let pushing = false;
let pending = false;

async function pushActiveTab() {
  if (pushing) { pending = true; return; }
  pushing = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab && tab.url && /^https?:/i.test(tab.url)) {
      await fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: tab.url, title: tab.title || "" }),
      });
    }
  } catch (e) {
    // IME 未运行 / 端口未开：静默
  } finally {
    pushing = false;
    if (pending) { pending = false; pushActiveTab(); }
  }
}

// ---- 光标位置转发（content.js -> 本机 IME）
let caretLastKey = "";
let caretLastAt = 0;

function sanitize(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > -20000 && n < 60000 ? Math.round(n) : null;
}

async function pushCaret(msg) {
  const x = sanitize(msg.x);
  const y = sanitize(msg.y);
  if (x === null || y === null) return;
  const h = sanitize(msg.h) || 0;
  const title = String(msg.title || "").slice(0, 200);
  // 同一位置 200ms 内重复上报直接丢弃（多点帧并发时会重复）
  const key = `${x},${y},${title}`;
  const now = Date.now();
  if (key === caretLastKey && now - caretLastAt < 200) return;
  caretLastKey = key;
  caretLastAt = now;
  try {
    await fetch(CARET_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, h, title }),
    });
  } catch (e) {
    // IME 未运行：静默
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "shangkey-caret") {
    pushCaret(msg);
  }
});

// 活动标签切换 / 页面加载完成 / 浏览器窗口聚焦变化 -> 上报
chrome.tabs.onActivated.addListener(pushActiveTab);
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.active) pushActiveTab();
});
chrome.windows.onFocusChanged.addListener((winId) => {
  if (winId !== chrome.windows.WINDOW_ID_NONE) pushActiveTab();
});

pushActiveTab();
