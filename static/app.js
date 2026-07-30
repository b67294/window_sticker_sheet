const $ = (id) => document.getElementById(id);
const stages = ["input", "generate", "key", "components", "geometry", "layout"];
const stageNames = { input: "输入", generate: "创新白底母版", key: "去背景与 Alpha", components: "像素组件", geometry: "分组与轮廓", layout: "候选 Sheet" };
const inputModeNotes = {
  master: "上传白底母版，通过 ComfyUI 去背景。",
  alpha: "完整保留原始软 Alpha，跳过去背景；仅接受带透明通道的 PNG / WEBP。",
  source: "可一次选择多张图；先逐张强衍生创新为白底母版，再自动去背景、提取组件并选择总分最高候选。",
};
const inputModeNames = { master: "白底母版", alpha: "透明 PNG", source: "电商原图" };
let defaults = null;
let windowTemplates = {};
let currentJob = null;
let currentBatch = null;
let activeStage = "input";
let inputMode = "master";
let selectedGroups = new Set();
let pollTimer = null;
let canvasImage = null;
let pendingUpload = false;
let syncingTemplateDimensions = false;
let promptPreviewTimer = null;

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
  $("file").multiple = value === "source";
  $("batch-pdf-option").hidden = value !== "source";
  if (value !== "source" && $("file").files.length > 1) {
    $("file").value = "";
    pendingUpload = false;
    renderUploadList();
  }
  $("key-settings-title").textContent = value === "alpha" ? "Alpha 与组件" : "ComfyUI 去背景与组件";
  $("key-step-label").textContent = value === "alpha" ? "Alpha 直通" : "去背景";
  $("key-stage-option").textContent = value === "alpha" ? "运行到 Alpha 直通" : "运行到去背景";
  if (activeStage === "key") $("stage-title").textContent = displayStageName("key", value);
  if (pendingUpload) markPendingUpload();
  validateSelectedImageAspect();
}

function selectedFiles() {
  return Array.from($("file").files || []);
}

function updateCreateButton() {
  const count = selectedFiles().length;
  $("create-run").innerHTML = inputMode === "source" && count > 1
    ? `批量创新并生成候选（${count}张） <span>→</span>`
    : `创建并运行完整链路 <span>→</span>`;
}

function renderUploadList() {
  const files = selectedFiles();
  const list = $("upload-list");
  list.hidden = files.length < 2;
  list.innerHTML = files.map((file) => `
    <div class="upload-item">
      <img src="${URL.createObjectURL(file)}" alt="">
      <span title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
      <small>${(file.size / 1024 / 1024).toFixed(1)} MB</small>
    </div>`).join("");
  $("file-name").textContent = files.length > 1
    ? `已选择 ${files.length} 张图片`
    : files[0]?.name || "PNG / JPG / WEBP · 最大 20MB";
  updateCreateButton();
}

function displayStageName(stage, mode = pendingUpload ? inputMode : (currentJob?.input_mode || inputMode)) {
  if (stage === "key" && mode === "alpha") return "Alpha 直通";
  if (stage === "key" && mode !== "alpha") return "ComfyUI 去背景";
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
  renderWindowTemplateInfo();
  updateWeightOutputs();
  schedulePromptPreview();
}

function currentTemplate() {
  return windowTemplates[$("window-template").value] || null;
}

function renderWindowTemplateInfo() {
  const template = currentTemplate();
  if (!template) {
    $("window-template-description").textContent = "历史任务未指定模板；再次生图前请选择单窗或双栏窗。";
    $("window-template-constraint").textContent = "旧版未指定";
    return;
  }
  $("window-template-description").textContent =
    `${template.description} · 生成画布 ${template.generation_size}`;
  $("window-template-constraint").textContent = template.prompt_constraint;
}

function applyWindowTemplate() {
  const template = currentTemplate();
  if (!template) return;
  syncingTemplateDimensions = true;
  $("install-width").value = template.default_mm[0];
  $("install-height").value = template.default_mm[1];
  syncingTemplateDimensions = false;
  renderWindowTemplateInfo();
  validateSelectedImageAspect();
  schedulePromptPreview();
}

function syncTemplateDimension(source) {
  if (syncingTemplateDimensions) return;
  const template = currentTemplate();
  if (!template) return;
  const ratio = Number(template.ratio[0]) / Number(template.ratio[1]);
  syncingTemplateDimensions = true;
  if (source === "width") {
    $("install-height").value = (Number($("install-width").value) / ratio).toFixed(1).replace(/\.0$/, "");
  } else {
    $("install-width").value = (Number($("install-height").value) * ratio).toFixed(1).replace(/\.0$/, "");
  }
  syncingTemplateDimensions = false;
  schedulePromptPreview();
}

