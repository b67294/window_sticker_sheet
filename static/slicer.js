const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

let currentRun = null;
let currentResize = null;

function ratioInputs() {
  const width = parseFloat($("ratio-w").value);
  const height = parseFloat($("ratio-h").value);
  if (!(width > 0) || !(height > 0)) throw new Error("请填写有效的宽高比（两个正数）");
  const columns = parseInt($("force-columns").value, 10) || null;
  const rows = parseInt($("force-rows").value, 10) || null;
  return { width, height, columns, rows };
}

async function runSlice() {
  const file = $("file").files[0];
  try {
    const ratio = ratioInputs();
    $("run").disabled = true;
    $("run").textContent = "切块中…";
    let payload;
    if (currentRun && !file) {
      const response = await fetch(`/api/slicer/${currentRun.id}/reslice`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ratio_width: ratio.width, ratio_height: ratio.height, columns: ratio.columns, rows: ratio.rows }),
      });
      payload = await parseResponse(response);
    } else {
      if (!file) throw new Error("请先选择图片");
      const form = new FormData();
      form.append("file", file);
      form.append("ratio_width", ratio.width);
      form.append("ratio_height", ratio.height);
      if (ratio.columns) form.append("columns", ratio.columns);
      if (ratio.rows) form.append("rows", ratio.rows);
      const response = await fetch("/api/slicer", { method: "POST", body: form });
      payload = await parseResponse(response);
      $("file").value = "";
      $("file-name").textContent = `已切换到任务 ${payload.id}（不选新文件时，再次点击切块会对同一张图重切）`;
    }
    renderResult(payload);
    loadHistory();
  } catch (error) {
    alert(error.message);
  } finally {
    $("run").disabled = false;
    $("run").textContent = "切块 →";
  }
}

async function parseResponse(response) {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || "请求失败");
  }
  return response.json();
}

function renderResult(payload) {
  currentRun = payload;
  $("empty").hidden = true;
  $("result").hidden = false;
  const plan = payload.plan;
  const lossPercent = (plan.crop_loss * 100).toFixed(1);
  $("plan-meta").innerHTML = `
    <span class="chip">原图 ${payload.source_size[0]}×${payload.source_size[1]} px</span>
    <span class="chip">目标比例 ${payload.ratio[0]} : ${payload.ratio[1]}</span>
    <span class="chip"><b>${plan.columns} 列 × ${plan.rows} 行 · 共 ${plan.tile_count} 块</b></span>
    <span class="chip">每块 ${plan.tile_width}×${plan.tile_height} px</span>
    <span class="chip ${plan.crop_loss > 0.05 ? "warn" : ""}">居中裁掉 ${lossPercent}%</span>
    ${escapeHtml(payload.source_name || "")}`;
  $("preview").src = `${payload.preview_url}?t=${Date.now()}`;
  $("download").href = payload.download_url;
  $("download-pdf").href = payload.pdf_url || "#";
  $("download-pdf").classList.toggle("disabled", !payload.pdf_url);
  $("tiles").innerHTML = (payload.tiles || []).map((tile) => `
    <a class="tile-item" href="${escapeHtml(tile.url)}" target="_blank" download>
      <img src="${escapeHtml(tile.url)}?t=${Date.now()}" loading="lazy" alt="${escapeHtml(tile.name)}">
      <small>第 ${tile.row} 行 · 第 ${tile.column} 列</small>
    </a>`).join("");
  $("alternatives").innerHTML = (payload.alternatives || []).map((alt) => `
    <div class="alt-plan" data-cols="${alt.columns}" data-rows="${alt.rows}">
      <span><b>${alt.columns} 列 × ${alt.rows} 行</b>（${alt.tile_count} 块，每块 ${alt.tile_width}×${alt.tile_height} px）</span>
      <span>裁掉 ${(alt.crop_loss * 100).toFixed(1)}%</span>
    </div>`).join("");
  $("alternatives").querySelectorAll(".alt-plan").forEach((item) => item.addEventListener("click", async () => {
    $("force-columns").value = item.dataset.cols;
    $("force-rows").value = item.dataset.rows;
    await runSlice();
    $("force-columns").value = "";
    $("force-rows").value = "";
  }));
}

