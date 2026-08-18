# learn-english-from-POI

用《疑犯追踪》(Person of Interest) 做个人化背单词的流水线。方法论:看剧是主活动,查词、词元归一、助记、复习压进最短旁路。

## 现状

- [x] 硬字幕 OCR 提取 (`scripts/extract_hardsub.py`) — 已在 2 分钟 1080p 片段上验证,词级准确率 ~99.7%,全程本地零成本
- [x] 字幕解析 / 分词 / 词元归一 (`app/ingest.py` + `app/db.py`) — srt → SQLite，token 小写归一、simplemma 词元、不排专名
- [x] 本地词典 (`scripts/build_ecdict.py`) — ECDICT 77 万词条 → `data/ecdict.db`（音标/中文释义/考试标签/词形变换）
- [x] 可点击字幕的本地播放器 (`app/static/player.html`) — 多界面（播放 / 生词本）+ 字幕三档 + 忽略白色的自然模糊遮罩 + OCR 词框热区 + 查询卡 + 助记展示
- [x] AI 助记异步生成骨架 (`app/annotate.py` + `app/providers/`) — 队列驱动 worker、provider 插件层、预算制、JSON schema 校验；离线 `fake` provider 全链路可跑，塞 key 即切真 provider
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

### 多界面

顶栏是终端风 tab：`[ 播放 ] [ 生词本 ]`，快捷键 `V` 循环切换，`Esc` 回播放界面。
**播放是主界面**，其余界面全屏铺开；切走时视频自动暂停，切回来恢复原播放状态
（切走前在播就接着播，切走前是暂停就还是暂停）。加界面只要在 JS 顶部的 `VIEWS`
数组里加一行 + 加一个 `<section id="view-xxx" class="view">`，tab 自动长出来。

- **播放界面**：视频 + 自绘控制条 + 当前段文本 + 字幕三档 + 词框热区 + 查询卡。
- **生词本界面**：卡片栅格（`auto-fill minmax(330px,1fr)`），每张卡展开
  词元/音标/词性/词典释义 + **AI 助记** + 全部 encounter（集数、时间、原形、原句），
  每条 encounter 有「去这句」按钮直接跳回播放界面定位到那一秒。
  数据只在**打开这个界面时拉一次**（`/vocab` 一次 + 每张卡的 `/mnemonic` 各一次），
  **不轮询**；worker 后来生成的助记要点右上角的「刷新」才会出现。

### 字幕三档与遮罩

- **三档**（快捷键 `1` / `2` / `3`，默认档 2）：
  1. 双语：不遮挡，烧录英文行上叠透明热区；
  2. 只遮中文：遮罩带只盖中文行（1080p 参考帧 `y∈[958,1026]`），英文原位可点；
  3. 裸听：中英全遮（`y∈[958,1080]`），无热区。
- **遮罩不是黑条**，是"忽略白色"的自然模糊：按 `SYNC_MS` 节奏把遮罩带
  （外加带上方 `PAD` 行——那里必定没有字，拿来当背景参考）drawImage 到一张
  横向 1/16 降采样的小 canvas，每列取参考行的中位数作为该列背景色，
  带内像素**与背景亮度差越大越往背景色靠**（白字、黑描边一起吃掉），
  差得少的真画面内容原样留着；再靠 CSS 放大 + `blur` 抹平块感。
  效果是"画面自然延续、只是没有字"。单帧处理实测约 1~2ms（1080p 源，120×22 小图）。
- **降级链**：canvas 不可用 / 被污染 / 连续超 `BUDGET_MS` → 纯 CSS
  `backdrop-filter: blur(18px) saturate(.9) brightness(.85)`，底色不透明度上限 `.35`
  （这一层平时也在，垫在 canvas 底下，保证首帧解出来之前也不漏字）。
- 强度参数全在 `CONF.MASK`：`PAD` / `DOWN_X` / `DOWN_Y` / `FLOOR` / `RANGE` /
  `STRENGTH`。字幕残影没吃干净就调小 `FLOOR`、`RANGE`；参考行蹭到字就调大 `PAD`。