function schedulePromptPreview() {
  clearTimeout(promptPreviewTimer);
  promptPreviewTimer = setTimeout(updatePromptPreview, 220);
}

async function updatePromptPreview() {
  if (!defaults || $("window-template").value === "legacy") {
    $("generation-prompt-preview").textContent = "旧版任务未指定模板，请先选择单窗或双栏窗。";
    return;
  }
  try {
    const result = await api("/api/prompts/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        generation_prompt_base: $("generation-prompt").value,
        settings: collectSettings(),
      }),
    });
    $("generation-prompt-preview").textContent = result.prompt;
  } catch (error) {
    $("generation-prompt-preview").textContent = error.message;
  }
}

function validateSelectedImageAspect() {
  const warning = $("aspect-warning");
  warning.hidden = true;
  const file = selectedFiles()[0];
  const template = currentTemplate();
  if (!file || inputMode === "source" || !template) return;
  const image = new Image();
  const url = URL.createObjectURL(file);
  image.onload = () => {
    URL.revokeObjectURL(url);
    const actual = image.naturalWidth / image.naturalHeight;
    const expected = Number(template.ratio[0]) / Number(template.ratio[1]);
    const deviation = Math.abs(actual / expected - 1);
    if (deviation > 0.02) {
      warning.textContent = `比例警告：上传图片为 ${image.naturalWidth}×${image.naturalHeight}，与${template.label}偏差 ${(deviation * 100).toFixed(1)}%。仍可继续，系统不会拉伸或自动裁切。`;
      warning.hidden = false;
    }
  };
  image.onerror = () => URL.revokeObjectURL(url);
  image.src = url;
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
  currentBatch = null;
  currentJob = job;
  if (pendingUpload) {
    if (["queued", "running"].includes(job.status)) schedulePoll();
    return;
  }
  setInputMode(job.input_mode || inputMode);
  if (job.settings?.window_template === "legacy" && !$("window-template").querySelector('option[value="legacy"]')) {
    $("window-template").insertAdjacentHTML("beforeend", '<option value="legacy">旧版未指定</option>');
  }
  populateSettings(job.settings || defaults?.settings || {});
  $("generation-prompt").value = job.generation_prompt_base || defaults?.generation_prompt || "";
  if (job.aspect_ratio_warning) {
    $("aspect-warning").textContent = `比例警告：${job.aspect_ratio_warning.message}`;
    $("aspect-warning").hidden = false;
  } else {
    $("aspect-warning").hidden = true;
  }
  localStorage.setItem("windowStickerJobId", job.id);
  localStorage.removeItem("windowStickerBatchId");
  $("batch-view").hidden = true;
  $("stepper").hidden = false;
  $("empty").hidden = true;
  $("stage-view").hidden = false;
  $("job-title").textContent = job.id;
  $("job-note").textContent = job.error || `当前阶段：${displayStageName(job.current_stage, job.input_mode)}`;
  const status = $("status");
  status.textContent = ({ ready: "已创建", queued: "排队中", running: "运行中", complete: "已完成", failed: "失败", interrupted: "已中断" })[job.status] || job.status;
  status.className = `status ${job.status === "ready" ? "idle" : job.status}`;
  const jobLocked = ["running", "queued"].includes(job.status);
  $("rerun").disabled = jobLocked;
  $("run-stage").disabled = jobLocked;
  document.querySelectorAll("[data-group-action]").forEach((button) => { button.disabled = jobLocked; });
  $("download").classList.toggle("disabled", !job.artifacts?.length);
  $("download").href = job.download_url;
  $("logs").textContent = (job.logs || []).join("\n");
  $("log-panel").hidden = !(job.logs || []).length;
  renderArtifacts();
  renderSpecialPanels();
  if (["queued", "running"].includes(job.status)) schedulePoll();
}

function batchStatusLabel(status) {
  return ({
    queued: "排队中", running: "批量运行中", complete: "已完成",
    partial_success: "部分成功", failed: "失败", interrupted: "已中断",
  })[status] || status;
}