async function loadHistory() {
  const runs = await fetch("/api/slicer").then((response) => response.json()).catch(() => []);
  $("history").innerHTML = runs.length ? runs.map((run) => `
    <div class="history-item" data-id="${escapeHtml(run.id)}">
      <img src="${escapeHtml(run.preview_url)}" loading="lazy" alt="">
      <div>
        <div style="font-size:12px">${run.plan.columns}×${run.plan.rows} · ${escapeHtml(run.source_name || run.id)}</div>
        <small>${escapeHtml((run.created_at || "").slice(0, 16).replace("T", " "))} · 比例 ${run.ratio[0]}:${run.ratio[1]}</small>
      </div>
    </div>`).join("") : '<small style="color:var(--muted)">暂无</small>';
  $("history").querySelectorAll(".history-item").forEach((item) => item.addEventListener("click", async () => {
    const run = runs.find((entry) => entry.id === item.dataset.id);
    if (run) {
      renderResult(run);
      $("ratio-w").value = run.ratio[0];
      $("ratio-h").value = run.ratio[1];
      $("file-name").textContent = `已载入任务 ${run.id}（不选新文件时点"切块"即对这张图重切）`;
    }
  }));
}

function resizeRatio() {
  const template = $("resize-template").value;
  if (template === "custom") {
    const width = parseFloat($("resize-ratio-w").value);
    const height = parseFloat($("resize-ratio-h").value);
    if (!(width > 0) || !(height > 0)) throw new Error("请填写有效的自定义宽高比");
    return { width, height };
  }
  const [width, height] = template.split("x").map(Number);
  return { width, height };
}

async function runResize() {
  const file = $("resize-file").files[0];
  try {
    if (!file) throw new Error("请先选择要 Resize 的图片");
    const ratio = resizeRatio();
    $("resize-run").disabled = true;
    $("resize-run").textContent = "处理中…";
    const form = new FormData();
    form.append("file", file);
    form.append("ratio_width", ratio.width);
    form.append("ratio_height", ratio.height);
    form.append("mode", $("resize-mode").value);
    const response = await fetch("/api/resize", { method: "POST", body: form });
    const payload = await parseResponse(response);
    $("resize-result").hidden = false;
    $("empty").hidden = true;
    $("resize-meta").innerHTML = `
      <span class="chip">原图 ${payload.source_size[0]}×${payload.source_size[1]} px</span>
      <span class="chip">目标比例 宽${payload.ratio[0]} : 高${payload.ratio[1]}</span>
      <span class="chip"><b>输出 ${payload.output_size[0]}×${payload.output_size[1]} px</b></span>
      <span class="chip">${payload.mode === "stretch" ? "拉伸" : "居中裁剪"}</span>
      ${escapeHtml(payload.source_name || "")}`;
    $("resize-preview").src = `${payload.output_url}?t=${Date.now()}`;
    $("resize-download").href = payload.output_url;
    currentResize = payload;
  } catch (error) {
    alert(error.message);
  } finally {
    $("resize-run").disabled = false;
    $("resize-run").textContent = "Resize →";
  }
}

$("file").addEventListener("change", () => {
  const file = $("file").files[0];
  if (file) {
    currentRun = null;
    $("file-name").textContent = `待切块：${file.name}`;
  }
});
$("run").addEventListener("click", runSlice);
$("resize-file").addEventListener("change", () => {
  const file = $("resize-file").files[0];
  if (file) $("resize-file-name").textContent = `待处理：${file.name}`;
});
$("resize-template").addEventListener("change", () => {
  $("resize-custom-ratio").hidden = $("resize-template").value !== "custom";
});
$("resize-run").addEventListener("click", runResize);
$("resize-to-slice").addEventListener("click", async () => {
  const button = $("resize-to-slice");
  try {
    if (!currentResize) throw new Error("请先完成一次 Resize");
    const ratio = ratioInputs();
    button.disabled = true;
    button.textContent = "切块中…";
    const response = await fetch(`/api/resize/${currentResize.id}/slice`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ratio_width: ratio.width, ratio_height: ratio.height, columns: ratio.columns, rows: ratio.rows }),
    });
    const payload = await parseResponse(response);
    renderResult(payload);
    $("file-name").textContent = `已载入任务 ${payload.id}（来源：Resize 结果）`;
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
    loadHistory();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "把结果送去切块 →";
  }
});
loadHistory();
