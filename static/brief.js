const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

let currentRun = null;
let pollTimer = null;
let briefLayouts = {};
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
    canvasRatios = Object.fromEntries((defaults.canvas_ratios || []).map((item) => [item.id, item]));
    const promptDefaults = defaults.brief_prompt_defaults || {};
    $("innovation-prompt").value = defaults.brief_innovation_prompt;
    $("prompt-style").innerHTML = (defaults.brief_prompt_styles || []).map((item) =>
      `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("");
    $("canvas-ratio").innerHTML = (defaults.canvas_ratios || []).map((item) =>
      `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("");
    $("core-prompt").value = promptDefaults.core_prompt || "";
    $("product-prompt").value = promptDefaults.product_prompt || "";
    $("prompt-style").value = promptDefaults.prompt_style || "large_elements";
    $("layout-prompt").value = promptDefaults.layout_prompt || briefLayouts[$("prompt-style").value]?.text || "";
    $("canvas-ratio").value = promptDefaults.canvas_id || defaults.brief_default_canvas_id || "1:1";
    await refreshPromptPreviews();
  } catch (error) {
    $("compose-info").style.color = "var(--coral)";
    $("compose-info").textContent = `Prompt 加载失败：${error.message}`;
  }
}

function renderComposeInfo(spec = null) {
  const canvas = canvasRatios[$("canvas-ratio").value];
  if (canvas) {
    $("compose-info").textContent = `${canvas.label} · 生成画布 ${canvas.generation_size}`;
  }
  if (spec?.prompt_constraint) $("spec-prompt").value = spec.prompt_constraint;
}

function composeConfig() {
  return {
    prompt_style: $("prompt-style").value,
    canvas_id: $("canvas-ratio").value,
    core_prompt: $("core-prompt").value,
    product_prompt: $("product-prompt").value,
    layout_prompt: $("layout-prompt").value,
  };
}

const promptFieldElements = {
  innovation_prompt: "innovation-prompt",
  core_prompt: "core-prompt",
  product_prompt: "product-prompt",
  layout_prompt: "layout-prompt",
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
      }),
    }));
    if (field === "layout_prompt") {
      briefLayouts[$("prompt-style").value].text = element.value.trim();
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
      $("batch-status").textContent = `批量已排队：${payload.total} 张，串行处理中（每张约 ${$("auto-generate").checked ? "8-15" : "1"} 分钟），结果见下方最近实验。`;
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
    const runs = await fetch("/api/brief").then((response) => response.json()).catch(() => []);
    renderHistory(runs);
    const busyCount = runs.filter(runBusy).length;
    if (busyCount) {
      $("batch-status").textContent = `队列进行中：${busyCount} 个任务未完成…`;
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

function runStatusBadge(run) {
  if (run.status === "queued") return '<span style="color:#8a6116">排队中</span>';
  if (run.status === "briefing") return '<span style="color:var(--teal-dark)">反推中…</span>';
  if (run.status === "failed") return '<span style="color:var(--coral)">失败</span>';
  const generating = (run.directions || []).filter((item) => item.status === "generating").length;
  if (generating) return `<span style="color:var(--teal-dark)">生成中(${generating})…</span>`;
  return "";
}

function renderHistory(runs) {
  $("history").innerHTML = runs.length ? runs.map((run) => `
    <div class="history-item" data-id="${escapeHtml(run.id)}">
      <img src="${escapeHtml(run.source_url)}" loading="lazy" alt="">
      <div>
        <div style="font-size:12px">${escapeHtml(run.source_name || run.id)} ${runStatusBadge(run)}</div>
        <small>${escapeHtml((run.created_at || "").slice(0, 16).replace("T", " "))} · ${run.directions?.length || 0} 方向 · ${(run.directions || []).reduce((total, item) => total + (item.images?.length || 0), 0)} 图</small>
      </div>
    </div>`).join("") : '<small style="color:var(--muted)">暂无</small>';
  $("history").querySelectorAll(".history-item").forEach((item) => item.addEventListener("click", async () => {
    const payload = await parseResponse(await fetch(`/api/brief/${item.dataset.id}`));
    renderRun(payload);
    schedulePoll();
  }));
}

async function loadHistory() {
  const runs = await fetch("/api/brief").then((response) => response.json()).catch(() => []);
  renderHistory(runs);
  if (runs.some(runBusy)) startHistoryPolling();
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
$("canvas-ratio").addEventListener("change", refreshPromptPreviews);
$("core-prompt").addEventListener("input", refreshPromptPreviews);
$("product-prompt").addEventListener("input", refreshPromptPreviews);
$("layout-prompt").addEventListener("input", refreshPromptPreviews);
document.querySelectorAll("button[data-save-prompt]").forEach((button) =>
  button.addEventListener("click", () => saveGlobalPrompt(button)));
loadComposeOptions();
loadHistory();