function renderBatch(batch) {
  currentBatch = batch;
  currentJob = null;
  pendingUpload = false;
  localStorage.setItem("windowStickerBatchId", batch.id);
  localStorage.removeItem("windowStickerJobId");
  $("empty").hidden = true;
  $("stage-view").hidden = true;
  $("stepper").hidden = true;
  $("log-panel").hidden = true;
  $("batch-view").hidden = false;
  $("job-title").textContent = batch.id;
  $("job-note").textContent = batch.error || `共 ${batch.total} 张，逐张串行执行；单张失败不会阻塞后续图片。`;
  const status = $("status");
  status.textContent = batchStatusLabel(batch.status);
  status.className = `status ${batch.status}`;
  $("rerun").disabled = true;
  $("run-stage").disabled = true;
  $("download").classList.add("disabled");
  $("batch-title").textContent = `批次 ${batch.id}`;
  const pdfStatus = batch.delivery?.pdfStatus || "pending";
  const pdfStatusText = ({
    pending: "PDF等待生成", queued: "PDF排队中", rendering: "PDF生成中",
    ready: "PDF已就绪", skipped: "已跳过PDF", failed: "部分PDF失败",
  })[pdfStatus] || pdfStatus;
  $("batch-progress-text").textContent = `${batch.completed}/${batch.total} 张完成${batch.failed ? ` · ${batch.failed} 张待重试` : ""} · ${pdfStatusText}`;
  $("batch-progress-bar").style.width = `${batch.total ? batch.completed / batch.total * 100 : 0}%`;
  const pdfBusy = ["queued", "rendering"].includes(pdfStatus);
  const terminal = ["complete", "partial_success", "failed", "interrupted"].includes(batch.status);
  $("download-batch").href = batch.delivery_download_url || "#";
  $("download-batch").classList.toggle("disabled", !batch.completed || pdfBusy);
  $("download-batch-archive").href = batch.download_url || "#";
  $("download-batch-archive").classList.toggle("disabled", !batch.completed);
  $("render-batch-pdf").hidden = !terminal || !batch.completed || pdfStatus === "ready";
  $("render-batch-pdf").disabled = pdfBusy;
  $("render-batch-pdf").textContent = pdfStatus === "failed" ? "重试生成PDF" : pdfBusy ? "PDF生成中…" : "补生成全部PDF";
  $("retry-batch").hidden = !["partial_success", "failed", "interrupted"].includes(batch.status);
  $("batch-grid").innerHTML = (batch.items || []).map((item, index) => `
    <article class="batch-card">
      <div class="batch-card-head">
        <strong>${index + 1}. ${escapeHtml(item.source_name)}</strong>
        <span class="status ${item.status}">${batchStatusLabel(item.status)}</span>
      </div>
      <div class="batch-images">
        ${batchImage(item.source_url, "电商原图")}
        ${batchImage(item.master_url, "创新白底图")}
        ${batchImage(item.candidate_url, "最高分候选")}
      </div>
      <div class="batch-card-meta">
        <span>阶段：${escapeHtml(displayStageName(item.current_stage || "input", "source"))}</span>
        ${item.selected_score !== null && item.selected_score !== undefined ? `<b>总分 ${formatPercent(item.selected_score)}</b>` : ""}
      </div>
      ${item.error ? `<p class="batch-error">${escapeHtml(item.error)}</p>` : ""}
      <div class="batch-card-links">
        ${item.final_pdf_url ? `<a href="${item.final_pdf_url}" target="_blank">最终 PDF</a>` : ""}
        ${item.job_download_url && item.status === "complete" ? `<a href="${item.job_download_url}">单张 ZIP</a>` : ""}
      </div>
    </article>`).join("");
  if (["queued", "running"].includes(batch.status) || pdfBusy) scheduleBatchPoll();
}

