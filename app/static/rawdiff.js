/* 两事件原始载荷（raw）结构差异比对抽屉 —— 复用 app.js 的
   state / el / esc / toast / focusEvent / shortId。
   纯只读：仅调用 /api/raw-diff，不导入、不改写任何记录。 */
"use strict";

const rdiffState = {
  events: [],          // 全量事件（ID 输入建议，不受页面筛选影响）
  result: null,        // 最近一次比对结果
};
const rd$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

async function rdiffOpen() {
  rd$("#rawdiff-overlay").classList.remove("hidden");
  rd$("#rawdiff-result").innerHTML = "";
  rd$("#rawdiff-result").appendChild(el("div", "cmp-empty", "载入事件列表…"));
  rd$("#rawdiff-export").disabled = true;
  try {
    const res = await fetch("/api/recompute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides: state.overrides || {} }),
    });
    if (!res.ok) throw new Error(await res.text());
    rdiffState.events = (await res.json()).events || [];
  } catch (e) {
    rdiffState.events = [];
  }
  rdiffFillDatalist();
  if (!rdiffState.events.length) {
    rd$("#rawdiff-result").innerHTML = "";
    rd$("#rawdiff-result").appendChild(
      el("div", "cmp-empty", "暂无可比对事件，请先导入 JSON。"));
  } else if (!rdiffState.result) {
    rd$("#rawdiff-result").innerHTML = "";
    rd$("#rawdiff-result").appendChild(
      el("div", "cmp-empty", "在上方输入左、右事件 ID 后点 ▶ 开始比对。"));
  }
  setTimeout(() => rd$("#rawdiff-left").focus(), 0);
}

function rdiffClose() {
  rd$("#rawdiff-overlay").classList.add("hidden");
}

function rdiffFillDatalist() {
  rd$("#rawdiff-events").innerHTML = rdiffState.events.map((e) => {
    const label = `${e.id} · ${e.source_name || e.source_id} · ${e.summary || e.event_type || ""}`;
    return `<option value="${esc(e.id)}">${esc(label)}</option>`;
  }).join("");
}

/* --------------------------------------------------------------- 比对 */

async function rdiffRun() {
  const left_id = rd$("#rawdiff-left").value.trim();
  const right_id = rd$("#rawdiff-right").value.trim();
  const hint = rd$("#rawdiff-hint");
  const box = rd$("#rawdiff-result");

  if (!left_id || !right_id) {
    toast("请填写左、右两侧事件 ID", "error");
    return;
  }

  hint.textContent = "比对中…";
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "正在对库内 raw 做递归结构比较…"));

  const res = await fetch("/api/raw-diff", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ left_id, right_id }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    rdiffState.result = null;
    rd$("#rawdiff-export").disabled = true;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "比对被拒绝：" + esc(msg)));
    toast("比对失败：" + msg, "error");
    return;
  }
  rdiffState.result = await res.json();
  rd$("#rawdiff-export").disabled = false;
  rdiffRender();
}

function rdiffSwap() {
  const l = rd$("#rawdiff-left"), r = rd$("#rawdiff-right");
  const t = l.value; l.value = r.value; r.value = t;
}

