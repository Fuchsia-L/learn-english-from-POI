# DESIGN.md — learn-english-from-POI 框架 v0.2 (2026-08-16)

> 分工：Claude 主会话负责设计与验收；代码由 Opus 子代理编写并跑测试。
> 本文是子代理的开工契约：模块边界、数据契约、验收标准以此为准。
> v0.2 变更（经 Codex 评审 + 用户裁决）：媒体改走 Range 接口；Lexeme 三表拆分；
> 处决 factual 自盖章字段；预热改预算制；token 小写归一、不排除专名；
> 字幕三档重定义 + OCR 词框热区。

## 0. 已拍板的产品决策

- 看剧是主活动，查词是旁路（M0 判据：连续看 20 分钟不被工具打断）。
- 生词发现 = **拉模型**：用户点哪个词收哪个，不做词汇量摸底、不预判已知词。
- 字幕来源：bilibili 硬字幕 → `scripts/extract_hardsub.py` 离线 OCR
  （2 分钟样本词级 ~99.7%，仅样本成绩，不外推为全剧保证）。
- 词元（lexeme）为生词本主键；每次点击存 encounter（原形、原句、集数、时间戳）。
- LLM 只产语义；装配、过滤、渲染、钱包全归代码。查词路径纯本地，LLM 永不阻塞播放。
- 仓库 Public 期间字幕文本/媒体不入库（.gitignore 已挡）。测试夹具一律用自造句子。

## 1. M0 架构：本地 Web 应用

FastAPI + SQLite + 单文件前端播放器（vanilla JS）。本地起服务，浏览器打开。
**视频由 FastAPI 提供支持 HTTP Range 的媒体接口**（`file://` 方案不成立，已废弃；
路径存在 Content 表里，服务端按 id 流式吐给 <video>）。

```
app/
├── db.py          # SQLite schema + 连接（表结构见 §2）
├── ingest.py      # srt → Content/Segment/token/lemma 入库
├── server.py      # FastAPI：API + Range 媒体接口 + 静态托管 player
├── annotate.py    # 异步助记 worker（AnnotationJob 队列驱动，provider 可插拔）
├── providers/     # anthropic.py（现在）/ deepseek.py（预留，钩子生成的目标提供方）
└── static/player.html
data/               # 不入库；由脚本在本地构建
├── ecdict.db      # ECDICT 本地词典（音标/释义/考试标签；无词源字段）
└── cet46.txt      # 预热词表
scripts/
├── extract_hardsub.py   # ✅ 已完成；待升级：同时输出词级包围盒（见 §4）
└── build_ecdict.py      # 下载/转换 ECDICT → data/ecdict.db
```

## 2. 数据表（v0.2：语义拆分，一表一职）

- `Content(id, title, season_ep, video_path, srt_path)`
- `Segment(id, content_id, idx, t_start, t_end, text_en, word_boxes_json?)`
- `Lexeme(id, lemma, pos, ipa, dict_gloss)`
  —— 客观词典条目缓存（来自 ECDICT），与用户行为无关
- `WordForm(surface, lexeme_id, note)`  —— surface 统一小写
- `VocabEntry(id, lexeme_id, added_at, note?)`  —— 用户收藏了什么
- `Encounter(id, vocab_entry_id, segment_id, surface, added_at)`
- `AnnotationJob(id, lexeme_id, status, priority, created_at, done_at?)`
  status: queued/running/done/failed —— 异步任务状态，与收藏解耦
- `Mnemonic(id, lexeme_id, kind, payload_json, provider, version, edited_by_user)`
- `Review(id, vocab_entry_id, at, result)`  # M1 才用，表先建好

## 3. API 契约（M0）

- `GET /episodes` → 集列表
- `GET /media/{content_id}` → 视频流，**必须支持 Range 请求**（拖进度条依赖它）
- `GET /segments?content_id=` → 全部字幕段（含 token、lemma 映射、词框；前端一次拉全）
- `GET /lookup?surface=&segment_id=` → {surface, lemma, ipa, dict_gloss,
  collected:bool, in_dict:bool}
  in_dict=false 时前端显示"词典未收录（专名？）"+ 手动修正词元入口。
  **性能红线：本地 P50 < 50ms**（纯 SQLite，不碰 LLM）
- `POST /collect` {surface, segment_id} → Lexeme(缺则建) + VocabEntry + Encounter
  + AnnotationJob(高优先级)