function batchImage(url, label) {
  return url
    ? `<a href="${url}" target="_blank"><img src="${url}" alt="${label}"><small>${label}</small></a>`
    : `<div class="batch-placeholder"><span>等待生成</span><small>${label}</small></div>`;
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
  const strategyNames = { tidy_rows: "整齐行列", maxrects: "MaxRects 紧凑", hybrid_fill: "异形填缝", center_compact: "中心紧凑" };
  grid.innerHTML = (currentJob.candidates || []).map((candidate) => `
    <article class="candidate-card ${currentJob.selected_candidate === candidate.id ? "selected" : ""} ${candidate.enabled === false ? "disabled" : ""}">
      <img src="${candidate.contact_sheet_url}" alt="${candidate.id}">
      <div class="candidate-meta">
        <div class="candidate-title"><strong>${strategyNames[candidate.strategy] || candidate.strategy}</strong>${currentJob.selected_candidate === candidate.id ? '<span>当前选中</span>' : ''}${candidate.enabled === false ? '<span style="color:#999;font-weight:normal">（已禁用）</span>' : ''}</div>
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
        <button data-candidate-id="${candidate.id}" ${candidate.enabled === false ? "disabled" : ""}>选择此方案</button>
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
  const files = selectedFiles();
  const file = files[0];
  if (!file) return;
  if (!pendingUpload && defaults?.settings) populateSettings(defaults.settings);
  pendingUpload = true;
  currentBatch = null;
  clearTimeout(pollTimer);
  $("batch-view").hidden = true;
  $("stepper").hidden = false;
  $("empty").hidden = true;
  $("stage-view").hidden = false;
  $("job-title").textContent = files.length > 1 ? `新批次：${files.length} 张电商原图` : `新文件：${file.name}`;
  $("job-note").textContent = files.length > 1
    ? "将共享当前创新 Prompt 和物理参数，逐张运行完整链路。"
    : `待创建${inputModeNames[inputMode] || inputMode}任务；运行按钮将使用这个新文件。`;
  const status = $("status");
  status.textContent = "待运行";
  status.className = "status idle";
  $("rerun").disabled = files.length > 1;
  $("run-stage").disabled = files.length > 1;
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

async function createBatchFromForm() {
  const files = selectedFiles();
  if (inputMode !== "source" || files.length < 2) throw new Error("批量任务需要至少两张电商原图");
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  form.append("settings_json", JSON.stringify(collectSettings()));
  form.append("generation_prompt", $("generation-prompt").value);
  form.append("render_pdf", $("render-pdf").checked ? "true" : "false");
  const batch = await api("/api/batches", { method: "POST", body: form });
  renderBatch(batch);
  return batch;
}

async function createAndRun(event) {
  event.preventDefault();
  try {
    $("create-run").disabled = true;
    if (inputMode === "source" && selectedFiles().length > 1) {
      await createBatchFromForm();
      scheduleBatchPoll();
    } else {
      await createJobFromForm();
      await startRun("all", null);
    }
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

function scheduleBatchPoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!currentBatch) return;
    try {
      const batch = await api(`/api/batches/${currentBatch.id}`);
      renderBatch(batch);
      if (["queued", "running"].includes(batch.status) || ["queued", "rendering"].includes(batch.delivery?.pdfStatus)) scheduleBatchPoll();
      else if (batch.status === "complete") toast("批量任务全部完成");
      else toast(batch.error || `批量任务${batchStatusLabel(batch.status)}`);
    } catch (error) { toast(error.message); }
  }, 1200);
}

async function renderBatchPdfs() {
  if (!currentBatch) return;
  try {
    $("render-batch-pdf").disabled = true;
    const batch = await api(
      `/api/batches/${currentBatch.id}/delivery/pdf`,
      { method: "POST" },
    );
    renderBatch(batch);
    scheduleBatchPoll();
  } catch (error) {
    toast(error.message);
  } finally {
    $("render-batch-pdf").disabled = false;
  }
}

async function retryBatch() {
  if (!currentBatch) return;
  try {
    $("retry-batch").disabled = true;
    renderBatch(await api(`/api/batches/${currentBatch.id}/retry`, { method: "POST" }));
    scheduleBatchPoll();
  } catch (error) {
    toast(error.message);
  } finally {
    $("retry-batch").disabled = false;
  }
}

async function showHistory() {
  try {
    const [batches, jobs] = await Promise.all([api("/api/batches"), api("/api/jobs")]);
    const batchHtml = batches.map((batch) => `
      <div class="history-item" data-batch-id="${batch.id}"><div><strong>${batch.id}</strong><br><small>批量 · ${batch.completed}/${batch.total} 张完成</small></div><small>${batchStatusLabel(batch.status)}<br>${batch.updated_at || ""}</small></div>`).join("");
    const jobHtml = jobs.map((job) => `
      <div class="history-item" data-job-id="${job.id}"><div><strong>${job.id}</strong><br><small>${job.input_mode} · ${job.current_stage}</small></div><small>${job.status}<br>${job.updated_at || ""}</small></div>`).join("");
    $("history-list").innerHTML = batchHtml || jobHtml
      ? `${batchHtml}${jobHtml}`
      : "暂无任务";
    $("history-list").querySelectorAll("[data-batch-id]").forEach((item) => item.addEventListener("click", async () => {
      pendingUpload = false;
      $("file").value = "";
      renderUploadList();
      renderBatch(await api(`/api/batches/${item.dataset.batchId}`));
      $("history-dialog").close();
    }));
    $("history-list").querySelectorAll("[data-job-id]").forEach((item) => item.addEventListener("click", async () => {
      pendingUpload = false;
      $("file").value = "";
      renderUploadList();
      renderJob(await api(`/api/jobs/${item.dataset.jobId}`));
      $("history-dialog").close();
    }));
    $("history-dialog").showModal();
  } catch (error) { toast(error.message); }
}

async function init() {
  try {
    defaults = await api("/api/defaults");
    windowTemplates = Object.fromEntries((defaults.window_templates || []).map((item) => [item.id, item]));
    $("window-template").innerHTML = (defaults.window_templates || []).map((item) =>
      `<option value="${item.id}">${escapeHtml(item.label)}${item.id === defaults.default_window_template ? "（默认）" : ""}</option>`
    ).join("");
    populateSettings(defaults.settings);
    $("generation-prompt").value = defaults.generation_prompt;
    $("generation-prompt-preview").textContent = defaults.generation_prompt_preview || "";
    const providers = [
      defaults.generation_configured ? "gpt-image-2 已配置" : "gpt-image-2 未配置",
      defaults.comfyui_configured ? "ComfyUI 工作流已配置" : "ComfyUI 工作流未配置",
      defaults.semantic_grouping_configured ? "语义分组已配置" : "语义分组未配置",
    ];
    $("provider-status").textContent = providers.join(" · ");
  } catch (error) { toast(error.message); }
  const previousBatch = localStorage.getItem("windowStickerBatchId");
  const previousJob = localStorage.getItem("windowStickerJobId");
  if (previousBatch) {
    try { renderBatch(await api(`/api/batches/${previousBatch}`)); return; } catch (_) {}
  }
  if (previousJob) {
    try { renderJob(await api(`/api/jobs/${previousJob}`)); } catch (_) {}
  }
}

document.querySelectorAll("#input-mode button").forEach((button) => button.addEventListener("click", () => setInputMode(button.dataset.value)));
document.querySelectorAll("#stepper button").forEach((button) => button.addEventListener("click", () => setActiveStage(button.dataset.stage)));
document.querySelectorAll("[data-group-action]").forEach((button) => button.addEventListener("click", () => groupAction(button.dataset.groupAction)));
$("file").addEventListener("change", () => {
  renderUploadList();
  if ($("file").files[0]) {
    markPendingUpload();
    validateSelectedImageAspect();
  }
});
$("job-form").addEventListener("submit", createAndRun);
$("compactness-weight").addEventListener("input", updateWeightOutputs);
$("alignment-weight").addEventListener("input", updateWeightOutputs);
$("balance-weight").addEventListener("input", updateWeightOutputs);
$("window-template").addEventListener("change", applyWindowTemplate);
$("install-width").addEventListener("input", () => syncTemplateDimension("width"));
$("install-height").addEventListener("input", () => syncTemplateDimension("height"));
$("generation-prompt").addEventListener("input", schedulePromptPreview);
$("rerun").addEventListener("click", rerun);
$("run-stage").addEventListener("click", runCurrentStage);
$("group-canvas").addEventListener("click", canvasClick);
$("save-group-options").addEventListener("click", saveGroupOptions);
$("load-jobs").addEventListener("click", showHistory);
$("restart-service").addEventListener("click", restartService);

async function restartService() {
  if (!confirm("确定重启服务吗？正在运行中的任务会被中断。")) return;
  const button = $("restart-service");
  button.disabled = true;
  button.textContent = "重启中…";
  try {
    await fetch("/api/restart", { method: "POST" });
  } catch (error) {
    // 服务退出瞬间连接可能被切断，属于预期，继续轮询健康检查。
  }
  const deadline = Date.now() + 60000;
  await new Promise((resolve) => setTimeout(resolve, 3000));
  while (Date.now() < deadline) {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (response.ok) {
        location.reload();
        return;
      }
    } catch (error) {
      // 服务还没起来，继续等。
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  button.disabled = false;
  button.textContent = "重启服务";
  alert("等待服务恢复超时，请检查 server.out.log 或手动运行 start.bat。");
}
$("retry-batch").addEventListener("click", retryBatch);
$("render-batch-pdf").addEventListener("click", renderBatchPdfs);
$("close-history").addEventListener("click", () => $("history-dialog").close());
$("toggle-log").addEventListener("click", () => {
  const logs = $("logs");
  logs.hidden = !logs.hidden;
  $("toggle-log").textContent = logs.hidden ? "展开" : "收起";
});
window.addEventListener("resize", () => { if (!$("component-workbench").hidden) drawGroupCanvas(); });

setInputMode("master");
init();
