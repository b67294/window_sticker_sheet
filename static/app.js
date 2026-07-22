const $ = (id) => document.getElementById(id);
const stages = ["input", "generate", "key", "components", "geometry", "layout"];
const stageNames = { input: "输入", generate: "纯色母版", key: "色键与 Alpha", components: "像素组件", geometry: "分组与轮廓", layout: "候选 Sheet" };
const inputModeNotes = {
  master: "从纯色背景计算 Alpha。",
  alpha: "完整保留原始软 Alpha，跳过色键；仅接受带透明通道的 PNG / WEBP。",
  source: "先调用生成服务重建母版，再执行 Alpha 提取。",
};
const inputModeNames = { master: "纯色母版", alpha: "透明 PNG", source: "电商原图" };

let defaults = null;
let currentJob = null;
let activeStage = "input";
let inputMode = "master";
let selectedGroups = new Set();
let pollTimer = null;
let canvasImage = null;
let pendingUpload = false;

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data.detail || data.error || data || `HTTP ${response.status}`);
  return data;
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3200);
}

function setInputMode(value) {
  inputMode = value;
  document.querySelectorAll("#input-mode button").forEach((button) => button.classList.toggle("active", button.dataset.value === value));
  $("prompt-details").hidden = value !== "source";
  $("input-mode-note").textContent = inputModeNotes[value] || "";
  $("file").accept = value === "alpha" ? "image/png,image/webp" : "image/png,image/jpeg,image/webp";
  document.querySelectorAll('[data-setting="key_low"], [data-setting="key_high"]').forEach((input) => {
    input.disabled = value === "alpha";
    input.title = value === "alpha" ? "透明图片保留原始 Alpha，不使用色键阈值" : "";
  });
  $("key-settings-title").textContent = value === "alpha" ? "Alpha 与组件" : "色键与组件";
  $("key-step-label").textContent = value === "alpha" ? "Alpha 直通" : "色键";
  $("key-stage-option").textContent = value === "alpha" ? "运行到 Alpha 直通" : "运行到色键";
  if (activeStage === "key") $("stage-title").textContent = displayStageName("key", value);
  if (pendingUpload) markPendingUpload();
}

function displayStageName(stage, mode = pendingUpload ? inputMode : (currentJob?.input_mode || inputMode)) {
  if (stage === "key" && mode === "alpha") return "Alpha 直通";
  return stageNames[stage] || stage;
}

function populateSettings(settings) {
  document.querySelectorAll("[data-setting]").forEach((input) => {
    const key = input.dataset.setting;
    if (settings[key] === undefined) return;
    if (input.type === "checkbox") input.checked = Boolean(settings[key]);
    else if (input.dataset.settingTransform === "percent") input.value = Number(settings[key]) * 100;
    else input.value = settings[key];
  });
  updateWeightOutputs();
}

function collectSettings() {
  const result = {};
  document.querySelectorAll("[data-setting]").forEach((input) => {
    if (input.type === "checkbox") result[input.dataset.setting] = input.checked;
    else if (input.dataset.settingType === "string") result[input.dataset.setting] = input.value;
    else if (input.dataset.settingTransform === "percent") result[input.dataset.setting] = Number(input.value) / 100;
    else result[input.dataset.setting] = Number(input.value);
  });
  return result;
}

function updateWeightOutputs() {
  $("compactness-output").value = `${Math.round(Number($("compactness-weight").value) * 100)}%`;
  $("alignment-output").value = `${Math.round(Number($("alignment-weight").value) * 100)}%`;
  $("balance-output").value = `${Math.round(Number($("balance-weight").value) * 100)}%`;
}

function artifactUrl(name, stage = null) {
  const artifact = currentJob?.artifacts?.find((item) => item.name === name && (!stage || item.stage === stage));
  return artifact?.url || null;
}

function setActiveStage(stage) {
  activeStage = stage;
  document.querySelectorAll("#stepper button").forEach((button) => button.classList.toggle("active", button.dataset.stage === stage));
  if (!currentJob && !pendingUpload) return;
  $("stage-kicker").textContent = `STEP ${stages.indexOf(stage) + 1}`;
  $("stage-title").textContent = displayStageName(stage);
  renderArtifacts();
  renderSpecialPanels();
}

