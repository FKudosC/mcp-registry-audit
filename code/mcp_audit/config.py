"""mcp-audit 全局配置。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 数据与仓库统一放 D 盘（C 盘空间不足）；代码仍在原目录
DATA_DIR = Path(r"D:\Data\mcp-audit")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mcp_audit.db"
REPOS_DIR = DATA_DIR / "repos"

# 数据源 ---------------------------------------------------------------
# 官方 MCP registry（generic registry API 规范实现，免 key）
OFFICIAL_REGISTRY_BASE = "https://registry.modelcontextprotocol.io"
OFFICIAL_SERVERS_PATH = "/v0/servers"

# Smithery 市场（免 key；列表 + 详情，详情含 tools 声明）
SMITHERY_BASE = "https://registry.smithery.ai"
SMITHERY_SERVERS_PATH = "/servers"

# 魔搭 ModelScope MCP 广场（PUT API + 真浏览器过阿里云 WAF）
MODELSCOPE_BASE = "https://www.modelscope.cn"
MODELSCOPE_API_PATH = "/api/v1/dolphin/mcpServers"
MODELSCOPE_PAGE_SIZE = 100       # 已实测可用
MODELSCOPE_PAGE_INTERVAL = 0.5   # 页间隔（秒）

# mcp.so（sitemap 收录 + 详情页 JSON-LD）
MCPSO_BASE = "https://mcp.so"
MCPSO_DETAIL_CONCURRENCY = 48    # 详情页并发上限：实测吞吐拐点（48→≈23 req/s，再高只增时延不增吞吐）
MCPSO_DETAIL_INTERVAL = 1.2      # 详情请求总 pacing（秒/请求，并发均摊）

# Glama（sitemap 身份收割：URL 即 owner/repo）
GLAMA_BASE = "https://glama.ai"

# Cursor Directory（Vercel 保护，Playwright 过检查；robots.txt 不存在=全站允许）
CURSOR_BASE = "https://cursor.directory"
CURSOR_INTERVAL = 1.0           # 未声明 crawl-delay，礼貌取 1s

# AIbase 仓库索引（列表页为前端渲染；真实数据接口在 app.chinaz.com，JSON 分页）
# 页面 https://www.aibase.com/zh/repos/topic/mcp，每条即一个 GitHub 仓库
AIBASE_BASE = "https://www.aibase.com"
AIBASE_API = (
    "https://app.chinaz.com/djflkdsoisknfoklsyhownfrlewfknoiaewf/"
    "aigit/GetGitUserList.aspx"
)
AIBASE_TAG = "mcp"              # 列表页主题标签：/repos/topic/mcp
AIBASE_PAGE_SIZE = 100          # 已实测可用（total ≈ 6216，约 63 页）
# 该接口 robots.txt 缺失（404=无限制）
# （实测吞吐拐点约 6~8；16 线程反而被服务端串行化、更慢）
# 故 worker 数按用户指定提到 48，但真正决定 aibase 吞吐的是 AIBASE_RATE（全局令牌限速）
AIBASE_CONCURRENCY = 48         # 页并发 worker 数（fresh/resume 同用；实际吞吐受 AIBASE_RATE 约束）
AIBASE_RATE = 6.0               # 全局聚合速率上限（req/s，礼貌性限速）——aibase 的吞吐天花板
# 接口对非浏览器 UA 可能拦截，沿用浏览器 UA + Referer（实测稳定）
AIBASE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 爬取行为 -------------------------------------------------------------
PAGE_SIZE = 50          # 每页条数（两源均验证过 100 可用，保守 50）
REQUEST_INTERVAL = 0.4  # 页/查询间间隔（秒），礼貌性限速
SMITHERY_DETAIL_RATE = 5.0      # 详情全局聚合速率上限（req/s）——真正决定吞吐，防 429
SMITHERY_DETAIL_CONCURRENCY = 8  # 详情并发 worker 数（仅用于掩盖网络时延，吞吐由 RATE 兜底）
MAX_RETRIES = 3         # 单请求最大重试次数
RETRY_BACKOFF = 2.0     # 5xx/异常重试退避基数（秒）：2, 4, 8
RATE_LIMIT_BACKOFF = 12.0  # 429 限流重试退避基数（秒）：12, 24, 36
TIMEOUT = 30.0          # 单请求超时（秒）

# HTTP 连接池（httpx 默认 max_connections=100 / keepalive=20，高并发下会排队+频繁重连）------
HTTP_MAX_CONNECTIONS = 64      # 连接池上限（须 ≥ 最大的源并发，当前池化源上限=48：mcpso/smithery/aibase）
HTTP_MAX_KEEPALIVE = 48        # 保活连接数（减少 TLS 握手，≤ max_connections）

# 克隆（M2a） ----------------------------------------------------------
CLONE_CONCURRENCY = 10      # 并发 git 进程数（用户指定提高；直连 GitHub，过高易超时/限流）
CLONE_RETRY_CONCURRENCY = 30  # 重试专用并发：failed 多为网络抖动类，可提高以加速回收（显式 --concurrency 优先）
CLONE_TIMEOUT = 180         # 单仓库克隆超时（秒）
# 不做原地重试：失败即登记 failed 让出并发槽（否则一条失败仓库占槽 1 分多钟，
# 大面积网络故障时整批进度停滞）。重试统一交给下一轮 `clone --retry-failed`。
GITHUB_TOKEN_FILE = PROJECT_ROOT / ".env"  # 内容形如 GITHUB_TOKEN=ghp_xxx

# 克隆体积闸（超限跳过并登记，不做分析）----------------------------------
CLONE_MAX_SIZE_MB = 50      # 单仓库体积上限（MB）；0 或 None 表示不限制
GITHUB_API_BASE = "https://api.github.com"  # 克隆前预筛体积（/repos/{owner}/{repo} 的 size 字段，单位 KB）

USER_AGENT = (
    "mcp-audit/0.1 (security measurement research; contact via github)"
)
