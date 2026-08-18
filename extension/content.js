/* POI 划词收词 —— 内容脚本（工单 11）。

   零依赖、零构建、零浏览器存储：状态只活在页面内存里，刷新即清空。
   本文件**不发网络请求**（内容脚本的 fetch 被同源策略挡死，理由见 bg.js 顶部），
   所有请求都通过 chrome.runtime 消息交给 bg.js；服务地址常量也在 bg.js 头上。

   交互契约（工单 11 验收口径）：
   1. 选中含英文字母的词/短语 → 选区旁浮出 ⌖ 小按钮，3 秒无操作自动消失；
   2. 点 ⌖ 才弹卡（永不自动弹），卡里给：当前形式/词元/音标/释义/整句/已收状态；
   3. [收入生词本] → POST /collect/web → 按钮变 ✓；重复收藏同词只加一次相遇；
   4. 词典未收录照样能收（服务端建 Lexeme 骨架行）；
   5. 本地服务没起 → 卡里一行小字提示，不弹窗不刷屏；
   6. Esc 或点卡外关卡。

   句子扩取（DOM → 纯字符串两层）：
   - DOM 层 poiFlatten：把选区所在的块级容器拍平成一行文本，行内标签（b/i/a/span…）
     直接拼起来，块级标签之间插 "\n" 当硬边界，顺便记下选区在拍平文本里的下标；
   - 纯函数层 poiSliceSentence：只认字符串和下标，往两侧扩到句边界。
     这一层没有 DOM 依赖，被 tests/test_extension_sentence.py 用 node 直接单测。 */

"use strict";

/* ========== 常量（改行为改这里） ========== */
var POI_FLAG_TIMEOUT_MS = 3000; // ⌖ 浮标无操作自动消失
var POI_MAX_SURFACE_CHARS = 64; // 选区超过这么长就不当"词/短语"
var POI_MAX_SURFACE_WORDS = 6;
var POI_MAX_SENTENCE_CHARS = 400; // 句子往两侧各扩的字符上限
var POI_OFFLINE_HINT = "本地服务未启动（python -m uvicorn app.server:app）";

// 句末标点后面黏着的收尾符号，一起算进句子里
var POI_TRAILING = "\"'”’)]》」』";
// 常见缩写：后面那个点不是句号
var POI_ABBREV = {
  mr: 1, mrs: 1, ms: 1, dr: 1, prof: 1, sr: 1, jr: 1, st: 1, mt: 1, vs: 1,
  etc: 1, inc: 1, ltd: 1, co: 1, corp: 1, dept: 1, est: 1, fig: 1, no: 1,
  vol: 1, al: 1, approx: 1, apt: 1, ave: 1, gen: 1, gov: 1, sen: 1, rep: 1
};
// 块级标签：拍平文本时两侧插 "\n"（硬句边界，段落之间绝不粘连）
var POI_BLOCK = {
  ADDRESS: 1, ARTICLE: 1, ASIDE: 1, BLOCKQUOTE: 1, BR: 1, DD: 1, DIV: 1,
  DL: 1, DT: 1, FIELDSET: 1, FIGCAPTION: 1, FIGURE: 1, FOOTER: 1, FORM: 1,
  H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, HEADER: 1, HR: 1, LI: 1, MAIN: 1,
  NAV: 1, OL: 1, P: 1, PRE: 1, SECTION: 1, TABLE: 1, TBODY: 1, TD: 1, TH: 1,
  TR: 1, UL: 1
};
var POI_SKIP = {
  SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, SELECT: 1, SVG: 1,
  CANVAS: 1, IFRAME: 1, VIDEO: 1, AUDIO: 1
};

/* ========== 纯函数层：句子扩取（node 单测直接调这几个） ========== */

function poiCollapse(s) {
  return String(s == null ? "" : s).replace(/\s+/g, " ");
}

/** 选区看着像不像英文词/短语：含英文字母、不太长、不是整段。 */
function poiLooksEnglish(text) {
  var t = poiCollapse(text).trim();
  if (!t || t.length > POI_MAX_SURFACE_CHARS) return false;
  if (!/[A-Za-z]/.test(t)) return false;
  return t.split(/\s+/).length <= POI_MAX_SURFACE_WORDS;
}