function rdiffExport() {
  const d = rdiffState.result;
  if (!d) return;
  const q = `?left_id=${encodeURIComponent(d.left.id)}`
    + `&right_id=${encodeURIComponent(d.right.id)}`;
  const a = document.createElement("a");
  a.href = "/api/raw-diff/export" + q;
  a.download = `raw_diff_${d.left.id}__${d.right.id}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  toast("已下载比对结果 JSON", "ok");
}

/* --------------------------------------------------------------- 渲染 */

const RDIFF_OP_META = {
  added: { cls: "add", icon: "＋", label: "新增" },
  removed: { cls: "del", icon: "－", label: "删除" },
  replaced: { cls: "rep", icon: "≠", label: "替换" },
};

function rdiffPreview(v, present) {
  if (!present) return '<span class="rdiff-missing" title="该侧路径不存在（缺失，值被省略而非 null）">— 缺失 —</span>';
  let text;
  try {
    text = JSON.stringify(v, null, 1);
  } catch (e) {
    text = String(v);
  }
  return `<pre class="rdiff-value">${esc(text)}</pre>`;
}

function rdiffRender() {
  const d = rdiffState.result;
  const box = rd$("#rawdiff-result");
  box.innerHTML = "";
  if (!d) return;

  box.appendChild(rdiffHeaderBlock(d));
  if (d.identical) {
    box.appendChild(el("div", "cmp-block",
      `<h3>✓ 两条记录的 raw 完全相同</h3>
       <div class="cmp-count">深度相等，差异列表为空（0 项）。</div>`));
    return;
  }

  const blk = el("div", "cmp-block");
  const c = d.counts;
  blk.innerHTML = `
    <h3>差异明细
      <span class="cmp-count">${c.total} 项：
        <span class="rdiff-badge add">＋新增 ${c.added}</span>
        <span class="rdiff-badge del">－删除 ${c.removed}</span>
        <span class="rdiff-badge rep">≠替换 ${c.replaced}</span>
        （顺序：递归遍历，对象按键名排序，数组按下标）
      </span>
    </h3>
    <div class="rdiff-colhead">
      <span>操作</span><span>JSON Pointer 路径</span>
      <span class="rdiff-side-a">左侧值（${esc(shortId(d.left.id))}）</span>
      <span class="rdiff-side-b">右侧值（${esc(shortId(d.right.id))}）</span>
    </div>`;

  const list = el("div", "rdiff-list");
  for (const it of d.items) {
    const meta = RDIFF_OP_META[it.op] || RDIFF_OP_META.replaced;
    const hasL = Object.prototype.hasOwnProperty.call(it, "left");
    const hasR = Object.prototype.hasOwnProperty.call(it, "right");
    const typeTag = (it.op === "replaced" && it.type_changed)
      ? '<span class="rdiff-type-tag" title="两侧 JSON 类型不同（布尔不与数值相等）">类型</span>' : "";
    const pathText = it.path === ""
      ? '<span class="rdiff-root" title="根路径为空字符串">（根 ""）</span>'
      : esc(it.path);
    const row = el("div", `rdiff-item ${meta.cls}`);
    row.innerHTML = `
      <span class="rdiff-op ${meta.cls}">${meta.icon} ${meta.label}${typeTag}</span>
      <span class="rdiff-path" title="${esc(it.path)}">${pathText}</span>
      <span class="rdiff-cell">${rdiffPreview(hasL ? it.left : undefined, hasL)}</span>
      <span class="rdiff-cell">${rdiffPreview(hasR ? it.right : undefined, hasR)}</span>`;
    list.appendChild(row);
  }
  blk.appendChild(list);
  box.appendChild(blk);
}

function rdiffHeaderBlock(d) {
  const side = (s, cls) => `
    <span class="rdiff-side-head ${cls}">
      <span class="chain-node rdiff-jump" data-eid="${esc(s.id)}"
            title="点击在主时间线定位">${esc(shortId(s.id))}</span>
      <span class="rdiff-side-meta">${esc(s.source_id)} · ${esc(s.event_type)}</span>
    </span>`;
  const blk = el("div", "cmp-block rdiff-head");
  blk.innerHTML = `
    <h3>比对对象
      <span class="cmp-count">只读比较库内 raw 原始载荷，不修改任何记录</span>
    </h3>
    <div class="rdiff-sides">
      ${side(d.left, "a")}
      <span class="rdiff-vs">⇄</span>
      ${side(d.right, "b")}
    </div>`;
  blk.querySelectorAll(".rdiff-jump").forEach((n) =>
    n.addEventListener("click", () => { rdiffClose(); focusEvent(n.dataset.eid); }));
  return blk;
}

/* --------------------------------------------------------------- 绑定 */

rd$("#btn-rawdiff").addEventListener("click", rdiffOpen);
rd$("#rawdiff-close").addEventListener("click", rdiffClose);
rd$("#rawdiff-run").addEventListener("click", rdiffRun);
rd$("#rawdiff-swap").addEventListener("click", rdiffSwap);
rd$("#rawdiff-export").addEventListener("click", rdiffExport);
rd$("#rawdiff-overlay").addEventListener("click", (e) => {
  if (e.target.id === "rawdiff-overlay") rdiffClose();
});
for (const inp of ["#rawdiff-left", "#rawdiff-right"]) {
  rd$(inp).addEventListener("keydown", (e) => {
    if (e.key === "Enter") rdiffRun();
  });
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !rd$("#rawdiff-overlay").classList.contains("hidden"))
    rdiffClose();
});