- `GET /vocab` → 生词本（lexeme 卡 + encounters 展开）
- `GET /mnemonic?lexeme_id=` → done 则返回内容，否则返回 job status（前端轮询）

## 4. 播放器（M0 范围）

- <video src=/media/id> + 进度条 + 集/段选择。
- **字幕三档**（用户定义）：
  1. **双语**：不遮烧录字幕；英文行上叠一层透明点击热区（词框来自 OCR）。
  2. **只遮中文**：遮罩条只盖中文行；英文仍是画面原字，原位可点（同热区机制）。
  3. **裸听**：中英全遮，无字幕层。
- 热区机制：extract_hardsub 升级为同时输出 tesseract 词级包围盒 →
  存 Segment.word_boxes_json → 前端按视频缩放比例映射成透明 <div> 热区。
- **对齐退路**（写进验收）：若热区与画面文字实测错位明显，档 2 退化为
  "中英都遮 + 自渲染可点击英文字幕"；档 1 降级为仅展示不可点。
- 点击 → 查询卡（默认暂停，设置可关）：当前形式 / 词元 / 音标 / 词典释义 /
  原句定位 / [加入生词本]。
- M0 验收只覆盖档 2（核心学习模式）；档 1、3 结构留好，M2 验收。

## 5. annotate 契约（M1 核心，M0 只建队列）

输入（代码组装，一词一包；speaker 可空——OCR 拿不到可靠说话人）：
```json
{"lemma":"stakeout","surface":"stakeout","pos":"n","ipa":"ˈsteɪkaʊt",
 "dict_gloss":"盯梢；监视","sentence":"I just got called in to a stakeout.",
 "speaker":null,"episode":"s0xe0x","t":12.33}
```
输出（强制 JSON schema，验证失败重试）：
```json
{"context_gloss":"（这句里）临时被叫去执行的盯梢任务",
 "hooks":[
  {"type":"morph","text":"stake 桩 + out 在外——像钉在外面的桩一样守着",
   "label":"拆分助记，未经词源核验"},
  {"type":"pun","text":"死盯凯特——盯梢就是死盯目标","label":"记忆钩子，非词源"}]}
```
规则（v0.2 关键变更）：
- **没有事实区**。`factual` 字段已处决：模型自报确信不算证据，ECDICT 也没有词源数据。
  morph 拆分是 hook 的一种，永远带"未经核验"标签。哪天接入 Wiktionary 词源解析，
  核验通过的条目才另立事实字段。
- hooks 类型开放（morph/pun/imagery/scene/multi…），宁缺毋滥，自然才给。
- 按 lexeme 缓存 + 版本化；用户可编辑/重生成/删除（edited_by_user 置位后不被覆盖）。
- provider 接口：`annotate(batch: list[dict]) -> list[dict]`；钩子生成的目标提供方是
  DeepSeek（用户后续接入），当前先用 anthropic 低档位糙跑。
- **预热 = 预算制**：ingest 完成时把 当集 lemma ∩ cet46.txt 按词频降序入队（低优先级），
  每集预算上限默认 ¥4（配置项），估算超限即截断。点击收藏的词永远高优先级插队。

## 6. 验收标准（主会话按此验收子代理产出）

| 模块 | 标准 |
|---|---|
| ingest | 2min 测试 srt：分词/lemma 抽查 ≥95% 正确；token 全小写归一；**不做专名排除**；同句重复词各自成 token |
| build_ecdict | 产出 ecdict.db 含音标/释义；抽 20 词人工核对 |
| server | 全 API 冒烟 + /lookup 本地 P50<50ms；/media 支持 Range（curl -r 验证）；无网络依赖（LLM 除外） |
| player | 手工流程：选集→播放→档2遮中文→点画面英文词→查卡→收藏→生词本可见原句；20 分钟不中断；热区错位则触发退路并记录 |
| annotate | 10 张样卡人工过品味关；JSON schema 100% 通过；断网/超时不阻塞播放；预算截断可复现 |
| 全局 | requirements.txt 锁版本；SQLite 单文件即全部状态；测试夹具用自造句子（不含真实台词） |

## 7. 待定（不阻塞 M0 开工）

- lemma 库：默认 simplemma（纯 Python 零模型）；验收不达标升级 spaCy。
- Wiktionary 词源解析（事实区的前置条件），远期。
- 复习调度：M1 先"最近几天滚动 + 会/不会"，FSRS 以后再说。
- DeepSeek provider 正式接入（含费用核算脚本）。
