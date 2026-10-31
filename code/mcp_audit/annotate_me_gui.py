"""E2 **人工标注** GUI：左侧看条目、右侧看 codebook，逐条填 label / note 落卡。

与 ``annotate_gui``（机器标注控制台）**完全隔离**：独立进程、独立端口（默认 8766）、
独立文件，运行中的 ``annotate llm`` 不受影响；两者只共享 ``annotation.py`` 里同一份
codebook 常量（**只读**，保证人机同卷）。

界面契约
--------
  - 卡片选择：只放开真人卡 ``label_Me.csv`` / ``label_other.csv``（``HUMAN_CARDS``）；
    选 LLM 卡会被服务端拒绝 —— 机器卡由 ``annotate llm`` 写，人工不该碰。
  - 展示：``sheet.csv`` 每行的 ``item_id`` / ``norm_url`` / 各来源 ``srcK`` 名称与其
    ``descK`` 描述（与 LLM 盲标看到的内容完全一致）。
  - 落卡：按 ``sheet.csv`` 行序整卡回写 ``label_<标注者>.csv``（``item_id,label,note``），
    未标的行留空；写入走「临时文件 + 原子替换」，不会写出半截 CSV。
  - label 归一：服务端用 ``annotation._norm_label`` 归一（容忍中文名 / 大小写），
    填了但认不出来的值**点名报错**，不静默丢弃。
  - 翻译：可选接入任意 OpenAI 兼容端点，为每条描述生成对照译文 —— 这只是**语言辅助**。
  - 建议：可选让同一个端点为当前条目给出 **label 建议 + note 建议**（与 ``annotate llm``
    用同一 ``SYSTEM_PROMPT`` 与同一 ``_llm_prompt``，即「机器标注者会怎么判」），
    面板**默认收起**且**只在你点「采纳」时才落到表单**，落卡仍由你决定。
  - 排障：端点 URL 会自动补全成 ``/chat/completions``；可勾「忽略系统代理」绕开坏代理；
    「诊断」按钮逐层报告 DNS/端口/代理/HTTP 状态与响应正文，错误同时打到本机终端。

关于 label 建议（**与「基准纯净」的取舍，务必知情**）
--------------------------------------------------
``label_Me`` 是全流程的信度基准（``score`` 默认以真人为 golden）。**采纳模型建议**等于
把人机一致性人为抬高，人–机 κ 会虚高、基准不再「纯人类」。所以：① 建议面板默认收起、
不自动生成；② 只有你点「采纳」才写入表单，绝不自动落卡；③ 建议与翻译走**同一个端点
但不同 system prompt** —— 翻译那段里**不含五类定义、不含 label 字样**，不会泄题。
**建议**：若要让 ``Me`` 保持纯人类基准，请盲标 ``Me``（不开建议），把建议只当 ``other``
的辅助；或两条都盲标、跑完再回看建议做敏感性分析。``key.csv``（真值）任何情况下都
不读、不显示。

用法：
    python -m mcp_audit.annotate_me_gui                    # 127.0.0.1:8766，自动开浏览器
    python -m mcp_audit.annotate_me_gui --port 8800 --no-browser
    python -m mcp_audit.annotate_me_gui --dir D:\\Data\\mcp-audit\\annotation
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from . import annotation, config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
TIMEOUT_DEFAULT = 60.0
RETRY_TIMES = 3

HUMAN_CARDS = tuple(n for n in annotation.ROSTER_DEFAULT
                    if n not in annotation.LLM_NAMES)

TRANSLATE_SYSTEM = (
    "你是技术文档翻译。用户会给你同一个软件项目在若干来源上的描述文本。\n"
    "请为每一条描述给出中文对照翻译；若原文已是中文，则给出简洁的英文回译；"
    "中英混排时补齐缺失的一侧。\n"
    "严格按下面格式逐条输出，一条一行，除序号和译文外不要输出任何内容：\n"
    "[序号] 译文\n"
    "不要复述原文，不要解释，不要加代码块或多余空行。"
)

SUGGEST_WARNING = (
    "采纳后本条即带模型影响，Me 不再是纯人类基准（人–机 κ 会虚高）。"
    "要让 Me 保持纯净请盲标，建议只当 other 的辅助。"
)

ENDPOINT_SUFFIX = "/chat/completions"

EXAMPLES = {
    "equivalent": "官方 “Query real-time weather for any city.” / 三方 “查询任意城市的实时天气。”",
    "subset": "一方 “支持 7 天预报 + 空气质量”，另一方只写 “支持 7 天预报”",
    "complementary": "一方只讲 “数据源覆盖全球”，另一方只讲 “支持批量查询”，互不矛盾",
    "capability_conflict": "一方 “支持批量查询”，另一方明确 “不支持批量查询”",
    "identity_conflict": "一方描述的是 foo/weather，另一方描述的是 bar/weather-pro 的功能",
}

ORDER_HINT = ("先看是否指向同一项目（否则 identity_conflict）→ 再看能力描述是否互相矛盾"
              "（否则 capability_conflict）→ 最后在良性三类中按信息量关系择一")

_LOCK = threading.Lock()


def _out_dir(path: str | None) -> Path:
    return Path(path) if path else config.DATA_DIR / "annotation"


def norm_url(raw: str) -> str:
    """把用户填的端点补全成 ``.../chat/completions``（最常见的「连不上」原因）。

    容忍这几种写法（都补成同一目标）：::

        https://host                     -> https://host/v1/chat/completions
        https://host/v1                  -> https://host/v1/chat/completions
        https://host/v1/                 -> https://host/v1/chat/completions
        https://host/v1/chat/completions -> 原样
        https://host/openai/deployments/x/chat/completions?api-version=1 -> 原样
    """
    url = (raw or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    path = parts.path or ""
    if path.rstrip("/").endswith(ENDPOINT_SUFFIX):
        base = path.rstrip("/")
    elif path.rstrip("/") in ("", "/"):
        base = "/v1" + ENDPOINT_SUFFIX
    else:
        base = path.rstrip("/") + ENDPOINT_SUFFIX
    return parts._replace(path=base).geturl()


def env_proxies() -> dict:
    """当前进程看到的代理相关环境变量（httpx 默认 ``trust_env=True`` 会用它们）。"""
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def _card_name(raw: str) -> tuple[str, str]:
    """校验卡片名，只放行真人卡。返回 ``(名字, 错误)``。"""
    name = (raw or "").strip()
    if not name:
        return "", "未指定卡片"
    if name in annotation.LLM_NAMES:
        return "", (f"{name} 是 LLM 卡，由 `annotate llm` 写；人工标注只允许 "
                    f"{' / '.join(HUMAN_CARDS)}")
    if name not in HUMAN_CARDS:
        return "", f"未知卡片 {name}（只允许 {' / '.join(HUMAN_CARDS)}）"
    return name, ""


def _sheet_items(out_dir: Path) -> list[dict]:
    """读 ``sheet.csv`` → 条目列表（含逐源 src/desc），保持行序。"""
    sheet = out_dir / "sheet.csv"
    if not sheet.exists():
        return []
    items: list[dict] = []
    for row in annotation._read_csv(sheet):
        iid = (row.get("item_id") or "").strip()
        if not iid:
            continue
        sources: list[dict] = []
        for k in range(1, annotation.MAX_SOURCE_COLS + 1):
            src = (row.get(f"src{k}") or "").strip()
            desc = (row.get(f"desc{k}") or "").strip()
            if src or desc:
                sources.append({"k": k, "src": src, "desc": desc})
        items.append({
            "item_id": iid,
            "norm_url": (row.get("norm_url") or "").strip(),
            "n_desc_sources": (row.get("n_desc_sources") or "").strip(),
            "sources": sources,
        })
    return items


def _read_card_raw(path: Path) -> dict[str, dict]:
    """原样读标注卡（**不归一、不改写**），供整卡回写时保留其他行的原值。"""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for row in annotation._read_csv(path):
        iid = (row.get("item_id") or "").strip()
        if iid:
            out[iid] = {"label": (row.get("label") or "").strip(),
                        "note": (row.get("note") or "").strip()}
    return out


def _bundle_payload(out_dir: Path, raw_card: str) -> dict:
    """一次性把整包数据发给前端：条目 + 该卡已填内容 + codebook + 举例。"""
    name, err = _card_name(raw_card)
    if err:
        return {"ok": False, "error": err}
    items = _sheet_items(out_dir)
    if not items:
        return {"ok": False, "error": f"{out_dir / 'sheet.csv'} 不存在或为空"}

    path = out_dir / f"{annotation.LABEL_PREFIX}{name}.csv"
    by = _read_card_raw(path)
    filled = 0
    invalid: list[dict] = []
    for it in items:
        rec = by.get(it["item_id"]) or {}
        raw = rec.get("label", "")
        it["label"] = annotation._norm_label(raw)
        it["note"] = rec.get("note", "")
        if raw and not it["label"]:
            invalid.append({"item_id": it["item_id"], "raw": raw[:40]})
        if it["label"]:
            filled += 1

    return {
        "ok": True,
        "dir": str(out_dir),
        "card": name,
        "card_file": path.name,
        "card_exists": path.exists(),
        "cards": [{"name": n,
                   "file": f"{annotation.LABEL_PREFIX}{n}.csv",
                   "exists": (out_dir / f"{annotation.LABEL_PREFIX}{n}.csv").exists()}
                  for n in HUMAN_CARDS],
        "items": items,
        "n_items": len(items),
        "filled": filled,
        "invalid": invalid,
        "labels": list(annotation.LABELS),
        "benign": list(annotation.BENIGN),
        "harmful": list(annotation.HARMFUL),
        "label_zh": dict(annotation.LABEL_ZH),
        "label_def": dict(annotation.LABEL_DEF),
        "label_expect": dict(annotation.LABEL_EXPECT),
        "codebook": [{"label": lb, "zh": annotation.LABEL_ZH[lb],
                      "def": annotation.LABEL_DEF[lb],
                      "expect": annotation.LABEL_EXPECT[lb],
                      "kind": "良性" if lb in annotation.BENIGN else "有害",
                      "example": EXAMPLES.get(lb, "")} for lb in annotation.LABELS],
        "order_hint": ORDER_HINT,
        "warn": SUGGEST_WARNING,
        "timeout_default": TIMEOUT_DEFAULT,
    }


def _save_one(out_dir: Path, body: dict) -> dict:
    """写入一条人工标注：整卡按 sheet 行序原子回写。"""
    name, err = _card_name(body.get("card"))
    if err:
        return {"ok": False, "error": err}
    item_id = (body.get("item_id") or "").strip()
    if not item_id:
        return {"ok": False, "error": "缺少 item_id"}

    items = _sheet_items(out_dir)
    ids = [it["item_id"] for it in items]
    if item_id not in ids:
        return {"ok": False, "error": f"{item_id} 不在 sheet.csv 里"}

    raw = (body.get("label") or "").strip()
    lb = annotation._norm_label(raw)
    if raw and not lb:
        return {"ok": False,
                "error": f"label 值「{raw[:20]}」不认识 —— 只能填五类之一"
                         f"（{ ' / '.join(annotation.LABELS) }，可写中文名）"}
    note = (body.get("note") or "").strip()

    path = out_dir / f"{annotation.LABEL_PREFIX}{name}.csv"
    with _LOCK:
        state = _read_card_raw(path)
        cur = state.get(item_id) or {"label": "", "note": ""}
        if not raw and cur.get("label") and not body.get("clear"):
            lb = cur["label"]
        state[item_id] = {"label": lb, "note": note}
        rows = [{"item_id": i,
                 "label": (state.get(i) or {}).get("label", ""),
                 "note": (state.get(i) or {}).get("note", "")} for i in ids]
        tmp = path.with_name(path.name + ".tmp")
        annotation._write_csv(tmp, rows)
        tmp.replace(path)

    filled = sum(1 for i in ids if annotation._norm_label((state.get(i) or {}).get("label", "")))
    return {"ok": True, "label": lb, "note": note, "filled": filled,
            "n_items": len(ids), "file": path.name}


def _chat(prov: dict, system: str, user: str, api_key: str, timeout: float,
          minimal: bool = False, retries: int = RETRY_TIMES,
          trust_env: bool = True) -> str:
    """一次 chat 调用；退避策略与 ``annotation._llm_call`` 一致。

    ``trust_env=False`` 会忽略 ``HTTP(S)_PROXY`` 等环境变量 —— 本机有个坏代理时，
    这是「所有端点都连不上」最常见的元凶，故做成开关而不是硬编码。
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict = {
        "model": prov["model"],
        "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if not minimal:
        payload["temperature"] = 0.0

    resp: httpx.Response | None = None
    for attempt in range(1, max(retries, 1) + 1):
        try:
            with httpx.Client(timeout=timeout, trust_env=trust_env) as own:
                resp = own.post(prov["url"], json=payload, headers=headers)
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise RuntimeError(f"网络异常：{type(exc).__name__} {exc}"[:300]) from exc
            time.sleep(min(2 ** (attempt - 1), 8.0))
            continue
        if resp.status_code < 400:
            break
        if resp.status_code not in annotation.RETRY_STATUS or attempt >= retries:
            raise RuntimeError(f"HTTP {resp.status_code}：{resp.text.strip()[:300]}")
        time.sleep(annotation._retry_wait(resp, attempt))
    if resp is None:
        raise RuntimeError("请求没有拿到响应")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"响应不是 JSON：{resp.text.strip()[:200]}")
    text = annotation._extract_content(data)
    if not text.strip():
        raise RuntimeError("响应里没有正文")
    return text