/** text[i] 是不是句末标点。缩写、小数、域名、首字母缩写一律排除。 */
function poiIsSentenceEnd(text, i) {
  var ch = text.charAt(i);
  if ("!?…。！？".indexOf(ch) < 0 && ch !== ".") return false;

  // 后面必须是空白或行尾（跨过收尾引号/括号）；"3.5"、"example.com" 因此出局
  var k = i + 1;
  while (k < text.length && POI_TRAILING.indexOf(text.charAt(k)) >= 0) k++;
  if (k < text.length && !/\s/.test(text.charAt(k))) return false;

  if (ch !== ".") return true; // ! ? … 没有缩写歧义

  // 点号前面那串字母：缩写词 / 单个首字母 / 内部带点的 e.g. i.e. U.S. → 不算句末
  var j = i - 1, word = "";
  while (j >= 0 && /[A-Za-z]/.test(text.charAt(j))) {
    word = text.charAt(j) + word;
    j--;
  }
  if (!word) return true; // "…" 之类，前面没字母
  if (word.length === 1 && text.charAt(j) === ".") return false; // e.g. / U.S.
  if (word.length === 1 && /[A-Z]/.test(word) && (j < 0 || !/[A-Za-z]/.test(text.charAt(j)))) {
    return false; // 姓名首字母 "J. K. Rowling"
  }
  return !POI_ABBREV[word.toLowerCase()];
}

/** 窗口边缘兜底：截断处别切在半个词上（往右找下一个词首）。 */
function poiWordStart(text, i) {
  while (i < text.length && !/\s/.test(text.charAt(i))) i++;
  while (i < text.length && /\s/.test(text.charAt(i))) i++;
  return i;
}

function poiWordEnd(text, i) {
  while (i > 0 && !/\s/.test(text.charAt(i - 1))) i--;
  return i;
}

/**
 * 纯函数：把 [start,end) 这段选区向两侧扩到句边界，返回整句（空白已归一）。
 *
 * 边界处理：
 * - "\n"（拍平时块级标签留下的硬边界）优先于标点，段落之间不会粘连；
 * - 缩写/小数/域名里的点不当句号（poiIsSentenceEnd）；
 * - 两侧各最多扩 POI_MAX_SENTENCE_CHARS，扩不到边界就在词的缝隙处截断；
 * - 选区本身跨了好几句：左端从第一句头扩起、右端到最后一句尾，整段都留着。
 */
function poiSliceSentence(text, start, end) {
  text = String(text == null ? "" : text);
  var n = text.length;
  if (!n) return "";
  start = Math.max(0, Math.min(start | 0, n));
  end = Math.max(start, Math.min(end | 0, n));

  var left = 0;
  var lLimit = Math.max(0, start - POI_MAX_SENTENCE_CHARS);
  var found = false;
  for (var i = start - 1; i >= lLimit; i--) {
    if (text.charAt(i) === "\n" || poiIsSentenceEnd(text, i)) {
      left = i + 1;
      found = true;
      break;
    }
  }
  if (found) {
    // 左边界紧跟在句末标点后面：上一句的收尾引号/括号别算到这一句头上
    // （只在真找到边界时跳，否则会把本句开头的引号也吃掉）
    while (left < n && (POI_TRAILING.indexOf(text.charAt(left)) >= 0 || /\s/.test(text.charAt(left)))) {
      left++;
    }
  } else if (lLimit > 0) {
    left = poiWordStart(text, lLimit);
  }

  var right = n;
  var rLimit = Math.min(n, end + POI_MAX_SENTENCE_CHARS);
  found = false;
  for (var j = Math.max(end - 1, left); j < rLimit; j++) {
    if (text.charAt(j) === "\n") {
      right = j;
      found = true;
      break;
    }
    if (poiIsSentenceEnd(text, j)) {
      var k = j + 1;
      while (k < n && POI_TRAILING.indexOf(text.charAt(k)) >= 0) k++;
      right = k;
      found = true;
      break;
    }
  }
  if (!found && rLimit < n) right = poiWordEnd(text, rLimit);

  return poiCollapse(text.slice(left, right)).trim();
}

/* ========== DOM 层：拍平 + 定位选区 ========== */

function poiBlockAncestor(node) {
  var el = node && node.nodeType === 1 ? node : node && node.parentNode;
  while (el && el.nodeType === 1) {
    if (el === document.body || POI_BLOCK[el.tagName]) return el;
    el = el.parentNode;
  }
  return document.body || document.documentElement;
}

/**
 * 把 root 子树拍平成一行文本，并记下 range 两端在这行文本里的下标。
 * 行内标签直接拼（`<b>stake</b>out` → "stakeout"），块级标签之间插 "\n"。
 */
