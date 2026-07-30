const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

let overview = null;
// 每张卡片的草稿状态：{ jobId: { types: Set, severity, note } }
const drafts = {};

async function loadOverview() {
  const response = await fetch("/api/review/overview");
  if (!response.ok) {
    alert("加载失败，请确认服务正常运行");
    return;
  }
  overview = await response.json();
  render();
}

function typeLabel(id) {
  const found = (overview.defect_types || []).find((item) => item.id === id);
  return found ? found.label : id;
}

function render() {
  $("anno-total").textContent = `已累积批注 ${overview.annotation_total} 条`;
  $("default-prompt").textContent = overview.default_prompt || "";
  renderDefectStats();
  renderConstraints();
  renderCards();
}

function renderDefectStats() {
  const counts = {};
  (overview.jobs || []).forEach((job) => (job.annotations || []).forEach((anno) => {
    (anno.defect_types || []).forEach((id) => { counts[id] = (counts[id] || 0) + 1; });
  }));
  $("defect-stats").innerHTML = (overview.defect_types || []).map((item) =>
    `<div class="defect-stat"><span>${escapeHtml(item.label)}</span><b>${counts[item.id] || 0}</b></div>`
  ).join("");
}

function renderConstraints() {
  $("template-constraints").innerHTML = Object.entries(overview.template_constraints || {})
    .filter(([, text]) => text)
    .map(([id, text]) => `
      <details>
        <summary>${escapeHtml(id)}</summary>
        <pre class="prompt-pre">${escapeHtml(text)}</pre>
      </details>`).join("") || '<small style="color:var(--muted)">无</small>';
}

function draftFor(jobId) {
  if (!drafts[jobId]) drafts[jobId] = { types: new Set(), severity: "medium", note: "" };
  return drafts[jobId];
}

function renderCards() {
  const jobs = overview.jobs || [];
  $("job-count").textContent = `共 ${jobs.length} 张`;
  $("empty").hidden = jobs.length > 0;
  $("cards").innerHTML = jobs.map((job) => {
    const draft = draftFor(job.job_id);
    const annos = (job.annotations || []).map((anno) => `
      <div class="anno-item sev-${escapeHtml(anno.severity || "medium")}">
        <div class="anno-head">
          ${(anno.defect_types || []).map((id) => `<span class="tag">${escapeHtml(typeLabel(id))}</span>`).join("")}
          <span>${escapeHtml((anno.created_at || "").slice(0, 16).replace("T", " "))}</span>
          <button class="del" data-del="${escapeHtml(anno.id)}">删除</button>
        </div>
        ${anno.note ? `<div class="anno-note">${escapeHtml(anno.note)}</div>` : ""}
      </div>`).join("");
    const checks = (overview.defect_types || []).map((item) => `
      <label class="chk ${draft.types.has(item.id) ? "on" : ""}">
        <input type="checkbox" data-job="${escapeHtml(job.job_id)}" data-type="${escapeHtml(item.id)}" ${draft.types.has(item.id) ? "checked" : ""}>
        ${escapeHtml(item.label)}
      </label>`).join("");
    return `
    <article class="review-card" id="card-${escapeHtml(job.job_id)}">
      <div class="imgbox">
        <a href="${escapeHtml(job.master_url)}" target="_blank" title="新窗口查看原尺寸">
          <img class="master" src="${escapeHtml(job.master_url)}" loading="lazy" alt="master">
        </a>
        ${job.source_url ? `<div class="src-row"><img src="${escapeHtml(job.source_url)}" loading="lazy" alt="source"><span>电商原图</span></div>` : ""}
      </div>
      <div>
        <div class="review-meta">
          <span class="chip">${escapeHtml((job.created_at || "").slice(0, 16).replace("T", " "))}</span>
          <span class="chip">${escapeHtml(job.window_template_label || job.window_template)}</span>
          <span class="chip hash" title="Prompt 版本指纹">prompt ${escapeHtml(job.prompt_hash || "?")}</span>
          <span>${escapeHtml(job.source_name || "")}</span>
        </div>
        <details>
          <summary>查看此图使用的 Prompt</summary>
          <pre class="prompt-pre">${escapeHtml(job.prompt || "（无）")}</pre>
        </details>
        <div class="anno-list">${annos || ""}</div>
        <div class="anno-form">
          <div class="chk-row">${checks}</div>
          <div class="sev-row">
            <span>严重度</span>
            ${["low", "medium", "high"].map((sev) => `
              <button data-job="${escapeHtml(job.job_id)}" data-sev="${sev}" class="${draft.severity === sev ? "on" : ""}">${{ low: "低", medium: "中", high: "高" }[sev]}</button>`).join("")}
          </div>
          <textarea data-note="${escapeHtml(job.job_id)}" placeholder="哪里不行？写具体现象，例如：分隔带右下被南瓜侵入约 1/4 宽">${escapeHtml(draft.note)}</textarea>
          <div class="form-foot">
            <button class="primary" data-save="${escapeHtml(job.job_id)}">保存批注</button>
            <small>类别与文字至少填一项</small>
          </div>
        </div>
      </div>
    </article>`;
  }).join("");
  bindCardEvents();
}

