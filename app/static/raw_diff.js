/* 两事件原始载荷结构差异对比抽屉 —— 复用 app.js 的 state / el / esc / toast /
   focusEvent 与 compare.js 的抽屉样式。
   只读：调用 POST /api/events/raw-diff，不修改任何记录；
   页面填两侧事件 ID、查看差异并下载结果 JSON。 */
"use strict";

const rdState = {
  result: null,           // 最近一次比较响应（下载 JSON 用）
};
const rd$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function rdOpen() {
  rd$("#rawdiff-overlay").classList.remove("hidden");
  rdFillEventList();
  rdUpdateRunEnabled();
}
function rdClose() {
  rd$("#rawdiff-overlay").classList.add("hidden");
}

function rdFillEventList() {
  // 复用主时间线已加载的事件做候选；首屏尚未加载时留空，仍可手输 ID
  const events = (state.result && state.result.events) || [];
  const box = rd$("#rawdiff-events");
  box.innerHTML = [...events]
    .sort((x, y) => x.id < y.id ? -1 : x.id > y.id ? 1 : 0)
    .map((e) => `<option value="${esc(e.id)}">`
      + `${esc(e.event_type)} · ${esc(e.source_id)}</option>`).join("");
}

function rdUpdateRunEnabled() {
  const l = rd$("#rawdiff-left").value.trim();
  const r = rd$("#rawdiff-right").value.trim();
  rd$("#rawdiff-run").disabled = !(l && r);
}

/* --------------------------------------------------------------- 比较 */

async function rdRun() {
  const left = rd$("#rawdiff-left").value.trim();
  const right = rd$("#rawdiff-right").value.trim();
  const hint = rd$("#rawdiff-hint");
  const box = rd$("#rawdiff-result");
  if (!left || !right) { toast("请填写两侧事件 ID", "error"); return; }

  hint.textContent = "比较中…";
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "正在读取库内 raw 并比较…"));

  const res = await fetch("/api/events/raw-diff", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ left, right }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    rdState.result = null;
    rd$("#rawdiff-export").disabled = true;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "比较被拒绝: " + esc(msg)));
    toast("比较被拒绝: " + msg, "error");
    return;
  }
  rdState.result = await res.json();
  rd$("#rawdiff-export").disabled = false;
  rdRender(rdState.result);
}

/* ------------------------------------------------------------ 结果渲染 */

const RD_OP_TEXT = {
  added: ["added ＋", "仅右侧新增"],
  removed: ["removed －", "仅左侧删除"],
  replaced: ["replaced ≈", "替换（值或类型不同）"],
};

function rdRender(d) {
  const box = rd$("#rawdiff-result");
  box.innerHTML = "";
  box.appendChild(rdSummary(d));

  const items = d.differences || [];
  const list = el("div", "cmp-block");
  list.innerHTML = `<h3>差异明细 <span class="cmp-count">${items.length} 项
    （路径采用 JSON Pointer，根为空串；缺失侧省略，不以 null 代替缺失）</span></h3>`;
  if (!items.length) {
    list.appendChild(el("div", "cmp-empty",
      "✅ 两条事件的 raw 载荷完全相同（结构与值均无差异）。"));
    box.appendChild(list);
    return;
  }
  for (const it of items) list.appendChild(rdItem(it));
  box.appendChild(list);
}