function renderJob(job) {
  currentJob = job;
  if (pendingUpload) {
    if (["queued", "running"].includes(job.status)) schedulePoll();
    return;
  }
  setInputMode(job.input_mode || inputMode);
  populateSettings(job.settings || defaults?.settings || {});
  localStorage.setItem("windowStickerJobId", job.id);
  $("empty").hidden = true;
  $("stage-view").hidden = false;
  $("job-title").textContent = job.id;
  $("job-note").textContent = job.error || `当前阶段：${displayStageName(job.current_stage, job.input_mode)}`;
  const status = $("status");
  status.textContent = ({ ready: "已创建", queued: "排队中", running: "运行中", complete: "已完成", failed: "失败", interrupted: "已中断" })[job.status] || job.status;
  status.className = `status ${job.status === "ready" ? "idle" : job.status}`;
  const jobLocked = ["running", "queued"].includes(job.status);
  $("rerun").disabled = jobLocked;
  document.querySelectorAll("[data-group-action]").forEach((button) => { button.disabled = jobLocked; });
  $("download").classList.toggle("disabled", !job.artifacts?.length);
  $("download").href = job.download_url;
  $("logs").textContent = (job.logs || []).join("\n");
  $("log-panel").hidden = !(job.logs || []).length;
  renderArtifacts();
  renderSpecialPanels();
  if (["queued", "running"].includes(job.status)) schedulePoll();
}

function renderArtifacts() {
  const grid = $("artifact-grid");
  if (pendingUpload) {
    grid.innerHTML = `<article class="artifact-card file-card"><div><strong>新文件待运行</strong><br><small>将创建新的${escapeHtml(inputModeNames[inputMode] || inputMode)}任务，不会再使用上一个任务的产物。</small></div></article>`;
    return;
  }
  if (!currentJob) return;
  const items = (currentJob.artifacts || []).filter((item) => item.stage === activeStage);
  grid.innerHTML = items.length ? items.map((item) => {
    if (item.kind === "image") {
      return `<article class="artifact-card"><a class="image-wrap" href="${item.url}" target="_blank"><img src="${item.url}" alt="${escapeHtml(item.label)}"></a><div class="meta"><strong>${escapeHtml(item.label)}</strong><small>${item.width}×${item.height}</small></div></article>`;
    }
    return `<article class="artifact-card file-card"><div><strong>${escapeHtml(item.label)}</strong><br><code>${escapeHtml(item.path)}</code></div><a class="secondary" href="${item.url}" target="_blank">查看</a></article>`;
  }).join("") : `<article class="artifact-card file-card"><div><strong>当前步骤暂无产物</strong><br><small>点击“只运行到这一步”生成。</small></div></article>`;
}

function renderSpecialPanels() {
  if (pendingUpload) {
    $("component-workbench").hidden = true;
    $("candidate-grid").hidden = true;
    return;
  }
  const componentVisible = activeStage === "components" && (currentJob?.groups || []).length;
  $("component-workbench").hidden = !componentVisible;
  $("candidate-grid").hidden = activeStage !== "layout" || !(currentJob?.candidates || []).length;
  if (componentVisible) {
    renderGroupList();
    drawGroupCanvas();
  }
  if (activeStage === "layout") renderCandidates();
}

function groupThumb(group) {
  const primitive = currentJob.primitives.find((item) => group.primitive_ids.includes(item.id));
  return primitive?.asset_url || "";
}

function renderGroupList() {
  const list = $("group-list");
  list.innerHTML = currentJob.groups.map((group) => `
    <div class="group-item ${selectedGroups.has(group.id) ? "selected" : ""} ${group.active ? "" : "inactive"}" data-group-id="${group.id}">
      <img class="group-thumb" src="${groupThumb(group)}" alt="">
      <div><strong>${group.id}</strong><small>${group.primitive_ids.length} 个原始组件 · ${Math.round(group.bbox[2])}×${Math.round(group.bbox[3])} px</small></div>
      <div class="chips">${group.rotatable ? '<span class="chip">旋转</span>' : '<span class="chip">锁向</span>'}${group.filler ? '<span class="chip">填缝</span>' : ''}${group.active ? '' : '<span class="chip">已删除</span>'}</div>
    </div>`).join("");
  list.querySelectorAll(".group-item").forEach((item) => item.addEventListener("click", () => toggleGroup(item.dataset.groupId)));
  syncSelectionOptions();
}