function bindCardEvents() {
  const cards = $("cards");
  cards.querySelectorAll("label.chk input").forEach((input) => input.addEventListener("change", () => {
    const draft = draftFor(input.dataset.job);
    if (input.checked) draft.types.add(input.dataset.type); else draft.types.delete(input.dataset.type);
    input.closest("label").classList.toggle("on", input.checked);
  }));
  cards.querySelectorAll(".sev-row button").forEach((button) => button.addEventListener("click", () => {
    const draft = draftFor(button.dataset.job);
    draft.severity = button.dataset.sev;
    button.closest(".sev-row").querySelectorAll("button").forEach((item) => item.classList.toggle("on", item === button));
  }));
  cards.querySelectorAll("textarea[data-note]").forEach((area) => area.addEventListener("input", () => {
    draftFor(area.dataset.note).note = area.value;
  }));
  cards.querySelectorAll("button[data-save]").forEach((button) => button.addEventListener("click", () => saveAnnotation(button.dataset.save)));
  cards.querySelectorAll("button[data-del]").forEach((button) => button.addEventListener("click", () => deleteAnnotation(button.dataset.del)));
}

async function saveAnnotation(jobId) {
  const draft = draftFor(jobId);
  const body = {
    job_id: jobId,
    defect_types: [...draft.types],
    severity: draft.severity,
    note: draft.note.trim(),
  };
  if (!body.defect_types.length && !body.note) {
    alert("缺陷类别和批注内容至少填一项");
    return;
  }
  const response = await fetch("/api/review/annotations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    alert(detail.detail || "保存失败");
    return;
  }
  const record = await response.json();
  const job = overview.jobs.find((item) => item.job_id === jobId);
  if (job) job.annotations = [...(job.annotations || []), record];
  overview.annotation_total += 1;
  delete drafts[jobId];
  render();
  const card = $(`card-${jobId}`);
  if (card) card.scrollIntoView({ block: "nearest" });
}

async function deleteAnnotation(annotationId) {
  if (!confirm("删除这条批注？")) return;
  const response = await fetch(`/api/review/annotations/${annotationId}`, { method: "DELETE" });
  if (!response.ok) {
    alert("删除失败");
    return;
  }
  overview.jobs.forEach((job) => {
    const before = (job.annotations || []).length;
    job.annotations = (job.annotations || []).filter((item) => item.id !== annotationId);
    overview.annotation_total -= before - job.annotations.length;
  });
  render();
}

async function addDefectType() {
  const input = $("new-type-label");
  const label = input.value.trim();
  if (!label) return;
  const response = await fetch("/api/review/defect-types", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    alert(detail.detail || "添加失败");
    return;
  }
  overview.defect_types.push(await response.json());
  input.value = "";
  render();
}

$("refresh").addEventListener("click", loadOverview);
$("add-type").addEventListener("click", addDefectType);
$("new-type-label").addEventListener("keydown", (event) => { if (event.key === "Enter") addDefectType(); });
loadOverview();
