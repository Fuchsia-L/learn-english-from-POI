# learn-english-from-POI

用《疑犯追踪》(Person of Interest) 做个人化背单词的流水线。方法论:看剧是主活动,查词、词元归一、助记、复习压进最短旁路。

## 现状

- [x] 硬字幕 OCR 提取 (`scripts/extract_hardsub.py`) — 已在 2 分钟 1080p 片段上验证,词级准确率 ~99.7%,全程本地零成本
- [x] 字幕解析 / 分词 / 词元归一 (`app/ingest.py` + `app/db.py`) — srt → SQLite，token 小写归一、simplemma 词元、不排专名
- [x] 本地词典 (`scripts/build_ecdict.py`) — ECDICT 77 万词条 → `data/ecdict.db`（音标/中文释义/考试标签/词形变换）
- [ ] 可点击字幕的本地播放器 (点词查询 → 收藏 → encounter 记录)
- [ ] AI 助记异步生成 (annotate)
- [ ] 生词本与复习

核心原则:LLM 只产语义,代码管装配和钱;词元(lexeme)为主键,encounter 记录每次真实语境;按需生产,看到哪集造到哪集。

## ingest：字幕入库

```bash
pip install -r requirements.txt          # 系统 pip 需加 --break-system-packages
python scripts/build_ecdict.py           # 克隆 ECDICT → data/ecdict.db（约 190MB 临时占用，用完自删）
python -m app.ingest episode.en.srt --title "Person of Interest" \
    --season-ep s01e01 --video episode.mp4 --db data/poi.db
pytest                                   # 全部夹具为自造句子，不含真实台词
```

`ingest` 幂等：同一 `(title, season_ep)` 重复跑只更新不新增。每段字幕的 token 化结果
（`[{surface, lemma, char_start, char_end}]`，char 偏移相对 `text_en`）存在
`Segment.tokens_json`，供 `GET /segments` 直接吐给前端渲染点击热区。

## server：本地服务

```bash
POI_DB=data/poi.db POI_ECDICT=data/ecdict.db uvicorn app.server:app --port 8000
python -m app.server --db data/poi.db --ecdict data/ecdict.db --port 8000   # 等价 CLI
```

端点见 DESIGN §3：`/episodes`、`/media/{content_id}`（HTTP Range，拖进度条用）、
`/segments?content_id=`、`/lookup?surface=&segment_id=`、`POST /collect`、`/vocab`、
`/mnemonic?lexeme_id=`；`/` 重定向到 `/static/player.html`。全程本地 SQLite，无网络调用；
`data/ecdict.db` 缺失时 `/lookup` 降级为 `in_dict=false` 而不报错。

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
