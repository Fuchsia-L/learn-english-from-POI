# learn-english-from-POI

用《疑犯追踪》(Person of Interest) 做个人化背单词的流水线。方法论:看剧是主活动,查词、词元归一、助记、复习压进最短旁路。

## 现状

- [x] 硬字幕 OCR 提取 (`scripts/extract_hardsub.py`) — 已在 2 分钟 1080p 片段上验证,词级准确率 ~99.7%,全程本地零成本
- [ ] 字幕解析 / 分词 / 词元归一 (parse_extract)
- [ ] 可点击字幕的本地播放器 (点词查询 → 收藏 → encounter 记录)
- [ ] AI 助记异步生成 (annotate)
- [ ] 生词本与复习

核心原则:LLM 只产语义,代码管装配和钱;词元(lexeme)为主键,encounter 记录每次真实语境;按需生产,看到哪集造到哪集。

## extract_hardsub.py

```bash
pip install pillow numpy wordfreq
# 另需 ffmpeg 和 tesseract-ocr (eng)
python scripts/extract_hardsub.py episode.mp4 -o episode.en.srt
```

默认 `--crop 1920:54:0:1026` 对准 bilibili 正版 POI 1080p 双语硬字幕的英文行;其他片源截一帧带字幕的画面调整 W:H:X:Y 即可。

流程:ffmpeg 按 3fps 裁剪采样英文字幕带 → 掩码 IoU 合并同句连续帧 → 每段取一帧跑 tesseract → 确定性清洗(| → I 等)+ 垃圾过滤(街灯/监控画面渗入误检)→ 输出带时间戳的 srt。

## 版权说明

仓库为 Public 期间,字幕文本、srt、媒体文件一律不入库(见 .gitignore),仅存代码。