function toggleGroup(groupId, additive = true) {
  if (!additive) selectedGroups.clear();
  if (selectedGroups.has(groupId)) selectedGroups.delete(groupId); else selectedGroups.add(groupId);
  renderGroupList();
  drawGroupCanvas();
}

function syncSelectionOptions() {
  const selected = currentJob.groups.filter((group) => selectedGroups.has(group.id));
  const one = selected.length === 1 ? selected[0] : null;
  const jobLocked = ["running", "queued"].includes(currentJob?.status);
  $("group-rotatable").disabled = !one || jobLocked;
  $("group-filler").disabled = !one || jobLocked;
  $("group-copies").disabled = !one || jobLocked;
  $("save-group-options").disabled = !one || jobLocked;
  if (one) {
    $("group-rotatable").checked = !!one.rotatable;
    $("group-filler").checked = !!one.filler;
    $("group-copies").value = one.max_copies ?? 2;
  }
}

async function drawGroupCanvas() {
  const canvas = $("group-canvas");
  const source = artifactUrl("foreground", "key") || artifactUrl("master", "generate") || artifactUrl("upload", "input");
  if (!source) return;
  const image = new Image();
  image.onload = () => {
    canvasImage = image;
    const maxWidth = Math.max(500, canvas.parentElement.clientWidth);
    const scale = Math.min(1, maxWidth / image.width);
    canvas.width = Math.round(image.width * scale);
    canvas.height = Math.round(image.height * scale);
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    currentJob.groups.forEach((group) => {
      const [x, y, width, height] = group.bbox.map((value) => value * scale);
      const selected = selectedGroups.has(group.id);
      context.strokeStyle = selected ? "#f46f57" : group.active ? "#0b7181" : "#8a999e";
      context.lineWidth = selected ? 4 : 2;
      context.setLineDash(group.active ? [] : [7, 5]);
      context.strokeRect(x, y, width, height);
      context.fillStyle = selected ? "rgba(244,111,87,.88)" : "rgba(11,113,129,.82)";
      context.fillRect(x, Math.max(0, y - 19), Math.max(42, group.id.length * 8 + 10), 19);
      context.fillStyle = "white";
      context.font = "12px sans-serif";
      context.fillText(group.id, x + 5, Math.max(13, y - 5));
    });
    context.setLineDash([]);
  };
  image.src = `${source}?t=${Date.now()}`;
}

function canvasClick(event) {
  if (!currentJob || !canvasImage) return;
  const canvas = $("group-canvas");
  const rect = canvas.getBoundingClientRect();
  const x = (event.clientX - rect.left) * canvasImage.width / rect.width;
  const y = (event.clientY - rect.top) * canvasImage.height / rect.height;
  const hit = [...currentJob.groups].reverse().find((group) => {
    const [gx, gy, width, height] = group.bbox;
    return x >= gx && x <= gx + width && y >= gy && y <= gy + height;
  });
  if (hit) toggleGroup(hit.id, event.shiftKey);
}

function renderCandidates() {
  const grid = $("candidate-grid");
  const strategyNames = { tidy_rows: "整齐行列", maxrects: "MaxRects 紧凑", hybrid_fill: "异形填缝", hybrid_search: "多起点搜索" };
  grid.innerHTML = (currentJob.candidates || []).map((candidate) => `
    <article class="candidate-card ${currentJob.selected_candidate === candidate.id ? "selected" : ""}">
      <img src="${candidate.contact_sheet_url}" alt="${candidate.id}">
      <div class="candidate-meta">
        <div class="candidate-title"><strong>${strategyNames[candidate.strategy] || candidate.strategy}</strong>${currentJob.selected_candidate === candidate.id ? '<span>当前选中</span>' : ''}</div>
        <div class="metrics">
          <div class="metric"><b>${candidate.page_count}</b><small>页数</small></div>
          <div class="metric"><b>${formatPercent(candidate.layout_scale)}</b><small>实际尺寸</small></div>
          <div class="metric"><b>${formatPercent(candidate.utilization)}</b><small>利用率</small></div>
          <div class="metric"><b>${formatPercent(candidate.compactness)}</b><small>紧凑度</small></div>
          <div class="metric"><b>${formatPercent(candidate.alignment)}</b><small>整齐度</small></div>
          <div class="metric"><b>${formatPercent(candidate.largest_void_ratio)}</b><small>最大空白</small></div>
          <div class="metric"><b>${formatPercent(candidate.balance)}</b><small>平衡</small></div>
          <div class="metric"><b>${formatPercent(candidate.score)}</b><small>总分</small></div>
        </div>
        <button data-candidate-id="${candidate.id}">选择此方案</button>
        ${currentJob.selected_candidate === candidate.id && currentJob.final_pdf_url ? `
          <div class="candidate-downloads">
            <a class="pdf-primary" href="${currentJob.final_pdf_url}" target="_blank">下载全部 Sheet PDF（${candidate.page_count} 页）</a>
            <div>${(currentJob.final_pdf_page_urls || []).map((url, index) => `<a href="${url}" target="_blank">第 ${index + 1} 页 PDF</a>`).join("")}</div>
          </div>` : ""}
      </div>
    </article>`).join("");
  grid.querySelectorAll("button[data-candidate-id]").forEach((button) => button.addEventListener("click", () => selectCandidate(button.dataset.candidateId)));
}

