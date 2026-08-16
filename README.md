# learn-english-from-POI

用《疑犯追踪》(Person of Interest) 做个人化背单词的流水线。方法论:看剧是主活动,查词、词元归一、助记、复习压进最短旁路。

## 现状

- [x] 硬字幕 OCR 提取 (`scripts/extract_hardsub.py`) — 已在 2 分钟 1080p 片段上验证,词级准确率 ~99.7%,全程本地零成本
- [x] 字幕解析 / 分词 / 词元归一 (`app/ingest.py` + `app/db.py`) — srt → SQLite，token 小写归一、simplemma 词元、不排专名
- [x] 本地词典 (`scripts/build_ecdict.py`) — ECDICT 77 万词条 → `data/ecdict.db`（音标/中文释义/考试标签/词形变换）
- [x] 可点击字幕的本地播放器 (`app/static/player.html`) — 字幕三档 + OCR 词框热区 + 查询卡 + 生词本侧栏
- [ ] AI 助记异步生成 (annotate)
- [ ] 生词本与复习

核心原则:LLM 只产语义,代码管装配和钱;词元(lexeme)为主键,encounter 记录每次真实语境;按需生产,看到哪集造到哪集。

## ingest：字幕入库

```bash
pip install -r requirements.txt          # 系统 pip 需加 --break-system-packages
python scripts/build_ecdict.py           # 克隆 ECDICT → data/ecdict.db（约 190MB 临时占用，用完自删）
python -m app.ingest episode.en.srt --title "Person of Interest" \
    --season-ep s01e01 --video episode.mp4 --db data/poi.db
# 顺带回填 OCR 词框（播放器热区）；不加这个参数不会清掉已回填的词框
python -m app.ingest episode.en.srt --title "Person of Interest" \
    --season-ep s01e01 --boxes-json episode.boxes.json
pytest                                   # 全部夹具为自造句子，不含真实台词
pytest -m "not slow"                     # 跳过需要 tesseract / ffmpeg+浏览器的端到端用例
```

`ingest` 幂等：同一 `(title, season_ep)` 重复跑只更新不新增。每段字幕的 token 化结果
（`[{surface, lemma, char_start, char_end}]`，char 偏移相对 `text_en`）存在
`Segment.tokens_json`，供 `GET /segments` 直接吐给前端渲染点击热区。

`--boxes-json` 吃 `extract_hardsub.py --boxes-json` 的产物，按 `idx`（= srt 序号 = `Segment.idx`）
把 `words` 数组**原样**存进 `Segment.word_boxes_json`，不做坐标变换（缩放交给前端）。
同样幂等（覆盖写）。idx 对不上、条目结构不合法、boxes.text 与库里 text_en 不一致
一律只在 stderr 告警、不中断——OCR 产物和 srt 可能不是同一批跑出来的。

## server：本地服务

```bash
POI_DB=data/poi.db POI_ECDICT=data/ecdict.db uvicorn app.server:app --port 8000
python -m app.server --db data/poi.db --ecdict data/ecdict.db --port 8000   # 等价 CLI
```

端点见 DESIGN §3：`/episodes`、`/media/{content_id}`（HTTP Range，拖进度条用）、
`/segments?content_id=`、`/lookup?surface=&segment_id=`、`POST /collect`、`/vocab`、
`/mnemonic?lexeme_id=`；`/` 重定向到 `/static/player.html`。全程本地 SQLite，无网络调用；
`data/ecdict.db` 缺失时 `/lookup` 降级为 `in_dict=false` 而不报错。

## player：单文件播放器

浏览器打开 `http://127.0.0.1:8000/`（等价 `/static/player.html`）。整个前端就
`app/static/player.html` 一个文件：HTML+CSS+JS 全内联，无外部依赖、无 CDN、
**不使用任何浏览器存储 API**（设置只活在 JS 变量里）。视觉是黑客终端 × POI 机器风。

- **字幕三档**（快捷键 `1` / `2` / `3`，默认档 2）：
  1. 双语：不遮挡，烧录英文行上叠透明热区；
  2. 只遮中文：磨黑遮罩条只盖中文行（1080p 参考帧 `y∈[958,1026]`），英文原位可点；
  3. 裸听：中英全遮（`y∈[958,1080]`），无热区。
- **热区**：词框来自 `Segment.word_boxes_json`，按 `<video>` 实际显示区域相对
  1920x1080 参考帧缩放（含 letterbox 偏移）映射成透明 `<div>`；hover 描白框，
  点击 → `GET /lookup` → 查询卡（当前形式/词元/音标/释义/原句/[加入生词本]）。
- **对齐退路**（DESIGN §4）：某段没有词框（或大面积丢框）时，档 2 自动改为
  中英全遮 + 用 `tokens_json` 在遮罩下沿自渲染一行可点击英文字幕；
  档 1 按 DESIGN 降级为仅展示不可点。个别词丢框只是那个词不可点，其余照常。
- 其他快捷键：`v` 生词本侧栏，`Esc` 关卡/关侧栏，`空格` 播放暂停，`←/→` ±5 秒。
- 三档坐标、退路阈值等都在 `player.html` 顶部的 `CONF` 常量里，换片源改那儿。

端到端用例见 `tests/test_player_e2e.py`（playwright + chromium，标记 `slow`）：
夹具全自造（自编 srt → ingest → 手编词框 → ffmpeg lavfi 黑场视频）。

```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers playwright install chromium   # 首次
pytest tests/test_player_e2e.py
```

## extract_hardsub.py

```bash
pip install pillow numpy wordfreq
# 另需 ffmpeg 和 tesseract-ocr (eng)
python scripts/extract_hardsub.py episode.mp4 -o episode.en.srt
# 顺带导出词级包围盒(播放器透明点击热区,DESIGN §4)
python scripts/extract_hardsub.py episode.mp4 -o episode.en.srt \
    --boxes-json episode.boxes.json
```

默认 `--crop 1920:54:0:1026` 对准 bilibili 正版 POI 1080p 双语硬字幕的英文行;其他片源截一帧带字幕的画面调整 W:H:X:Y 即可。

`--boxes-json` 输出 `[{idx, start, end, text, words:[{w, x, y, width, height}]}]`:
`idx` 与 srt 序号(1 起)一一对应,`w` 是清洗后的词(标点随词),坐标为**视频原始帧**整数像素
(已还原 2x 放大并加回 crop 偏移)。tesseract 置信度过低或框异常时该词 `x=null`
——丢框不丢词,前端跳过该词热区。加不加 `--boxes-json`,srt 输出逐字节一致。

流程:ffmpeg 按 3fps 裁剪采样英文字幕带 → 掩码 IoU 合并同句连续帧 → 每段取一帧跑 tesseract → 确定性清洗(| → I 等)+ 垃圾过滤(街灯/监控画面渗入误检)→ 输出带时间戳的 srt。

## 版权说明

仓库为 Public 期间,字幕文本、srt、媒体文件一律不入库(见 .gitignore),仅存代码。