- 视频暂停时也会在 seek / 换段 / 改档后**刷新一帧**（不是只在播放时更新）。

### 热区、查询卡、助记

- **热区**：词框来自 `Segment.word_boxes_json`，按 `<video>` 实际显示区域相对
  1920x1080 参考帧缩放（含 letterbox 偏移）映射成透明 `<div>`；hover 描白框，
  点击 → `GET /lookup` → 查询卡（当前形式/词元/音标/释义/原句/[加入生词本]）。
  查询卡有 `data-state`（`loading` / `done` / `error`）当终态标志，E2E 断言等它。
- **助记展示**（DESIGN §5）：收藏即排队，查询卡按 2s 间隔轮询
  `GET /mnemonic?lexeme_id=`（最多 30s），`status=done` 渲染语境释义 + 各条 hook
  （类型徽标 + **免责标签小字弱化**），`queued/running` 显示"助记生成中…"，
  无 job 整区不显示。生词本界面打开时对每张可见卡片各拉一次（不轮询），
  想再看新结果点「刷新」。助记拉不到永远不打断看剧（LLM 是旁路）。
- **对齐退路**（DESIGN §4）：某段没有词框（或大面积丢框）时，档 2 自动改为
  中英全遮 + 用 `tokens_json` 在遮罩下沿自渲染一行可点击英文字幕；
  档 1 按 DESIGN 降级为仅展示不可点。个别词丢框只是那个词不可点，其余照常。
- 其他快捷键：`空格` 播放暂停，`←/→` ±5 秒，`Esc` 关查询卡。
- 三档坐标、遮罩强度、轮询节奏、退路阈值都在 `player.html` 顶部的 `CONF` 常量里。

端到端用例见 `tests/test_player_e2e.py`（playwright + chromium，标记 `slow`）：
夹具全自造（自编 srt → ingest → 手编词框 → ffmpeg lavfi `testsrc2` 彩色测试视频；
遮罩用例要读 canvas 像素断言"不是黑条"，纯黑视频验不出来）。助记用例在测试内
跑一轮 `fake` provider 的 worker，再断言前端轮询把内容显示出来。

```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers playwright install chromium   # 首次
pytest tests/test_player_e2e.py
```

## annotate：助记 worker

助记是**异步旁路**：收藏时只往 `AnnotationJob` 排个队，播放器该放放该点点；
worker 单独跑，生成完写进 `Mnemonic`，前端再从 `GET /mnemonic?lexeme_id=` 拿结果。
**两个界面取法不同**：查询卡开着时按 2s 轮询（最多 30s）；生词本界面只在打开时
对每张卡各拉一次，**不轮询**，想看新结果点「刷新」。LLM 永不阻塞播放（DESIGN §0）。

```bash
# 离线跑一轮：fake provider，确定性假内容，0 元、0 网络请求
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db --once
# 常驻跟队列
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db --loop --poll 5
# 先看要花多少钱、prompt 长什么样（不发请求）
python -m app.annotate --db data/poi.db --provider deepseek --dry-run
```

**塞 key 即可切真 provider**——代码已经写完，缺的只是钱：

```bash
export DEEPSEEK_API_KEY=sk-...        # 或 ANTHROPIC_API_KEY=sk-ant-...
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db \
    --provider deepseek --loop --budget 4.0
```

没设对应环境变量时，真 provider 在**发 HTTP 的那一刻**抛 `ProviderNotConfigured`
（构造、拼 prompt、`estimate_cost` 全都照常工作，所以离线也能调提示词、估预算）。

- **取任务口径**：`status='queued'`，`priority DESC, id ASC`。点击收藏 = priority 10
  插队，预热 = priority 0。
- **输入包**（一词一包，DESIGN §5）：lemma + ECDICT 音标/词性/释义 + 最近一条
  Encounter 的原句/时间戳/集数；预热词没有 encounter 就退回"当集任一含该词的字幕原句"。