function formatPercent(value) { return `${(Number(value || 0) * 100).toFixed(1)}%`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }

function markPendingUpload() {
  const file = $("file").files[0];
  if (!file) return;
  if (!pendingUpload && defaults?.settings) populateSettings(defaults.settings);
  pendingUpload = true;
  clearTimeout(pollTimer);
  $("empty").hidden = true;
  $("stage-view").hidden = false;
  $("job-title").textContent = `新文件：${file.name}`;
  $("job-note").textContent = `待创建${inputModeNames[inputMode] || inputMode}任务；运行按钮将使用这个新文件。`;
  const status = $("status");
  status.textContent = "待运行";
  status.className = "status idle";
  $("rerun").disabled = false;
  $("download").classList.add("disabled");
  $("download").href = "#";
  $("logs").textContent = "";
  $("log-panel").hidden = true;
  renderArtifacts();
  renderSpecialPanels();
}

async function createJobFromForm() {
  const file = $("file").files[0];
  if (!file) throw new Error("请先选择图片");
  const form = new FormData();
  form.append("input_mode", inputMode);
  form.append("file", file);
  form.append("settings_json", JSON.stringify(collectSettings()));
  form.append("generation_prompt", $("generation-prompt").value);
  const job = await api("/api/jobs", { method: "POST", body: form });
  pendingUpload = false;
  renderJob(job);
  return job;
}

async function createAndRun(event) {
  event.preventDefault();
  try {
    $("create-run").disabled = true;
    await createJobFromForm();
    await startRun("all", null);
  } catch (error) {
    toast(error.message);
  } finally {
    $("create-run").disabled = false;
  }
}

async function patchSettings() {
  if (!currentJob) return null;
  const job = await api(`/api/jobs/${currentJob.id}/settings`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: collectSettings() }),
  });
  renderJob(job);
  return job;
}

function nextRunnableStage(stage) {
  const index = stages.indexOf(stage);
  return stages[Math.min(stages.length - 1, index + 1)];
}

