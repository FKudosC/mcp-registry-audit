"""跨源描述相似度后端（E1 消融：seq / token / embed）。

背景（为什么要换掉 SequenceMatcher）
------------------------------------
E1（``desc_diff``）原用 ``SequenceMatcher.ratio`` 逐**字符**比较，其 ratio =
2M/(len1+len2) 是**字符级**最长公共子序列占位。这有两个结构性缺陷：

1. **跨语言假分歧**：同一能力用英文和中文各写一遍（"search files" vs
   "搜索文件"），字符重叠≈0 ⇒ 相似度≈0 ⇒ 被推入 ``different``。但人工语义
   标注会判 ``equivalent``。⇒ ``P(有害 | different)`` 被良性跨语言对稀释，
   实测精确率仅 **0.033**（E2 效度实验）。
2. **长度受限**：短描述即便内容一致也达不到 NEAR（ratio 被较短串封顶）。

本模块提供三个可对照的后端，供 E1 消融：

- ``seq``  ：原 ``SequenceMatcher`` 字符级（**基线，保持既有结果可复现**）
- ``token``：纯标准库。**语言感知归一化 + 词集 Dice**——把描述与代码侧工具名
             都归一到「能力词集」再算集合相似度（顺序无关、抗改写），并对
             跨语言对用**中英技术词典**做规范名映射，直接消除跨语言假分歧。
- ``embed``：``sentence-transformers`` 多语言句向量（``paraphrase-multilingual-
             MiniLM-L12-v2``）余弦相似度。语义最强，但需外部依赖。

三者共用同一 ``pairwise()`` 接口，保证消融对比的**唯一变量是相似度度量**。
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

BACKENDS = ("seq", "token", "embed")
EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# 与 E1 分档阈值配套（沿用 analysis.py 的 NEAR / PARTIAL，避免口径漂移）。
NEAR = 0.9
PARTIAL = 0.5

# ---------------------------------------------------------------- 语言判定 --
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")


def detect_lang(text: str | None) -> str:
    """粗粒度语言判定：含 CJK 字符 -> 'cjk'，否则 'latin'。"""
    if text and _CJK_RE.search(text):
        return "cjk"
    return "latin"


# ------------------------------------------------------- 中英技术词典（规范名）--
# 目的：把中文描述里的能力词映射到英文规范名，使跨语言对可在**同一词空间**比较。
# 只收录 MCP/软件能力高频词，不追求全量；未收录的中文词保持原样（诚实计空）。
_ZH2EN = {
    "搜索": "search", "查询": "query", "检索": "search", "查找": "search",
    "文件": "file", "文件夹": "directory", "目录": "directory", "文档": "document",
    "读取": "read", "写入": "write", "删除": "delete", "更新": "update",
    "创建": "create", "修改": "edit", "编辑": "edit", "列出": "list",
    "列表": "list", "上传": "upload", "下载": "download", "导入": "import",
    "导出": "export", "同步": "sync", "备份": "backup", "恢复": "restore",
    "数据": "data", "数据库": "database", "表": "table", "记录": "record",
    "字段": "field", "索引": "index", "缓存": "cache", "存储": "storage",
    "邮件": "email", "消息": "message", "通知": "notify", "提醒": "reminder",
    "日历": "calendar", "日程": "calendar", "会议": "meeting", "联系": "contact",
    "群": "group", "频道": "channel", "聊天": "chat", "对话": "conversation",
    "用户": "user", "账号": "account", "权限": "permission", "角色": "role",
    "登录": "login", "认证": "auth", "授权": "authorize", "令牌": "token",
    "密钥": "key", "密码": "password", "加密": "encrypt", "解密": "decrypt",
    "代码": "code", "仓库": "repository", "提交": "commit", "分支": "branch",
    "问题": "issue", "请求": "request", "合并": "merge", "拉取": "pull",
    "推送": "push", "发布": "release", "部署": "deploy", "构建": "build",
    "测试": "test", "调试": "debug", "日志": "log", "监控": "monitor",
    "分析": "analysis", "统计": "statistics", "报告": "report", "图表": "chart",
    "图像": "image", "图片": "image", "视频": "video", "音频": "audio",
    "语音": "voice", "文本": "text", "翻译": "translate", "语言": "language",
    "模型": "model", "训练": "train", "推理": "inference", "预测": "predict",
    "分类": "classify", "识别": "recognize", "检测": "detect", "生成": "generate",
    "网页": "web", "网站": "website", "链接": "link", "地址": "url",
    "网络": "network", "请求头": "header", "接口": "api", "服务": "service",
    "工具": "tool", "功能": "function", "命令": "command", "脚本": "script",
    "执行": "execute", "运行": "run", "调用": "call", "返回": "return",
    "参数": "parameter", "配置": "config", "设置": "setting", "环境": "environment",
    "变量": "variable", "系统": "system", "应用": "application", "程序": "program",
    "自动": "automatic", "智能": "intelligent", "批量": "batch", "实时": "realtime",
    "定时": "scheduled", "任务": "task", "工作流": "workflow", "模板": "template",
    "格式化": "format", "转换": "convert", "解析": "parse", "校验": "validate",
    "验证": "verify", "计算": "compute", "处理": "process", "管理": "manage",
    "获取": "fetch", "发送": "send", "接收": "receive", "订阅": "subscribe",
    "推送通知": "notify", "支付": "payment", "订单": "order", "商品": "product",
    "库存": "inventory", "财务": "finance", "股票": "stock", "交易": "trade",
    "地址簿": "address", "地图": "map", "位置": "location", "天气": "weather",
    "新闻": "news", "文章": "article", "评论": "comment", "标签": "tag",
    "分类标签": "category", "元数据": "metadata", "版本": "version", "历史": "history",
    "状态": "status", "进度": "progress", "错误": "error", "异常": "exception",
    "重试": "retry", "超时": "timeout", "并发": "concurrency", "队列": "queue",
    "代理": "agent", "助手": "assistant", "机器人": "bot", "插件": "plugin",
    "扩展": "extension", "集成": "integration", "客户端": "client", "服务器": "server",
}

_ZH_KEYS = sorted(_ZH2EN, key=len, reverse=True)

# ---------------------------------------------------------------- 词元切分 --
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

_MIN_LEN = 3


def _norm_word(w: str) -> str:
    w = w.lower().strip("-_")
    if len(w) >= 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _latin_tokens(text: str) -> set[str]:
    return {_norm_word(m.group()) for m in _WORD_RE.finditer(text or "")
            if len(_norm_word(m.group())) >= _MIN_LEN}


def _cjk_terms(text: str) -> set[str]:
    """CJK 侧词元：词典命中映射为英文规范名 + 汉字 bigram 兜底。"""
    terms: set[str] = set()
    for zh, en in ((zh, _ZH2EN[zh]) for zh in _ZH_KEYS):
        if zh in (text or ""):
            terms.add(en)
    for run in _CJK_RUN_RE.finditer(text or ""):
        s = run.group()
        if len(s) == 1:
            continue
        for i in range(len(s) - 1):
            terms.add(s[i:i + 2])
    return terms


def canonical_tokens(text: str | None) -> set[str]:
    """归一化"能力词集"：英文取词元；中文取词典规范名 + 汉字 bigram。"""
    if not text:
        return set()
    lang = detect_lang(text)
    if lang == "cjk":
        return _cjk_terms(text) | _latin_tokens(text)
    return _latin_tokens(text)


def token_similarity(a: str, b: str) -> float:
    """词集 Dice 相似度（顺序无关、抗改写；跨语言经词典归一）。"""
    sa, sb = canonical_tokens(a), canonical_tokens(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return 2 * inter / (len(sa) + len(sb))


def seq_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a or "", b or "").ratio()


# ---------------------------------------------------------------- embed 后端 --
_EMBED_MODEL = None
_EMBED_VECTORS: dict[str, list[float]] = {}


def embed_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except Exception:
        return False


def _embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        from sentence_transformers import SentenceTransformer
        _EMBED_MODEL = SentenceTransformer(EMBED_MODEL_NAME)
    return _EMBED_MODEL


def _cosine(u: list[float], v: list[float]) -> float:
    num = sum(x * y for x, y in zip(u, v))
    du = math.sqrt(sum(x * x for x in u))
    dv = math.sqrt(sum(x * x for x in v))
    if du == 0 or dv == 0:
        return 0.0
    return num / (du * dv)


def _embed_one(text: str) -> list[float]:
    if text not in _EMBED_VECTORS:
        vec = _embed_model().encode([text], normalize_embeddings=False)[0]
        _EMBED_VECTORS[text] = vec.tolist()
    return _EMBED_VECTORS[text]


# ---------------------------------------------------------------- 统一接口 --
def pairwise(reps: list[str], backend: str = "seq") -> list[float]:
    """返回 reps 中所有无序对的相似度列表（i<j，按字典序）。

    embed 后端整批 encode 一次（避免逐对调用），其余后端逐对计算。
    """
    if len(reps) < 2:
        return []
    if backend == "embed":
        model = _embed_model()
        vecs = model.encode(reps, normalize_embeddings=False)
        pairs = []
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                pairs.append(_cosine(vecs[i].tolist(), vecs[j].tolist()))
        return pairs
    sim = token_similarity if backend == "token" else seq_similarity
    return [sim(reps[i], reps[j])
            for i in range(len(reps))
            for j in range(i + 1, len(reps))]


def best_pairwise(reps: list[str], backend: str = "seq") -> float:
    """跨源最高两两相似度（E1 判决量）。"""
    vals = pairwise(reps, backend)
    return max(vals) if vals else 0.0


def verdict_of(best: float) -> str:
    if best >= NEAR:
        return "near"
    if best >= PARTIAL:
        return "partial"
    return "different"


def backend_available(backend: str) -> tuple[bool, str]:
    if backend == "embed" and not embed_available():
        return False, "sentence-transformers 未安装"
    return True, ""