def _translate_prompt(sources: list[dict]) -> str:
    lines = ["同一项目在多个来源上的描述："]
    for s in sources:
        lines.append(f"[{s['k']}] 来源 {s.get('src') or '(无名)'}：{s.get('desc') or ''}")
    lines.append("")
    lines.append(f"请按 [序号] 译文 的格式，逐条给出上述 {len(sources)} 条描述的中文对照翻译。")
    return "\n".join(lines)


def _parse_translations(text: str, keys: list[int]) -> dict[str, str]:
    """从模型输出里抠 ``[k] 译文``（容忍多余空行 / 无序号单条兜底）。"""
    out: dict[str, str] = {}
    cur: int | None = None
    for line in (text or "").splitlines():
        m = re.match(r"^\s*\[?\s*(\d+)\s*\]?\s*[.、:：]?\s*(.*)$", line)
        if m and int(m.group(1)) in keys:
            cur = int(m.group(1))
            out[str(cur)] = m.group(2).strip()
        elif cur is not None and line.strip():
            out[str(cur)] = (out[str(cur)] + " " + line.strip()).strip()
    if not out and len(keys) == 1 and (text or "").strip():
        out[str(keys[0])] = text.strip()
    return {k: v for k, v in out.items() if v}


def _log(msg: str) -> None:
    """把错误同步打到本机终端（浏览器 toast 看不全长报错时来这里捞）。"""
    print(f"[me-gui] {msg}", flush=True)


