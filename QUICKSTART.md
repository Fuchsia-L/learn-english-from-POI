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
python scripts/extract_hardsub.py 第一集.mp4 \
  -o data/ep01.en.srt --boxes-json data/ep01.boxes.json
```

默认 `--crop 1920:54:0:1026` 对准 bilibili 正版 POI 1080p；其他片源截帧调 W:H:X:Y。

**srt 和 boxes 一律写到 `data/`（已被忽略）或仓库外**：`.boxes.json` 里存的是
每个词的坐标 + 完整字幕文本，性质等同字幕原文；仓库 Public 期间字幕/媒体一概
不入库（DESIGN §0）。`.gitignore` 已挡 `*.srt` / `*.boxes.json` / `data/`，但
换个后缀名放到仓库根目录照样会被 `git add .` 收进去，别自己绕过去。

## 3. 入库

```bash
python -m app.ingest data/ep01.en.srt --title POI --season-ep s01e01 \
  --video 第一集.mp4 --db data/poi.db --boxes-json data/ep01.boxes.json
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

# 接真模型：塞 key 即切（DeepSeek deepseek-v4-flash，约 ¥0.004/词，按峰时价保守估）
set DEEPSEEK_API_KEY=sk-...        # PowerShell: $env:DEEPSEEK_API_KEY="sk-..."
python -m app.annotate --db data/poi.db --provider deepseek --once --budget 4

# 接真 API 前先看会发什么、花多少：
python -m app.annotate --db data/poi.db --provider deepseek --once --dry-run
```

提示词在 `app/providers/prompts.py`，想调助记口味只改这一个文件。

预热（可选）：先建 CET4+CET6 词表，再把当集命中的词按词频降序低优先级入队——
点击收藏的词永远插在它们前面。

```bash
python scripts/build_cet46.py                                   # → data/cet46.txt（约 5.6k 词）
python scripts/prefetch.py --db data/poi.db --content-id 1 --limit 200
```

## 6. 复习（M1 后端，界面还没接）

```bash
curl 'http://127.0.0.1:8000/review/next?limit=20'                       # 今日待复习卡
curl -X POST http://127.0.0.1:8000/review/answer -H 'content-type: application/json' \
     -d '{"vocab_entry_id": 1, "result": "know"}'                       # know / dont
curl http://127.0.0.1:8000/review/stats                                 # 今日已复习/待复习/毕业
```

规则：最近 7 天收藏的词 + 答错过还没毕业的词进队；连续 2 次 `know` 且最后一次复习
距首次收藏 ≥3 天就毕业、不再出现；同一天重复提交同一答案幂等。改规则改
`app/review.py` 顶部的常量即可。

## 故障排查

- 播放器提示库里没内容 → 第 3 步没跑或 --db 路径不一致（服务端读 POI_DB，默认 data/poi.db）。
- 点词无反应 → 看状态栏是否"词框 0 段"：入库时漏了 --boxes-json，会自动走自渲染退路。
- 音标/释义空 → data/ecdict.db 没构建，或 POI_ECDICT 指错路径。
