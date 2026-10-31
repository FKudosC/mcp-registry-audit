"""M2 **LLM 交叉核验控制台**：填「端点 URL + 模型名」→ 验证 → 开始判定。

M2 补做阶段的前端，**复用**第一篇 ``annotate_gui``（E2 LLM 控制台）的结构与交互，但
判定口径换成 M2：9 类危险面、三值 ``present``/``absent``/``unsure``、M2 版 ``sheet.csv``，
运行器改调 ``agcm_audit.llm``（逐类三值判定、落 ``label_<模型>.csv``、``llm_cache.json``
断点续跑）。它与 ``agcm_annotate_me_gui``（M2 人工前端）**完全隔离**：独立进程、独立端口
（默认 8775）、独立文件；两者只共享 ``agcm_audit`` 里同一份 codebook 常量（**只读**）。

设计要点
--------
  - 只用标准库 ``http.server``（项目未装 Web 框架），``ThreadingHTTPServer`` 绑在
    ``127.0.0.1``；长任务丢后台线程，前端轮询进度。
  - **API Key 绝不落盘**：只在请求体与本机内存里流转。落盘的 ``llm_ui.json`` 只存
    端点 URL / 模型名。为免「刷新即丢」，Key 在**本进程内存**里暂存（``_SESSION_KEYS``），
    页面刷新可回填；进程重启即清空。
  - 开跑前服务端二次校验：「该模型本次已用**同一 URL + 模型名**验证通过」，否则拒绝
    —— 把「验证成功后才判定」钉在服务端，不只靠前端。
  - 判定可直接中止（``should_stop``），已标部分落卡不丢，靠 ``llm_cache.json`` 续跑。

用法：
    python -m mcp_audit.agcm_annotate_gui               # 127.0.0.1:8775，自动开浏览器
    python -m mcp_audit.agcm_annotate_gui --port 8800 --no-browser
    python -m mcp_audit.agcm_annotate_gui --dir D:\\Data\\prov-mcp\\m2

    # 路线2b 厚层重标：默认落点 D:\\Data\\prov-mcp\\m2b、独立端口 8776
    python -m mcp_audit.agcm_annotate_gui --route2b
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
import webbrowser
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import agcm_audit as m2
from . import annotation

HTML_NAME = "agcm_annotate_gui.html"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8775
PORT_R2B = 8776                    # 路线2b 厚层模式独立端口（避免与 m2 控制台抢口）
PROFILE_M2 = "m2"                  # 薄层：M2 全量（一致项已由 LLM 标 + 人工盲审抽样）
PROFILE_R2B = "route2b"            # 厚层：路线2b 分歧集 D 加厚重标（材料 maxlen 4000）
R2B_NOTICE = ("路线2b · 厚层重标：仅对**分歧集 D**用加厚材料（maxlen 4000 / max_files 200）"
              "重跑三家 LLM；薄层一致项不动。两层层材料条件不同，信度/效度须**分层报告**，"
              "不得合并为单一 κ。")
LOG_MAX = 500
DEFAULT_SLEEP = 0.6
DEFAULT_TIMEOUT = 60.0
REP_MAX = 5
WORKER_MAX = annotation.WORKERS_MAX
WORKER_DEFAULT = annotation.WORKERS_DEFAULT

M2_LABEL_ZH = {"present": "存在", "absent": "不存在", "unsure": "存疑"}

_LOCK = threading.Lock()
# 会话内 API Key **内存暂存**：只在本进程内存，供页面刷新后回填；**绝不写盘**。
# 进程重启即清空（凭据不留痕）。见 ``_load_ui_config`` / ``_save_ui_config``。
_SESSION_KEYS: dict = {"relay": "", "providers": {}}
_STATE: dict = {
    "running": False,
    "stop": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "items": 0,
    "total": 0,
    "reps": 1,
    "workers": WORKER_DEFAULT,
    "parallel": False,
    "rate": 0.0,
    "eta": 0.0,
    "order": [],
    "models": {},
    "log": deque(maxlen=LOG_MAX),
    "result": None,
    "error": "",
    "verified": {},
}


def _out_dir(path: str | None, profile: str = PROFILE_M2) -> Path:
    """解析标注包目录：显式 ``--dir`` 优先，否则按 profile 落到 m2 / m2b。"""
    if path:
        return Path(path)
    return m2.M2B_DIR if profile == PROFILE_R2B else m2.M2_DIR


def _to_jsonable(obj: object) -> object:
    """把 Counter / set / Path / NaN 等一律转成 JSON 可序列化形态。"""
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [_to_jsonable(x) for x in sorted(obj, key=str)]
    if isinstance(obj, (list, tuple, deque)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


def _log(level: str, text: str) -> None:
    _STATE["log"].append({"t": time.strftime("%H:%M:%S"), "lv": level, "s": text})


def _sheet_items(out_dir: Path) -> list[dict]:
    """读 ``sheet.csv`` → 条目列表（含仓库键 / 能力类 / 文件清单 / 源码片段），保持行序。"""
    sheet = out_dir / m2.SHEET_CSV
    if not sheet.exists():
        return []
    items: list[dict] = []
    for row in m2._read_csv(sheet):
        iid = (row.get("item_id") or "").strip()
        if not iid:
            continue
        items.append({
            "item_id": iid,
            "repo_key": (row.get("repo_key") or "").strip(),
            "category": (row.get("category") or "").strip(),
            "category_zh": (row.get("category_zh") or "").strip(),
            "file_list": (row.get("file_list") or "").strip(),
            "snippet": row.get("snippet") or "",
        })
    return items


def _probe_prompt(out_dir: Path) -> str:
    """连接测试用的样例 prompt：优先取 sheet 首条，否则用一条合成样例。"""
    items = _sheet_items(out_dir)
    item = items[0] if items else {
        "repo_key": "demo/repo", "category": "shell_true",
        "file_list": "demo.py", "snippet": "subprocess.run(cmd, shell=True)",
    }
    return m2._m2_llm_prompt(item)


# --------------------------------------------------------------------------- #
# 判定进度回调 / 后台线程
# --------------------------------------------------------------------------- #
def _on_progress(ev: dict) -> None:
    name = ev.get("name", "")
    rep = int(ev.get("rep") or 1)
    reps = int(ev.get("reps") or _STATE["reps"] or 1)
    workers = int(ev.get("workers") or _STATE["workers"] or 1)
    tag = f"r{rep}/{reps} " if reps > 1 else ""
    with _LOCK:
        m = _STATE["models"].setdefault(name, {
            "filled": 0, "cached": 0, "errors": 0, "retries": 0, "i": 0,
            "total": _STATE["total"], "status": "pending", "last_item": "",
            "last_label": "", "rep": 1, "reps": reps, "rep_done": 0,
            "workers": workers, "workers_now": workers, "gap": 0.0,
            "rate": 0.0, "eta": 0.0,
        })
        for key in ("i", "filled", "cached", "errors"):
            if ev.get(key) is not None:
                m[key] = ev[key]
        m["total"] = ev.get("total", m.get("total", 0))
        m["status"] = ev.get("status", m["status"])
        m["last_item"] = ev.get("item_id", m["last_item"])
        m["last_label"] = ev.get("label", m["last_label"])
        m["rep"] = rep
        m["reps"] = reps
        m["workers"] = workers
        if ev.get("workers_now") is not None:
            m["workers_now"] = int(ev["workers_now"])
        if ev.get("gap") is not None:
            m["gap"] = float(ev["gap"])
        if ev.get("rate") is not None:
            m["rate"] = float(ev["rate"])
            m["eta"] = float(ev.get("eta") or 0.0)
            _STATE["rate"] = m["rate"]
            _STATE["eta"] = m["eta"]
        if ev.get("status") in ("filled", "error", "cached"):
            m["rep_done"] = int(ev.get("i") or 0)
        _STATE["items"] = max(_STATE["items"], int(ev.get("i") or 0))

        st = ev.get("status")
        if st == "filled":
            _log("ok", f"{name} {tag}{ev.get('i')}/{ev.get('total')} "
                       f"{ev.get('item_id')} → {ev.get('label', '')}")
        elif st == "throttle":
            _log("warn", f"{name} {tag}限流自适应：{ev.get('note', '')}")
        elif st == "retry":
            m["retries"] = int(m.get("retries") or 0) + 1
            _log("warn", f"{name} {tag}{ev.get('i')}/{ev.get('total')} {ev.get('item_id')} "
                         f"退避重试：{ev.get('note', '')}")
        elif st == "error":
            _log("err", f"{name} {tag}{ev.get('i')}/{ev.get('total')} {ev.get('item_id')} "
                        f"调用失败：{(ev.get('note') or '')[:90]}")


def _run_summary(res: dict) -> dict:
    reps = int(res.get("reps") or 1)
    workers = int(res.get("workers") or 1)
    base = Path(res["dir"]) if res.get("dir") else None

    def _rel(p: object) -> str:
        if not p:
            return ""
        p = Path(p)
        if base:
            try:
                return p.relative_to(base).as_posix()
            except ValueError:
                pass
        return p.name

    out: dict = {"items": res.get("items"), "total": res.get("total"),
                 "stopped": res.get("stopped"), "reps": reps, "workers": workers,
                 "runs": _rel(res.get("runs")), "models": {}}
    for name, e in (res.get("models") or {}).items():
        out["models"][name] = {
            "model": e.get("model", ""), "url": e.get("url", ""),
            "filled": e.get("filled"), "cached": e.get("cached"), "errors": e.get("errors"),
            "retries": e.get("retries"), "reps": int(e.get("reps") or reps),
            "workers": int(e.get("workers") or workers),
            "throttle": e.get("throttle"),
            "skipped": e.get("skipped", ""), "stopped": bool(e.get("stopped")),
            "card": _rel(e.get("card")),
            "cards": {int(k): _rel(v) for k, v in (e.get("cards") or {}).items()},
            "last_error": e.get("last_error", ""),
        }
    return out


def _run_worker(out_dir: Path, models: list[str], limit: int, force: bool,
                timeout: float, sleep: float, providers: dict, reps: int,
                workers: int, parallel: bool = False,
                sheet_name: str = m2.SHEET_CSV) -> None:
    try:
        res = m2.llm(out_dir, models, limit, force, timeout, sleep,
                     providers=providers, on_progress=_on_progress,
                     should_stop=lambda: _STATE["stop"], reps=reps,
                     workers=workers, parallel=parallel, sheet_name=sheet_name)
        with _LOCK:
            _STATE["result"] = _run_summary(res)
            _log("info", "判定结束" + ("（已中止）" if _STATE["stop"] else ""))
    except BaseException as exc:  # noqa: BLE001 —— 后台线程必须兜住一切，否则界面静默
        with _LOCK:
            _STATE["error"] = f"{type(exc).__name__}: {exc}"
            _log("err", f"任务异常：{type(exc).__name__}: {exc}")
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["stop"] = False
            _STATE["finished_at"] = time.time()


def _start_run(out_dir: Path, models: list[str], limit: int, force: bool,
               timeout: float, sleep: float, providers: dict,
               reps: int = 1, workers: int = WORKER_DEFAULT,
               parallel: bool = False,
               sheet_name: str = m2.SHEET_CSV) -> tuple[bool, str]:
    reps = max(1, int(reps or 1))
    workers = max(1, min(int(workers or WORKER_DEFAULT), WORKER_MAX))
    parallel = bool(parallel and len(models) > 1)
    with _LOCK:
        if _STATE["running"]:
            return False, "已有判定任务在跑，先等它结束或点「中止」"
        _STATE.update(running=True, stop=False, started_at=time.time(), finished_at=0.0,
                      items=0, total=0, reps=reps, workers=workers, parallel=parallel,
                      rate=0.0, eta=0.0,
                      order=list(models), models={}, result=None, error="")
        for name in models:
            _STATE["models"][name] = {"filled": 0, "cached": 0, "errors": 0, "retries": 0,
                                      "i": 0, "total": 0, "status": "pending",
                                      "last_item": "", "last_label": "",
                                      "rep": 1, "reps": reps, "rep_done": 0,
                                      "workers": workers, "workers_now": workers,
                                      "gap": 0.0, "rate": 0.0, "eta": 0.0}
        _STATE["log"].clear()
        _log("info", f"开始判定：{', '.join(models)}"
                     + (f"｜每模型 {reps} 轮" if reps > 1 else "")
                     + (f"｜每模型 {workers} 线程" if workers > 1 else "")
                     + ("｜模型并行" if parallel else "｜模型串行"))
    threading.Thread(target=_run_worker,
                     args=(out_dir, models, limit, force, timeout, sleep, providers,
                           reps, workers, parallel, sheet_name),
                     name="agcm-annotate-llm", daemon=True).start()
    return True, ""


# --------------------------------------------------------------------------- #
# UI 配置（只存 URL / 模型名，不存 Key）
# --------------------------------------------------------------------------- #
def _reset_session_keys() -> None:
    """清空会话内存里的 Key（测试/重置用）。"""
    with _LOCK:
        _SESSION_KEYS["relay"] = ""
        _SESSION_KEYS["providers"] = {}


def _keys_snapshot() -> dict:
    """返回会话内存里 Key 的快照（深拷贝，供页面回填）。"""
    with _LOCK:
        return {"relay": _SESSION_KEYS["relay"],
                "providers": dict(_SESSION_KEYS["providers"])}


def _load_ui_config(out_dir: Path) -> dict:
    """读 UI 配置：``providers.json``（CLI 侧）作基线，``llm_ui.json``（GUI 侧）覆盖。

    只保留 url / model，**绝不读入 Key**。``providers.json`` 的 ``default.url`` 作为
    「共享中转站」URL；其 url/model 亦按具名合入各通道。返回含 ``relay`` 键。

    另附 ``keys``：本进程**内存**里暂存的 Key（页面刷新后回填用，**绝不落盘**）。
    """
    providers: dict = {}
    relay = {"url": ""}
    pj = out_dir / "providers.json"
    if pj.exists():
        try:
            raw = json.loads(pj.read_text("utf-8")) or {}
            base = raw.get("default") or {}
            if isinstance(base, dict):
                relay["url"] = str(base.get("url") or "")
            for name, v in raw.items():
                if name == "default" or not isinstance(v, dict) \
                        or name not in m2.llm_providers():
                    continue
                merged = {**base, **v}
                providers[name] = {"url": str(merged.get("url") or ""),
                                   "model": str(merged.get("model") or "")}
        except (ValueError, OSError):
            providers = {}
    path = out_dir / "llm_ui.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text("utf-8")) or {}
            rel = raw.get("relay") or {}
            if isinstance(rel, dict) and rel.get("url"):
                relay["url"] = str(rel.get("url"))
            for name, v in (raw.get("providers") or {}).items():
                if not isinstance(v, dict):
                    continue
                cur = providers.get(name) or {}
                providers[name] = {"url": str(v.get("url") or cur.get("url") or ""),
                                   "model": str(v.get("model") or cur.get("model") or "")}
        except (ValueError, OSError):
            pass
    presets = {k: {"url": v["url"], "model": v["model"]}
               for k, v in m2.llm_providers().items()}
    return {"dir": str(out_dir), "providers": providers, "presets": presets,
            "relay": relay, "keys": _keys_snapshot()}


def _save_ui_config(out_dir: Path, body: dict) -> dict:
    """落盘 URL/模型名/中转站 URL（``llm_ui.json``）；Key 只**暂存内存**，不写盘。"""
    cur = _load_ui_config(out_dir)
    prov = dict(cur["providers"])
    incoming = (body or {}).get("providers") or {}
    for name, v in incoming.items():
        if name in m2.llm_providers() and isinstance(v, dict):
            prov[name] = {"url": str(v.get("url") or "").strip(),
                          "model": str(v.get("model") or "").strip()}
    relay_url = cur["relay"]["url"]
    rel = (body or {}).get("relay")
    if isinstance(rel, dict) and rel.get("url") is not None:
        relay_url = str(rel.get("url") or "").strip()
    keys = (body or {}).get("keys")
    if isinstance(keys, dict):
        with _LOCK:
            if keys.get("relay") is not None:
                _SESSION_KEYS["relay"] = str(keys.get("relay") or "")
            prov_keys = keys.get("providers")
            if isinstance(prov_keys, dict):
                for name, k in prov_keys.items():
                    if name in m2.llm_providers():
                        _SESSION_KEYS["providers"][name] = str(k or "")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "llm_ui.json").write_text(
        json.dumps({"relay": {"url": relay_url}, "providers": prov},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "providers": prov, "relay": {"url": relay_url},
            "keys": _keys_snapshot()}


# --------------------------------------------------------------------------- #
# 各 API 载荷
# --------------------------------------------------------------------------- #
def _state_payload(out_dir: Path, profile: str = PROFILE_M2) -> dict:
    """M2 总览：条目数取自 ``sheet.csv``，各卡按**三值**归一后计已填。

    ``profile`` 决定层标签（thin / thick）与提示语：路线2b 厚层模式额外返回
    ``layer="thick"`` 与 ``notice``，供前端显示「仅分歧集 D」banner。
    """
    out_dir = Path(out_dir)
    items = _sheet_items(out_dir)
    n_items = len(items)
    ids = {it["item_id"] for it in items}
    # 通道顺序：薄层只跑前三家；路线2b 厚层追加第 4 家 StepFun（step-5-preview）认证通道。
    llm_names = (list(m2.llm_providers()) if profile == PROFILE_R2B
                 else list(m2.LLM_DEFAULT))

    annotators: dict = {}
    files: list[str] = []
    invalid: list[dict] = []
    for path in sorted(out_dir.glob(f"{m2.LABEL_PREFIX}*.csv")):
        name = path.stem[len(m2.LABEL_PREFIX):]
        if not name:
            continue
        files.append(path.name)
        dist: Counter = Counter()
        done = 0
        for row in m2._read_csv(path):
            iid = (row.get("item_id") or "").strip()
            raw = (row.get("label") or "").strip()
            lb = m2._norm_m2_label(raw)
            if lb:
                dist[lb] += 1
                if not ids or iid in ids:
                    done += 1
            elif raw:
                invalid.append({"file": path.name, "annotator": name,
                                "item_id": iid, "raw": raw[:40]})
        if name in m2.llm_providers():
            kind = "LLM"
        elif name in m2.ROSTER_DEFAULT:
            kind = "人"
        else:
            kind = "其他"
        annotators[name] = {"done": done, "kind": kind, "dist": dict(dist)}

    cache: dict = {}
    cpath = out_dir / m2.LLM_CACHE_JSON
    if cpath.exists():
        try:
            raw = json.loads(cpath.read_text("utf-8"))
            cache = {k: len(v) for k, v in (raw or {}).items()}
        except (ValueError, OSError):
            cache = {}

    return {
        "dir": str(out_dir),
        "profile": profile,
        "layer": "thick" if profile == PROFILE_R2B else "thin",
        "notice": R2B_NOTICE if profile == PROFILE_R2B else "",
        "n_items": n_items,
        "annotators": annotators,
        "llm_cache": cache,
        "files": files,
        "labels": list(m2.M2_LABELS),
        "label_zh": dict(M2_LABEL_ZH),
        "roster": list(m2.ROSTER_DEFAULT),
        "llm_names": llm_names,
        "llm_default": list(m2.LLM_DEFAULT),  # 前三家（残余子集模式下应被排除，仅跑第 4 家）
        "sheet_exists": (out_dir / m2.SHEET_CSV).exists(),
        "running": _STATE["running"],
        "invalid": invalid,
        "reps": _STATE["reps"],
        "rep_max": REP_MAX,
        "workers": _STATE["workers"],
        "worker_max": WORKER_MAX,
        "worker_default": WORKER_DEFAULT,
        "parallel": _STATE["parallel"],
    }


def _progress_payload() -> dict:
    with _LOCK:
        return {
            "running": _STATE["running"],
            "stop": _STATE["stop"],
            "items": _STATE["items"],
            "total": _STATE["total"],
            "reps": _STATE["reps"],
            "workers": _STATE["workers"],
            "parallel": _STATE["parallel"],
            "rate": _STATE["rate"],
            "eta": _STATE["eta"],
            "elapsed": (time.time() - _STATE["started_at"]) if _STATE["started_at"] else 0.0,
            "order": list(_STATE["order"]),
            "models": {k: dict(v) for k, v in _STATE["models"].items()},
            "log": list(_STATE["log"]),
            "result": _STATE["result"],
            "error": _STATE["error"],
            "verified": {k: dict(v) for k, v in _STATE["verified"].items()},
        }


def _do_verify(out_dir: Path, body: dict) -> dict:
    """连接测试：真跑一条样例，必须能解析出三值标签才算通过；顺带探明是否需最小载荷。"""
    reg = m2.llm_providers()
    name = str(body.get("provider") or "").strip()
    if name not in reg:
        return {"ok": False, "error": f"未知模型 {name!r}"}
    prov = {**(reg.get(name) or {}),
            "url": str(body.get("url") or "").strip(),
            "model": str(body.get("model") or "").strip()}
    key = str(body.get("key") or "").strip()
    if not key:
        return {"ok": False, "ms": 0, "mode": "", "minimal": False,
                "label": "", "note": "", "url": prov.get("url", ""),
                "error": "没填 API Key"}
    try:
        timeout = float(body.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    t0 = time.time()
    try:
        text = m2._m2_llm_call(prov, _probe_prompt(out_dir), key, timeout)
        err = ""
    except (RuntimeError, KeyError, ValueError, TypeError, OSError) as exc:
        text, err = "", f"{type(exc).__name__}: {exc}"
    ms = int((time.time() - t0) * 1000)
    mode = "标准载荷"
    if err:
        _log("err", f"验证 {name}：失败｜{err}")
        with _LOCK:
            _STATE["verified"][name] = {"ok": False, "url": prov.get("url", ""),
                                        "model": prov.get("model", ""), "mode": mode,
                                        "minimal": False, "at": time.time()}
        return {"ok": False, "ms": ms, "mode": mode, "minimal": False,
                "label": "", "note": "", "url": prov.get("url", ""), "error": err,
                "provider": {"url": prov.get("url", ""), "model": prov.get("model", "")}}
    lb, note = m2._parse_m2_json(text, "")
    with _LOCK:
        _STATE["verified"][name] = {"ok": bool(lb), "url": prov.get("url", ""),
                                    "model": prov.get("model", ""), "mode": mode,
                                    "minimal": False, "at": time.time()}
        _log("ok" if lb else "err",
             f"验证 {name}：{'通过' if lb else '失败'}｜{mode}｜{ms}ms"
             + ("" if lb else "｜返回里解析不出三值标签"))
    return {"ok": bool(lb), "ms": ms, "mode": mode, "minimal": False,
            "label": lb, "note": note, "url": prov.get("url", ""),
            "raw": text.strip()[:400],
            "error": "" if lb else "返回里解析不出三值标签（检查是不是对话模型）",
            "provider": {"url": prov.get("url", ""), "model": prov.get("model", "")}}


def _do_run(out_dir: Path, body: dict) -> dict:
    models = [str(m) for m in (body.get("models") or []) if str(m) in m2.llm_providers()]
    if not models:
        return {"ok": False, "error": "未选择要判定的模型"}
    raw = body.get("providers") or {}
    providers: dict = {}
    for name in models:
        p = raw.get(name) or {}
        url = str(p.get("url") or "").strip()
        model = str(p.get("model") or "").strip()
        key = str(p.get("key") or "").strip()
        if not url or not model:
            return {"ok": False, "error": f"{name}：URL 或模型名没填"}
        if not key:
            return {"ok": False, "error": f"{name}：没填 API Key"}
        providers[name] = {"url": url, "model": model, "key": key}
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "error": "已有判定任务在跑"}
        for name in models:
            v = _STATE["verified"].get(name) or {}
            if (not v.get("ok") or v.get("url") != providers[name]["url"]
                    or v.get("model") != providers[name]["model"]):
                return {"ok": False,
                        "error": f"{name} 还没通过验证（或验证后又改了 URL / 模型名）。请先点「验证」。"}
            if v.get("minimal"):
                providers[name]["minimal"] = True
    _save_body = {"providers": {n: {"url": providers[n]["url"],
                                    "model": providers[n]["model"]} for n in models}}
    if body.get("relay_url") is not None:
        _save_body["relay"] = {"url": str(body.get("relay_url") or "")}
    _save_body["keys"] = {"providers": {n: providers[n]["key"] for n in models}}
    _save_ui_config(out_dir, _save_body)
    try:
        limit = int(body.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        timeout = float(body.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    sleep = body.get("sleep", None)
    if sleep is None or sleep == "":
        sleep = DEFAULT_SLEEP
    else:
        try:
            sleep = max(float(sleep), 0.0)
        except (TypeError, ValueError):
            sleep = DEFAULT_SLEEP
    try:
        reps = int(body.get("reps") or 1)
    except (TypeError, ValueError):
        reps = 1
    reps = max(1, min(reps, REP_MAX))
    try:
        workers = int(body.get("workers") or WORKER_DEFAULT)
    except (TypeError, ValueError):
        workers = WORKER_DEFAULT
    workers = max(1, min(workers, WORKER_MAX))
    parallel = bool(body.get("parallel"))
    scope = str(body.get("scope") or "").strip()
    sheet_name = m2.SHEET_CSV
    if scope == "residual":
        # 省 token：只跑厚层三家仍分歧的残余子集。**仅供第 4 家**——对三家使用会把其
        # 满量卡缩成残余行数（llm 按 sheet 全行回写卡）而丢失共识项。
        bad = [n for n in models if n in set(m2.LLM_DEFAULT)]
        if bad:
            return {"ok": False, "error": (
                f"残余子集只能对第 4 家模型跑（否则会缩小 {'/'.join(bad)} 的满量卡）；"
                "请只勾选第 4 家。")}
        try:
            # 残余子集是三家厚层卡的派生物，每次重跑都按当前卡重算（幂等、可安全覆盖）
            info = m2.residual_sheet(out_dir, force=True)
        except SystemExit as exc:
            return {"ok": False, "error": str(exc)}
        if info["residual"] == 0:
            return {"ok": False, "error": "无残余分歧项（三家已全一致）—— 无需跑第 4 家"}
        sheet_name = m2.RESIDUAL_SHEET_CSV
    ok, err = _start_run(out_dir, models, limit, bool(body.get("force")), timeout, sleep,
                         providers, reps, workers, parallel, sheet_name)
    return {"ok": ok, "error": err}


def _score_payload(out_dir: Path) -> dict:
    """M2 信度 / 效度：读人工金标算逐类 P/R/F1 + 双真人 κ + 放行闸，并附三方混淆矩阵。"""
    out_dir = Path(out_dir)
    res = m2.score(out_dir)
    mx = m2.matrix(out_dir)
    return {
        "dir": str(out_dir),
        "n_items": len(_sheet_items(out_dir)),
        "labels": list(m2.M2_LABELS),
        "label_zh": dict(M2_LABEL_ZH),
        "gold_source": res.get("gold_source"),
        "gate": res.get("gate"),
        "overall": res.get("overall"),
        "per_class": res.get("per_class"),
        "matrix": mx.get("rows") or [],
        "files": [Path(f).name for f in (res.get("files") or [])]
        + [Path(f).name for f in (mx.get("files") or [])],
    }


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
class _GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    out_dir: Path
    html_path: Path
    profile: str = PROFILE_M2


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-audit-agcm-annotate-gui"

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
        body = json.dumps(_to_jsonable(obj), ensure_ascii=False).encode("utf-8")
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
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = Path(self.server.html_path).read_bytes()
            except OSError as exc:
                return self._json({"error": f"读不到前端文件：{exc}"}, 500)
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(_state_payload(self.server.out_dir, self.server.profile))
        if path == "/api/progress":
            return self._json(_progress_payload())
        if path == "/api/config":
            return self._json(_load_ui_config(self.server.out_dir))
        return self._json({"error": f"未知路径 {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        body = self._body()
        if path == "/api/config":
            return self._json(_save_ui_config(self.server.out_dir, body))
        if path == "/api/verify":
            return self._json(_do_verify(self.server.out_dir, body))
        if path == "/api/run":
            return self._json(_do_run(self.server.out_dir, body))
        if path == "/api/cancel":
            with _LOCK:
                if _STATE["running"]:
                    _STATE["stop"] = True
                    _log("info", "收到中止请求，当前条目标完后停…")
            return self._json({"ok": True})
        if path == "/api/score":
            if self.server.profile == PROFILE_R2B:
                return self._json({
                    "error": "路线2b 厚层模式不做单层信度/效度；"
                             "请用 `agcm-audit route2b-gold` 组装分层金标与分层报告。"})
            try:
                return self._json(_score_payload(self.server.out_dir))
            except (OSError, KeyError, ValueError) as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": f"未知路径 {path}"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mcp_audit.agcm_annotate_gui",
        description="M2 LLM 交叉核验控制台：填 URL / 模型名 → 验证 → 判定")
    ap.add_argument("--dir", default=None, help="标注包目录（默认 m2；--route2b 时为 m2b）")
    ap.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认只绑本机）")
    ap.add_argument("--port", type=int, default=None,
                    help=f"端口（默认 {DEFAULT_PORT}；--route2b 时为 {PORT_R2B}）")
    ap.add_argument("--route2b", action="store_true",
                    help="路线2b 厚层重标模式：默认落点 m2b、独立端口，仅对分歧集 D 重标")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    profile = PROFILE_R2B if args.route2b else PROFILE_M2
    port = args.port if args.port is not None else (
        PORT_R2B if profile == PROFILE_R2B else DEFAULT_PORT)
    out_dir = _out_dir(args.dir, profile)
    if not (out_dir / m2.SHEET_CSV).exists():
        raise SystemExit(f"[x] {out_dir / m2.SHEET_CSV} 不存在 —— 先跑 `agcm-audit sheet`"
                         + (" / `thick-sheet`" if profile == PROFILE_R2B else ""))
    html_path = Path(__file__).with_name(HTML_NAME)
    if not html_path.exists():
        raise SystemExit(f"[x] 找不到前端文件 {html_path}")

    srv = _GuiServer((args.host, port), _Handler)
    srv.out_dir = out_dir
    srv.html_path = html_path
    srv.profile = profile
    url = f"http://{args.host}:{port}/"

    print()
    if profile == PROFILE_R2B:
        print("=== 路线2b · AGCM LLM 厚层重标控制台（填 URL / 模型名 → 验证 → 判定） ===")
        print("模式      : 厚层（仅对**分歧集 D**用加厚材料 maxlen 4000 / max_files 200 重标）")
        print("纪律      : 薄层一致项不动；两层材料条件不同，信度/效度须**分层报告**，")
        print("            不得合并为单一 κ。单层评分面板已停用（用 route2b-gold）。")
    else:
        print("=== M2 LLM 交叉核验控制台（填 URL / 模型名 → 验证 → 判定） ===")
    print(f"标注包目录 : {out_dir}")
    shown = m2.llm_providers() if profile == PROFILE_R2B else m2.LLM_DEFAULT
    print(f"可判定模型 : {' / '.join(shown)}（跨家族交叉核验）")
    print(f"控制台地址 : {url}")
    print("判定口径  : 9 类危险面，三值 present/absent/unsure（与真人同卷，防锚定）。")
    print("落卡      : 逐模型写 label_<模型>.csv；reps>1 时第 2 轮起写 reps/r{k}/。")
    print("API Key 只在内存与本机请求体里流转，不落盘；Ctrl+C 退出。")
    print()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
