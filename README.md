# 窗贴 Sheet Workbench

本地调试型 Web 原型，用于把透明底或纯色底窗贴母版转换为独立组件、裁切轮廓和四套候选 Sheet。也可以上传电商原图，通过公司内部 `gpt-image-2` 兼容接口先重建纯绿底母版。

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
```

视觉语义分组默认复用上述 key，模型可单独设置：

```powershell
$env:LP_VISION_MODEL="codex-gpt-5.6-luna"
```

也可以写入项目根目录中已被 Git 忽略的 `.env`。可选变量见 `.env.example`。Token 只由后端读取，不会返回给浏览器或写入任务文件。

备用的 `LP_IMAGE_PROVIDER=direct` 会调用 `/gptImage/generateImageDirect`，但它只接受远端 HTTP 参考图，必须先上传 Longpean 图云；统一兼容接口更适合本地 Workbench。

如果兼容接口的 `gpt-image-2` 响应返回 `192.168.x.x` 等本机不可达的内网图片 URL，需暂时切换到 `direct`。这只影响生图链路；语义分组仍使用 `LP_COMPAT_BASE_URL` 和同一个授权码。根治方式是让网关将内部文件地址改写为公网 URL，或由网关代理图片下载。

`gpt-image-2` 插件不提供原生 Alpha；系统要求模型输出纯 `#ff00ff` 背景，再由本地色键生成透明 PNG。图片接口实际输出受约 1.57M 像素预算限制，配置 2K/4K 仍可能被服务端降采样。

## 调试流程

1. 上传电商图、透明底母版或纯色母版。透明底入口会完整保留原始软 Alpha，跳过色键。
2. 调整安装尺寸、Sheet 尺寸、色键阈值和生产间距。
3. 运行到色键、组件、轮廓或完整排版。
4. 在“组件”步骤点击覆盖框，多选后合并、取消分组、删除，或设置方向与填缝复制。
5. 分组修改后从“轮廓”继续运行，无需重新生图和色键。
6. 比较四套候选的页数、利用率、平衡分和总分，选择最终方案。

任务保存在 `runs/<job-id>/`。ZIP 包含输入、母版、蒙版、组件、轮廓、四候选、最终 300 DPI 透明 PNG、白底 JPG、布局 JSON 和日志。

## MVP 限制

- 像素已经接触或重叠的两个对象不会自动拆开，需要重新生成更干净的母版。
- 不包含 OCR、字体重排、白墨层、刀机格式和工业级 No-Fit Polygon。
- 当前排版是离散 90° 旋转、简化多边形碰撞和启发式候选搜索。

## 测试

```powershell
C:\Users\melonedoe\miniconda3\python.exe -m pytest -q
```
