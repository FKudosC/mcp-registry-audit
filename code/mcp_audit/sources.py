"""数据源适配层：每个市场实现统一接口，爬虫主体无差别对待。

接口契约（iterate_entries）：
    产出统一 entry dict（见 db.extract_*_entry），
    并在每批后调用 save_progress(cursor, seen, pages) 落断点。

Smithery 特别说明（逆向实测，2026-09-06）：
- /servers 翻页只暴露 ~500 个槽位（去重后 ~200 个「精选」server），
  与 totalCount（1.1 万+）严重不符；
- q= 搜索为分词前缀匹配，每次查询最多返回 ~200 条，
  但不同查询词返回不同子集 → 用「查询词网格」取并集可渐进覆盖全量
  （实测 60 个词并集 2,931 个且未饱和）；
- 详情端点 GET /servers/{qualifiedName} 返回 tools 声明（研究核心数据）。
"""

import threading
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlunparse

import httpx

from . import config, db


class RobotsGate:
    """robots.txt 合规层（RFC 9309）：抓取前先取 robots.txt 并强制遵守。

    判定规则（与 RFC 9309 / Google 一致）：
    - 200        -> 解析规则，逐 URL 判定（User-agent 段 + Disallow）
    - 其它 4xx   -> 视为「无 robots.txt」，全站允许（404/410/401/403 皆然，
                    避免 WAF 用 403 返回挑战页时被误判为全站禁止）
    - 5xx/不可达 -> 失败开放（视为允许），但配合 Crawl-delay/本源间隔限速
    按 host 缓存，同一站点只取一次；并暴露 crawl_delay() 供礼貌限速。
    """

    def __init__(self, user_agent: str, fetcher):
        self.user_agent = user_agent
        self._fetcher = fetcher          # callable(url) -> (status:int | None, text:str)
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    @staticmethod
    def robots_url(url: str) -> str:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, "/robots.txt", "", "", ""))

    def _parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        host = urlparse(url).netloc
        if host in self._cache:
            return self._cache[host]
        robots_url = self.robots_url(url)
        try:
            status, text = self._fetcher(robots_url)
        except Exception as exc:
            print(f"  [robots] {host} 取 robots.txt 异常：{exc!r:.50}")
            status, text = None, ""
        rp: urllib.robotparser.RobotFileParser | None = None
        if status == 200 and text:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.parse(text.splitlines())
            print(f"  [robots] {host} 已加载 robots.txt（200），将强制遵守")
        elif status is not None and 400 <= status < 500:
            print(f"  [robots] {host} 无 robots.txt（HTTP {status}），视为允许")
        else:
            print(f"  [robots] {host} robots.txt 不可达（{status}），视为允许（限速）")
        self._cache[host] = rp
        return rp

    def can_fetch(self, url: str) -> bool:
        rp = self._parser(url)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float:
        rp = self._parser(url)
        if rp is None:
            return 0.0
        try:
            d = rp.crawl_delay(self.user_agent)
        except Exception:
            return 0.0
        return float(d) if d else 0.0


class BaseSource:
    name: str = "base"

    def __init__(self):
        self.client = httpx.Client(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.TIMEOUT,
            limits=httpx.Limits(
                max_connections=config.HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=config.HTTP_MAX_KEEPALIVE,
            ),
        )
        self.robots = RobotsGate(config.USER_AGENT, self._fetch_robots_raw)

    def _fetch_robots_raw(self, url: str) -> tuple[int | None, str]:
        """默认用 httpx 取 robots.txt，返回 (status, text)。

        WAF 保护的源会覆盖此方法，改用浏览器取（见 _VercelSitemapSource）。
        """
        try:
            resp = self.client.get(url, headers={"Accept": "text/plain,*/*"})
            return resp.status_code, resp.text
        except httpx.HTTPError:
            return None, ""

    def can_fetch(self, url: str) -> bool:
        """URL 是否被 robots.txt 允许（爬取前的强制闸门）。"""
        return self.robots.can_fetch(url)

    def polite_interval(self, url: str) -> float:
        """取站点声明的 Crawl-delay 与本源默认间隔的较大值。"""
        return max(getattr(self, "INTERVAL", config.REQUEST_INTERVAL),
                   self.robots.crawl_delay(url))

    def allowed(self, url: str) -> bool:
        """robots.txt 强制闸门：不允许则打印并返回 False（调用方应跳过）。"""
        ok = self.can_fetch(url)
        if not ok:
            print(f"  [robots] {self.name} 不允许抓取，跳过：{url}")
        return ok

    def throttle(self, url: str) -> None:
        """按站点 Crawl-delay 与本源间隔的较大值限速（礼貌间隔）。"""
        time.sleep(self.polite_interval(url))

    def close(self) -> None:
        self.client.close()

    def _get(self, url: str, params: dict | None = None) -> dict:
        """带重试退避的 GET，返回 JSON。

        429（限流）用长退避（窗口通常按分钟计，短重试是浪费）；
        5xx 用指数退避。
        """
        last_exc: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    wait = config.RATE_LIMIT_BACKOFF * (attempt + 1)
                    print(f"  [warn] HTTP 429 限流，{wait:.0f}s 后重试"
                          f"（{attempt + 1}/{config.MAX_RETRIES}）")
                    time.sleep(wait)
                    continue
                if 500 <= resp.status_code < 600:
                    wait = config.RETRY_BACKOFF * (2 ** attempt)
                    print(f"  [warn] HTTP {resp.status_code}，{wait:.0f}s 后重试"
                          f"（{attempt + 1}/{config.MAX_RETRIES}）")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                wait = config.RETRY_BACKOFF * (2 ** attempt)
                print(f"  [warn] 请求异常 {exc!r}，{wait:.0f}s 后重试"
                      f"（{attempt + 1}/{config.MAX_RETRIES}）")
                time.sleep(wait)
        raise RuntimeError(f"重试 {config.MAX_RETRIES} 次后仍失败: {last_exc}")


