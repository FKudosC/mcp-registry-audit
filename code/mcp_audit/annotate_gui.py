"""E2 标注 GUI：本地网页控制台 —— 填「端点 URL + 模型名」→ 验证 → 开始判定。

用户用**第三方统一网关**（如商汤）调 DeepSeek / Kimi / GLM，端点与模型名都不是官方
默认值，所以把 URL / 模型名交给界面填；并且只在**验证成功之后**才放开「开始判定」，
避免 key 错 / 模型名错 / 参数不兼容时白跑 600 条。

设计要点
--------
  - 只用标准库 ``http.server``（项目未装 Web 框架），``ThreadingHTTPServer`` 绑在
    ``127.0.0.1``；长任务丢后台线程，前端轮询进度。
  - **API Key 绝不落盘**：只在请求体与本机内存里流转。落盘的 ``llm_ui.json`` 只存
    端点 URL / 模型名。
  - 开跑前服务端二次校验：「该模型本次已用**同一 URL + 模型名**验证通过」，否则拒绝
    —— 把「验证成功后才判定」钉在服务端，不只靠前端。
  - 判定可直接中止（``should_stop``），已标部分落卡不丢，靠 ``llm_cache.json`` 续跑。

用法：
    python -m mcp_audit gui                      # 起服务并打开浏览器（127.0.0.1:8765）
    python -m mcp_audit gui --port 8800 --no-browser
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

from . import annotation, config

HTML_NAME = "annotate_gui.html"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOG_MAX = 500
DEFAULT_SLEEP = 0.6
DEFAULT_TIMEOUT = 60.0
REP_MAX = 5
WORKER_MAX = annotation.WORKERS_MAX
WORKER_DEFAULT = annotation.WORKERS_DEFAULT

_LOCK = threading.Lock()
_STATE: dict = {
    "running": False,
    "stop": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "items": 0,
    "total": 0,
    "reps": 1,
    "workers": WORKER_DEFAULT,
    "rate": 0.0,
    "eta": 0.0,
    "order": [],
    "models": {},
    "log": deque(maxlen=LOG_MAX),
    "result": None,
    "error": "",
    "verified": {},
}


def _out_dir(path: str | None) -> Path:
    return Path(path) if path else config.DATA_DIR / "annotation"


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
                workers: int) -> None:
    try:
        res = annotation.llm(out_dir, models, limit, force, timeout, sleep,
                             providers=providers, on_progress=_on_progress,
                             should_stop=lambda: _STATE["stop"], reps=reps,
                             workers=workers)
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
               reps: int = 1, workers: int = WORKER_DEFAULT) -> tuple[bool, str]:
    reps = max(1, int(reps or 1))
    workers = max(1, min(int(workers or WORKER_DEFAULT), WORKER_MAX))
    with _LOCK:
        if _STATE["running"]:
            return False, "已有判定任务在跑，先等它结束或点「中止」"
        _STATE.update(running=True, stop=False, started_at=time.time(), finished_at=0.0,
                      items=0, total=0, reps=reps, workers=workers, rate=0.0, eta=0.0,
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
                     + (f"｜每模型 {workers} 线程" if workers > 1 else ""))
    threading.Thread(target=_run_worker,
                     args=(out_dir, models, limit, force, timeout, sleep, providers,
                           reps, workers),
                     name="annotate-llm", daemon=True).start()
    return True, ""


# --------------------------------------------------------------------------- #
# UI 配置（只存 URL / 模型名，不存 Key）
# --------------------------------------------------------------------------- #
def _load_ui_config(out_dir: Path) -> dict:
    providers: dict = {}
    path = out_dir / "llm_ui.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text("utf-8"))
            for name, v in ((raw or {}).get("providers") or {}).items():
                if isinstance(v, dict):
                    providers[name] = {"url": str(v.get("url") or ""),
                                       "model": str(v.get("model") or "")}
        except (ValueError, OSError):
            providers = {}
    presets = {k: {"url": v["url"], "model": v["model"]}
               for k, v in annotation.LLM_PROVIDERS.items()}
    return {"dir": str(out_dir), "providers": providers, "presets": presets}


def _save_ui_config(out_dir: Path, body: dict) -> dict:
    cur = _load_ui_config(out_dir)["providers"]
    incoming = (body or {}).get("providers") or {}
    for name, v in incoming.items():
        if name in annotation.LLM_PROVIDERS and isinstance(v, dict):
            cur[name] = {"url": str(v.get("url") or "").strip(),
                         "model": str(v.get("model") or "").strip()}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "llm_ui.json").write_text(
        json.dumps({"providers": cur}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "providers": cur}


# --------------------------------------------------------------------------- #
# 各 API 载荷
# --------------------------------------------------------------------------- #
def _state_payload(out_dir: Path) -> dict:
    st = annotation.status(out_dir)
    annotators: dict = {}
    for name, labels in st["annotators"].items():
        if name in annotation.LLM_NAMES:
            kind = "LLM"
        elif name in annotation.ROSTER_DEFAULT:
            kind = "人"
        else:
            kind = "其他"
        annotators[name] = {"done": len(labels), "kind": kind,
                            "dist": dict(Counter(labels.values()))}
    cache: dict = {}
    cpath = out_dir / "llm_cache.json"
    if cpath.exists():
        try:
            raw = json.loads(cpath.read_text("utf-8"))
            cache = {k: len(v) for k, v in (raw or {}).items()}
        except (ValueError, OSError):
            cache = {}
    return {
        "dir": str(out_dir),
        "n_items": st["n_items"],
        "annotators": annotators,
        "llm_cache": cache,
        "files": [f.name for f in st["files"]],
        "labels": list(annotation.LABELS),
        "benign": list(annotation.BENIGN),
        "label_zh": annotation.LABEL_ZH,
        "roster": list(annotation.ROSTER_DEFAULT),
        "llm_names": list(annotation.LLM_NAMES),
        "sheet_exists": (out_dir / "sheet.csv").exists(),
        "running": _STATE["running"],
        "invalid": st.get("invalid") or [],
        "reps": _STATE["reps"],
        "rep_max": REP_MAX,
        "workers": _STATE["workers"],
        "worker_max": WORKER_MAX,
        "worker_default": WORKER_DEFAULT,
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
    name = str(body.get("provider") or "").strip()
    if name not in annotation.LLM_PROVIDERS:
        return {"ok": False, "error": f"未知模型 {name!r}"}
    prov = annotation.resolve_provider(name, {
        "url": str(body.get("url") or "").strip(),
        "model": str(body.get("model") or "").strip(),
    })
    key = str(body.get("key") or "").strip()
    try:
        timeout = float(body.get("timeout") or 60.0)
    except (TypeError, ValueError):
        timeout = 60.0
    res = annotation.verify(prov, key, timeout, out_dir)
    with _LOCK:
        _STATE["verified"][name] = {
            "ok": bool(res.get("ok")), "url": prov.get("url", ""),
            "model": prov.get("model", ""), "mode": res.get("mode", ""),
            "minimal": bool(res.get("minimal")), "at": time.time(),
        }
        _log("ok" if res.get("ok") else "err",
             f"验证 {name}：{'通过' if res.get('ok') else '失败'}"
             f"｜{res.get('mode', '')}｜{res.get('ms', 0)}ms"
             + ("" if res.get("ok") else f"｜{res.get('error', '')}"))
    res["provider"] = {"url": prov.get("url", ""), "model": prov.get("model", "")}
    return res


def _do_run(out_dir: Path, body: dict) -> dict:
    models = [str(m) for m in (body.get("models") or []) if str(m) in annotation.LLM_PROVIDERS]
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
    _save_ui_config(out_dir, {"providers": {n: {"url": providers[n]["url"],
                                                "model": providers[n]["model"]} for n in models}})
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
    ok, err = _start_run(out_dir, models, limit, bool(body.get("force")), timeout, sleep,
                         providers, reps, workers)
    return {"ok": ok, "error": err}


def _score_payload(out_dir: Path, golden: str | None) -> dict:
    res = annotation.score(out_dir, golden)
    strata: dict = {}
    for k, c in (res.get("strata") or {}).items():
        if isinstance(k, tuple) and len(k) == 2:
            key = f"{k[0]}×{'受限' if int(k[1]) else '自由'}"
        else:
            key = str(k)
        strata[key] = c
    payload = {
        "dir": str(out_dir), "base": res.get("base"), "n_items": res.get("n_items"),
        "coverage": res.get("coverage"), "dist": res.get("dist"),
        "pairs": res.get("pairs"), "groups": res.get("groups"),
        "fleiss": res.get("fleiss"), "fleiss_n": res.get("fleiss_n"),
        "alpha": res.get("alpha"), "alpha_n": res.get("alpha_n"),
        "base_dist": res.get("base_dist"), "conf_by_verdict": res.get("conf_by_verdict"),
        "strata": strata, "label_zh": annotation.LABEL_ZH,
        "labels": list(annotation.LABELS), "benign": list(annotation.BENIGN),
        "invalid": res.get("invalid") or [],
    }
    path = annotation.dump_disagreements(res, out_dir)
    payload["disagreements"] = Path(path).name if path else ""
    return payload


def _stability_payload(out_dir: Path, golden: str | None) -> dict:
    res = annotation.stability(out_dir, golden)

    def _rel(p: object) -> str:
        if not p:
            return ""
        p = Path(p)
        try:
            return p.relative_to(out_dir).as_posix()
        except ValueError:
            return p.name

    def _v(x: object) -> object:
        if isinstance(x, (list, tuple)):
            return x[0] if x else None
        return x

    models: dict = {}
    for name, info in (res.get("models") or {}).items():
        models[name] = {
            "reps": info.get("reps", 0), "reps_list": info.get("reps_list", []),
            "n_items": info.get("n_items"), "unanimous": info.get("unanimous"),
            "unanimous_rate": info.get("unanimous_rate"), "pairwise": info.get("pairwise"),
            "fleiss": _v(info.get("fleiss")), "tie": info.get("tie"),
            "tie_rate": info.get("tie_rate"), "maj_dist": info.get("maj_dist"),
            "human_items": info.get("human_items"), "human_kappa": _v(info.get("human_kappa")),
            "note": info.get("note", ""),
        }
    payload = {
        "dir": str(out_dir), "base": res.get("base"), "reps": res.get("reps"),
        "runs": _rel(res.get("runs_path")), "models": models,
        "invalid": res.get("invalid") or [], "note": res.get("note", ""),
        "label_zh": annotation.LABEL_ZH,
    }
    path = annotation.dump_stability(res, out_dir)
    payload["csv"] = _rel(path)
    return payload


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
class _GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    out_dir: Path
    html_path: Path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-audit-annotate-gui"

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
            return self._json(_state_payload(self.server.out_dir))
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
            golden = body.get("golden") or None
            try:
                return self._json(_score_payload(self.server.out_dir, golden))
            except (OSError, KeyError, ValueError) as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/stability":
            golden = body.get("golden") or None
            try:
                return self._json(_stability_payload(self.server.out_dir, golden))
            except (OSError, KeyError, ValueError) as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": f"未知路径 {path}"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser(prog="mcp_audit gui",
                                 description="E2 标注 GUI：填 URL / 模型名 → 验证 → 判定")
    ap.add_argument("--dir", default=None, help="标注包目录（默认 DATA_DIR/annotation）")
    ap.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认只绑本机）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8765）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    out_dir = _out_dir(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = Path(__file__).with_name(HTML_NAME)
    if not html_path.exists():
        raise SystemExit(f"[x] 找不到前端文件 {html_path}")

    srv = _GuiServer((args.host, args.port), _Handler)
    srv.out_dir = out_dir
    srv.html_path = html_path
    url = f"http://{args.host}:{args.port}/"

    print()
    print("=== E2 标注 GUI（填 URL / 模型名 → 验证 → 判定） ===")
    print(f"标注包目录 : {out_dir}")
    print(f"控制台地址 : {url}")
    print("API Key 只在本机内存与请求体里流转，不落盘；Ctrl+C 退出。")
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
