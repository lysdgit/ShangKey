// ShangKey 光标上报
//
// 为什么需要它：Chrome / Edge 不向 Windows 暴露网页输入框的插入符
// （GetGUIThreadInfo 取不到，UIA 在 Chromium 上直接 E_NOTIMPL），
// 所以在网页里打字时候选窗只能靠猜。本脚本用 JS 精确算出光标
// 屏幕坐标，经 background 转发给本机 ShangKey 输入法（127.0.0.1:61823）。
//
// 计算方式：
//   textarea / input  -> 镜像元素测量（同字体同宽度的隐藏 div）
//   contenteditable   -> Selection Range 的客户端矩形
//   屏幕坐标 = 浏览器内容区左上角屏幕位置 + 光标相对视口位置，再乘
//   devicePixelRatio 转成物理像素（输入法按物理像素定位）。
//
// 上报节奏：500ms 一次，仅当本页面有焦点且焦点在可编辑元素上。
// 光标不动时重复上报同样坐标，让输入法知道这个位置仍然有效。

(() => {
  const MSG_TYPE = "shangkey-caret";
  const PERIOD_MS = 500;

  const TEXT_INPUT_TYPES = new Set([
    "", "text", "search", "url", "tel", "email", "password", "number",
  ]);

  function isEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName;
    if (tag === "TEXTAREA") return !el.readOnly && !el.disabled;
    if (tag === "INPUT") {
      return TEXT_INPUT_TYPES.has((el.type || "").toLowerCase())
        && !el.readOnly && !el.disabled;
    }
    if (el.isContentEditable) return true;
    return el.getAttribute && el.getAttribute("role") === "textbox";
  }

  // ---- textarea / input：镜像元素测量光标位置
  const MIRROR_PROPS = [
    "boxSizing", "width", "height",
    "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "fontStyle", "fontVariant", "fontWeight", "fontStretch", "fontSize",
    "lineHeight", "fontFamily", "textAlign", "textTransform", "textIndent",
    "letterSpacing", "wordSpacing", "tabSize", "whiteSpace", "overflowWrap",
    "wordBreak",
  ];

  function textControlCaretRect(el) {
    const cs = window.getComputedStyle(el);
    const div = document.createElement("div");
    const st = div.style;
    st.position = "absolute";
    st.top = "0";
    st.left = "-9999px";
    st.visibility = "hidden";
    st.overflow = "hidden";
    st.whiteSpace = "pre-wrap";
    st.wordWrap = "break-word";
    for (const p of MIRROR_PROPS) {
      if (cs[p]) st[p] = cs[p];
    }
    // 内容宽度：文本框可视区宽度（不含边框），保证换行与真实一致
    st.width = el.clientWidth + "px";

    const pos = el.selectionStart == null ? el.value.length : el.selectionStart;
    div.textContent = el.value.substring(0, pos);
    const span = document.createElement("span");
    span.textContent = el.value.substring(pos) || ".";
    div.appendChild(span);
    (document.body || document.documentElement).appendChild(div);

    const divRect = div.getBoundingClientRect();
    const spanRect = span.getBoundingClientRect();
    document.body.removeChild(div);

    const borderTop = parseFloat(cs.borderTopWidth) || 0;
    const borderLeft = parseFloat(cs.borderLeftWidth) || 0;
    const rect = el.getBoundingClientRect();
    const left = rect.left + borderLeft + (spanRect.left - divRect.left)
      - el.scrollLeft;
    const top = rect.top + borderTop + (spanRect.top - divRect.top)
      - el.scrollTop;
    const height = spanRect.height
      || parseFloat(cs.lineHeight)
      || parseFloat(cs.fontSize) * 1.2
      || 18;
    return { left, top, height };
  }

  // ---- contenteditable：Selection 矩形
  function selectionCaretRect(el) {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    if (!el.contains(node) && node !== el) return null;

    let rect = null;
    const rects = range.getClientRects();
    if (rects && rects.length) {
      rect = rects[rects.length - 1];
    } else {
      // 折叠光标没有矩形：临时插入零宽字符量一下
      const span = document.createElement("span");
      span.appendChild(document.createTextNode("\u200b"));
      const probe = range.cloneRange();
      probe.collapse(false);
      try {
        probe.insertNode(span);
        const rc = span.getBoundingClientRect();
        if (rc && (rc.width || rc.height)) rect = rc;
      } catch (e) {
        return null;
      } finally {
        const parent = span.parentNode;
        if (parent) {
          parent.removeChild(span);
          parent.normalize();
        }
      }
    }
    if (!rect) return null;
    return { left: rect.left, top: rect.top, height: rect.height || 18 };
  }

  function caretRect(el) {
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      return textControlCaretRect(el);
    }
    return selectionCaretRect(el) || el.getBoundingClientRect();
  }

  // ---- 视口左上角在屏幕上的位置（CSS 像素）
  function viewportOrigin() {
    let isTop = true;
    try {
      isTop = window.top === window;
    } catch (e) {
      isTop = false;   // 跨域 iframe：访问 top 会抛异常
    }
    if (!isTop) {
      // iframe：screenX/screenY 直接给出本框架视口在屏幕上的位置
      return { x: window.screenX, y: window.screenY };
    }
    // 顶层窗口：screenX/Y 是窗口外框左上角，要加上不可见边框宽度
    // 才是内容区原点。边框四周等宽，(outerWidth - innerWidth)/2 就是它。
    // 注意 outerHeight - innerHeight 含"顶部 chrome 高度 + 底部边框"，
    // 所以垂直方向要再减掉一个边框宽度（实测不减会恒定偏下 8px）。
    const border = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
    const chromeH = Math.max(
      0, window.outerHeight - window.innerHeight - border);
    return { x: window.screenX + border, y: window.screenY + chromeH };
  }

  function report() {
    try {
      if (document.visibilityState !== "visible") return;
      if (!document.hasFocus()) return;
      const el = document.activeElement;
      if (!isEditable(el)) return;
      const r = caretRect(el);
      if (!r) return;
      const origin = viewportOrigin();
      const dpr = window.devicePixelRatio || 1;
      const x = (origin.x + r.left) * dpr;
      const y = (origin.y + r.top + r.height) * dpr;
      chrome.runtime.sendMessage({
        type: MSG_TYPE,
        x: Math.round(x),
        y: Math.round(y),
        h: Math.round(r.height * dpr),
        title: document.title || "",
      }, () => void chrome.runtime.lastError);   // 吞掉"接收端不存在"
    } catch (e) {
      // 扩展上下文失效（重载/更新）时静默
    }
  }

  // 焦点/点击/滚动/输入变化时立即上报，其余靠定时器
  for (const ev of ["focusin", "mousedown", "mouseup", "keyup",
                    "selectionchange", "input", "scroll", "resize"]) {
    window.addEventListener(ev, () => setTimeout(report, 0), true);
  }
  document.addEventListener("selectionchange", () => setTimeout(report, 0), true);

  setInterval(report, PERIOD_MS);
  report();
})();