def _cfg_from(body: dict) -> dict:
    """从请求体里取 LLM 配置（统一做 URL 补全 / 代理开关 / 超时归一）。"""
    try:
        timeout = float(body.get("timeout") or TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        timeout = TIMEOUT_DEFAULT
    raw = (body.get("url") or "").strip()
    return {
        "url": norm_url(raw),
        "url_raw": raw,
        "model": (body.get("model") or "").strip(),
        "key": (body.get("key") or "").strip(),
        "timeout": timeout,
        "trust_env": not bool(body.get("no_proxy_env")),
        "minimal": bool(body.get("minimal")),
    }


def _check_cfg(cfg: dict) -> str:
    if not cfg["url"]:
        return "未填端点 URL"
    if not cfg["model"]:
        return "未填模型名"
    if not cfg["key"]:
        return "未填 API Key"
    return ""


def _call(cfg: dict, system: str, user: str) -> tuple[str, bool, str]:
    """调一次 chat，返回 ``(正文, 是否兼容模式, 错误)``；参数不兼容时自动降级重试。"""
    prov = {"url": cfg["url"], "model": cfg["model"], "temp": 0.0}
    try:
        return (_chat(prov, system, user, cfg["key"], cfg["timeout"],
                      minimal=cfg["minimal"], trust_env=cfg["trust_env"]),
                cfg["minimal"], "")
    except RuntimeError as exc:
        if cfg["minimal"] or not annotation._PARAM_ERR_RE.search(str(exc)):
            return "", cfg["minimal"], str(exc)
        try:
            return (_chat(prov, system, user, cfg["key"], cfg["timeout"],
                          minimal=True, trust_env=cfg["trust_env"]), True, "")
        except (RuntimeError, httpx.HTTPError) as exc2:
            return "", True, str(exc2)
    except httpx.HTTPError as exc:
        return "", cfg["minimal"], f"{type(exc).__name__}: {exc}"


def _row_of(item: dict) -> dict:
    """把前端条目还原成 ``annotation._llm_prompt`` 认的 ``srcK/descK`` 行。"""
    row: dict = {}
    for s in item.get("sources") or []:
        row[f"src{s.get('k')}"] = s.get("src") or ""
        row[f"desc{s.get('k')}"] = s.get("desc") or ""
    return row


def _do_translate(out_dir: Path, body: dict) -> dict:
    """对某一条目的各来源描述生成对照译文（语言辅助，system prompt 不含 label 字样）。"""
    cfg = _cfg_from(body)
    err = _check_cfg(cfg)
    if err:
        return {"ok": False, "error": err, "translations": {}, "raw": ""}

    sources = [s for s in (body.get("sources") or [])
               if isinstance(s, dict) and (s.get("desc") or s.get("src"))]
    if not sources:
        return {"ok": False, "error": "本条没有可翻译的描述", "translations": {}, "raw": ""}
    keys = [int(s.get("k") or i + 1) for i, s in enumerate(sources)]
    prompt = _translate_prompt(sources)

    t0 = time.time()
    text, minimal, err = _call(cfg, TRANSLATE_SYSTEM, prompt)
    if err:
        _log(f"translate 失败 url={cfg['url']} model={cfg['model']} :: {err}")
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "translations": {},
                "raw": "", "minimal": minimal, "url": cfg["url"], "error": err}
    trans = _parse_translations(text, keys)
    return {"ok": bool(trans), "ms": int((time.time() - t0) * 1000),
            "minimal": minimal, "url": cfg["url"], "translations": trans,
            "raw": text.strip()[:1000],
            "error": "" if trans else "译文里解析不出 [序号] 行（换行格式可能不标准）"}


def _do_verify(out_dir: Path, body: dict) -> dict:
    """连接测试：真跑一条样例，必须能解析出五类标签才算通过；顺带探明是否需最小载荷。"""
    cfg = _cfg_from(body)
    err = _check_cfg(cfg)
    if err:
        return {"ok": False, "ms": 0, "mode": "", "minimal": False,
                "label": "", "note": "", "url": cfg["url"], "error": err}
    t0 = time.time()
    text, minimal, err = _call(cfg, annotation.SYSTEM_PROMPT,
                               annotation._probe_prompt(out_dir))
    ms = int((time.time() - t0) * 1000)
    mode = "兼容模式（省略 temperature）" if minimal else "标准载荷"
    if err:
        _log(f"verify 失败 url={cfg['url']} model={cfg['model']} :: {err}")
        return {"ok": False, "ms": ms, "mode": mode, "minimal": minimal,
                "label": "", "note": "", "url": cfg["url"], "error": err}
    lb, note = annotation._parse_model_json(text, "")
    return {"ok": bool(lb), "ms": ms, "mode": mode, "minimal": minimal,
            "label": lb, "note": note, "url": cfg["url"], "raw": text.strip()[:400],
            "error": "" if lb else "返回里解析不出五类标签（检查是不是对话模型）"}


