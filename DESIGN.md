# DESIGN.md — learn-english-from-POI 框架 v0.1 (2026-08-16)

> 分工：Claude 主会话负责设计与验收；代码由 Opus 子代理编写并跑测试。
> 本文是子代理的开工契约：模块边界、数据契约、验收标准以此为准。

## 0. 已拍板的产品决策（来源：两份需求文档 + 会话讨论）

- 看剧是主活动，查词是旁路（M0 判据：连续看 20 分钟不被工具打断）。
- 生词发现 = **拉模型**：用户点哪个词收哪个，不做词汇量摸底、不预判已知词。
- CET4/6 词表只当**预热缓存**：当集出现 ∩ 词表 → 提前批量生成 AI 注释；猜错无成本。
- 字幕来源：bilibili 硬字幕 → `scripts/extract_hardsub.py` 离线 OCR（已验证，~99.7% 词级准确率）。
- 词元（lexeme）为生词本主键；每次点击存 encounter（原形、原句、集数、时间戳）。
- LLM 只产语义（语境释义、助记）；装配、过滤、渲染、钱包全归代码。
- 仓库 Public 期间字幕文本/媒体不入库（.gitignore 已挡）。

## 1. M0 架构：本地 Web 应用

FastAPI + SQLite + 单文件前端播放器（vanilla JS）。本地起服务，浏览器打开；
视频文件由 <video> 直接加载本地 mp4，不经服务器转发。

```
app/
├── db.py          # SQLite schema + 连接（表结构见 §2）
├── ingest.py      # srt → Content/Segment/token/lemma 入库
├── server.py      # FastAPI：API + 静态托管 player
├── annotate.py    # 异步助记 worker（队列驱动，provider 可插拔）
├── providers/     # anthropic.py（现在）/ deepseek.py（预留接口）
└── static/player.html
data/
├── ecdict.db      # ECDICT 本地词典（音标/释义/考试标签）
└── cet46.txt      # 预热词表
scripts/
└── extract_hardsub.py   # ✅ 已完成
```

## 2. 数据表（沿用 Lux 需求草案 §4，字段微调）

- `Content(id, title, season_ep, video_path, srt_path)`
- `Segment(id, content_id, idx, t_start, t_end, text_en)`
- `Lexeme(id, lemma, pos, ipa, dict_gloss, status)`  status: new/annotating/ready
- `WordForm(surface, lexeme_id, note)`
- `Encounter(id, lexeme_id, segment_id, surface, added_at)`
- `Mnemonic(id, lexeme_id, kind, payload_json, provider, version, edited_by_user)`
- `Review(id, lexeme_id, at, result)`  # M1 才用，表先建好

## 3. API 契约（M0）

- `GET /episodes` → 集列表
- `GET /segments?content_id=` → 全部字幕段（含 token + lemma 映射，前端一次拉全）
- `GET /lookup?surface=&segment_id=` → {surface, lemma, ipa, dict_gloss, known:bool}
  **性能红线：本地 P50 < 50ms**（纯 SQLite 查询，不碰 LLM）
- `POST /collect` {surface, segment_id} → 建/更新 Lexeme + Encounter，入 annotate 队列
- `GET /vocab` → 生词本（lexeme 卡 + encounters 展开）
- `GET /mnemonic?lexeme_id=` → 就绪则返回，未就绪返回 status:annotating（前端轮询）

## 4. 播放器（M0 范围）

- <video> 本地文件 + 进度条 + 集/段选择。
- 自渲染字幕层：每词一个 span，全词可点；点击 → 查询卡（默认暂停，设置可关）。
- 查询卡：当前形式 / 词元 / 音标 / 词典释义 / 原句定位 / [加入生词本]。
- 烧录字幕遮罩条（可开关）+ 字幕三模式（双语=不遮 / 纯英=遮+显字幕层 / 裸听=遮+关）。
  三模式属 M2，但遮罩 DOM 结构 M0 就留好。

## 5. annotate 契约（M1 核心，M0 只建队列）

输入（代码组装，一词一包）：
```json
{"lemma":"stakeout","surface":"stakeout","pos":"n","ipa":"ˈsteɪkaʊt",
 "dict_gloss":"盯梢；监视","sentence":"I just got called in to a stakeout.",
 "speaker":"Lionel","episode":"s0xe0x","t":12.33}
```
输出（强制 JSON schema，验证失败重试）：
```json
{"context_gloss":"（这句里）临时被叫去执行的盯梢任务",
 "morphology":{"parts":["stake 桩","out 在外"],"note":"像钉在外面的桩一样守着","factual":true},
 "hooks":[{"type":"pun","text":"死盯凯特——盯梢就是死盯目标","label":"记忆钩子，非词源"}]}
```
规则：
- morphology 只在确信时给，不确定留空——事实区宁缺毋滥。
- hooks 类型开放（pun/imagery/scene/multi…），每条必带"非词源"标签；宁缺毋滥，自然才给。
- 按 lexeme 缓存 + 版本化；用户可编辑/重生成/删除（edited_by_user 置位后不被覆盖）。
- provider 接口：`annotate(batch: list[dict]) -> list[dict]`，现 anthropic 低档位，
  未来 deepseek 只新增 providers/deepseek.py + 配置切换。
- 预热：ingest 完成时把 当集 lemma ∩ cet46.txt 批量入队。

## 6. 验收标准（主会话按此验收子代理产出）

| 模块 | 标准 |
|---|---|
| ingest | 用 2min 测试 srt：分词/lemma 抽查 ≥95% 正确；专有名词不建 Lexeme（大写启发+白名单） |
| server | 全 API 冒烟 + /lookup 本地 P50<50ms；无网络依赖（LLM 除外） |
| player | 手工流程：加载视频→点词→查卡→收藏→生词本可见原句；20 分钟不中断 |
| annotate | 10 张样卡人工过品味关；JSON schema 100% 通过；断网/超时不阻塞播放 |
| 全局 | 零全局安装依赖冲突（requirements.txt 锁版本）；SQLite 单文件即全部状态 |

## 7. 待定（不阻塞 M0 开工）

- lemma 库：默认 simplemma（纯 Python 零模型）；验收不达标升级 spaCy。
- ECDICT 数据文件的获取与瘦身脚本（data/ 不入库，构建脚本入库）。
- 复习调度：M1 先"最近几天滚动 + 会/不会"，FSRS 以后再说。
