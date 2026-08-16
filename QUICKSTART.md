# QUICKSTART — 在自己电脑上跑起来

从零到"打开浏览器看剧点词"的最短路径。

## 0. 前置依赖

- Python 3.11+
- ffmpeg（硬字幕 OCR 用；Windows: `winget install ffmpeg`）
- tesseract-ocr 含英文数据（OCR 用；Windows 装 UB-Mannheim 构建版并加进 PATH）
- git

只想看播放器不跑 OCR 的话，ffmpeg/tesseract 可以先不装。

## 1. 安装

```bash
git clone https://github.com/Fuchsia-L/learn-english-from-POI.git
cd learn-english-from-POI
pip install -r requirements.txt
python scripts/build_ecdict.py        # 构建本地词典 data/ecdict.db（77 万词条，~15s）
```

## 2. 从硬字幕片源制作字幕（每集一次）

```bash
python scripts/extract_hardsub.py 第一集.mp4 -o ep01.en.srt --boxes-json ep01.boxes.json
```

默认 `--crop 1920:54:0:1026` 对准 bilibili 正版 POI 1080p；其他片源截帧调 W:H:X:Y。

## 3. 入库

```bash
python -m app.ingest ep01.en.srt --title POI --season-ep s01e01 \
  --video 第一集.mp4 --db data/poi.db --boxes-json ep01.boxes.json
```

`--video` 给视频绝对路径最稳；视频不拷贝、不上传，播放时由本地服务流式读。

## 4. 看剧

```bash
python -m uvicorn app.server:app --port 8000
```

浏览器开 http://127.0.0.1:8000 。快捷键：`1/2/3` 字幕三档（双语 / 遮中文 / 裸听），
`v` 生词本，空格播放暂停。点字幕里的英文词 → 查询卡 → 加入生词本。

## 5. 助记（M1，可选）

```bash
# 先用假 provider 看流程（不花钱）
python -m app.annotate --db data/poi.db --provider fake --once

# 接真模型：塞 key 即切（DeepSeek 约 ¥0.0035/词）
set DEEPSEEK_API_KEY=sk-...        # PowerShell: $env:DEEPSEEK_API_KEY="sk-..."
python -m app.annotate --db data/poi.db --provider deepseek --once --budget 4

# 接真 API 前先看会发什么、花多少：
python -m app.annotate --db data/poi.db --provider deepseek --once --dry-run
```

提示词在 `app/providers/prompts.py`，想调助记口味只改这一个文件。

## 故障排查

- 播放器提示库里没内容 → 第 3 步没跑或 --db 路径不一致（服务端读 POI_DB，默认 data/poi.db）。
- 点词无反应 → 看状态栏是否"词框 0 段"：入库时漏了 --boxes-json，会自动走自渲染退路。
- 音标/释义空 → data/ecdict.db 没构建，或 POI_ECDICT 指错路径。