- **输出**强制过 JSON schema（`app/providers/__init__.py` 的 `ANNOTATION_SCHEMA`），
  不合规就重试，`--retries`（默认 2）次后该任务置 `failed`，不影响同批其他词。
- **落库**：`context_gloss` 单独一行 `kind="gloss"`，每条 hook 按 `type` 拆行
  （morph/pun/imagery/…），同一 lexeme 每次重生成 `version` 递增；
  **`edited_by_user=1` 的 kind 永不被覆盖**（用户改过的那条就是最终版）。
- **预算制**（DESIGN §5）：累计 `estimate_cost` 超 `--budget`（默认 ¥4）后，
  低优先级任务一律不再送出、保持 `queued`（调高预算再跑就继续）；
  高优先级（收藏的词）不受预算限制。
- **估价 fail closed**（工单 8b）：`estimate_cost` 抛异常 / 返回 `None` / `NaN` /
  `inf` / 负数时，**本轮立刻停手**——一次调用都不发，任务保持 `queued`，日志写明
  原因，进程退出码 `3`（`skipped_estimate` 计数）。**高优先级也停**：估价系统坏了
  就是坏了，不确定成本时一分钱都不许花。绝不再按 ¥0 继续跑。
- 免责标签由**代码**兜底，不指望模型自觉：`morph` 拆分永远标"未经词源核验"，
  其余 hook 一律带"非词源"。没有事实区（DESIGN §5：`factual` 已处决）。
- 单任务失败、provider 抛任何异常、落库出错都不会掀翻 worker。

### 调提示词

提示词全在 `app/providers/prompts.py` 一个文件里，改完跑 `pytest tests/test_providers.py`
验契约还在。**音标（ipa）是显式喂进 prompt 的**——谐音钩子必须依据读音而不是拼写，
这条写死在模板里。`python -m app.annotate --dry-run` 会打印真实输入包**和真会发出去的
请求体样例**（模型名 / `thinking` 开关 / `max_tokens`；messages 正文折成一行摘要，
不含 key，也不发包），上线前拿它眼验一遍；`provider.dump_prompt(batch)` 打印完整
system + user prompt。

价目表写在各 provider 模块顶部的常量里（`app/providers/deepseek.py` 是
`PRICE_*_CNY_PER_MTOK` 一组**官方人民币牌价 2026-08**，直接给到类上的
`price_in_cny_per_mtok` / `price_out_cny_per_mtok`——**变价改这一处**），
**随时可能过期，自己核对官网后改**。deepseek-v4-flash：峰时输入（cache-miss）
¥3.0/百万 token、输出 ¥9.0/百万 token；错峰减半、缓存命中 ¥0.10 只作注释备查，
**不参与估算**（保守原则）。峰时 = 北京时间 9:00–12:00 与 14:00–18:00。

DeepSeek 默认模型是 **`deepseek-v4-flash`**（旧的 `deepseek-chat` / `deepseek-reasoner`
已被官方宣布退役）。该系列**默认开 thinking 且 effort=high**，助记生成用不上，
请求体里已显式写死 `"thinking": {"type": "disabled"}`，不然要重度多计费。
换模型用 `--model`（只对真 provider 有意义，`fake` 忽略）。
估价一律按**峰时 + cache-miss**（牌价最贵那档）算 —— 预算是硬顶，不许乐观估。
按默认 `--batch-size 4` 算下来约 **¥0.0039/词**，¥4 预算够跑 ~1000 个词。

## prefetch：预热入队

```bash
python scripts/prefetch.py --db data/poi.db --content-id 1 \
    --wordlist data/cet46.txt --limit 200
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db --once --budget 4.0
```

当集 lemma ∩ 词表 → 按 wordfreq 词频降序 → 低优先级（0）入队；已 queued/running/done
的词跳过（失败过的允许重排），重复跑幂等。`data/cet46.txt` 不存在时会打印生成方法
（从 `data/ecdict.db` 的 `tag` 字段一条命令导出 CET4/6 词表）并以非零码退出。
`--dry-run` 只算不写。

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