function poiFlatten(root, range) {
  var out = { text: "", start: -1, end: -1 };

  function pushBreak() {
    if (out.text && out.text.charAt(out.text.length - 1) !== "\n") out.text += "\n";
  }

  function walk(node) {
    if (node.nodeType === 3) {
      var raw = node.nodeValue || "";
      if (node === range.startContainer) {
        out.start = out.text.length + poiCollapse(raw.slice(0, range.startOffset)).length;
      }
      if (node === range.endContainer) {
        out.end = out.text.length + poiCollapse(raw.slice(0, range.endOffset)).length;
      }
      out.text += poiCollapse(raw);
      return;
    }
    if (node.nodeType !== 1) return;
    if (POI_SKIP[node.tagName]) return;
    if (node.hasAttribute && node.hasAttribute("data-poi-ui")) return; // 别把自己的 UI 读进来
    var block = POI_BLOCK[node.tagName] === 1;
    if (block) pushBreak();
    for (var c = node.firstChild; c; c = c.nextSibling) walk(c);
    if (block) pushBreak();
  }

  walk(root);
  return out;
}

/** 选区 → 整句。任何一步塌了都退回选中的文本本身，绝不抛异常打断页面。 */
function poiSentenceForRange(range, selected) {
  var fallback = poiCollapse(selected).trim();
  try {
    var root = poiBlockAncestor(range.commonAncestorContainer);
    if (!root) return fallback;
    var flat = poiFlatten(root, range);
    var s = flat.start, e = flat.end;
    if (s < 0 || e < 0 || e <= s) {
      // 选区端点落在元素节点上（双击选词、跨节点选择）→ 退化成按文本找一次
      var idx = fallback ? flat.text.indexOf(fallback) : -1;
      if (idx < 0) return fallback;
      s = idx;
      e = idx + fallback.length;
    }
    return poiSliceSentence(flat.text, s, e) || fallback;
  } catch (err) {
    return fallback;
  }
}

/* ========== 与后台通信 ========== */

// Firefox 也提供回调式的 chrome.* 别名（browser.* 只认 Promise，回调会被当成
// options 报错），所以统一用 chrome.*：一套代码两边跑。
function poiApi() {
  return typeof chrome !== "undefined" ? chrome : browser;
}

function poiSend(msg) {
  var api = poiApi();
  return new Promise(function (resolve) {
    try {
      api.runtime.sendMessage(msg, function (resp) {
        // 扩展被重载 / SW 挂了：lastError 有值，按"连不上"处理
        var err = api.runtime.lastError;
        if (err) resolve({ ok: false, offline: true, error: String(err.message || err) });
        else resolve(resp || { ok: false, offline: true, error: "无响应" });
      });
    } catch (e) {
      resolve({ ok: false, offline: true, error: String((e && e.message) || e) });
    }
  });
}

function poiCssURL() {
  try {
    return poiApi().runtime.getURL("styles.css");
  } catch (e) {
    return "";
  }
}

/* ========== UI（全在 Shadow DOM 里，页面 CSS 进不来也出不去） ========== */

var POI = {
  flagHost: null,   // ⌖ 浮标
  cardHost: null,   // 查询卡
  flagTimer: 0,
  pending: null,    // {surface, sentence} —— 浮标点下去时要查的东西
  card: null        // {surface, sentence, data}
};

function poiMakeHost(id) {
  var host = document.createElement("div");
  host.setAttribute("data-poi-ui", id);
  host.style.cssText = "all:initial; position:absolute; z-index:2147483646;";
  var shadow = host.attachShadow({ mode: "open" });
  var href = poiCssURL();
  if (href) {
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    shadow.appendChild(link);
  }
  var box = document.createElement("div");
  box.className = "poi-root";
  shadow.appendChild(box);
  (document.body || document.documentElement).appendChild(host);
  host.__box = box;
  return host;
}

