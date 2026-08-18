"""跨模块共享的常量（工单 9）。

以前这些常量散在 app/server.py 和 scripts/prefetch.py 里，worker 想用就得反过来
import web 层。放这儿：谁都能 import，谁都不依赖谁（本模块只依赖标准库，零 import）。
"""

from __future__ import annotations

# --- 默认路径（DESIGN §1 data/ 目录不入库，由脚本本地构建） ------------------
DEFAULT_DB = "data/poi.db"
DEFAULT_ECDICT = "data/ecdict.db"
DEFAULT_WORDLIST = "data/cet46.txt"

# --- annotate 输入包的 episode 字段（工单 11） ------------------------------
# 网页划词收藏的词没有"集数"，但 episode 是输入契约里的字段（DESIGN §5）。
# 填 "web" 比填 null 有信息量：模型据此知道这句不是台词，是网页上的句子。
WEB_EPISODE = "web"

# --- 剧集导入（工单 12：app/library.py + POST /import） ---------------------
# 媒体落地目录：<poi.db 所在目录>/library/<uuid>/，即默认 data/library/<uuid>/。
# data/ 整个被 .gitignore 覆盖，版权素材不会进仓库。
LIBRARY_DIRNAME = "library"
# 导入时每集自带的元数据（有无音轨/是否合并过/告警），/episodes 读它显示媒体状态
LIBRARY_META = "meta.json"
# 上传逐块写盘的块大小：整集视频有几个 G，绝不允许 read() 整读进内存
UPLOAD_CHUNK = 1024 * 1024
# 音视频分离导入时，两条流的时长差阈值（秒）：
# 差 ≤2s 视为同一版片源（片头 logo 帧/末尾静音的正常误差），只在界面上提示；
# 差 >5s 基本可以断定拿错了片源（导演剪辑版 / 不同语区版本），直接拒绝——
# 硬合上去的结果是全程字幕对不上口型，比不给导入更糟。中间地带按"警告放行"。
DURATION_WARN_S = 2.0
DURATION_REJECT_S = 5.0
# 内存里保留最近多少条导入作业状态（前端轮询用，进程重启即丢，不落库）
IMPORT_JOB_KEEP = 32

# --- AnnotationJob 优先级（DESIGN §5 预算制） -------------------------------
# 用户点击收藏触发的助记任务：永远插队，不受预算限制（花费仍然记账）
COLLECT_JOB_PRIORITY = 10
# 预热入队（当集 lemma ∩ 词表）：低优先级，按预算截断
PREFETCH_PRIORITY = 0
# priority >= 这个值算「高优先级」（= 用户点击收藏）
HIGH_PRIORITY = COLLECT_JOB_PRIORITY
