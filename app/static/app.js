"use strict";

// ---------- 校验行的动态编辑 ----------
const checksBox = document.getElementById("checks");

function addCheckRow(membersText = "", parity = "0") {
  const row = document.createElement("div");
  row.className = "check-row";
  const idx = checksBox.children.length;
  row.innerHTML = `
    <span class="idx">#<span class="n">${idx + 1}</span></span>
    <input class="members" type="text" placeholder="引用通道，逗号/空格分隔" value="">
    <input class="parity" type="text" maxlength="1" value="0" title="观测奇偶值 0/1">
    <button type="button" class="del" title="删除此校验">✕</button>`;
  row.querySelector(".members").value = membersText;
  row.querySelector(".parity").value = String(parity);
  row.querySelector(".del").addEventListener("click", () => {
    row.remove();
    renumber();
  });
  checksBox.appendChild(row);
}

function renumber() {
  [...checksBox.children].forEach((r, i) => {
    r.querySelector(".n").textContent = i + 1;
  });
}

function splitTokens(text) {
  return text.split(/[\s,，;；]+/).map((t) => t.trim()).filter(Boolean);
}

function collectPayload() {
  const channels = splitTokens(document.getElementById("channels").value);
  const checks = [];
  for (const row of checksBox.querySelectorAll(".check-row")) {
    const members = splitTokens(row.querySelector(".members").value);
    // 未引用任何通道的行视为未添加的空白行，直接跳过。
    if (members.length === 0) continue;
    const rawParity = row.querySelector(".parity").value.trim();
    // 0/1 以数字提交；其它内容原样发送，由接口给出可定位的拒绝信息。
    const parity = rawParity === "0" || rawParity === "1" ? Number(rawParity) : rawParity;
    checks.push({ channels: members, parity });
  }
  return { channels, checks };
}

// ---------- 证据区与错误区 ----------
const resultBox = document.getElementById("result");
const errorBox = document.getElementById("errors");
const errorList = document.getElementById("errorList");
const sealPanel = document.getElementById("sealPanel");
const sealLedger = document.getElementById("sealLedger");
const sealExisting = document.getElementById("sealExisting");
const sealResult = document.getElementById("sealResult");
const sealIdInput = document.getElementById("sealId");
let currentReviewId = null;

function clearEvidence() {
  // 任何新的提交/取回尝试前，清除上一次的结论、封存与告警，
  // 但表单编辑内容原样保留。
  resultBox.innerHTML = "";
  errorBox.classList.add("hidden");
  errorList.innerHTML = "";
  sealPanel.classList.add("hidden");
  sealLedger.innerHTML = "";
  sealExisting.innerHTML = "";
  sealResult.innerHTML = "";
  currentReviewId = null;
}