function rdSummary(d) {
  const c = d.op_counts;
  const blk = el("div", "cmp-block cmp-summary");
  blk.innerHTML = `
    <div class="cmp-cols">
      <div class="cmp-col-head a">左 · ${esc(d.left.event_id)}</div>
      <div class="cmp-col-head mid">差异</div>
      <div class="cmp-col-head b">右 · ${esc(d.right.event_id)}</div>
    </div>
    <div class="cmp-sum-row">
      <div class="cmp-col a">raw 原始载荷</div>
      <div class="cmp-col mid">
        <span class="cmp-delta added" title="仅右侧出现">＋新增 ${c.added}</span>
        <span class="cmp-delta removed" title="仅左侧出现">－删除 ${c.removed}</span>
        <span class="cmp-delta persisted" title="值或类型不同">≈替换 ${c.replaced}</span>
      </div>
      <div class="cmp-col b">raw 原始载荷</div>
    </div>
    <div class="cmp-sum-row">
      <div class="cmp-col mid rd-sum-verdict">${d.identical
        ? "✅ 相同记录，差异为空"
        : `共 <b>${c.added + c.removed + c.replaced}</b> 项差异（只读比较，记录未被修改）`}
      </div>
    </div>`;
  return blk;
}

function rdItem(it) {
  const [opLabel, opTitle] = RD_OP_TEXT[it.op] || [it.op, ""];
  const row = el("div", `rd-item rd-${it.op}`);
  const hasLeft = Object.prototype.hasOwnProperty.call(it, "left");
  const hasRight = Object.prototype.hasOwnProperty.call(it, "right");
  const pathText = it.path === "" ? "（根）" : it.path;
  row.innerHTML = `
    <div class="rd-item-head">
      <span class="rd-op" title="${esc(opTitle)}">${esc(opLabel)}</span>
      <code class="rd-path">${esc(pathText)}</code>
    </div>
    <div class="rd-sides">
      <div class="rd-half a ${hasLeft ? "" : "missing"}">
        <span class="rd-side-tag">左</span>
        ${hasLeft ? `<pre class="rd-val">${esc(rdPreview(it.left))}</pre>`
                  : `<span class="rd-missing">（缺失，已省略）</span>`}
      </div>
      <div class="rd-half b ${hasRight ? "" : "missing"}">
        <span class="rd-side-tag">右</span>
        ${hasRight ? `<pre class="rd-val">${esc(rdPreview(it.right))}</pre>`
                   : `<span class="rd-missing">（缺失，已省略）</span>`}
      </div>
    </div>`;
  return row;
}

function rdPreview(v) {
  // 大子树截断展示，完整值以导出的 JSON 为准
  let text;
  try {
    text = JSON.stringify(v, null, 2);
  } catch {
    text = String(v);
  }
  const LIMIT = 2000;
  return text.length > LIMIT
    ? text.slice(0, LIMIT) + `\n…（已截断，完整值见下载的 JSON）`
    : text;
}

/* --------------------------------------------------------------- 导出 */

async function rdExport() {
  if (!rdState.result) return toast("请先执行一次比较", "error");
  const blob = new Blob([JSON.stringify(rdState.result, null, 2)],
                        { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const l = rdState.result.left.event_id;
  const r = rdState.result.right.event_id;
  a.href = url;
  a.download = `raw_diff_${l}__vs__${r}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  toast("比较结果 JSON 已下载", "ok");
}

/* --------------------------------------------------------------- 事件 */

rd$("#btn-raw-diff").addEventListener("click", rdOpen);
rd$("#rawdiff-close").addEventListener("click", rdClose);
rd$("#rawdiff-overlay").addEventListener("click", (e) => {
  if (e.target.id === "rawdiff-overlay") rdClose();
});
rd$("#rawdiff-run").addEventListener("click", rdRun);
rd$("#rawdiff-export").addEventListener("click", rdExport);
rd$("#rawdiff-swap").addEventListener("click", () => {
  const l = rd$("#rawdiff-left"), r = rd$("#rawdiff-right");
  const t = l.value; l.value = r.value; r.value = t;
  rdUpdateRunEnabled();
});
for (const id of ["#rawdiff-left", "#rawdiff-right"]) {
  rd$(id).addEventListener("input", rdUpdateRunEnabled);
  rd$(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") rdRun();
  });
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !rd$("#rawdiff-overlay").classList.contains("hidden")) {
    rdClose();
  }
});
