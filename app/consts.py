"""跨模块共享的常量（工单 9）。

以前这些常量散在 app/server.py 和 scripts/prefetch.py 里，worker 想用就得反过来
import web 层。放这儿：谁都能 import，谁都不依赖谁（本模块只依赖标准库，零 import）。
"""

from __future__ import annotations

# --- 默认路径（DESIGN §1 data/ 目录不入库，由脚本本地构建） ------------------
DEFAULT_DB = "data/poi.db"
DEFAULT_ECDICT = "data/ecdict.db"
DEFAULT_WORDLIST = "data/cet46.txt"

# --- AnnotationJob 优先级（DESIGN §5 预算制） -------------------------------
# 用户点击收藏触发的助记任务：永远插队，不受预算限制（花费仍然记账）
COLLECT_JOB_PRIORITY = 10
# 预热入队（当集 lemma ∩ 词表）：低优先级，按预算截断
PREFETCH_PRIORITY = 0
# priority >= 这个值算「高优先级」（= 用户点击收藏）
HIGH_PRIORITY = COLLECT_JOB_PRIORITY