def _do_suggest(out_dir: Path, body: dict) -> dict:
    """让模型按 ``annotate llm`` 同一口径给当前条目出 label + note 建议（不落卡）。"""
    cfg = _cfg_from(body)
    err = _check_cfg(cfg)
    if err:
        return {"ok": False, "error": err}
    item = body.get("item") if isinstance(body.get("item"), dict) else {}
    row = _row_of(item)
    if not any((row.get(f"desc{k}") or "").strip()
               for k in range(1, annotation.MAX_SOURCE_COLS + 1)):
        return {"ok": False, "error": "本条没有可判定的描述"}

    t0 = time.time()
    text, minimal, err = _call(cfg, annotation.SYSTEM_PROMPT, annotation._llm_prompt(row))
    ms = int((time.time() - t0) * 1000)
    if err:
        _log(f"suggest 失败 url={cfg['url']} model={cfg['model']} :: {err}")
        return {"ok": False, "ms": ms, "url": cfg["url"], "error": err}
    lb, note = annotation._parse_model_json(text, "")
    return {"ok": bool(lb), "ms": ms, "minimal": minimal, "url": cfg["url"],
            "label": lb, "label_zh": annotation.LABEL_ZH.get(lb, ""),
            "note": note, "raw": text.strip()[:600],
            "error": "" if lb else "建议里没解析出五类标签（看原始回文）"}


