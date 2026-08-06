const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

let currentRun = null;
let currentBatch = null;
let pollTimer = null;
let batchPollTimer = null;
let briefLayouts = {};
let briefFrames = {};
let canvasRatios = {};
let previewTimer = null;

async function loadComposeOptions() {
  try {
    const defaults = await parseResponse(await fetch("/api/defaults"));
    if (!defaults.brief_prompt_defaults
        || !(defaults.brief_prompt_styles || []).length
        || !defaults.brief_innovation_prompt) {
      throw new Error("当前仍在运行旧版后端，请关闭旧服务并重新启动后刷新页面");
    }
    briefLayouts = Object.fromEntries((defaults.brief_prompt_styles || []).map((item) => [item.id, item]));
    briefFrames = Object.fromEntries((defaults.brief_window_frames || []).map((item) => [item.id, item]));
    canvasRatios = Object.fromEntries((defaults.canvas_ratios || []).map((item) => [item.id, item]));
    const promptDefaults = defaults.brief_prompt_defaults || {};
    $("innovation-prompt").value = defaults.brief_innovation_prompt;
    $("prompt-style").innerHTML = (defaults.brief_prompt_styles || []).map((item) =>
      `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("");
    $("window-frame").innerHTML = (defaults.brief_window_frames || []).map((item) =>
      `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("");
    $("canvas-ratio").innerHTML = (defaults.canvas_ratios || []).map((item) =>
      `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("");
    $("core-prompt").value = promptDefaults.core_prompt || "";
    $("product-prompt").value = promptDefaults.product_prompt || "";
    $("prompt-style").value = promptDefaults.prompt_style || "large_elements";
    $("layout-prompt").value = promptDefaults.layout_prompt || briefLayouts[$("prompt-style").value]?.text || "";
    $("window-frame").value = promptDefaults.frame_id || "pane1";
    $("frame-prompt").value = promptDefaults.frame_prompt || briefFrames[$("window-frame").value]?.prompt_constraint || "";
    $("canvas-ratio").value = promptDefaults.canvas_id || defaults.brief_default_canvas_id || "1:1";
    await refreshPromptPreviews();
  } catch (error) {
    $("compose-info").style.color = "var(--coral)";
    $("compose-info").textContent = `Prompt 加载失败：${error.message}`;
  }
}

function renderComposeInfo(spec = null) {
  const canvas = canvasRatios[$("canvas-ratio").value];
  const frame = briefFrames[$("window-frame").value];
  if (canvas) {
    $("compose-info").textContent = `${frame ? `${frame.label} × ` : ""}${canvas.label} · 生成画布 ${canvas.generation_size}`;
  }
  if (spec?.prompt_constraint) $("spec-prompt").value = spec.prompt_constraint;
}

function composeConfig() {
  return {
    prompt_style: $("prompt-style").value,
    canvas_id: $("canvas-ratio").value,
    frame_id: $("window-frame").value,
    core_prompt: $("core-prompt").value,
    product_prompt: $("product-prompt").value,
    layout_prompt: $("layout-prompt").value,
    frame_prompt: $("frame-prompt").value,
  };
}

const promptFieldElements = {
  innovation_prompt: "innovation-prompt",
  core_prompt: "core-prompt",
  product_prompt: "product-prompt",
  layout_prompt: "layout-prompt",
  frame_prompt: "frame-prompt",
};

async function saveGlobalPrompt(button) {
  const field = button.dataset.savePrompt;
  const element = $(promptFieldElements[field]);
  const previous = button.textContent;
  try {
    button.disabled = true;
    button.textContent = "保存中…";
    await parseResponse(await fetch("/api/brief/prompts/defaults", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        field,
        value: element.value,
        prompt_style: field === "layout_prompt" ? $("prompt-style").value : null,
        frame_id: field === "frame_prompt" ? $("window-frame").value : null,
      }),
    }));
    if (field === "layout_prompt") {
      briefLayouts[$("prompt-style").value].text = element.value.trim();
    }
    if (field === "frame_prompt") {
      if (briefFrames[$("window-frame").value]) briefFrames[$("window-frame").value].prompt_constraint = element.value.trim();
    }
    button.textContent = "已永久保存";
    setTimeout(() => { button.textContent = previous; }, 1600);
  } catch (error) {
    button.textContent = "保存失败";
    alert(error.message);
    setTimeout(() => { button.textContent = previous; }, 1600);
  } finally {
    button.disabled = false;
  }
}

async function previewPrompt(prompt) {
  return parseResponse(await fetch("/api/brief/prompts/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, ...composeConfig() }),
  }));
}

async function refreshPromptPreviews() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const areas = [...document.querySelectorAll("textarea[data-dir]")];
    try {
      if (!areas.length) {
        const preview = await previewPrompt("此处将在选择实验后插入该方向的内容简报。")
        renderComposeInfo(preview.spec);
        return;
      }
      for (const area of areas) {
        const index = area.dataset.dir;
        const finalArea = document.querySelector(`textarea[data-final="${index}"]`);
        const meta = document.querySelector(`[data-preview-meta="${index}"]`);
        if (meta) meta.textContent = "组装中…";
        const preview = await previewPrompt(area.value);
        renderComposeInfo(preview.spec);
        if (finalArea) finalArea.value = preview.prompt;
        if (meta) meta.textContent = `${preview.sections.length} 段 · ${preview.prompt.length} 字符`;
      }
    } catch (error) {
      document.querySelectorAll("[data-preview-meta]").forEach((item) => { item.textContent = `无法组装：${error.message}`; });
    }
  }, 250);
}

async function parseResponse(response) {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || "请求失败");
  }
  return response.json();
}

async function fetchList(url) {
  try {
    const response = await fetch(url);
    if (!response.ok) return [];
    const payload = await response.json();
    return Array.isArray(payload) ? payload : [];
  } catch (error) {
    return [];
  }
}

async function startRun() {
  const files = [...$("file").files];
  try {
    if (!files.length) throw new Error("请先选择图片");
    $("run").disabled = true;
    $("run").textContent = files.length > 1 ? `批量提交 ${files.length} 张…` : "反推中…";
    if (files.length > 1) {
      const form = new FormData();
      files.forEach((file) => form.append("files", file));
      form.append("innovation_prompt", $("innovation-prompt").value);
      form.append("auto_generate", $("auto-generate").checked ? "true" : "false");
      const config = composeConfig();
      Object.entries(config).forEach(([key, value]) => { if (value) form.append(key, value); });
      const payload = await parseResponse(await fetch("/api/brief/batch", { method: "POST", body: form }));
      $("batch-status").textContent = `批量已排队：${payload.total} 张，已打开整批看板。`;
      renderBatch(payload);
      scheduleBatchPoll();
    } else {
      const form = new FormData();
      form.append("file", files[0]);
      form.append("innovation_prompt", $("innovation-prompt").value);
      const payload = await parseResponse(await fetch("/api/brief", { method: "POST", body: form }));
      renderRun(payload);
      schedulePoll();
    }
    $("file").value = "";
    $("dropzone").querySelector("strong").textContent = "选择或拖入图片（可多选）";
    loadHistory();
    startHistoryPolling();
  } catch (error) {
    alert(error.message);
  } finally {
    $("run").disabled = false;
    $("run").textContent = "反推 + 出 3 个衍生方向 →";
  }
}

let historyPollTimer = null;

function runBusy(run) {
  return run.status === "queued" || run.status === "briefing"
    || (run.directions || []).some((item) => item.status === "generating");
}

function startHistoryPolling() {
  clearInterval(historyPollTimer);
  historyPollTimer = setInterval(async () => {
    const [runs, batches] = await Promise.all([
      fetchList("/api/brief"),
      fetchList("/api/brief/batches"),
    ]);
    renderHistory(runs, batches);
    const busyCount = batches.filter(batchBusy).length + runs.filter((run) => !run.batch_id && runBusy(run)).length;
    if (busyCount) {
      $("batch-status").textContent = `队列进行中：${busyCount} 个任务未完成…`;
      if (currentBatch) {
        const freshBatch = batches.find((batch) => batch.id === currentBatch.id);
        if (freshBatch) renderBatch(freshBatch);
      }
      if (currentRun) {
        const fresh = runs.find((run) => run.id === currentRun.id);
        if (fresh) renderRun(fresh, { keepDrafts: true });
      }
    } else {
      $("batch-status").textContent = "";
      clearInterval(historyPollTimer);
      historyPollTimer = null;
    }
  }, 5000);
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (!currentRun) return;
  const busy = currentRun.status === "briefing"
    || (currentRun.directions || []).some((item) => item.status === "generating");
  if (busy) pollTimer = setTimeout(refreshRun, 3000);
}

async function refreshRun() {
  if (!currentRun) return;
  try {
    const payload = await parseResponse(await fetch(`/api/brief/${currentRun.id}`));
    renderRun(payload, { keepDrafts: true });
  } catch (error) {
    // 网络抖动继续轮询
  }
  schedulePoll();
}

function statusLabel(direction) {
  if (direction.status === "generating") return '<span class="status running">生成中 <span class="spin">◌</span></span>';
  if (direction.status === "failed") return `<span class="status failed">失败：${escapeHtml(direction.error || "")}</span>`;
  if (direction.status === "done") return '<span class="status complete">已生成</span>';
  return "";
}

function renderRun(payload, options = {}) {
  // 轮询刷新时保留用户正在编辑的提示词草稿
  const drafts = {};
  if (options.keepDrafts && currentRun) {
    document.querySelectorAll("textarea[data-dir]").forEach((area) => {
      drafts[area.dataset.dir] = area.value;
    });
  }
  currentRun = payload;
  currentBatch = null;
  $("batch-view").hidden = true;
  if (!options.keepDrafts && payload.innovation_prompt) {
    $("innovation-prompt").value = payload.innovation_prompt;
  }
  $("empty").hidden = true;
  $("run-view").hidden = false;
  $("run-meta").innerHTML = `
    <span class="chip">${escapeHtml((payload.created_at || "").slice(0, 16).replace("T", " "))}</span>
    <span class="chip">${escapeHtml(payload.source_name || "")}</span>
    ${payload.status === "briefing" ? '<span class="chip">VLM 反推中… <span class="spin">◌</span></span>' : ""}
    ${payload.vlm_model ? `<span class="chip">模型 ${escapeHtml(payload.vlm_model)}</span>` : ""}
    ${payload.fallback ? '<span class="chip" style="background:#fdf3e2;color:#8a6116">兜底模式</span>' : ""}
    ${payload.status === "failed" ? `<span class="chip" style="background:#fde2e2;color:#8a1616">失败：${escapeHtml(payload.error || "")}</span>` : ""}`;
  $("source-img").src = payload.source_url;
  $("source-link").href = payload.source_url;
  $("summary").textContent = payload.summary || (payload.status === "briefing" ? "VLM 正在反推图案要点，约 1 分钟…" : "");
  $("directions").innerHTML = (payload.directions || []).map((direction) => `
    <div class="brief-card direction-card">
      <h3>衍生方向 ${direction.index} ${statusLabel(direction)}</h3>
      <div class="dir-note">${escapeHtml(direction.direction || "")}</div>
      <label style="display:block;margin:8px 0 5px;font-size:12px;font-weight:700">4 内容简报 <small style="color:var(--muted);font-weight:400">该方向独立，可编辑</small></label>
      <textarea data-dir="${direction.index}">${escapeHtml(drafts[direction.index] ?? direction.prompt)}</textarea>
      <div style="display:flex;justify-content:flex-end;margin-top:6px"><button class="ghost prompt-save" data-save-direction="${direction.index}">保存内容简报</button></div>
      <div class="final-prompt-block">
        <div class="final-prompt-head">
          <div><strong>最终合并 Prompt</strong> <small data-preview-meta="${direction.index}">等待组装</small></div>
          <button class="ghost copy-prompt" data-copy-prompt="${direction.index}">复制</button>
        </div>
        <textarea data-final="${direction.index}" readonly></textarea>
        ${direction.last_prompt_url ? `<a href="${escapeHtml(direction.last_prompt_url)}" target="_blank" style="display:inline-block;margin-top:6px;font-size:11px">查看上次实际发送的 Prompt</a>` : ""}
      </div>
      <div class="dir-foot">
        <button class="primary" data-generate="${direction.index}" ${direction.status === "generating" ? "disabled" : ""}>
          ${direction.images?.length ? "再生成一张" : "生成这个方向"}
        </button>
        <small style="color:var(--muted)">严格发送上方显示的五段合并 Prompt · 约 2-4 分钟${direction.config_label ? ` · 上次：${escapeHtml(direction.config_label)}` : ""}</small>
      </div>
      <div class="dir-images">
        ${(direction.image_urls || []).map((url) => `<a href="${escapeHtml(url)}" target="_blank"><img src="${escapeHtml(url)}" loading="lazy"></a>`).join("")}
      </div>
    </div>`).join("");
  $("directions").querySelectorAll("button[data-generate]").forEach((button) =>
    button.addEventListener("click", () => generateDirection(Number(button.dataset.generate))));
  $("directions").querySelectorAll("textarea[data-dir]").forEach((area) =>
    area.addEventListener("input", refreshPromptPreviews));
  $("directions").querySelectorAll("button[data-save-direction]").forEach((button) =>
    button.addEventListener("click", () => saveDirectionPrompt(button)));
  $("directions").querySelectorAll("button[data-copy-prompt]").forEach((button) =>
    button.addEventListener("click", async () => {
      const finalArea = document.querySelector(`textarea[data-final="${button.dataset.copyPrompt}"]`);
      await navigator.clipboard.writeText(finalArea?.value || "");
      const previous = button.textContent;
      button.textContent = "已复制";
      setTimeout(() => { button.textContent = previous; }, 1200);
    }));
  refreshPromptPreviews();
}

async function saveDirectionPrompt(button) {
  const index = Number(button.dataset.saveDirection);
  const area = document.querySelector(`textarea[data-dir="${index}"]`);
  const previous = button.textContent;
  try {
    button.disabled = true;
    button.textContent = "保存中…";
    const payload = await parseResponse(await fetch(`/api/brief/${currentRun.id}/directions/${index}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: area.value }),
    }));
    currentRun = payload;
    button.textContent = "已永久保存";
    setTimeout(() => { button.textContent = previous; }, 1600);
  } catch (error) {
    button.textContent = "保存失败";
    alert(error.message);
    setTimeout(() => { button.textContent = previous; }, 1600);
  } finally {
    button.disabled = false;
  }
}

