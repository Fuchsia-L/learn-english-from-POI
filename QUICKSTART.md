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

### 素材音轨（片子没声音时看这里）

网上下到的片源常常是**只有视频、没有音轨**的默片——DASH 流把视频和音频拆成两个文件，
只拿了视频流就是这个结果。先自检：

```bash
ffprobe -v error -show_entries stream=codec_type,codec_name \
  -of default=noprint_wrappers=1 你的视频.mp4
```

输出里只有 `codec_type=video`、没有 `codec_type=audio`，就是缺音轨。

修法：下载时选"含音轨"的档，或者把**视频流和音频流两个文件分别拿到**（下载工具随你），
然后合成一个文件：

```bash
ffmpeg -i 视频流.m4s -i 音频流.m4s -c copy 合并后.mp4
```

`-c copy` 只封装不重编码，画质无损、几秒钟就完事；输入是 `.m4s` 还是 `.mp4` 后缀都行。

另外：浏览器对 AV1 解码支持不一（旧版本/无硬解的机器可能黑屏或卡死）。播放器里只有声音
没画面、或者一片黑，就转成 H.264 再试：

```bash
ffmpeg -i 合并后.mp4 -c:v libx264 -crf 20 -preset veryfast -c:a copy 兼容版.mp4
```

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

# 接真模型：塞 key 即切（DeepSeek deepseek-v4-flash，约 ¥0.001/词，按 cache-miss 保守估）
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

## 6. 复习（播放器第四个界面「复习」，或直接打接口）

播放器顶部点 `[ 复习 ]`（或按 `V` 循环切到它）：正面是词 + 音标 + 遮住目标词的原句，
空格 / 回车 / 点卡片翻面看释义与助记，`J` = 会、`K` = 不会，答完自动下一张；
翻面后点例句（或「去这句」）跳回原片那一秒。

```bash
curl 'http://127.0.0.1:8000/review/next?limit=20'                       # 今日待复习卡
curl -X POST http://127.0.0.1:8000/review/answer -H 'content-type: application/json' \
     -d '{"vocab_entry_id": 1, "result": "know"}'                       # know / dont
curl http://127.0.0.1:8000/review/stats                                 # 今日已复习/待复习/毕业
```

规则（间隔重复简化版）：新收藏当天就到期；答 `know` 进一档 —— 第 1 次答对隔 1 天
再问、第 2 次隔 3 天，**第 3 次答对直接毕业、不再出现**；答 `dont` 档位归零、明天再来；
同一天同一个词只出现一次，重复提交同一答案幂等。所以生效的间隔就 1 / 3 两档
（毕业档没有"下一次"）。改规则改 `app/review.py` 顶部的常量即可（历史按新规则自动重推）。

## 7. 浏览器划词插件（M3-lite）

在**任意网页**上选中英文词 → 旁边浮出 ⌖ → 点开查询卡 → 收进同一个生词本
（相遇记录里存网页 URL 和整句，播放器生词本里显示 `🌐 页面标题`）。

先决条件：本地服务开着（第 4 步那条命令）。插件只连 `127.0.0.1`，不连别的地方，
也不用任何浏览器存储 API。**换端口**就改 `extension/bg.js` 第一行的 `API_BASE`。

零构建：下面几步加载的就是仓库里的 `extension/` 目录本身，改完代码点一下"重新加载"即可。

### Chrome / Edge

1. 地址栏进 `chrome://extensions`（Edge 是 `edge://extensions`）；
2. 右上角打开「开发者模式」；
3. 点「加载已解压的扩展程序」，选仓库里的 `extension/` 目录；
4. 随便打开一个英文网页，双击一个单词 → 右下方浮出 ⌖ → 点它。

### Firefox

1. 地址栏进 `about:debugging#/runtime/this-firefox`；
2. 点「临时载入附加组件…」，选 `extension/manifest.json`（选目录里的这个文件，不是目录）；
3. 同上试一个英文网页。

临时载入的插件重启浏览器就没了，重新载一次即可（Firefox 对未签名扩展就是这个规矩）。

### 用法与边界

- 只在**点击** ⌖ 后才弹卡，绝不自动弹；浮标 3 秒无操作自动消失；`Esc` 或点卡外关卡。
- 词典没收录的词（专名等）也能收，卡上会写「词典未收录」。
- 本地服务没起：卡里只有一行小字提示，不会弹窗刷屏。
- 已经收过的词，卡上直接显示 `✓ 已收 · N 次相遇`；再点一次按钮只是多记一次相遇。

## 故障排查

- 播放器提示库里没内容 → 第 3 步没跑或 --db 路径不一致（服务端读 POI_DB，默认 data/poi.db）。
- 点词无反应 → 看状态栏是否"词框 0 段"：入库时漏了 --boxes-json，会自动走自渲染退路。
- 音标/释义空 → data/ecdict.db 没构建，或 POI_ECDICT 指错路径。
- 插件选中词没浮标 → 页面是 `file://` 或 PDF 阅读器（内容脚本只注入 http/https 页面）；
  或选区里没有英文字母、超过 6 个词/64 字符（那不算"词或短语"）。
- 插件卡里写"本地服务未启动" → 服务没跑，或端口不是 `extension/bg.js` 里写的那个。
  改完 `bg.js` 要回扩展管理页点一下「重新加载」，再刷新网页。