function poiEl(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function poiPlace(host, rect, dy) {
  // rect 是视口坐标，页面上要用文档坐标；再夹一下右边界，别顶出屏幕
  var x = rect.left + window.scrollX;
  var y = rect.bottom + window.scrollY + (dy || 0);
  var maxX = window.scrollX + document.documentElement.clientWidth - 24;
  host.style.left = Math.max(window.scrollX + 4, Math.min(x, maxX)) + "px";
  host.style.top = y + "px";
}

function poiHideFlag() {
  if (POI.flagTimer) {
    clearTimeout(POI.flagTimer);
    POI.flagTimer = 0;
  }
  if (POI.flagHost) {
    POI.flagHost.remove();
    POI.flagHost = null;
  }
}

function poiShowFlag(rect, surface, sentence) {
  poiHideFlag();
  POI.pending = { surface: surface, sentence: sentence };
  var host = poiMakeHost("flag");
  var btn = poiEl("button", "poi-flag", "⌖");
  btn.title = "POI // 查这个词";
  // mousedown 不能让浏览器处理：一处理选区就没了（也就没有句子了）
  btn.addEventListener("mousedown", function (ev) { ev.preventDefault(); ev.stopPropagation(); });
  btn.addEventListener("click", function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    var p = POI.pending;
    poiHideFlag();
    if (p) poiOpenCard(p.surface, p.sentence, rect);
  });
  host.__box.appendChild(btn);
  poiPlace(host, rect, 4);
  POI.flagHost = host;
  POI.flagTimer = setTimeout(poiHideFlag, POI_FLAG_TIMEOUT_MS);
}

function poiCloseCard() {
  if (POI.cardHost) {
    POI.cardHost.remove();
    POI.cardHost = null;
  }
  POI.card = null;
}

function poiCardShell(rect) {
  poiCloseCard();
  var host = poiMakeHost("card");
  var card = poiEl("div", "poi-card");
  card.setAttribute("data-state", "loading");
  var top = poiEl("div", "poi-top");
  top.appendChild(poiEl("span", null, "■ POI // LOOKUP"));
  var x = poiEl("span", "poi-x", "[关闭 ESC]");
  x.addEventListener("click", function (ev) { ev.stopPropagation(); poiCloseCard(); });
  top.appendChild(x);
  card.appendChild(top);
  card.appendChild(poiEl("div", "poi-body"));
  host.__box.appendChild(card);
  poiPlace(host, rect, 6);
  POI.cardHost = host;
  return card;
}

function poiRow(label, valueNode) {
  var r = poiEl("div", "poi-row");
  r.appendChild(poiEl("div", "poi-lb", label));
  var v = poiEl("div", "poi-vl");
  v.appendChild(valueNode);
  r.appendChild(v);
  return r;
}

function poiGlossLines(gloss) {
  // 释义里的分隔符是**字面两个字符** "\n"（服务端不折行，见 app/server.py 文件头）
  if (!gloss) return [];
  return String(gloss).split(/\\n|\n/).map(function (s) { return s.trim(); })
    .filter(function (s) { return !!s; });
}

async function poiOpenCard(surface, sentence, rect) {
  var card = poiCardShell(rect);
  var body = card.querySelector(".poi-body");
  body.appendChild(poiEl("div", "poi-dim", "查询中…"));

  var resp = await poiSend({ type: "lookup", surface: surface });
  if (!POI.cardHost || POI.cardHost !== card.getRootNode().host) return; // 卡已被关掉

  body.textContent = "";
  if (!resp.ok) {
    card.setAttribute("data-state", resp.offline ? "offline" : "error");
    body.appendChild(poiEl("div", "poi-surface", poiCollapse(surface).trim()));
    body.appendChild(poiEl("div", "poi-note", resp.offline ? POI_OFFLINE_HINT : "查询失败：" + resp.error));
    return;
  }

  var d = resp.data || {};
  POI.card = { surface: surface, sentence: sentence, data: d };

  body.appendChild(poiRow("当前形式", poiEl("span", "poi-surface", d.surface || surface)));
  body.appendChild(poiRow("词元", poiEl("span", "poi-lemma", (d.lemma || "—") + (d.pos ? "  [" + d.pos + "]" : ""))));
  body.appendChild(poiRow("音标", poiEl("span", "poi-ipa", d.ipa ? "/" + d.ipa + "/" : "—")));

  var gl = poiEl("div", "poi-gloss");
  var lines = poiGlossLines(d.dict_gloss);
  if (lines.length) {
    lines.forEach(function (t) { gl.appendChild(poiEl("div", null, t)); });
  } else {
    gl.appendChild(poiEl("div", "poi-dim", "—"));
  }
  body.appendChild(poiRow("释义", gl));

  if (!d.in_dict) {
    body.appendChild(poiEl("div", "poi-note", "词典未收录（专名？）— 词元按 " + (d.lemma || surface) + " 记录，仍可收藏"));
  }

  if (sentence) body.appendChild(poiEl("div", "poi-sent", sentence));

  var foot = poiEl("div", "poi-foot");
  var btn = poiEl("button", "poi-btn", "收入生词本");
  btn.addEventListener("click", function (ev) { ev.stopPropagation(); poiCollect(card); });
  foot.appendChild(btn);
  var msg = poiEl("span", "poi-msg", "");
  foot.appendChild(msg);
  body.appendChild(foot);

  if (d.collected) poiMarkCollected(card, d.encounters || 0, false);
  card.setAttribute("data-state", "done");
}