async function generateDirection(index) {
  const area = document.querySelector(`textarea[data-dir="${index}"]`);
  try {
    const payload = await parseResponse(await fetch(`/api/brief/${currentRun.id}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index, prompt: area.value, ...composeConfig() }),
    }));
    renderRun(payload, { keepDrafts: true });
    schedulePoll();
  } catch (error) {
    alert(error.message);
  }
}

function batchBusy(batch) {
  return ["queued", "running", "pausing", "paused", "cancelling"].includes(batch?.status);
}

function batchStatusText(status) {
  return ({
    queued: "排队中",
    running: "运行中",
    pausing: "等待当前步骤结束后暂停",
    paused: "已暂停",
    cancelling: "等待当前步骤结束后中断",
    cancelled: "已中断",
    completed: "已完成",
    completed_with_errors: "完成，但有失败项",
    interrupted: "被服务重启中断",
  })[status] || status || "未知";
}

function batchItemStatus(item) {
  if (item.status === "queued") return "等待处理";
  if (item.status === "briefing") return "正在反推";
  if (item.status === "failed") return `反推失败：${item.error || "未知错误"}`;
  const generating = (item.directions || []).find((direction) => direction.status === "generating");
  if (generating) return `正在生成方向 ${generating.index}`;
  const failed = (item.directions || []).filter((direction) => direction.status === "failed").length;
  if (failed) return `${failed} 个方向失败`;
  const done = (item.directions || []).filter((direction) => direction.status === "done").length;
  return `${done}/${item.directions?.length || 0} 个方向已生成`;
}

function renderBatch(payload) {
  currentBatch = payload;
  currentRun = null;
  clearTimeout(pollTimer);
  $("empty").hidden = true;
  $("run-view").hidden = true;
  $("batch-view").hidden = false;
  $("batch-title").textContent = `批量任务 · ${payload.total || payload.items?.length || 0} 张输入`;
  $("batch-meta").innerHTML = `
    <span class="chip">${escapeHtml((payload.created_at || "").slice(0, 16).replace("T", " "))}</span>
    <span class="chip">${escapeHtml(batchStatusText(payload.status))}</span>
    <span class="chip">进度 ${payload.completed_items || 0}/${payload.total || 0}</span>
    <span class="chip">失败 ${payload.failed_items || 0}</span>
    <span class="chip">第 ${payload.attempt || 1} 次运行</span>`;
  $("batch-output-path").textContent = `最终图片统一目录：${payload.output_directory || "等待后端返回"}`;
  $("batch-download").href = payload.download_url || "#";

  const terminal = ["completed", "completed_with_errors", "cancelled", "interrupted"].includes(payload.status);
  const paused = ["paused", "pausing"].includes(payload.status);
  document.querySelector('[data-batch-action="pause"]').disabled = terminal || paused;
  document.querySelector('[data-batch-action="resume"]').disabled = terminal || !paused;
  document.querySelector('[data-batch-action="cancel"]').disabled = terminal;
  document.querySelector('[data-batch-action="retry"]').disabled = !terminal;

  $("batch-items").innerHTML = (payload.items || []).map((item, itemIndex) => {
    const directions = item.directions || [];
    // 槽位数跟随该任务实际的方向数，避免"永远画三格"造成还有方向在排队的错觉。
    const slotCount = Math.max(directions.length, 1);
    const slots = Array.from({ length: slotCount }, (_, position) => {
      const index = position + 1;
      const direction = directions.find((entry) => entry.index === index);
      const url = direction?.image_urls?.at(-1);
      if (url) return `<a class="brief-batch-image" href="${escapeHtml(url)}" target="_blank"><img src="${escapeHtml(url)}" loading="lazy"><span>方向 ${index}</span></a>`;
      const text = direction?.status === "failed" ? `方向 ${index} 失败：${direction.error || ""}`
        : direction?.status === "generating" ? `方向 ${index} 生成中…`
        : `方向 ${index} 等待中`;
      return `<div class="brief-batch-image"><span>${escapeHtml(text)}</span></div>`;
    }).join("");
    const modelBadge = item.fallback
      ? `<small style="color:#8a6116" title="${escapeHtml(item.summary || "")}">⚠ 兜底模式（VLM 未生效）</small>`
      : `<small>${escapeHtml(item.vlm_model || "")}</small>`;
    return `<div class="brief-batch-item">
      <div class="brief-batch-source">
        <a href="${escapeHtml(item.source_url)}" target="_blank"><img src="${escapeHtml(item.source_url)}" loading="lazy"></a>
        <strong title="${escapeHtml(item.source_name || "")}">${itemIndex + 1}. ${escapeHtml(item.source_name || item.id)}</strong>
      </div>
      <div>
        <div class="brief-batch-item-head"><strong>${escapeHtml(batchItemStatus(item))}</strong>${modelBadge}</div>
        <div class="brief-batch-images">${slots}</div>
      </div>
    </div>`;
  }).join("");
}

async function controlBatch(action) {
  if (!currentBatch) return;
  try {
    const payload = await parseResponse(await fetch(`/api/brief/batches/${currentBatch.id}/${action}`, { method: "POST" }));
    renderBatch(payload);
    scheduleBatchPoll();
    loadHistory();
  } catch (error) {
    alert(error.message);
  }
}

function scheduleBatchPoll() {
  clearTimeout(batchPollTimer);
  if (batchBusy(currentBatch)) batchPollTimer = setTimeout(refreshBatch, 3000);
}

async function refreshBatch() {
  if (!currentBatch) return;
  try {
    const payload = await parseResponse(await fetch(`/api/brief/batches/${currentBatch.id}`));
    renderBatch(payload);
    if (!batchBusy(payload)) loadHistory();
  } catch (error) {
    // 网络抖动时继续轮询当前批次。
  }
  scheduleBatchPoll();
}

function runStatusBadge(run) {
  if (run.status === "queued") return '<span style="color:#8a6116">排队中</span>';
  if (run.status === "briefing") return '<span style="color:var(--teal-dark)">反推中…</span>';
  if (run.status === "failed") return '<span style="color:var(--coral)">失败</span>';
  if (run.status === "interrupted") return '<span style="color:#8a6116">被重启中断</span>';
  const generating = (run.directions || []).filter((item) => item.status === "generating").length;
  if (generating) return `<span style="color:var(--teal-dark)">生成中(${generating})…</span>`;
  return "";
}

function renderHistory(runs, batches = []) {
  // 批量与单张按创建时间统一倒序，最新的任务永远在最上面。
  const entries = [
    ...batches.map((batch) => ({
      time: batch.created_at || "",
      html: `<div class="history-item" data-batch-id="${escapeHtml(batch.id)}">
        ${batch.items?.[0] ? `<img src="${escapeHtml(batch.items[0].source_url)}" loading="lazy" alt="">` : ""}
        <div>
          <div style="font-size:12px"><strong>批量 · ${batch.total || 0} 张</strong> <span style="color:var(--teal-dark)">${escapeHtml(batchStatusText(batch.status))}</span></div>
          <small>${escapeHtml((batch.created_at || "").slice(0, 16).replace("T", " "))} · ${batch.output_urls?.length || 0} 张最终图</small>
        </div>
      </div>`,
    })),
    ...runs.filter((run) => !run.batch_id).map((run) => ({
      time: run.created_at || "",
      html: `<div class="history-item" data-id="${escapeHtml(run.id)}">
        <img src="${escapeHtml(run.source_url)}" loading="lazy" alt="">
        <div>
          <div style="font-size:12px">单张 · ${escapeHtml(run.source_name || run.id)} ${runStatusBadge(run)}</div>
          <small>${escapeHtml((run.created_at || "").slice(0, 16).replace("T", " "))} · ${run.directions?.length || 0} 方向 · ${(run.directions || []).reduce((total, item) => total + (item.images?.length || 0), 0)} 图</small>
        </div>
      </div>`,
    })),
  ].sort((a, b) => b.time.localeCompare(a.time));
  $("history").innerHTML = entries.map((entry) => entry.html).join("") || '<small style="color:var(--muted)">暂无</small>';
  $("history").querySelectorAll(".history-item[data-batch-id]").forEach((item) => item.addEventListener("click", async () => {
    const payload = await parseResponse(await fetch(`/api/brief/batches/${item.dataset.batchId}`));
    renderBatch(payload);
    scheduleBatchPoll();
  }));
  $("history").querySelectorAll(".history-item").forEach((item) => item.addEventListener("click", async () => {
    if (!item.dataset.id) return;
    const payload = await parseResponse(await fetch(`/api/brief/${item.dataset.id}`));
    renderRun(payload);
    schedulePoll();
  }));
}

async function loadHistory() {
  const [runs, batches] = await Promise.all([
    fetchList("/api/brief"),
    fetchList("/api/brief/batches"),
  ]);
  renderHistory(runs, batches);
  if (batches.some(batchBusy) || runs.some((run) => !run.batch_id && runBusy(run))) startHistoryPolling();
}

$("run").addEventListener("click", startRun);
$("file").addEventListener("change", () => {
  const file = $("file").files[0];
  if (file) $("dropzone").querySelector("strong").textContent = file.name;
});
$("prompt-style").addEventListener("change", () => {
  $("layout-prompt").value = briefLayouts[$("prompt-style").value]?.text || "";
  refreshPromptPreviews();
});
$("window-frame").addEventListener("change", () => {
  $("frame-prompt").value = briefFrames[$("window-frame").value]?.prompt_constraint || "";
  refreshPromptPreviews();
});
$("canvas-ratio").addEventListener("change", refreshPromptPreviews);
$("core-prompt").addEventListener("input", refreshPromptPreviews);
$("product-prompt").addEventListener("input", refreshPromptPreviews);
$("layout-prompt").addEventListener("input", refreshPromptPreviews);
$("frame-prompt").addEventListener("input", refreshPromptPreviews);
document.querySelectorAll("button[data-save-prompt]").forEach((button) =>
  button.addEventListener("click", () => saveGlobalPrompt(button)));
document.querySelectorAll("button[data-batch-action]").forEach((button) =>
  button.addEventListener("click", () => controlBatch(button.dataset.batchAction)));
loadComposeOptions();
loadHistory();