function showErrors(payload) {
  clearEvidence();
  errorBox.classList.remove("hidden");
  const list = (payload && payload.errors) || [
    { field: "body", message: (payload && payload.error) || "请求被拒绝" },
  ];
  for (const e of list) {
    const div = document.createElement("div");
    div.className = "err";
    div.innerHTML = `<span class="field"></span> <span class="msg"></span>`;
    div.querySelector(".field").textContent = `[${e.field}]`;
    div.querySelector(".msg").textContent = e.message;
    errorList.appendChild(div);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- 结论渲染 ----------
function renderConclusion(data) {
  const c = data.conclusion;
  const inp = data.input || {};
  let html = "";

  html += `<p style="margin-top:14px">复核编号：<span class="review-id">${esc(data.review_id)}</span>
           <span class="meta">（提交时间 ${esc(data.created_at || "")}；刷新后可凭编号取回）</span></p>`;

  if (c.feasible) {
    const chips = c.faulty.map((f) => `<span class="chip">${esc(f)}</span>`).join("");
    html += `
      <p class="verdict-ok" style="font-size:15px">
        ✓ 可行最优解：最小汉明重量 <strong>${c.weight}</strong>，
        故障通道 ${c.faulty.length ? chips : "（无，全板零故障即满足全部校验）"}
      </p>
      <table>
        <thead><tr><th>通道（升序）</th><th>选择向量 x</th></tr></thead><tbody>`;
    for (const [ch, bit] of Object.entries(c.vector)) {
      html += `<tr><td>${esc(ch)}</td><td>${bit}</td></tr>`;
    }
    html += `</tbody></table>
      <p class="meta">折半：左半 ${c.left_size} 个通道，左半综合征索引 ${c.left_index_size} 条；
           按总重量、再按选择向量字典序精确裁决。</p>
      <h2 style="font-size:14px;color:var(--accent);margin-top:18px">逐校验复算</h2>
      <table>
        <thead><tr><th>#</th><th>引用通道</th><th>观测奇偶</th><th>复算 XOR</th><th>结果</th></tr></thead><tbody>`;
    c.recompute.forEach((r, i) => {
      html += `<tr>
        <td>${i + 1}</td>
        <td>${r.members.map((m) => esc(m)).join(" ⊕ ")}</td>
        <td>${r.observed}</td>
        <td>${r.recomputed}</td>
        <td><span class="badge ${r.pass ? "pass" : "fail"}">${r.pass ? "一致" : "不一致"}</span></td>
      </tr>`;
    });
    html += "</tbody></table>";
  } else {
    html += `
      <p class="verdict-bad" style="font-size:15px">
        ✗ ${esc(c.message)}
      </p>
      <p class="meta">已保存不可行结论（复核编号见上）。系统未返回任何近似或部分满足的通道集合。
        折半：左半 ${c.left_size} 个通道，左半综合征索引 ${c.left_index_size} 条，
        两侧候选精确合并后无匹配。</p>`;
  }

  resultBox.innerHTML = html;
  renderSealArea(data);
}

// ---------- 证据封存 ----------
function renderSealArea(data) {
  currentReviewId = data.review_id;
  if (data.ledger) {
    sealLedger.innerHTML =
      `<p class="meta">账本序号 <strong>#${data.ledger.seq}</strong>` +
      `（服务端递增、只追加）｜ 叶摘要（排序后输入、观测与结论的规范字节摘要）</p>` +
      `<p class="meta"><code>${esc(data.ledger.leaf)}</code></p>`;
  } else {
    sealLedger.innerHTML = `<p class="meta">该记录尚无账本叶。</p>`;
  }
  renderExistingSeals(data.seals || []);
  sealPanel.classList.remove("hidden");
}

function renderExistingSeals(seals) {
  if (!seals.length) {
    sealExisting.innerHTML = "";
    return;
  }
  let html = `<p class="meta">本复核已锚定的封存（批次 = 该复核提交瞬间的连续账本前缀）：</p>`;
  for (const s of seals) {
    html += `<div class="meta">标识 <span class="chip">${esc(s.seal_id)}</span>` +
      ` 批次大小 ${s.size} ｜ 根 <code>${esc(s.root)}</code>` +
      ` <button type="button" class="secondary viewSeal" data-seal="${esc(s.seal_id)}"` +
      ` style="margin:2px 0 2px 8px;padding:3px 10px">查看包含路径</button></div>`;
  }
  sealExisting.innerHTML = html;
  sealExisting.querySelectorAll(".viewSeal").forEach((b) =>
    b.addEventListener("click", () => loadSeal(b.dataset.seal)));
}

async function loadSeal(id) {
  sealResult.innerHTML = "";
  const resp = await fetch("/api/seal/" + encodeURIComponent(id));
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    sealResult.innerHTML = `<p class="verdict-bad">✗ 封存取回失败</p>`;
    return;
  }
  renderSealProof(data);
}

document.getElementById("sealBtn").addEventListener("click", async () => {
  if (!currentReviewId) return;
  sealResult.innerHTML = "";
  let resp;
  try {
    resp = await fetch("/api/seal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        seal_id: sealIdInput.value.trim(),
        review_id: currentReviewId,
      }),
    });
  } catch (err) {
    sealResult.innerHTML = `<p class="verdict-bad">✗ 请求失败: ${esc(err)}</p>`;
    return;
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    sealResult.innerHTML =
      `<p class="verdict-bad">✗ ${esc((data && data.error) || "封存被拒绝")}</p>`;
    return;
  }
  renderSealProof(data);
  // 刷新“已锚定封存”列表（新建或幂等重传后保持一致）。
  const r = await fetch("/api/review/" + encodeURIComponent(currentReviewId));
  const rd = await r.json().catch(() => null);
  if (r.ok && rd) renderExistingSeals(rd.seals || []);
});

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