class OfficialRegistry(BaseSource):
    """官方 MCP registry：cursor 翻页，列表即全量。"""

    name = "official"

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        cursor, seen, page_count = resume_cursor, 0, 0
        base = f"{config.OFFICIAL_REGISTRY_BASE}{config.OFFICIAL_SERVERS_PATH}"
        if not self.allowed(base):
            raise RuntimeError(f"robots.txt 不允许抓取: {base}")
        while True:
            if pages_limit is not None and page_count >= pages_limit:
                print(f"达到页数限制 {pages_limit}，停止。")
                return
            params = {"limit": config.PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            page = self._get(base, params)
            entries = page.get("servers") or []
            out = []
            for entry in entries:
                e = db.extract_official_entry(entry)
                if e["name"]:
                    out.append(e)
            seen += len(entries)
            page_count += 1
            yield out
            cursor = (page.get("metadata") or {}).get("nextCursor")
            self.save_progress(cursor, seen, page_count)
            if not cursor:
                print("官方源爬取完成：无更多数据。")
                return
            self.throttle(base)

    def save_progress(self, cursor, seen, pages) -> None:  # 由爬虫注入
        pass


# ---------------------------------------------------------------- Smithery --

_CHARS = [chr(c) for c in range(ord("a"), ord("z") + 1)] + [
    str(d) for d in range(10)
]
_COMMON_WORDS = [
    "mcp", "server", "tool", "api", "data", "search", "file", "web",
    "agent", "cloud", "code", "docs", "db", "chat", "bot", "github",
    "com", "io", "test", "sql", "pdf", "git", "notion", "slack",
    "figma", "gmail", "drive", "sheet", "discord", "database",
    "browser", "scrape", "weather", "news", "finance", "crypto",
    "game", "image", "video", "text", "email", "map", "log",
    "monitor", "security",
]


def query_grid() -> list[str]:
    """查询词网格：单字符 + 常见词 + 二字符组合。

    单字符 36 + 常见词 ~45 + 二字符 1296 ≈ 1,377 个查询，
    每个最多返回 ~200 条，并集渐进覆盖全量 corpus。
    """
    grid = _CHARS + _COMMON_WORDS
    grid += [a + b for a in _CHARS for b in _CHARS]
    return grid


class Smithery(BaseSource):
    """Smithery 市场：查询词网格枚举 + 详情（tools 声明）。

    断点游标格式：'qidx:<已完成的查询数>'。
    """

    name = "smithery"

    def __init__(self, with_details: bool = True,
                 existing_names: set[str] | None = None,
                 detail_concurrency: int | None = None):
        super().__init__()
        self.with_details = with_details
        self.existing_names = existing_names or set()  # 本轮已有（断点续爬）
        self.detail_errors = 0
        self.detail_concurrency = (
            detail_concurrency or config.SMITHERY_DETAIL_CONCURRENCY
        )
        self._pool = ThreadPoolExecutor(
            max_workers=self.detail_concurrency,
            thread_name_prefix="smithery-detail",
        )
        self._detail_lock = threading.Lock()
        self._next_slot = 0.0

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        super().close()

    def _fetch_detail(self, qualified_name: str) -> tuple[str, dict | None]:
        """拉取单个 server 详情（tools/security 声明）。失败 detail 为 None。

        全局令牌限速：无论多少 worker，总体请求速率上限 = SMITHERY_DETAIL_RATE
        req/s（并尊重 robots Crawl-delay）；并发只用于掩盖网络时延。
        """
        url = (f"{config.SMITHERY_BASE}{config.SMITHERY_SERVERS_PATH}/"
               f"{urllib.parse.quote(qualified_name, safe='')}")
        rate = config.SMITHERY_DETAIL_RATE
        crawl_delay = self.robots.crawl_delay(url)
        if crawl_delay and crawl_delay > 0:
            rate = min(rate, 1.0 / crawl_delay)
        self._wait_rate_slot(rate)
        try:
            return qualified_name, self._get(url)
        except RuntimeError:
            self.detail_errors += 1
            return qualified_name, None

    def _wait_rate_slot(self, rate: float) -> None:
        """全局令牌式节流：无论多少 worker，全局每秒最多发 rate 个请求。"""
        horizon = 1.0 / rate
        with self._detail_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + horizon
        if slot > now:
            time.sleep(slot - now)

    def _fetch_details_concurrent(self, names: list[str]) -> dict[str, dict | None]:
        """并发拉取一批详情。返回 {name: detail_or_None}。"""
        results: dict[str, dict | None] = {}
        futures = {self._pool.submit(self._fetch_detail, n): n for n in names}
        for fut in as_completed(futures):
            name, detail = fut.result()
            results[name] = detail
        return results

    def _fetch_query(self, q: str) -> list[dict]:
        """单个查询词：拉全它的 ~200 条（pageSize=100 × 2 页）。"""
        base = f"{config.SMITHERY_BASE}{config.SMITHERY_SERVERS_PATH}"
        items: list[dict] = []
        page_no = 1
        while True:
            page = self._get(base, {"q": q, "pageSize": 100, "page": page_no})
            batch = page.get("servers") or []
            items.extend(batch)
            total_pages = (page.get("pagination") or {}).get("totalPages") or 1
            if page_no >= total_pages or not batch:
                break
            page_no += 1
            self.throttle(base)
        return items

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        """按查询词网格枚举全 corpus。resume_cursor='qidx:N' 跳过前 N 个查询。

        pages_limit 语义：最多处理多少个查询词（测试用）。
        """
        start_idx = 0
        if resume_cursor and resume_cursor.startswith("qidx:"):
            start_idx = int(resume_cursor.split(":", 1)[1])
        base = f"{config.SMITHERY_BASE}{config.SMITHERY_SERVERS_PATH}"
        if not self.allowed(base):
            raise RuntimeError(f"robots.txt 不允许抓取: {base}")
        grid = query_grid()
        if pages_limit is not None:
            grid = grid[:pages_limit]

        seen_names: set[str] = set(self.existing_names)
        total_new = 0

        for i in range(start_idx, len(grid)):
            q = grid[i]
            try:
                items = self._fetch_query(q)
            except RuntimeError as exc:
                print(f"  [warn] q={q} 拉取失败（{exc}），跳过该查询词")
                continue

            new_items = [it for it in items
                        if (it.get("qualifiedName") or "") not in seen_names]
            names = [it.get("qualifiedName") or "" for it in new_items]
            # 并发拉详情（速率 = 并发数 / (间隔 + 延迟)，礼貌且快）
            details = (self._fetch_details_concurrent(names)
                       if (self.with_details and names) else {})
            out = []
            for item, name in zip(new_items, names):
                seen_names.add(name)
                e = db.extract_smithery_entry(item, details.get(name))
                if e["name"]:
                    out.append(e)
            total_new += len(out)
            if out:
                yield out
            self.save_progress(f"qidx:{i + 1}", len(seen_names), i + 1)
            if (i + 1) % 20 == 0 or i + 1 == len(grid):
                print(f"  [smithery] 网格进度 {i + 1}/{len(grid)}（q={q}，"
                      f"本轮新增累计 {total_new}）")
            self.throttle(base)

        print(f"Smithery 枚举完成：{len(grid) - start_idx} 个查询词，"
              f"本轮新增 {total_new} 个唯一 server"
              f"（详情失败 {self.detail_errors} 个）")

    def save_progress(self, cursor, seen, pages) -> None:  # 由爬虫注入
        pass


def get_source(name: str, **kwargs) -> BaseSource:
    if name == "official":
        return OfficialRegistry()
    if name == "smithery":
        return Smithery(
            with_details=kwargs.get("with_details", True),
            existing_names=kwargs.get("existing_names"),
            detail_concurrency=kwargs.get("detail_concurrency"),
        )
    if name == "modelscope":
        return ModelScopeSource()
    if name == "mcpso":
        return McpSoSource(existing_names=kwargs.get("existing_names"))
    if name == "glama":
        return GlamaSource()
    if name == "cursor":
        return CursorSource(existing_names=kwargs.get("existing_names"))
    if name == "aibase":
        return AibaseSource()
    raise ValueError(
        f"未知数据源: {name}（可选: official / smithery / modelscope / mcpso /"
        f" glama / cursor / aibase）"
    )


# --------------------------------------------- Vercel 保护的 sitemap 源基类 --

class SourceBlocked(RuntimeError):
    """目标站点对本机出口 IP 硬封禁（403/429），继续抓取只会加剧封禁。"""


class _VercelSitemapSource(BaseSource):
    """Vercel 保护的 sitemap 源（如 cursor.directory）共用骨架。

    Vercel Security Checkpoint 拦截无 JS 的请求（curl 直接 403 挑战页）。
    方案：Playwright 无头真浏览器访问首页过检查（即普通访客流程，
    合法），把 cookie 装进 httpx 复用 —— 实测 sitemap 与详情页均可直连。
    子类实现 _parse_detail(url, html) -> dict | None。
    """

    INTERVAL = 1.0  # 秒/请求
    USE_PLAYWRIGHT = False  # True：全部请求走常驻无头浏览器（过 Cloudflare）
    HEADLESS = True  # False：可见窗口，用于过强反爬
    WARMUP_HOME = False  # True：常驻浏览器先访问首页过 Vercel 挑战
    BAN_ABORT_THRESHOLD = 5  # 连续被 WAF 拒绝达到该次数 -> 中止整源

    def __init__(self, existing_names: set[str] | None = None):
        super().__init__()
        self.existing_names = existing_names or set()
        self._bootstrapped = False
        self._bootstrap_cookies: dict[str, str] = {}
        self._blocked_hits = 0  # 被 WAF 拒绝（403/429）累计次数
        self._pw = None
        self._browser = None
        self._page = None

    def close(self) -> None:
        self._pw_shutdown()
        super().close()

    def _pw_shutdown(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self._page = None

    _CHALLENGE_MARKERS = (  # 可通过等待/刷新自动放行的 JS 挑战页
        "Vercel Security Checkpoint",
        "Just a moment",
        "Enable JavaScript and cookies",
        "Checking your browser",
        "cf-challenge",
        "Attention Required",
    )
    _BAN_MARKERS = (  # WAF 硬拒绝页（非挑战，等待无用）
        "403: Forbidden",
        "Error: Forbidden",
        "Access Denied",
    )

    def _is_challenge(self, html: str) -> bool:
        if len(html) > 50000:
            return False  # 真页面/大 sitemap 不可能是挑战页
        head = html[:4000]
        return any(m in head for m in self._CHALLENGE_MARKERS)

    def _is_ban(self, html: str, status: int | None) -> bool:
        """是否为 WAF 硬拒绝（非可自动放行的 JS 挑战）。

        Vercel 的挑战页与封禁页都可能是 403，靠内容区分：挑战页含
        checkpoint 标记、可自动放行；封禁页是明确的 Forbidden 文本。
        """
        head = (html or "")[:2000]
        if any(m in head for m in self._BAN_MARKERS):
            return True
        # 明确拒绝码且内容不像挑战页 -> 视为封禁
        return status in (401, 403, 451) and not self._is_challenge(html)

    def _note_blocked(self, status: int | None) -> None:
        """记一次被 WAF 拒绝；累计达阈值则中止整源。"""
        self._blocked_hits += 1
        kind = "限流(429)" if status == 429 else "拒绝"
        print(f"  [{self.name}] 被 WAF {kind}（HTTP {status}），"
              f"累计 {self._blocked_hits}/{self.BAN_ABORT_THRESHOLD}")
        if self._blocked_hits >= self.BAN_ABORT_THRESHOLD:
            raise SourceBlocked(
                f"{self.name} 连续 {self._blocked_hits} 次被 WAF 拒绝"
                f"（HTTP {status}），本机 IP 疑似被封，已中止该源以免加剧封禁。"
                f"请稍后重试或更换出口 IP。"
            )

    def _pw_page(self):
        """常驻无头浏览器 page（只启一次，复用整个爬取过程）。

        屏蔽图片/字体/CSS/媒体资源以大幅提速（我们只要 DOM 文本）。
        不预热首页：实测预热会让后续 goto 卡死；挑战交给
        _pw_get 的等待/刷新逻辑处理。
        """
        if self._page is not None:
            return self._page
        self._pw_shutdown()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = self._browser.new_context(
            user_agent=config.USER_AGENT,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        # 反检测：抹掉 navigator.webdriver，伪装成真浏览器（过 Vercel/Cloudflare）
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        ctx.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media", "stylesheet")
            else route.continue_(),
        )
        self._page = ctx.new_page()
        if self.WARMUP_HOME:
            # 先访问首页把 Vercel/Cloudflare 挑战跑完、拿到域 cookie，
            # 之后同 context 内请求 sitemap/详情即放行。失败不致命。
            try:
                resp = self._page.goto(self.HOME, wait_until="domcontentloaded",
                                       timeout=45000)
                status = resp.status if resp is not None else None
                for _ in range(8):
                    html = self._page.content()
                    if self._is_ban(html, status):
                        self._note_blocked(status)  # 达阈值会抛 SourceBlocked
                        break
                    if not self._is_challenge(html):
                        print(f"  [{self.name}] 首页预热完成")
                        break
                    self._page.wait_for_timeout(1000)
                else:
                    print(f"  [{self.name}] 首页挑战未过（继续）")
            except SourceBlocked:
                raise
            except Exception as exc:
                print(f"  [{self.name}] 首页预热失败（继续）: {exc!r:.60}")
        return self._page

    def _pw_get(self, url: str) -> str | None:
        """用常驻浏览器抓取（Vercel / Cloudflare 站点专用）。

        - 401/403/451 硬封禁：本机 IP 被拒，重试无用 -> 放弃该 URL；
        - 429 / 挑战页始终不过：退避重试，三轮后仍无内容 -> 放弃该 URL。
        每个「拿不到内容」的 URL 计一次被拒；累计达 BAN_ABORT_THRESHOLD
        即抛 SourceBlocked 中止整源（避免对着被封站点空转成千上万次）。
        """
        last_status: int | None = None
        for attempt in range(3):
            banned = False
            try:
                page = self._pw_page()
                resp = None
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    print(f"  [{self.name}] 导航未完成: {exc!r:.60}")
                if resp is None:
                    time.sleep(3 * (attempt + 1))
                    continue  # 导航失败：不读内容（可能是上一页残留），退避重试
                status = resp.status
                last_status = status
                for i in range(12):  # 等 JS 挑战自动放行后再取 DOM
                    html = page.content()
                    if html and self._is_ban(html, status):
                        banned = True  # 硬封禁：不再重试该 URL
                        break
                    if status == 429:
                        break  # 限流/挑战：交外层退避重试
                    if html and not self._is_challenge(html):
                        return html
                    if i == 4:  # 第 5 秒仍在挑战页 -> 主动刷新一次
                        try:
                            resp = page.reload(wait_until="domcontentloaded",
                                               timeout=30000)
                            if resp is not None:
                                status = resp.status
                                last_status = status
                        except Exception:
                            pass
                    page.wait_for_timeout(1000)
                if banned:
                    self._note_blocked(status)
                    return None
            except SourceBlocked:
                raise
            except Exception as exc:
                print(f"  [warn] Playwright 抓取异常 {exc!r:.80}")
                self._pw_shutdown()  # 彻底重启，避免二次 init 报 asyncio 错
            time.sleep(3 * (attempt + 1))
        # 三轮都没拿到内容（挑战/限流始终未过）：计一次被拒以推动整源中止
        self._note_blocked(last_status)
        return None

    def _fetch_robots_raw(self, url: str) -> tuple[int | None, str]:
        """Vercel/Cloudflare 后的 robots.txt：用常驻浏览器「导航」取原始文本。

        实测（venv 运行时）：
        - context.request.get() 不带导航上下文，被 Vercel 判为机器人，
          直接回 429/挑战页 —— 会把真实 robots.txt（实为 200）误读成
          「不可达」而放行；
        - page.goto() 才真正过检查并返回真实状态码与文本
          （实测 cursor.directory=404 -> 无 robots.txt，按允许处理）。
        返回真实状态码，交 RobotsGate 按 4xx=无 robots.txt 分支处理。
        """
        page = self._pw_page()
        for attempt in range(2):
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if resp is not None:
                    return resp.status, (resp.text() or "")
            except Exception as exc:
                print(f"  [{self.name}] 取 robots.txt 失败（视为允许）: {exc!r:.60}")
            if attempt == 0:
                time.sleep(2)
        return None, ""

    def _bootstrap(self) -> None:
        from playwright.sync_api import sync_playwright

        print("  [bootstrap] 启动无头浏览器过 Vercel 检查…")
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=config.USER_AGENT)
            page.goto(self.HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            # 关键：以「普通 dict」形式复用 cookie（实测 domain 作用域会失配），
            # 每次请求用 cookies= 显式带上，等价于探针验证通过的写法。
            self._bootstrap_cookies = {
                c["name"]: c["value"] for c in page.context.cookies()
            }
            browser.close()
        finally:
            p.stop()
        self._bootstrapped = True
        print("  [bootstrap] Vercel cookie 已获取")

    def _get_html(self, url: str, allow_playwright: bool = False) -> str | None:
        """带 Vercel 重引导的 GET，返回 HTML 文本。

        allow_playwright：httpx 重试耗尽后，用 Playwright 渲染兜底
        （sitemap 索引这类一次性大文件值得兜底）。
        """
        if self.USE_PLAYWRIGHT:
            return self._pw_get(url)
        for attempt in range(config.MAX_RETRIES):
            if not self._bootstrapped:
                self._bootstrap()
            try:
                resp = self.client.get(url, headers={
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }, cookies=self._bootstrap_cookies)
                if resp.status_code == 200:
                    if "Vercel Security Checkpoint" in resp.text[:3000]:
                        print("  [warn] Vercel 挑战重现，重新引导…")
                        self._bootstrapped = False
                        continue
                    return resp.text
                if resp.status_code in (403, 429) or 500 <= resp.status_code < 600:
                    # Vercel 用 403 发挑战；429 限流；5xx 服务端异常
                    wait = (config.RATE_LIMIT_BACKOFF if resp.status_code in (403, 429)
                            else config.RETRY_BACKOFF * (2 ** attempt))
                    print(f"  [warn] HTTP {resp.status_code}，{wait:.0f}s 后重试")
                    time.sleep(wait)
                    continue
                return None  # 404 等，静默跳过
            except httpx.HTTPError as exc:
                print(f"  [warn] {exc!r:.80}")
                time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
        if allow_playwright:
            print(f"  [warn] httpx 耗尽，Playwright 兜底: {url}")
            return self._playwright_fetch(url)
        return None

    def _playwright_fetch(self, url: str) -> str | None:
        """Playwright 渲染兜底抓取（含重试）。"""
        for attempt in range(2):
            from playwright.sync_api import sync_playwright

            p = sync_playwright().start()
            try:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=config.USER_AGENT)
                try:  # 先访问首页过 Vercel 检查（同一 context 内 cookie 生效）
                    page.goto(self.HOME, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(4000)
                except Exception:
                    pass
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                content = page.content()
                browser.close()
                if "Vercel Security Checkpoint" not in content[:3000]:
                    return content
                print("  [warn] Playwright 仍在挑战页，重试…")
            except Exception as exc:
                print(f"  [warn] Playwright 异常 {exc!r:.80}")
            finally:
                p.stop()
            time.sleep(10 * (attempt + 1))
        return None

    def _sitemap_urls(self, index_url: str, pattern: str) -> list[str]:
        """sitemap 索引 -> 目标 URL 列表（支持索引嵌套一层）。

        httpx 被 429 时降级用 Playwright 渲染抓（浏览器环境最稳）。
        索引与各子 sitemap 均先过 robots.txt 闸门。
        """
        import re

        if not self.allowed(index_url):
            raise RuntimeError(f"robots.txt 不允许抓取 sitemap 索引: {index_url}")
        xml = self._get_html(index_url, allow_playwright=True)
        if xml is None:
            raise RuntimeError(f"sitemap 索引获取失败: {index_url}")
        locs = re.findall(r"<loc>([^&<]+)</loc>", xml)
        # 若是索引（子 sitemap），递归展开一层
        if "sitemapindex" in xml[:2000]:
            out: list[str] = []
            for sub in locs:
                if not self.allowed(sub):
                    continue
                sub_xml = self._get_html(sub)
                if sub_xml:
                    out.extend(re.findall(pattern, sub_xml))
                self.throttle(sub)
            urls = list(dict.fromkeys(out))  # 保序去重（同一 URL 可能重复出现）
        else:
            urls = list(dict.fromkeys(re.findall(pattern, xml)))
        if not urls:
            raise RuntimeError(
                f"sitemap 未解析到任何 URL（疑似被 WAF 拦截或页面结构变化）: "
                f"{index_url}"
            )
        return urls

    def _parse_detail(self, url: str, html: str) -> dict | None:
        raise NotImplementedError

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        urls = self._sitemap_urls(self.SITEMAP_INDEX, self.SITEMAP_PATTERN)
        print(f"  [{self.name}] sitemap 收录：{len(urls)} 条")
        start_idx = 0
        if resume_cursor and resume_cursor.startswith("idx:"):
            start_idx = int(resume_cursor.split(":", 1)[1])
        todo_all = [
            u for i, u in enumerate(urls)
            if i >= start_idx
            and u.rstrip("/").split("/")[-1] not in self.existing_names
        ]
        skipped = len(urls) - len(todo_all)
        todo = todo_all[:pages_limit] if pages_limit else todo_all  # 测试限流
        print(f"  [{self.name}] 待抓详情：{len(todo)}"
              f"（sitemap {len(urls)}，跳过已有 {skipped}）")
        done = 0
        errors = 0
        blocked = 0
        batch: list[dict] = []
        for url in todo:
            if not self.allowed(url):  # robots.txt 闸门
                blocked += 1
                done += 1
                continue
            html = self._get_html(url)
            done += 1
            if html is None:
                errors += 1
            else:
                e = self._parse_detail(url, html)
                if e and e.get("name"):
                    batch.append(e)
            if len(batch) >= 200:
                yield batch
                self.save_progress(f"idx:{start_idx + done}", done, done)
                batch = []
            if done % 500 == 0:
                print(f"  [{self.name}] 进度 {done}/{len(todo)}"
                      f"（失败 {errors}，robots 跳过 {blocked}）")
            self.throttle(url)
        if batch:
            yield batch
            self.save_progress(f"idx:{start_idx + done}", done, done)
        print(f"{self.name} 完成：{done} 详情（失败 {errors}，robots 跳过 {blocked}）")

    def save_progress(self, cursor, seen, pages) -> None:  # 由爬虫注入
        pass


# --------------------------------------------------------- Cursor Directory --

class CursorSource(_VercelSitemapSource):
    """Cursor Directory（cursor.directory）。

    - robots.txt 不存在（404）= 站点未设限，全站允许
    - sitemap.xml（~3053 URL）-> /plugins/{slug} 详情页
      （其中 ~2400 个为 MCP server，is_mcp 标记）
    - 详情页无 JSON-LD：og:title + meta description + GitHub 链接
    - 断点游标：'idx:N'
    """

    name = "cursor"
    HOME = f"{config.CURSOR_BASE}/"
    SITEMAP_INDEX = f"{config.CURSOR_BASE}/sitemap.xml"
    SITEMAP_PATTERN = r"<loc>(https://cursor\.directory/plugins/[^<]+)</loc>"
    INTERVAL = config.CURSOR_INTERVAL
    USE_PLAYWRIGHT = True  # Vercel 挑战：走常驻浏览器（带反检测）
    HEADLESS = False  # 可见窗口，过 Vercel 机器人检测
    WARMUP_HOME = True  # 先过首页挑战

    def _parse_detail(self, url: str, html: str) -> dict | None:
        import re as _re

        md = _re.search(r'<meta name="description" content="([^"]*)"', html)
        ot = _re.search(r'<meta property="og:title" content="([^"]*)"', html)
        ghs = _re.findall(r'https://github\.com/[A-Za-z0-9_.\-/]+', html)
        if not (md or ot):
            return None
        return db.extract_cursor_entry(
            url,
            md.group(1) if md else None,
            ot.group(1) if ot else None,
            ghs,
        )


# ------------------------------------------------------------ ModelScope --

class ModelScopeSource(BaseSource):
    """魔搭 ModelScope MCP 广场。

    - API：PUT {base}/api/v1/dolphin/mcpServers，
      body {"PageSize":100,"PageNumber":N,"Query":"","Criterion":[]}，
      响应 Data.McpServer.McpServers[]（单条 51 字段，含 Tools/FromSite/CallVolume）
    - 阿里云 WAF：需 acw_sc__v2 cookie，用 Playwright 无头真浏览器访问页面
      过挑战获取（执行网站自己的挑战代码，即普通访客流程，合法合规；
      robots.txt 未禁止该 API 路径）
    - cookie 有效期约 1 小时，过期自动重新引导
    - 断点游标：'page:N'
    """

    name = "modelscope"

    def __init__(self):
        super().__init__()
        self._bootstrapped = False

    def _bootstrap_cookies(self) -> None:
        """Playwright 真浏览器过 WAF 挑战，把 cookie 装进 httpx 客户端。"""
        from playwright.sync_api import sync_playwright

        print("  [modelscope] 启动无头浏览器过 WAF 挑战…")
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(
                f"{config.MODELSCOPE_BASE}/mcp",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(5000)
            for c in page.context.cookies():
                self.client.cookies.set(c["name"], c["value"],
                                         domain=c.get("domain", ""))
            browser.close()
        finally:
            p.stop()
        self._bootstrapped = True
        print("  [modelscope] WAF cookie 已获取")

    def _fetch_robots_raw(self, url: str) -> tuple[int | None, str]:
        """ModelScope 有阿里云 WAF，robots.txt 用真浏览器取原始文本。"""
        try:
            from playwright.sync_api import sync_playwright

            p = sync_playwright().start()
            try:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f"{config.MODELSCOPE_BASE}/mcp",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                resp = page.context.request.get(url, timeout=30000)
                body = resp.text()
                if body.lstrip()[:1] == "<":
                    return None, ""
                return resp.status, body
            finally:
                p.stop()
        except Exception as exc:
            print(f"  [modelscope] 取 robots.txt 失败（视为允许）: {exc!r:.60}")
            return None, ""

    def _put_page(self, page_number: int) -> dict:
        """PUT 一页列表；若响应是 WAF 挑战页则重新引导后重试。"""
        import json as _json

        url = f"{config.MODELSCOPE_BASE}{config.MODELSCOPE_API_PATH}"
        payload = {
            "PageSize": config.MODELSCOPE_PAGE_SIZE,
            "PageNumber": page_number,
            "Query": "",
            "Criterion": [],
        }
        last: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            if not self._bootstrapped:
                self._bootstrap_cookies()
            try:
                resp = self.client.put(url, json=payload, headers={
                    "Origin": config.MODELSCOPE_BASE,
                    "Referer": f"{config.MODELSCOPE_BASE}/mcp",
                    "Accept": "application/json, text/plain, */*",
                })
                ctype = resp.headers.get("content-type", "")
                if "html" in ctype:
                    # WAF 挑战重现：重新引导
                    print("  [modelscope] WAF 挑战重现，重新获取 cookie…")
                    self._bootstrapped = False
                    continue
                if resp.status_code == 200:
                    d = resp.json()
                    if d.get("Success"):
                        return d
                    raise RuntimeError(f"API 返回失败: {d.get('Message')}")
                if resp.status_code in (429,) or 500 <= resp.status_code < 600:
                    wait = config.RATE_LIMIT_BACKOFF * (attempt + 1)
                    print(f"  [modelscope] HTTP {resp.status_code}，{wait:.0f}s 后重试")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            except (httpx.HTTPError, _json.JSONDecodeError) as exc:
                last = exc
                wait = config.RETRY_BACKOFF * (2 ** attempt)
                print(f"  [modelscope] 请求异常 {exc!r}，{wait:.0f}s 后重试")
                time.sleep(wait)
                last = exc
                continue
        raise RuntimeError(f"魔搭第 {page_number} 页重试耗尽: {last}")

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        start_page = 1
        if resume_cursor and resume_cursor.startswith("page:"):
            start_page = int(resume_cursor.split(":", 1)[1])
        api_url = f"{config.MODELSCOPE_BASE}{config.MODELSCOPE_API_PATH}"
        if not self.allowed(api_url):
            raise RuntimeError(f"robots.txt 不允许抓取: {api_url}")
        page_no, total = start_page, 0
        while True:
            if pages_limit is not None and page_no - start_page >= pages_limit:
                print(f"达到页数限制 {pages_limit}，停止。")
                return
            d = self._put_page(page_no)
            mcps = d["Data"]["McpServer"]
            servers = mcps.get("McpServers") or []
            total_count = mcps.get("TotalCount") or 0
            out = []
            for m in servers:
                e = db.extract_modelscope_entry(m)
                if e["name"]:
                    out.append(e)
            total += len(out)
            yield out
            self.save_progress(f"page:{page_no}", total, page_no)
            print(f"  [modelscope] 第 {page_no} 页：{len(servers)} 条"
                  f"（总数 {total_count}，本轮累计 {total}）")
            if page_no * config.MODELSCOPE_PAGE_SIZE >= total_count or not servers:
                print("魔搭源爬取完成。")
                return
            page_no += 1
            self.throttle(api_url)

    def save_progress(self, cursor, seen, pages) -> None:  # 由爬虫注入
        pass


# ---------------------------------------------------------------- mcp.so --

class McpSoSource(BaseSource):
    """mcp.so 市场：sitemap 收录全部 server URL，再抓详情页解析 JSON-LD。

    - robots.txt 允许 /servers/* 页面（仅禁 /api/ 与带 q= 的搜索 URL），
      sitemap 本身就是站点主动提供的爬取入口 —— 合法
    - 详情页 SSR，含 JSON-LD SoftwareApplication（name/description/
      category/author）与 og meta
    - 断点游标：'idx:N'（第 N 个详情已处理）；已入库的自动跳过
    """

    name = "mcpso"

    def __init__(self, existing_names: set[str] | None = None):
        super().__init__()
        self.existing_names = existing_names or set()
        self._pool = ThreadPoolExecutor(
            max_workers=config.MCPSO_DETAIL_CONCURRENCY,
            thread_name_prefix="mcpso-detail",
        )
        self.detail_errors = 0

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        super().close()

    def _fetch_detail_urls(self) -> list[str]:
        """sitemap 索引 -> servers 段全部页 -> 详情 URL 列表。"""
        import re

        index_url = f"{config.MCPSO_BASE}/sitemap.xml"
        if not self.allowed(index_url):
            raise RuntimeError(f"robots.txt 不允许抓取 sitemap: {index_url}")
        resp = self.client.get(index_url)
        idx_xml = resp.text
        server_maps = re.findall(
            r"<loc>https://mcp\.so/sitemap\.xml\?section=servers(?:&amp;page=(\d+))?</loc>",
            idx_xml,
        )
        pages = sorted({int(p) for p in server_maps if p})
        print(f"  [mcpso] sitemap servers 段共 {len(pages)} 页")
        urls: list[str] = []
        for i, p in enumerate(pages, 1):
            resp = self.client.get(
                index_url,
                params={"section": "servers", "page": p},
            )
            found = re.findall(
                r"<loc>(https://mcp\.so/servers/[^<]+)</loc>", resp.text
            )
            urls.extend(found)
            print(f"  [mcpso] sitemap 页 {i}/{len(pages)}（page={p}）："
                  f"{len(found)} 条（累计 {len(urls)}）")
            self.throttle(index_url)
        print(f"  [mcpso] 详情 URL 总数：{len(urls)}")
        return urls

    def _fetch_detail(self, url: str) -> tuple[str, dict | None, str | None, str | None]:
        """抓单个详情页，解析 JSON-LD + meta description + 页内 repoUrl。"""
        import json as _json
        import re as _re

        delay = max(config.MCPSO_DETAIL_INTERVAL,
                    self.robots.crawl_delay(url)) / config.MCPSO_DETAIL_CONCURRENCY
        time.sleep(delay)
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.client.get(url, headers={
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                if resp.status_code == 429:
                    wait = config.RATE_LIMIT_BACKOFF * (attempt + 1)
                    print(f"  [warn] mcpso 429 限流，{wait:.0f}s 后重试"
                          f"（{attempt + 1}/{config.MAX_RETRIES}）: {url}")
                    time.sleep(wait)
                    continue
                if 500 <= resp.status_code < 600:
                    wait = config.RETRY_BACKOFF * (2 ** attempt)
                    print(f"  [warn] mcpso HTTP {resp.status_code}，{wait:.0f}s 后重试"
                          f"（{attempt + 1}/{config.MAX_RETRIES}）: {url}")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    self.detail_errors += 1
                    return url, None, None, None
                h = resp.text
                jsonld = None
                for m in _re.findall(
                    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', h, _re.S
                ):
                    try:
                        d = _json.loads(m)
                        graph = d.get("@graph", [d]) if isinstance(d, dict) else d
                        for node in graph:
                            if isinstance(node, dict) and node.get("@type") == "SoftwareApplication":
                                jsonld = node
                                break
                    except _json.JSONDecodeError:
                        continue
                    if jsonld:
                        break
                md = _re.search(r'<meta name="description" content="([^"]*)"', h)
                meta_desc = md.group(1) if md else None
                repo_url = None
                rm = _re.search(r'repoUrl:\\?"(https?://[^"\\]+)', h)
                if rm:
                    repo_url = rm.group(1)
                else:  # 兜底：页首的 GitHub 按钮锚点（排除站点自身仓库）
                    for am in _re.finditer(
                        r'<a[^>]+href="(https?://github\.com/[^"]+)"', h
                    ):
                        if "chatmcp/mcp-directory" not in am.group(1):
                            repo_url = am.group(1)
                            break
                return url, jsonld, meta_desc, repo_url
            except httpx.HTTPError as exc:
                wait = config.RETRY_BACKOFF * (2 ** attempt)
                print(f"  [warn] mcpso 请求异常 {exc!r}，{wait:.0f}s 后重试"
                      f"（{attempt + 1}/{config.MAX_RETRIES}）: {url}")
                time.sleep(wait)
        self.detail_errors += 1
        print(f"  [warn] mcpso 重试 {config.MAX_RETRIES} 次后放弃（计入失败）: {url}")
        return url, None, None, None

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        all_urls = self._fetch_detail_urls()
        start_idx = 0
        if resume_cursor and resume_cursor.startswith("idx:"):
            start_idx = int(resume_cursor.split(":", 1)[1])
        todo = [
            u for i, u in enumerate(all_urls)
            if i >= start_idx and u.rstrip("/").split("/")[-1] not in self.existing_names
        ]
        print(f"  [mcpso] 待抓详情：{len(todo)}（跳过已有 {len(all_urls) - len(todo)}）")
        print(f"  [mcpso] 详情并发 {config.MCPSO_DETAIL_CONCURRENCY}"
              f"（每 100 条打印一次进度）")

        done = 0
        futures = []
        batch_out: list[dict] = []

        from concurrent.futures import as_completed as _ac
        blocked = 0
        for url in todo:
            if not self.allowed(url):  # robots.txt 闸门
                blocked += 1
                continue
            futures.append(self._pool.submit(self._fetch_detail, url))

        for fut in _ac(futures):
            url, jsonld, meta_desc, repo_url = fut.result()
            done += 1
            if jsonld or meta_desc:
                batch_out.append(
                    db.extract_mcpso_entry(url, jsonld, meta_desc, repo_url)
                )
            if len(batch_out) >= 200:
                yield batch_out
                self.save_progress(f"idx:{start_idx + done}", done, done)
                batch_out = []
            if done % 100 == 0:
                print(f"  [mcpso] 详情进度 {done}/{len(todo)}"
                      f"（{done * 100 // len(todo)}%，失败 {self.detail_errors}）")
        if batch_out:
            yield batch_out
            self.save_progress(f"idx:{start_idx + done}", done, done)
        print(f"mcp.so 枚举完成：{done} 个详情（失败 {self.detail_errors}，"
              f"robots 跳过 {blocked}）")

    def save_progress(self, cursor, seen, pages) -> None:  # 由爬虫注入
        pass


# ----------------------------------------------------------------- Glama --

class GlamaSource(BaseSource):
    """Glama 市场：sitemap 身份收割。

    - robots.txt 允许 /mcp/servers/* 页面与 sitemap（仅禁 /api/ 等路径）；
      sitemap 为站点主动提供 —— 合法
    - URL 结构即身份：/mcp/servers/{owner}/{repo} 是跨市场 join 键
    - 断点游标：'sm:N'（第 N 个 sitemap 页已处理）
    """

    name = "glama"

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        import re

        start_idx = 0
        if resume_cursor and resume_cursor.startswith("sm:"):
            start_idx = int(resume_cursor.split(":", 1)[1])
        index_url = f"{config.GLAMA_BASE}/sitemap.xml"
        if not self.allowed(index_url):
            raise RuntimeError(f"robots.txt 不允许抓取 sitemap: {index_url}")
        resp = self.client.get(index_url)
        maps = re.findall(
            r"<loc>(https://glama\.ai/sitemaps/mcp-servers/[^<]+\.xml)</loc>",
            resp.text,
        )
        print(f"  [glama] sitemap 段数：{len(maps)}")
        if pages_limit is not None:
            maps = maps[:pages_limit]

        total = 0
        blocked = 0
        for i, sm_url in enumerate(maps):
            if i < start_idx:
                continue
            if not self.allowed(sm_url):  # robots.txt 闸门
                blocked += 1
                continue
            for attempt in range(config.MAX_RETRIES):
                try:
                    r = self.client.get(sm_url)
                    if r.status_code == 200:
                        break
                    time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
                except httpx.HTTPError:
                    time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
            else:
                print(f"  [glama] 放弃 sitemap 页：{sm_url}")
                continue
            urls = re.findall(r"<loc>(https://glama\.ai/mcp/servers/[^<]+)</loc>", r.text)
            out = [db.extract_glama_entry(u) for u in urls]
            out = [e for e in out if e["name"]]
            total += len(out)
            yield out
            self.save_progress(f"sm:{i + 1}", total, i + 1)
            print(f"  [glama] sitemap {i + 1}/{len(maps)}：{len(out)} 条"
                  f"（累计 {total}）")
            self.throttle(sm_url)
        print(f"Glama 身份收割完成：{total} 个 server（robots 跳过 {blocked}）")


# --------------------------------------------------------------- AIbase --

class AibaseSource(BaseSource):
    """AIbase 中文仓库索引：MCP 标签下的 GitHub 仓库列表（JSON 接口分页）。

    - 列表页 /zh/repos/topic/mcp 为前端渲染，真实数据接口在 app.chinaz.com
      （免 key，返回 GitHub API 风格 JSON），每条即一个 GitHub 仓库
    - html_url 为跨源 join 键；robots.txt 缺失（404）= 允许
    - 断点游标：'page:N'
    """

    name = "aibase"

    def __init__(self):
        super().__init__()
        self._rate_lock = threading.Lock()
        self._next_slot = 0.0

    def _wait_rate_slot(self) -> None:
        """全局令牌式节流：无论多少 worker，全局每 1/AIBASE_RATE 秒发一个请求。"""
        horizon = 1.0 / config.AIBASE_RATE
        with self._rate_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + horizon
        if slot > now:
            time.sleep(slot - now)

    @staticmethod
    def _parse_page(d: dict) -> tuple[list[dict], dict]:
        out = []
        for it in (d.get("data") or []):
            e = db.extract_aibase_entry(it)
            if e["name"]:
                out.append(e)
        return out, (d.get("pagination") or {})

    def _get_page(self, page: int) -> dict:
        """拉取单页 JSON，带 429/5xx 退避重试。"""
        last: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.client.get(
                    config.AIBASE_API,
                    params={
                        "flag": "zh",
                        "page": page,
                        "pagesize": config.AIBASE_PAGE_SIZE,
                        "type": -1,
                        "tags": config.AIBASE_TAG,
                    },
                    headers={
                        "User-Agent": config.AIBASE_UA,
                        "Referer": f"{config.AIBASE_BASE}/",
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    wait = config.RATE_LIMIT_BACKOFF * (attempt + 1)
                    print(f"  [warn] aibase 429 限流，{wait:.0f}s 后重试"
                          f"（{attempt + 1}/{config.MAX_RETRIES}）")
                    time.sleep(wait)
                    continue
                if 500 <= resp.status_code < 600:
                    wait = config.RETRY_BACKOFF * (2 ** attempt)
                    print(f"  [warn] aibase HTTP {resp.status_code}，"
                          f"{wait:.0f}s 后重试（{attempt + 1}/"
                          f"{config.MAX_RETRIES}）")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                wait = config.RETRY_BACKOFF * (2 ** attempt)
                print(f"  [warn] aibase 请求异常 {exc!r}，{wait:.0f}s 后重试"
                      f"（{attempt + 1}/{config.MAX_RETRIES}）")
                time.sleep(wait)
        raise RuntimeError(f"AIbase 第 {page} 页重试耗尽: {last}")

    def iterate_entries(self, resume_cursor: str | None, pages_limit: int | None):
        start_page = 1
        if resume_cursor and resume_cursor.startswith("page:"):
            start_page = int(resume_cursor.split(":", 1)[1])
        if not self.allowed(config.AIBASE_API):
            raise RuntimeError(f"robots.txt 不允许抓取: {config.AIBASE_API}")

        # 先取首页探明总量（复用其结果，避免重复请求）
        self._wait_rate_slot()
        first_entries, pag = self._parse_page(self._get_page(start_page))
        total_count = pag.get("total") or 0
        page_size = pag.get("pageSize") or config.AIBASE_PAGE_SIZE
        if total_count and page_size:
            last_page = max(start_page, -(-total_count // page_size))
        else:
            last_page = start_page
        if pages_limit is not None:
            last_page = min(last_page, start_page + pages_limit - 1)
        print(f"  [aibase] 页 {start_page}~{last_page}（总数 {total_count}，"
              f"每页 {page_size}）；页并发 {config.AIBASE_CONCURRENCY}，"
              f"全局聚合 ≤{config.AIBASE_RATE} req/s")

        def work(p: int) -> tuple[int, list[dict]]:
            self._wait_rate_slot()
            return p, self._parse_page(self._get_page(p))[0]

        def drain(start: int):
            """按页序取出「已连续完成」的页，保证断点游标单调连续。"""
            p = start
            while p in done_pages:
                yield p, done_pages.pop(p)
                p += 1

        done_pages: dict[int, list[dict]] = {start_page: first_entries}
        committed_upto, total = start_page, 0
        pages = list(range(start_page + 1, last_page + 1))
        pool = ThreadPoolExecutor(max_workers=config.AIBASE_CONCURRENCY,
                                  thread_name_prefix="aibase-page")
        try:
            futures = [pool.submit(work, p) for p in pages]
            pending, it = len(futures), as_completed(futures)
            while True:
                for cp, entries in drain(committed_upto):
                    committed_upto = cp + 1
                    total += len(entries)
                    yield entries
                    self.save_progress(f"page:{cp + 1}", total, cp)
                    print(f"  [aibase] 第 {cp} 页：{len(entries)} 条"
                          f"（本轮累计 {total}）")
                if pending == 0:
                    break
                fut = next(it)
                pending -= 1
                p, entries = fut.result()
                done_pages[p] = entries
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        print("AIbase 源爬取完成。")

    def save_progress(self, cursor, seen, pages) -> None:  # 由爬虫注入
        pass
