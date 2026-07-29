# 窗贴 Sheet Workbench

本地调试型 Web 工具，用于把透明底或白底窗贴母版转换为独立组件、裁切轮廓和四套候选 Sheet。电商原图会先通过现有 `gpt-image-2` 接口生成同系列强衍生创新白底母版，再经 ComfyUI 去背景并完成候选排版。

## 安装

```powershell
cd F:\Longpean-AIGC\19-脚本代码\window_sticker_sheet_workbench
C:\Users\melonedoe\miniconda3\python.exe -m pip install -r requirements.txt
```

## 启动

双击 `start.bat`，或者：

```powershell
.\start.ps1
```

打开 <http://127.0.0.1:8790>。

## 生图与语义分组配置

推荐使用统一的 OpenAI 兼容接口：同一个 endpoint 和 client key 同时供 `gpt-image-2` 生图及 `codex-gpt-5.6-luna` 语义分组使用。图片以 data URL 内联，不需要先上传图云：

```powershell
$env:LP_IMAGE_PROVIDER="chat_compat"
$env:LP_COMPAT_BASE_URL="https://test-plugin.longpean.com/v1/chat/completions"
$env:LP_COMPAT_TOKEN="your-client-key"
$env:LP_IMAGE_SIZE="1056x1408" # 对应默认 450×600 mm 的 3:4 竖版母版
```

视觉语义分组默认复用上述 key，模型可单独设置：

```powershell
$env:LP_VISION_MODEL="codex-gpt-5.6-luna"
```

也可以写入项目根目录中已被 Git 忽略的 `.env`。可选变量见 `.env.example`。Token 只由后端读取，不会返回给浏览器或写入任务文件。

备用的 `LP_IMAGE_PROVIDER=direct` 会调用 `/gptImage/generateImageDirect`，但它只接受远端 HTTP 参考图，必须先上传 Longpean 图云；统一兼容接口更适合本地 Workbench。

如果兼容接口的 `gpt-image-2` 响应返回 `192.168.x.x` 等本机不可达的内网图片 URL，需暂时切换到 `direct`。这只影响生图链路；语义分组仍使用 `LP_COMPAT_BASE_URL` 和同一个授权码。根治方式是让网关将内部文件地址改写为公网 URL，或由网关代理图片下载。

`gpt-image-2` 插件不提供原生 Alpha；系统要求模型输出纯白背景。生成结果上传到 `LP_IMAGE_UPLOAD_URL`，再用 `comfyui/BiRefNet.json` 去背景。工作流里的 `#{image}` 会替换为七牛云返回 URL，透明结果通过 ComfyUI `/history` 与 `/view` 下载。

ComfyUI 默认配置：

```powershell
$env:LP_COMFYUI_BASE_URL="http://127.0.0.1:6070"
$env:LP_COMFYUI_WORKFLOW="" # 留空使用 comfyui/BiRefNet.json
$env:LP_COMFYUI_TIMEOUT_SECONDS="600"
```

## 调试流程

1. 上传电商图、透明底母版或白底母版。电商原图可一次多选；透明底入口会完整保留原始软 Alpha，其他入口使用 ComfyUI 去背景。
2. 选择整套窗贴的推荐铺贴规格，调整 Sheet 尺寸和生产间距。
3. 运行到去背景、组件、轮廓或完整排版。
4. 在“组件”步骤点击覆盖框，多选后合并、取消分组、删除，或设置方向与填缝复制。
5. 分组修改后从“轮廓”继续运行，无需重新生图或去背景。
6. 比较整齐行列、MaxRects 紧凑、异形填缝和中心紧凑四套候选。电商原图自动采用 `candidate.score` 最高方案生成最终成品；其他输入仍采用优先减少页数和缩放的综合生产排名。新任务默认保留 10 mm 纸张边缘安全区。

任务保存在 `runs/<job-id>/`。ZIP 包含输入、母版、蒙版、组件、轮廓、四候选、最终 300 DPI 透明 PNG、白底 JPG、布局 JSON 和日志。

## 电商图批量生产

- 切换到“电商原图”后可选择多张图片；两张以上时按钮会变为“批量创新并生成候选（N张）”。
- 整个批次共享当前文本框中的强衍生 Prompt 和物理参数，并按文件逐张串行运行，避免同时占用多路生图与 ComfyUI。
- 单张失败不会阻止后续图片。终态为部分成功、失败或服务重启中断时，可点击“重试失败项”，已完成子任务会跳过。
- 批次记录保存在 `runs/_batches/<batch-id>/batch.json`。批量 ZIP 只汇总每张成功图片的创新白底母版、最高分最终透明/白底 Sheet、PDF、选中布局和评分摘要；四套候选仍保留在各自 job 中供检查。

## 推荐铺贴规格与真实尺寸

- 页面中的宽高表示整套窗贴建议铺开后的范围，不代表参考电商图里窗户的真实尺寸。
- 默认使用小号 `450×600 mm`，也可整批选择标准 `600×800 mm`、大号 `750×1000 mm` 或自定义；三档均为 3:4。
- 同一批次共享一个推荐铺贴规格。程序检测有效图案的整体外接范围，忽略来源图片不稳定的外围白边，再按默认 `85%` 内容占比计算各组件的真实毫米尺寸。
- 每个任务在 `geometry/physical-scale.json` 保存推荐铺贴范围、有效内容范围、毫米/像素换算值和每个组件的真实尺寸，供后续窗户示意图按比例合成。

## 批量产品交付包

- 电商图批量设置默认勾选“生成交付PDF”；关闭后仍会生成300 DPI透明PNG，批次完成后可补生成PDF，无需重新生图、去背景、分组或排版。
- 产品交付包按文件类型整理为 `PNG/`、`PDF/`、`manifest.csv` 和 `manifest.json`。PNG只使用最高分候选，不包含安全区、占用轮廓或其他辅助线；一款多Sheet时按 `p01`、`p02` 命名。
- 每款只交付一个多页PDF。PDF失败不会将已完成的PNG任务标记为失败，可在批次页面单独重试。
- “下载产品交付包”提供精简生产文件；“完整归档”继续包含创新母版、四候选、布局数据和调试产物。

## MVP 限制

- 像素已经接触或重叠的两个对象不会自动拆开，需要重新生成更干净的母版。
- 不包含 OCR、字体重排、白墨层、刀机格式和工业级 No-Fit Polygon。
- 当前排版是离散 90° 旋转、简化多边形碰撞和启发式候选搜索。

## 测试

```powershell
C:\Users\melonedoe\miniconda3\python.exe -m pytest -q
```