async function startRun(throughStage, fromStage = null) {
  if (!currentJob) return;
  const job = await api(`/api/jobs/${currentJob.id}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ through_stage: throughStage, from_stage: fromStage, sync: false }),
  });
  renderJob(job);
  schedulePoll();
}

async function rerun() {
  try {
    if (pendingUpload) {
      await createJobFromForm();
      await startRun($("through-stage").value, null);
      return;
    }
    const patched = await patchSettings();
    const through = $("through-stage").value;
    const from = nextRunnableStage(patched.current_stage);
    await startRun(through, from);
  } catch (error) { toast(error.message); }
}

async function runCurrentStage() {
  if (activeStage === "input") return;
  try {
    if (pendingUpload) {
      await createJobFromForm();
      await startRun(activeStage, null);
      return;
    }
    if (!currentJob) return toast("请先选择图片");
    await patchSettings();
    await startRun(activeStage, activeStage);
  } catch (error) { toast(error.message); }
}

async function groupAction(action) {
  if (!currentJob || !selectedGroups.size) return toast("请先选择分组");
  const ids = [...selectedGroups];
  const payload = { action, group_ids: ids, group_id: ids[0], values: {} };
  try {
    const job = await api(`/api/jobs/${currentJob.id}/groups`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    selectedGroups.clear();
    renderJob(job);
    toast("分组已更新；轮廓和排版已失效，可继续重跑");
  } catch (error) { toast(error.message); }
}

async function saveGroupOptions() {
  const id = [...selectedGroups][0];
  if (!id || selectedGroups.size !== 1) return;
  try {
    const job = await api(`/api/jobs/${currentJob.id}/groups`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "update", group_id: id, group_ids: [id],
        values: { rotatable: $("group-rotatable").checked, filler: $("group-filler").checked, max_copies: Number($("group-copies").value) },
      }),
    });
    renderJob(job);
    toast("分组属性已保存");
  } catch (error) { toast(error.message); }
}

async function selectCandidate(candidateId) {
  try {
    renderJob(await api(`/api/jobs/${currentJob.id}/candidates/${candidateId}/select`, { method: "POST" }));
    toast(`已生成 ${currentJob.final_pdf_page_urls?.length || 0} 张单页 PDF 和合并 PDF`);
  } catch (error) { toast(error.message); }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!currentJob) return;
    try {
      const job = await api(`/api/jobs/${currentJob.id}`);
      renderJob(job);
      if (["queued", "running"].includes(job.status)) schedulePoll();
      else if (job.status === "failed") toast(job.error || "任务失败");
      else if (job.status === "complete") toast("任务运行完成");
    } catch (error) { toast(error.message); }
  }, 1000);
}

async function showHistory() {
  try {
    const jobs = await api("/api/jobs");
    $("history-list").innerHTML = jobs.length ? jobs.map((job) => `
      <div class="history-item" data-job-id="${job.id}"><div><strong>${job.id}</strong><br><small>${job.input_mode} · ${job.current_stage}</small></div><small>${job.status}<br>${job.updated_at || ""}</small></div>`).join("") : "暂无任务";
    $("history-list").querySelectorAll("[data-job-id]").forEach((item) => item.addEventListener("click", async () => {
      pendingUpload = false;
      $("file").value = "";
      $("file-name").textContent = "PNG / JPG / WEBP · 最大 20MB";
      renderJob(await api(`/api/jobs/${item.dataset.jobId}`));
      $("history-dialog").close();
    }));
    $("history-dialog").showModal();
  } catch (error) { toast(error.message); }
}

async function init() {
  try {
    defaults = await api("/api/defaults");
    populateSettings(defaults.settings);
    $("generation-prompt").value = defaults.generation_prompt;
    const providers = [
      defaults.generation_configured ? "gpt-image-2 已配置" : "gpt-image-2 未配置",
      defaults.semantic_grouping_configured ? "语义分组已配置" : "语义分组未配置",
    ];
    $("provider-status").textContent = providers.join(" · ");
  } catch (error) { toast(error.message); }
  const previous = localStorage.getItem("windowStickerJobId");
  if (previous) {
    try { renderJob(await api(`/api/jobs/${previous}`)); } catch (_) {}
  }
}

document.querySelectorAll("#input-mode button").forEach((button) => button.addEventListener("click", () => setInputMode(button.dataset.value)));
document.querySelectorAll("#stepper button").forEach((button) => button.addEventListener("click", () => setActiveStage(button.dataset.stage)));
document.querySelectorAll("[data-group-action]").forEach((button) => button.addEventListener("click", () => groupAction(button.dataset.groupAction)));
$("file").addEventListener("change", () => {
  $("file-name").textContent = $("file").files[0]?.name || "PNG / JPG / WEBP · 最大 20MB";
  if ($("file").files[0]) markPendingUpload();
});
$("job-form").addEventListener("submit", createAndRun);
$("compactness-weight").addEventListener("input", updateWeightOutputs);
$("alignment-weight").addEventListener("input", updateWeightOutputs);
$("balance-weight").addEventListener("input", updateWeightOutputs);
$("rerun").addEventListener("click", rerun);
$("run-stage").addEventListener("click", runCurrentStage);
$("group-canvas").addEventListener("click", canvasClick);
$("save-group-options").addEventListener("click", saveGroupOptions);
$("load-jobs").addEventListener("click", showHistory);
$("close-history").addEventListener("click", () => $("history-dialog").close());
$("toggle-log").addEventListener("click", () => {
  const logs = $("logs");
  logs.hidden = !logs.hidden;
  $("toggle-log").textContent = logs.hidden ? "展开" : "收起";
});
window.addEventListener("resize", () => { if (!$("component-workbench").hidden) drawGroupCanvas(); });

setInputMode("master");
init();
