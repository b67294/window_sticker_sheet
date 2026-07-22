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

## 生图配置

“透明 PNG”和“直接上传纯色母版”不需要任何服务配置。“电商原图”模式需要在启动前设置：

```powershell
$env:LP_AI_BASE_URL="https://your-host/v1/chat/completions"
$env:LP_AI_TOKEN="your-token"
```

可选变量见 `.env.example`。Token 只由后端读取，不会返回给浏览器或写入任务文件。

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