async function foldPath(leafHex, steps) {
  // 与服务端一致的折叠：节点 = SHA-256(0x01 || 左 || 右)。
  let acc = hexToBytes(leafHex);
  for (const st of steps) {
    const sib = hexToBytes(st.hash);
    const buf = new Uint8Array(65);
    buf[0] = 1;
    if (st.side === "left") {
      buf.set(sib, 1);
      buf.set(acc, 33);
    } else {
      buf.set(acc, 1);
      buf.set(sib, 33);
    }
    acc = new Uint8Array(await crypto.subtle.digest("SHA-256", buf));
  }
  return Array.from(acc).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function renderSealProof(seal) {
  const steps = seal.path ? seal.path.steps : [];
  let html =
    `<p class="verdict-ok" style="font-size:15px">✓ 封存 <strong>${esc(seal.seal_id)}</strong>` +
    `：批次大小 ${seal.size}，本条复核序号 #${seal.seq}` +
    `${seal.created === false ? "（同一标识同一载荷，返回原封存）" : ""}</p>` +
    `<p class="meta">根摘要</p><p style="margin:2px 0"><code>${esc(seal.root)}</code></p>` +
    `<p class="meta">本条复核的包含路径（自叶向根 ${steps.length} 步）：</p>`;
  if (seal.path) {
    html += `<table><thead><tr><th>#</th><th>兄弟方位</th><th>兄弟子树摘要</th></tr></thead><tbody>`;
    steps.forEach((st, i) => {
      html += `<tr><td>${i + 1}</td>` +
        `<td>${st.side === "left" ? "左" : "右"}</td>` +
        `<td><code>${esc(st.hash)}</code></td></tr>`;
    });
    html += `</tbody></table>`;
  } else {
    html += `<p class="meta">（账本完整性异常，路径暂不可复算）</p>`;
  }
  html += `<div id="sealVerify" class="meta"></div>`;
  sealResult.innerHTML = html;
  // 页面内复算：叶摘要沿包含路径折叠应等于根摘要。
  const box = document.getElementById("sealVerify");
  if (seal.path && window.crypto && crypto.subtle) {
    try {
      const got = await foldPath(seal.leaf, steps);
      box.innerHTML = got === seal.root
        ? `<span class="verdict-ok">页面复算 ✓ 路径折叠结果与根摘要一致</span>`
        : `<span class="verdict-bad">页面复算 ✗ 与根摘要不一致</span>`;
    } catch (e) {
      box.textContent = "页面内复算不可用，可凭路径离线核对。";
    }
  } else if (seal.path) {
    box.textContent = "当前环境不支持页面内复算，可凭路径离线核对。";
  }
}

// ---------- 提交 ----------
document.getElementById("addCheck").addEventListener("click", () => addCheckRow());
document.getElementById("submitBtn").addEventListener("click", async () => {
  clearEvidence();
  let payload;
  try {
    payload = collectPayload();
  } catch (err) {
    showErrors({ errors: [{ field: "form", message: String(err) }] });
    return;
  }
  let resp;
  try {
    resp = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    showErrors({ errors: [{ field: "network", message: `请求失败: ${err}` }] });
    return;
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    showErrors(data);
    return;
  }
  // 合法提交（含不可行结论）：写入编号到地址栏，刷新后可直接取回。
  history.replaceState(null, "", "#" + data.review_id);
  document.getElementById("reviewId").value = data.review_id;
  renderConclusion(data);
});

// ---------- 按编号取回 ----------
async function loadReview(id) {
  clearEvidence();
  document.getElementById("reviewId").value = id;
  const resp = await fetch("/api/review/" + encodeURIComponent(id));
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    showErrors(data || { errors: [{ field: "review_id", message: "取回失败" }] });
    return;
  }
  renderConclusion(data);
}

document.getElementById("loadBtn").addEventListener("click", () => {
  const id = document.getElementById("reviewId").value.trim();
  if (id) loadReview(id);
});
document.getElementById("reviewId").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const id = e.target.value.trim();
    if (id) loadReview(id);
  }
});

// ---------- 示例 ----------
document.getElementById("sampleBtn").addEventListener("click", () => {
  document.getElementById("channels").value = "CH0 CH1 CH2 CH3 CH4 CH5";
  checksBox.innerHTML = "";
  // 唯一故障解：仅 CH3 失效。
  addCheckRow("CH0 CH1 CH3", "1");
  addCheckRow("CH2 CH3 CH4", "1");
  addCheckRow("CH3 CH5", "1");
  addCheckRow("CH0 CH2 CH4", "0");
});

// ---------- 初始化：至少 3 条空校验行；带编号哈希时刷新即取回 ----------
for (let i = 0; i < 3; i++) addCheckRow();
const hashId = decodeURIComponent(location.hash || "").replace(/^#/, "");
if (hashId) loadReview(hashId);