/**
 * 画"已收"状态。locked=true 表示这次是本卡片刚点的收藏 —— 按钮锁掉，
 * 免得手抖连点给同一句话记两次相遇。
 *
 * 开卡时就已收藏的词**不锁**：换了页面、换了句子再遇到同一个词，那是一次
 * 新的相遇，本来就该记下来（相遇模型的意义就在这儿）。
 */
function poiMarkCollected(card, n, locked) {
  var btn = card.querySelector(".poi-btn");
  var msg = card.querySelector(".poi-msg");
  if (btn) {
    btn.textContent = locked ? "✓ 已收" : "再记一次相遇";
    btn.disabled = !!locked;
    if (locked) btn.classList.add("done");
  }
  if (msg) msg.textContent = n ? "✓ 已收 · " + n + " 次相遇" : "✓ 已收";
  card.setAttribute("data-collected", "1");
}

async function poiCollect(card) {
  if (!POI.card) return;
  var btn = card.querySelector(".poi-btn");
  var msg = card.querySelector(".poi-msg");
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = "写入中…";

  var resp = await poiSend({
    type: "collect",
    surface: POI.card.surface,
    sentence: POI.card.sentence || null,
    url: location.href,
    title: document.title || ""
  });
  if (!POI.cardHost) return;

  if (!resp.ok) {
    if (btn) btn.disabled = false;
    if (msg) msg.textContent = "";
    var note = poiEl("div", "poi-note", resp.offline ? POI_OFFLINE_HINT : "收藏失败：" + resp.error);
    card.querySelector(".poi-body").appendChild(note);
    return;
  }
  poiMarkCollected(card, (resp.data && resp.data.encounters) || 1, true);
}

/* ========== 事件接线（只点击触发，永不自动弹卡） ========== */

function poiCurrentSelection() {
  var sel = window.getSelection && window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  var text = sel.toString();
  if (!poiLooksEnglish(text)) return null;
  var range = sel.getRangeAt(0);
  var rect = range.getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) return null;
  return {
    surface: poiCollapse(text).trim(),
    sentence: poiSentenceForRange(range, text),
    rect: { left: rect.left, bottom: rect.bottom, top: rect.top, right: rect.right }
  };
}

function poiInsideUI(node) {
  while (node) {
    if (node.nodeType === 1 && node.hasAttribute && node.hasAttribute("data-poi-ui")) return true;
    node = node.parentNode || node.host; // 穿过 shadow 边界
  }
  return false;
}

function poiOnSelectionDone(ev) {
  if (ev && poiInsideUI(ev.target)) return;
  // 选区在 mouseup 之后才定稿，等一拍再读
  setTimeout(function () {
    var s = poiCurrentSelection();
    if (!s) {
      poiHideFlag();
      return;
    }
    poiShowFlag(s.rect, s.surface, s.sentence);
  }, 0);
}

function poiInit() {
  document.addEventListener("mouseup", poiOnSelectionDone, true);
  document.addEventListener("keyup", function (ev) {
    if (ev.key === "Shift" || (ev.key && ev.key.indexOf("Arrow") === 0)) poiOnSelectionDone(ev);
  }, true);

  document.addEventListener("mousedown", function (ev) {
    if (poiInsideUI(ev.target)) return;
    poiHideFlag();
    poiCloseCard(); // 点卡外关卡
  }, true);

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      poiHideFlag();
      poiCloseCard();
    }
  }, true);

  // 页面滚动/换宽度时浮标会飘在错位置，直接收掉（卡是钉住的，用户自己关）
  window.addEventListener("scroll", poiHideFlag, true);
  window.addEventListener("resize", poiHideFlag, true);
}

if (typeof document !== "undefined" && typeof window !== "undefined") poiInit();

/* node 单测入口（tests/test_extension_sentence.py）：
   浏览器里 module 不存在，这段不会执行；node 里 document 不存在，poiInit 不会执行。 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    sliceSentence: poiSliceSentence,
    isSentenceEnd: poiIsSentenceEnd,
    looksEnglish: poiLooksEnglish,
    collapse: poiCollapse,
    MAX_SENTENCE_CHARS: POI_MAX_SENTENCE_CHARS
  };
}