def _do_diag(out_dir: Path, body: dict) -> dict:
    """逐层排障：URL 补全 → 代理环境 → DNS/TCP → HTTP 探测，把报告打到终端。"""
    cfg = _cfg_from(body)
    steps: list[dict] = []
    steps.append({
        "step": "端点解析", "ok": bool(cfg["url"]),
        "detail": (f"你填的是 {cfg['url_raw'] or '(空)'} → 实际请求 "
                   f"{cfg['url'] or '(空)'}")})

    parts = urlsplit(cfg["url"]) if cfg["url"] else None
    host = (parts.hostname or "") if parts else ""
    port = ((parts.port or 0) if parts else 0) or (443 if parts and parts.scheme == "https" else 80)

    px = env_proxies()
    steps.append({
        "step": "代理环境变量", "ok": True,
        "detail": ("未设置（直连）" if not px
                   else "、".join(f"{k}={v}" for k, v in px.items())
                        + "　→ 本次请求" + ("已忽略代理" if not cfg["trust_env"] else "会走代理（若是坏代理就勾「忽略系统代理」）"))})

    if host:
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=8):
                pass
            steps.append({"step": "DNS / TCP", "ok": True,
                          "detail": f"{host}:{port} 已连通（{int((time.time() - t0) * 1000)} ms）"})
        except OSError as exc:
            try:
                socket.getaddrinfo(host, port)
                steps.append({"step": "DNS / TCP", "ok": False,
                              "detail": f"DNS 能解析，但 TCP 连不上 {host}:{port} —— "
                                        f"{type(exc).__name__}: {exc}"})
            except OSError as exc2:
                steps.append({"step": "DNS / TCP", "ok": False,
                              "detail": f"DNS 解析失败 —— {type(exc2).__name__}: {exc2}"})
    else:
        steps.append({"step": "DNS / TCP", "ok": False, "detail": "URL 无主机名，跳过"})

    if cfg["url"] and cfg["model"] and cfg["key"]:
        t0 = time.time()
        text, minimal, err = _call(cfg, annotation.SYSTEM_PROMPT,
                                   annotation._probe_prompt(out_dir))
        ms = int((time.time() - t0) * 1000)
        if err:
            steps.append({"step": "HTTP 探测", "ok": False, "detail": f"{err}（{ms} ms）"})
        else:
            lb, note = annotation._parse_model_json(text, "")
            steps.append({"step": "HTTP 探测", "ok": bool(lb),
                          "detail": f"通、正文可解析：label={lb or '(解析不出)'} "
                                    f"note={note}（{ms} ms{', 兼容模式' if minimal else ''}）"})
    else:
        steps.append({"step": "HTTP 探测", "ok": False,
                      "detail": "URL / 模型名 / Key 未填齐，跳过"})

    report = "\n".join(f"{'[OK]' if s['ok'] else '[NG]'} {s['step']}：{s['detail']}"
                       for s in steps)
    _log("诊断\n" + report)
    return {"ok": all(s["ok"] for s in steps), "steps": steps, "report": report,
            "url": cfg["url"], "url_raw": cfg["url_raw"], "proxies": px}


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E2 人工标注</title>
<style>
  :root{
    --bg:#f4f6f8; --panel:#fff; --line:#dfe3e8; --text:#1b1f24; --muted:#6b7280;
    --accent:#2563eb; --accent-d:#1d4ed8; --ok:#15803d; --warn:#b45309; --err:#dc2626;
    --benign:#e8f5ec; --harm:#fdeaea;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    background:var(--bg);color:var(--text);display:grid;
    grid-template-rows:auto 1fr auto;height:100vh;overflow:hidden;
    font:14px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;
  }
  code{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12px}
  button{
    font:inherit;border:1px solid var(--line);background:#fff;border-radius:7px;
    padding:6px 12px;cursor:pointer;color:var(--text);
  }
  button:hover:not(:disabled){border-color:#b9c0c9;background:#fafbfc}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.pri{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.pri:hover:not(:disabled){background:var(--accent-d)}
  input,select,textarea{
    font:inherit;color:var(--text);border:1px solid var(--line);border-radius:7px;
    padding:6px 9px;background:#fff;
  }
  input:focus,select:focus,textarea:focus{outline:2px solid #bfdbfe;outline-offset:0}

  header{
    background:var(--panel);border-bottom:1px solid var(--line);
    padding:9px 14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;
  }
  header .brand{font-weight:700}
  header .dir{color:var(--muted);font-size:12px;max-width:340px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  header .sp{flex:1}
  .pos{font-variant-numeric:tabular-nums;color:var(--muted)}
  .pos b{color:var(--text);font-size:15px}
  .bar{width:130px;height:7px;background:#e7eaee;border-radius:99px;overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--ok);width:0}

  .body{display:grid;grid-template-columns:minmax(0,1fr) 7px var(--aside,560px);
    overflow:hidden}
  main{overflow:auto;padding:16px 18px 24px}
  #split{cursor:col-resize;background:var(--line);position:relative}
  #split:hover,#split.drag{background:var(--accent)}
  #split::after{content:"";position:absolute;left:-4px;right:-4px;top:0;bottom:0}
  aside{background:var(--panel);overflow:auto;padding:14px}

  .itemhead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:12px}
  .iid{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums}
  .url{color:var(--muted);font-size:12.5px;font-family:ui-monospace,Consolas,monospace;
    word-break:break-all}
  .chip{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);
    color:var(--muted);background:#fbfcfd}
  .chip.done{background:#e8f5ec;border-color:#bfe3ca;color:var(--ok)}

  .src{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:11px 13px;margin-bottom:11px}
  .srchead{display:flex;gap:9px;align-items:center;margin-bottom:6px}
  .srchead .k{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#fff;
    background:#64748b;border-radius:5px;padding:1px 6px}
  .srchead .name{font-weight:600;word-break:break-all}
  .desc{white-space:pre-wrap;word-break:break-word;line-height:1.75}
  .desc.zh{color:#111}
  .tr{margin-top:9px;padding-top:9px;border-top:1px dashed var(--line);
    color:#0f5132;background:#f2fbf5;border-radius:8px;padding:8px 10px;display:none;
    white-space:pre-wrap;word-break:break-word}
  .tr.show{display:block}
  .empty{color:var(--muted)}

  aside h3{font-size:13px;margin:2px 0 8px;color:var(--muted);letter-spacing:.04em}
  aside details{border:1px solid var(--line);border-radius:9px;padding:9px 11px;margin-bottom:13px}
  aside summary{cursor:pointer;font-weight:600;font-size:13px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:9px}
  .grid2 label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}
  .grid2 input{width:100%}
  .full{grid-column:1/-1}
  .hint{font-size:11.5px;color:var(--muted);margin-top:7px}
  .msg{font-size:12px;margin-top:7px}

  .cb{border:1px solid var(--line);border-radius:9px;padding:8px 10px;margin-bottom:8px}
  .cbt{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;font-size:12.5px}
  .cbt code{background:#f1f5f9;border-radius:4px;padding:0 5px}
  .tag{font-size:11px;border-radius:99px;padding:0 7px}
  .tag.b{background:var(--benign);color:var(--ok)}
  .tag.h{background:var(--harm);color:var(--err)}
  .exp{color:var(--muted);font-size:11.5px;flex:0 0 100%}
  .cbd{font-size:12.5px;color:#374151;margin-top:3px}
  .cbe{font-size:12px;color:var(--muted);margin-top:4px;word-break:break-word}
  .cbe b{color:#475569}
  .order{background:#fffbe8;border:1px solid #f5e3b3;border-radius:9px;
    padding:8px 10px;font-size:12.5px;margin-bottom:12px}

  .row.chk{display:flex;gap:7px;align-items:center;font-size:12.5px;color:var(--muted);
    margin-top:8px;flex-wrap:wrap;cursor:pointer}
  .diag{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;white-space:pre-wrap;
    background:#0f172a;color:#e2e8f0;border-radius:8px;padding:8px 10px;margin-top:8px;
    max-height:220px;overflow:auto;word-break:break-word}
  .sug{border:1px solid var(--line);border-radius:9px;padding:9px 11px;background:#fbfcfd}
  .sug .lb2{font-size:15px;font-weight:700}
  .sug .zh{color:var(--muted);font-size:12px;margin-top:2px}
  .sug .nt{font-size:12.5px;color:#374151;margin-top:6px;white-space:pre-wrap}
  .sug .acts{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap}
  .warnbox{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:8px;
    padding:7px 9px;font-size:11.5px;margin-top:8px}

  footer{background:var(--panel);border-top:1px solid var(--line);padding:10px 14px}
  .labels{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:9px}
  .lb{border:2px solid var(--line);background:#fff;border-radius:9px;padding:5px 11px;
    text-align:left;line-height:1.25}
  .lb b{font-size:13.5px}
  .lb small{display:block;color:var(--muted);font-size:11px}
  .lb.on{border-color:var(--accent);background:#eff6ff}
  .lb.on small{color:var(--accent-d)}
  .row{display:flex;gap:8px;align-items:flex-end}
  .row textarea{flex:1;resize:vertical;min-height:44px;max-height:130px}
  .note-wrap{flex:1;display:flex;flex-direction:column;gap:3px}
  .cnt{font-size:11px;color:var(--muted);text-align:right}

  #toast{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:9;
    display:flex;flex-direction:column;gap:7px;align-items:center;pointer-events:none}
  #toast div{background:#111827;color:#fff;border-radius:8px;padding:7px 14px;font-size:13px;
    box-shadow:0 6px 20px rgba(0,0,0,.18);max-width:70vw}
  #toast div.ok{background:#15803d}
  #toast div.err{background:#b91c1c}
  #toast div.warn{background:#b45309}
</style>
</head>
<body>
<header>
  <span class="brand">E2 人工标注</span>
  <select id="card" title="选择要标注的卡片"></select>
  <span class="dir" id="dir"></span>
  <span class="sp"></span>
  <span class="pos">第 <b id="pos">-</b> / <span id="n">-</span> 条</span>
  <span class="pos">已填 <b id="filled">0</b></span>
  <span class="bar"><i id="prog"></i></span>
  <input id="jump" type="number" min="1" style="width:76px" placeholder="跳至">
  <button id="btnJump">跳转</button>
  <button id="btnUnfilled">首条未填</button>
  <button id="btnPrev">← 上一条</button>
  <button id="btnNext">下一条 →</button>
</header>

<div class="body">
  <main id="main"></main>
  <div id="split" title="拖动调整宽度；双击在宽 / 窄之间切换"></div>
  <aside>
    <details id="llmBox" open>
      <summary>LLM 连接（翻译 / 建议 / 诊断）</summary>
      <div class="grid2">
        <label class="full">端点 URL<input id="url" placeholder="https://token.sensenova.cn/v1"></label>
        <label>模型名<input id="model" placeholder="glm-5.2"></label>
        <label>超时（秒）<input id="timeout" type="number" min="5" step="5"></label>
        <label class="full">API Key（只在内存，不落盘）<input id="key" type="password" autocomplete="off"></label>
      </div>
      <div class="hint">URL 可只填域名或 <code>/v1</code>，会自动补 <code>/chat/completions</code>；已带则原样。<span id="urlEcho"></span></div>
      <label class="row chk"><input id="noproxy" type="checkbox"> 忽略系统代理（连不上时勾上再试）</label>
      <div class="row" style="margin-top:9px">
        <button id="btnConn">测试连接</button>
        <button id="btnDiag">诊断</button>
        <button id="btnTr" class="pri">翻译本条（T）</button>
      </div>
      <div class="msg" id="trMsg"></div>
      <div class="diag" id="diag" style="display:none"></div>
    </details>

    <details id="sugBox">
      <summary>LLM 建议（label + note，默认不生成）</summary>
      <div class="hint">让模型按 <code>annotate llm</code> 同一口径先判一遍，供你参考；<b>需点「采纳」才进表单</b>。</div>
      <div class="row" style="margin-top:8px">
        <button id="btnSug" class="pri">生成建议（G）</button>
      </div>
      <div class="warnbox" id="sugWarn"></div>
      <div id="sugOut" style="margin-top:9px"></div>
    </details>

    <h3>CODEBOOK（五类判定标准）</h3>
    <div class="order" id="order"></div>
    <div id="codebook"></div>

    <div class="hint">本界面不读、不显示 <code>key.csv</code>（真值），保持盲标。</div>
  </aside>
</div>

<footer>
  <div class="labels" id="labels"></div>
  <div class="row">
    <div class="note-wrap">
      <textarea id="note" placeholder="note（可留空；写一句判定理由，仲裁时用得上）"></textarea>
      <div class="cnt" id="cnt">0 字</div>
    </div>
    <button id="btnSave">仅保存 (Ctrl+S)</button>
    <button id="btnClear">清空本条</button>
    <button id="btnSkip">跳过 →</button>
    <button id="btnSubmit" class="pri">提交并下一条 (Ctrl+Enter)</button>
  </div>
</footer>
<div id="toast"></div>

<script>
const S = {card:"", items:[], idx:0, pick:"", filled:0, saving:false, dirty:false,
           labels:[], benign:[], harmful:[], zh:{},
           cfg:{url:"",model:"",timeout:60,minimal:false,no_proxy_env:false}};

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function toast(msg, kind){
  const d = document.createElement("div");
  d.className = kind || ""; d.textContent = msg;
  $("toast").appendChild(d);
  setTimeout(() => d.remove(), kind === "err" ? 6000 : 3200);
}

async function api(path, body){
  const opt = body ? {method:"POST", headers:{"Content-Type":"application/json"},
                      body:JSON.stringify(body)} : {};
  try{
    const r = await fetch(path, opt);
    const t = await r.text();
    try{ return JSON.parse(t); }
    catch(e){ return {ok:false, error:"返回不是 JSON：" + t.slice(0,200)}; }
  }catch(e){ return {ok:false, error:"请求失败：" + e.message}; }
}

function normUrl(u){
  u = (u || "").trim();
  if (!u) return "";
  if (/\/chat\/completions\b/.test(u)) return u;
  return u.replace(/\/+$/, "") + "/chat/completions";
}
function echoUrl(){
  const raw = $("url").value.trim(), u = normUrl(raw);
  $("urlEcho").innerHTML = (u && u !== raw)
    ? `　实际请求 <code>${esc(u)}</code>` : "";
}
function loadCfg(){
  try{
    const c = JSON.parse(localStorage.getItem("e2me_cfg") || "{}");
    S.cfg.url = c.url || ""; S.cfg.model = c.model || "";
    S.cfg.timeout = c.timeout || 60; S.cfg.no_proxy_env = !!c.no_proxy_env;
  }catch(e){}
  $("url").value = S.cfg.url; $("model").value = S.cfg.model;
  $("timeout").value = S.cfg.timeout; $("noproxy").checked = !!S.cfg.no_proxy_env;
  echoUrl();
}
function saveCfg(){
  S.cfg.url = $("url").value.trim(); S.cfg.model = $("model").value.trim();
  S.cfg.timeout = Number($("timeout").value) || 60;
  S.cfg.no_proxy_env = $("noproxy").checked;
  try{ localStorage.setItem("e2me_cfg", JSON.stringify({url:S.cfg.url, model:S.cfg.model,
      timeout:S.cfg.timeout, no_proxy_env:S.cfg.no_proxy_env})); }catch(e){}
  echoUrl();
  return {url:S.cfg.url, model:S.cfg.model, timeout:S.cfg.timeout,
          no_proxy_env:S.cfg.no_proxy_env, key:$("key").value.trim()};
}

function renderChrome(){
  $("pos").textContent = S.idx + 1;
  $("n").textContent = S.items.length;
  $("filled").textContent = S.filled;
  $("prog").style.width = (S.items.length ? (S.filled / S.items.length * 100) : 0) + "%";
  $("btnPrev").disabled = S.idx <= 0;
  $("btnNext").disabled = S.idx >= S.items.length - 1;
}

function renderLabels(){
  $("labels").innerHTML = S.labels.map((lb, i) => {
    const kind = S.benign.includes(lb) ? "良性" : "有害";
    return `<button class="lb" data-lb="${lb}"><b>${i+1} ${esc(S.zh[lb] || lb)}</b>` +
           `<small><code>${esc(lb)}</code> · ${kind}</small></button>`;
  }).join("") +
  `<button class="lb" data-lb=""><b>0 清空</b><small>本条不标</small></button>`;
  $("labels").querySelectorAll(".lb").forEach(b =>
    b.onclick = () => selectLabel(b.dataset.lb));
}

function selectLabel(lb){
  S.pick = lb; S.dirty = true;
  $("labels").querySelectorAll(".lb").forEach(b =>
    b.classList.toggle("on", b.dataset.lb === lb));
}

function render(){
  const it = S.items[S.idx];
  if (!it) return;
  const head = `<div class="itemhead">
    <span class="iid">${esc(it.item_id)}</span>
    <span class="chip">${it.sources.length} 个来源</span>
    ${it.label ? `<span class="chip done">已标：${esc(it.label)}</span>`
               : `<span class="chip">未标</span>`}
    ${it.raw_label && !it.label
        ? `<span class="chip" style="color:var(--err);border-color:#f3c2c2">
           卡里是「${esc(it.raw_label)}」——认不出来，重选一次即可覆盖</span>` : ""}
    <span class="url">${esc(it.norm_url)}</span></div>`;

  const body = it.sources.map(s => `<div class="src">
      <div class="srchead"><span class="k">[${s.k}]</span>
        <span class="name">${esc(s.src || "(无名来源)")}</span></div>
      <div class="desc">${s.desc ? esc(s.desc) : '<span class="empty">（该源无描述）</span>'}</div>
      <div class="tr" id="tr${s.k}"></div></div>`).join("");

  $("main").innerHTML = head + body;
  $("main").scrollTop = 0;
  $("note").value = it.note || "";
  if ($("sugOut")) $("sugOut").innerHTML = "";
  updateCnt();
  selectLabel(it.label || "");
  S.dirty = false;
  renderChrome();
}

function updateCnt(){
  $("cnt").textContent = $("note").value.length + " 字";
}

function goTo(i){
  if (i < 0 || i >= S.items.length) return;
  if (S.dirty && !confirm("本条的修改还没保存，确定离开？")) return;
  S.idx = i; render();
}
const nextItem = () => goTo(S.idx + 1);
const prevItem = () => goTo(S.idx - 1);

function firstUnfilled(){
  const i = S.items.findIndex(x => !x.label);
  if (i < 0){ toast("全部已填", "ok"); return; }
  if (S.dirty && !confirm("本条的修改还没保存，确定离开？")) return;
  S.idx = i; render();
  toast("跳到第一条未填：" + S.items[i].item_id, "ok");
}

async function save(advance, clear){
  if (S.saving) return;
  if (advance && !S.pick){ toast("请先选一个 label（按 1-5）再提交", "warn"); return; }
  if (advance && S.pick && !S.labels.includes(S.pick)){ toast("label 非法", "err"); return; }
  const it = S.items[S.idx];
  S.saving = true; setBusy(true);
  const r = await api("/api/save", {card:S.card, item_id:it.item_id,
      label: clear ? "" : S.pick, note: clear ? "" : $("note").value, clear: !!clear});
  S.saving = false; setBusy(false);
  if (!r.ok){ toast(r.error || "保存失败", "err"); return; }
  it.label = r.label; it.note = r.note; it.raw_label = "";
  S.filled = r.filled;
  S.dirty = false;
  toast(`已写入 ${r.file}（${S.filled}/${r.n_items}）`, "ok");
  renderChrome();
  if (advance) nextItem();
}

function setBusy(b){
  ["btnSubmit","btnSave","btnClear","btnSkip","btnPrev","btnNext"]
    .forEach(id => { $(id).disabled = b; });
  if (!b) renderChrome();
}

async function translate(){
  const it = S.items[S.idx];
  if (!it) return;
  const cfg = saveCfg();
  if (!cfg.url || !cfg.model || !cfg.key){
    toast("先在右侧填 端点 URL / 模型名 / API Key", "warn");
    $("llmBox").open = true;
    return;
  }
  $("trMsg").style.color = "var(--muted)";
  $("trMsg").textContent = "翻译中…";
  $("btnTr").disabled = true;
  const r = await api("/api/translate", Object.assign({sources: it.sources,
      minimal: S.cfg.minimal}, cfg));
  $("btnTr").disabled = false;
  if (!r.ok){
    $("trMsg").style.color = "var(--err)";
    $("trMsg").textContent = "失败：" + (r.error || "未知错误");
    toast("翻译失败：" + (r.error || ""), "err");
    return;
  }
  if (r.minimal) S.cfg.minimal = true;
  it.sources.forEach(s => {
    const el = $("tr" + s.k);
    const t = r.translations[String(s.k)];
    if (!t){ el.classList.remove("show"); return; }
    el.innerHTML = `<b style="font-size:11.5px;color:#15803d">译文</b> ` + esc(t);
    el.classList.add("show");
  });
  $("trMsg").style.color = "var(--ok)";
  $("trMsg").textContent = `完成（${r.ms} ms${r.minimal ? "，兼容模式" : ""}）`;
}

async function testConn(){
  const cfg = saveCfg();
  $("trMsg").style.color = "var(--muted)";
  $("trMsg").textContent = "测试中…";
  $("btnConn").disabled = true;
  const r = await api("/api/verify", cfg);
  $("btnConn").disabled = false;
  if (r.ok){
    S.cfg.minimal = !!r.minimal;
    $("trMsg").style.color = "var(--ok)";
    $("trMsg").textContent = `连接正常（${r.ms} ms，${r.mode}）`;
    toast("连接正常", "ok");
  }else{
    $("trMsg").style.color = "var(--err)";
    $("trMsg").textContent = "失败：" + (r.error || "");
    toast("连接失败：" + (r.error || ""), "err");
  }
}

async function suggest(){
  const it = S.items[S.idx];
  if (!it) return;
  const cfg = saveCfg();
  if (!cfg.url || !cfg.model || !cfg.key){
    toast("先在右侧填 端点 URL / 模型名 / API Key", "warn");
    $("llmBox").open = true; $("sugBox").open = true; return;
  }
  $("sugOut").innerHTML = '<span class="hint">生成中…</span>';
  $("btnSug").disabled = true;
  const r = await api("/api/suggest", Object.assign({item:it, minimal:S.cfg.minimal}, cfg));
  $("btnSug").disabled = false;
  if (!r.ok){
    if (r.minimal != null) S.cfg.minimal = !!r.minimal;
    $("sugOut").innerHTML = `<span style="color:var(--err)">失败：${esc(r.error || "未知错误")}</span>`;
    toast("生成建议失败：" + (r.error || ""), "err");
    return;
  }
  S.cfg.minimal = !!r.minimal;
  const lb = r.label || "";
  $("sugOut").innerHTML = `<div class="sug">
    <div class="lb2">${esc(r.label_zh || lb)} <code>${esc(lb)}</code></div>
    <div class="zh">${r.ms} ms · ${r.minimal ? "兼容模式" : "标准载荷"}</div>
    <div class="nt">${r.note ? esc(r.note) : '<span class="hint">（模型没给 note）</span>'}</div>
    <div class="acts">
      <button id="sugL">采纳 label</button>
      <button id="sugN">采纳 note</button>
      <button id="sugLN" class="pri">都采纳</button>
    </div></div>`;
  $("sugL").onclick = () => { if (lb) selectLabel(lb);
      toast("已填入 label（还没落卡，确认后提交）", "warn"); };
  $("sugN").onclick = () => { $("note").value = r.note || ""; updateCnt(); S.dirty = true;
      toast("已填入 note（还没落卡，确认后提交）", "warn"); };
  $("sugLN").onclick = () => { if (lb) selectLabel(lb);
      $("note").value = r.note || ""; updateCnt(); S.dirty = true;
      toast("已填入 label + note —— 提交才落卡", "warn"); };
  if (r.raw && !lb){
    $("sugOut").innerHTML += `<div class="hint" style="margin-top:7px">原始回文：<br>${esc(r.raw)}</div>`;
  }
}

async function diag(){
  const cfg = saveCfg();
  const box = $("diag");
  box.style.display = "block"; box.textContent = "诊断中…";
  $("btnDiag").disabled = true;
  const r = await api("/api/diag", cfg);
  $("btnDiag").disabled = false;
  if (r.error && !r.steps){
    box.textContent = "请求失败：" + r.error;
    toast("诊断请求失败", "err"); return;
  }
  const head = (r.url && r.url !== (r.url_raw || "")) ? `实际请求：${r.url}\n\n` : "";
  box.textContent = head + (r.report || "无结果");
  toast(r.ok ? "诊断通过：各层都通" : "诊断发现问题（详见右侧黑框）", r.ok ? "ok" : "err");
}

(function initSplit(){
  const split = $("split"), root = document.documentElement;
  const clamp = w => Math.max(320, Math.min(window.innerWidth - 360, w));
  const saved = Number(localStorage.getItem("e2me_aside"));
  if (saved) root.style.setProperty("--aside", clamp(saved) + "px");
  const cur = () => parseInt(getComputedStyle(root).getPropertyValue("--aside")) || 560;
  let dragging = false;
  split.addEventListener("mousedown", e => {
    dragging = true; split.classList.add("drag");
    document.body.style.userSelect = "none"; e.preventDefault();
  });
  window.addEventListener("mousemove", e => {
    if (dragging) root.style.setProperty("--aside", clamp(window.innerWidth - e.clientX) + "px");
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; split.classList.remove("drag");
    document.body.style.userSelect = "";
    try{ localStorage.setItem("e2me_aside", cur()); }catch(e){}
  });
  split.addEventListener("dblclick", () => {
    const w = clamp(cur() >= 780 ? 560 : 900);
    root.style.setProperty("--aside", w + "px");
    try{ localStorage.setItem("e2me_aside", w); }catch(e){}
  });
})();

async function boot(card){
  const r = await api("/api/bundle?card=" + encodeURIComponent(card));
  if (!r.ok){ toast(r.error || "加载失败", "err"); return; }
  S.card = r.card; S.items = r.items; S.idx = 0; S.filled = r.filled;
  S.labels = r.labels; S.benign = r.benign; S.harmful = r.harmful; S.zh = r.label_zh;
  $("dir").textContent = r.dir;
  $("card").innerHTML = r.cards.map(c =>
    `<option value="${c.name}"${c.name === r.card ? " selected" : ""}>` +
    `${r.label_zh ? "label_" : ""}${c.name}.csv${c.exists ? "" : "（新建）"}</option>`).join("");
  $("timeout").value = $("timeout").value || r.timeout_default;
  $("order").textContent = r.order_hint;
  $("sugWarn").textContent = r.warn || "采纳模型建议会抬高人–机一致性；要保 Me 为纯人类基准请盲标。";
  $("codebook").innerHTML = r.codebook.map((c, i) => `<div class="cb">
      <div class="cbt"><b>${i+1}</b> ${esc(c.zh)} <code>${esc(c.label)}</code>
        <span class="tag ${c.kind === "良性" ? "b" : "h"}">${c.kind}</span>
        <span class="exp">E1 自动档预期：${esc(c.expect)}</span></div>
      <div class="cbd">${esc(c.def)}</div>
      <div class="cbe"><b>举例</b> ${esc(c.example)}</div></div>`).join("");
  renderLabels();
  render();
  if (r.invalid && r.invalid.length){
    toast(`有 ${r.invalid.length} 条卡里的 label 认不出来（已在条目顶部提示）`, "warn");
  }
  const first = S.items.findIndex(x => !x.label);
  toast(first > 0 ? `本条已填，第 ${first+1} 条起仍未填（可用「首条未填」）`
                  : "从第一条开始", "ok");
}

$("card").onchange = e => {
  if (S.dirty && !confirm("本条的修改还没保存，确定切换卡片？")){
    e.target.value = S.card; return;
  }
  boot(e.target.value);
};
$("note").oninput = () => { S.dirty = true; updateCnt(); };
$("btnPrev").onclick = prevItem;
$("btnNext").onclick = nextItem;
$("btnSkip").onclick = () => { S.dirty = false; nextItem(); };
$("btnSubmit").onclick = () => save(true, false);
$("btnSave").onclick = () => save(false, false);
$("btnClear").onclick = () => { if (confirm("清空本条 label 与 note？")) save(false, true); };
$("btnTr").onclick = translate;
$("btnConn").onclick = testConn;
$("btnSug").onclick = suggest;
$("btnDiag").onclick = diag;
$("url").oninput = echoUrl;
$("noproxy").onchange = () => saveCfg();
$("btnUnfilled").onclick = firstUnfilled;
$("btnJump").onclick = () => {
  const v = Number($("jump").value);
  if (v >= 1 && v <= S.items.length) goTo(v - 1);
};
$("jump").onkeydown = e => { if (e.key === "Enter") $("btnJump").click(); };

document.addEventListener("keydown", e => {
  const t = e.target || {};
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter"){ e.preventDefault(); save(true, false); return; }
  if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")){ e.preventDefault(); save(false, false); return; }
  if (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT") return;
  if (e.key === "ArrowRight"){ e.preventDefault(); nextItem(); }
  else if (e.key === "ArrowLeft"){ e.preventDefault(); prevItem(); }
  else if ("12345".includes(e.key)) selectLabel(S.labels[Number(e.key) - 1]);
  else if (e.key === "0") selectLabel("");
  else if (e.key === "t" || e.key === "T") translate();
  else if (e.key === "g" || e.key === "G") suggest();
  else if (e.key === "u" || e.key === "U") firstUnfilled();
});

loadCfg();
boot("Me");
</script>
</body>
</html>
"""


class _GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    out_dir: Path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-audit-annotate-me-gui"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: object, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 约定
        parts = urlsplit(self.path)
        if parts.path in ("/", "/index.html"):
            return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        if parts.path == "/api/bundle":
            card = (parse_qs(parts.query).get("card") or ["Me"])[0]
            try:
                return self._json(_bundle_payload(self.server.out_dir, card))
            except (OSError, KeyError, ValueError) as exc:
                return self._json({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"ok": False, "error": f"未知路径 {parts.path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        body = self._body()
        try:
            if path == "/api/save":
                return self._json(_save_one(self.server.out_dir, body))
            if path == "/api/verify":
                return self._json(_do_verify(self.server.out_dir, body))
            if path == "/api/translate":
                return self._json(_do_translate(self.server.out_dir, body))
            if path == "/api/suggest":
                return self._json(_do_suggest(self.server.out_dir, body))
            if path == "/api/diag":
                return self._json(_do_diag(self.server.out_dir, body))
        except (OSError, KeyError, ValueError) as exc:
            return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"ok": False, "error": f"未知路径 {path}"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mcp_audit.annotate_me_gui",
        description="E2 人工标注 GUI：看 sheet → 填 label/note → 落真人卡")
    ap.add_argument("--dir", default=None, help="标注包目录（默认 DATA_DIR/annotation）")
    ap.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认只绑本机）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8766）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    out_dir = _out_dir(args.dir)
    if not (out_dir / "sheet.csv").exists():
        raise SystemExit(f"[x] {out_dir / 'sheet.csv'} 不存在 —— 先跑 `annotate sample`")
    url = f"http://{args.host}:{args.port}/"

    print()
    print("=== E2 人工标注 GUI（看 sheet → 填 label/note → 落真人卡） ===")
    print(f"标注包目录 : {out_dir}")
    print(f"可标注卡片 : {' / '.join(HUMAN_CARDS)}（LLM 卡不在受理范围）")
    print(f"控制台地址 : {url}")
    print("LLM 端点  : URL 会自动补 /chat/completions；连不上可勾「忽略系统代理」或用「诊断」逐层排查。")
    print("右侧栏    : 可拖拽调宽（双击在宽/窄间切换）；「LLM 建议」面板默认收起。")
    print("API Key 只在内存与本机请求体里流转，不落盘；Ctrl+C 退出。")
    print()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    srv = _GuiServer((args.host, args.port), _Handler)
    srv.out_dir = out_dir
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
