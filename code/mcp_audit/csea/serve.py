"""Static server + AI proxy for the annotation page.

Why a proxy: a static page cannot call an OpenAI-compatible endpoint directly —
the browser blocks it (CORS) and the key would have to live in the HTML. So the
page posts here, and this process makes the call. The key travels from the
browser to 127.0.0.1 only and is never written to disk or to the log.

    python serve.py [--port 8765] [--host 127.0.0.1]

    GET  /                 review.html
    GET  /<file>           any file under annotation/
    POST /api/suggest      {"id": "...", "base_url": "...", "model": "...",
                            "api_key": "..."}  ->  {"ok": true, "facts": {...},
                                                     "rationale": "...", "raw": "..."}

The endpoint speaks the OpenAI chat-completions shape. A bare host, a /v1 suffix
and a full /chat/completions URL are all accepted, mirroring annotate_me_gui.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paths import ANN

CLASSES = ["arbitrary_code", "credential_paths", "db_access", "env_access",
           "fs_destructive", "network_egress", "subprocess", "shell_exec",
           "unsafe_deserialize"]

DECLARABLE = {"env_access", "network_egress", "credential_paths"}

CLASS_DOC = """\
- arbitrary_code: evaluates code or expressions supplied at run time (eval/exec/Function)
- credential_paths: touches private keys, ~/.ssh, ~/.aws, .netrc, keychains
- db_access: opens a database connection or issues SQL
- env_access: reads environment variables
- fs_destructive: deletes or overwrites files outside a temp directory
- network_egress: makes an outbound HTTP/DNS connection
- subprocess: spawns a child process
- shell_exec: runs a shell command line (sh -c, subprocess with shell=True)
- unsafe_deserialize: pickle/yaml.load/eval of serialized input
"""

SYSTEM = """You are a careful software supply-chain auditor. You annotate ONE
registry record at a time by answering eight factual questions.

Answer only from the evidence given. You cannot open the repository. The
detector output is a regex scan with the per-class precision shown; a hit is
evidence of a pattern, not of behaviour. A hit whose only occurrence is in a
test file, a CI script, or the scanner's own source is usually NOT a real
implementation of that capability.

Capability classes:
""" + CLASS_DOC + """
The first three (env_access, network_egress, credential_paths) can be declared in
a server.json manifest; the other six cannot be declared in any manifest format.

Questions and allowed answers:
A1 identity unique to one repository?          是 | 否 | 不可见
A2 a declaration comparable with the code?     是 | 否 | 无法判断
A3 code available and non-empty?               是 | 否 | 无法判断
A4 which classes does the RECORD declare?      list, subset of the classes
A5 which classes does the CODE visibly implement?  list, subset of the classes
A6 version pinned to an immutable revision?    是 | 否 | 不可见
A7 a second source carries a comparable declaration?  是 | 否 | 无法判断
A8 does a decision procedure exist for this property?  是 | 否 | 不适用

Reply with JSON only, no prose outside it:
{"A1":"...","A2":"...","A3":"...","A4":[...],"A5":[...],"A6":"...","A7":"...",
 "A8":"...","confidence":0.0-1.0,"rationale":"one or two sentences, <=200 chars"}
"""


def _endpoint(base: str) -> str:
    b = (base or "").strip().rstrip("/")
    if not b:
        raise ValueError("base_url 为空")
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    if re.search(r"/v\d+$", b):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def _evidence_text(it: dict) -> str:
    lines = [
        f"Listing: {it['norm_url']}",
        f"Sources reporting it: {it.get('sources') or '-'}",
        f"Clone status: {it.get('clone_status')}  files_scanned={it.get('files_scanned')}",
        f"Manifest kinds: {', '.join(it.get('manifest_kinds') or []) or 'none'}",
        f"Manifests: {', '.join(it.get('manifests') or []) or 'none'}",
        f"Commit: {it.get('commit') or '-'}",
        "",
        "Record descriptions:",
    ]
    descs = it.get("descriptions") or {}
    if not descs:
        lines.append("  (none)")
    for k, v in descs.items():
        lines.append(f"  [{k}] {v}")
    lines += ["", "Declared capability classes per source:"]
    ps = it.get("declared_per_source") or {}
    if not ps:
        lines.append("  (no source declares any class)")
    for k, v in ps.items():
        lines.append(f"  [{k}] {', '.join(v) if v else '(empty)'}")
    lines += ["", "Regex detector output (class, precision, first triggering file):"]
    det = it.get("detected") or []
    if not det:
        lines.append("  (no class detected)")
    prec = it.get("precision") or {}
    for c in det:
        p = prec.get(c)
        lines.append(f"  {c}  precision={p if p is not None else '?'}%  "
                     f"file={(it.get('detected_examples') or {}).get(c) or '?'}")
    return "\n".join(lines)


def _chat(base: str, model: str, key: str, system: str, user: str,
          timeout: float = 90.0, temperature: float = 0.0) -> str:
    body = json.dumps({
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        _endpoint(base), data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"连接失败: {e.reason}")
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("响应不是 chat-completions 形状: " + str(payload)[:300])


#: Process-memory result cache. Never written to disk: asking the same item
#: twice must not cost two model calls, but nothing about which items were asked
#: survives the process.
_CACHE: dict[tuple, dict] = {}
_CACHE_MAX = 4000


def _suggest(item: dict, base: str, model: str, key: str) -> tuple[dict, str]:
    ck = (item.get("id"), model, base)
    hit = _CACHE.get(ck)
    if hit is not None:
        return hit, "(cached)"
    raw = _chat(base, model, key, SYSTEM, _evidence_text(item))
    facts = _parse(raw)
    if len(_CACHE) < _CACHE_MAX:
        _CACHE[ck] = facts
    return facts, raw


def _parse(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("回复里没有 JSON")
    obj = json.loads(t[i:j + 1])

    yes = {"是", "否", "不可见", "无法判断", "不适用"}
    out = {}
    for k in ("A1", "A2", "A3", "A6", "A7", "A8"):
        v = str(obj.get(k, "")).strip()
        out[k] = v if v in yes else ""
    for k in ("A4", "A5"):
        v = obj.get(k) or []
        if isinstance(v, str):
            v = [x for x in re.split(r"[,，;；\s]+", v) if x]
        out[k] = [c for c in map(str, v) if c in CLASSES]
    conf = obj.get("confidence")
    out["confidence"] = conf if isinstance(conf, (int, float)) else None
    out["rationale"] = str(obj.get("rationale", ""))[:400]
    return out


class Handler(SimpleHTTPRequestHandler):
    items: dict = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ANN), **kw)

    def log_message(self, *a, **kw):
        """Write nothing. Which items were asked for, and when, is a usage record
        and there is no operational reason to keep one."""
        pass

    def log_error(self, *a, **kw):
        pass

    def end_headers(self):
        # The page is rebuilt often; a cached copy shows behaviour that no longer
        # exists in the file. Never let the browser keep one.
        p = self.path.split("?")[0]
        if p.endswith(".html") or p in ("/", ""):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] in ("/favicon.ico", "/apple-touch-icon.png"):
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _batch(self, req: dict) -> None:
        """Fan out server-side. The browser caps concurrent connections to one
        origin at about six, so parallelism has to live here, not in page workers."""
        ids = [str(i) for i in (req.get("ids") or [])][:200]
        base, model, key = req.get("base_url"), req.get("model"), req.get("api_key")
        if not (base and model and key):
            return self._json(400, {"ok": False,
                                    "error": "base_url / model / api_key 三者都要填"})
        if not ids:
            return self._json(400, {"ok": False, "error": "ids 为空"})
        try:
            conc = max(1, min(32, int(req.get("concurrency") or 8)))
        except (TypeError, ValueError):
            conc = 8

        def work(iid: str) -> dict:
            it = self.items.get(iid)
            if not it:
                return {"id": iid, "error": "unknown id"}
            try:
                was_cached = (iid, model, base) in _CACHE
                facts, _ = _suggest(it, base, model, key)
                return {"id": iid, "facts": facts, "_hit": was_cached}
            except Exception as e:                      # one failure must not sink the batch
                return {"id": iid, "error": str(e)[:300]}

        with ThreadPoolExecutor(max_workers=conc) as ex:
            results = list(ex.map(work, ids))
        cached = sum(1 for r in results if r.get("facts") is not None and r.get("_hit"))
        for r in results:
            r.pop("_hit", None)
        return self._json(200, {"ok": True, "results": results,
                                "cache_size": len(_CACHE)})

    def do_POST(self):
        if self.path.rstrip("/") == "/api/suggest_batch":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception as e:
                return self._json(400, {"ok": False, "error": f"bad request: {e}"})
            return self._batch(req)

        if self.path.rstrip("/") != "/api/suggest":
            return self._json(404, {"ok": False, "error": "unknown endpoint"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"bad request: {e}"})

        item = self.items.get(str(req.get("id")))
        if not item:
            return self._json(404, {"ok": False, "error": "unknown id"})
        base = req.get("base_url") or ""
        model = req.get("model") or ""
        key = req.get("api_key") or ""
        if not (base and model and key):
            return self._json(400, {"ok": False,
                                    "error": "base_url / model / api_key 三者都要填"})
        try:
            facts, raw = _suggest(item, base, model, key)
        except Exception as e:
            return self._json(502, {"ok": False, "error": str(e)[:600]})
        return self._json(200, {"ok": True, "facts": facts, "raw": raw[:2000]})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    items = json.loads((ANN / "items.json").read_text(encoding="utf-8"))
    Handler.items = {str(i["id"]): i for i in items}

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving {ANN} on http://{args.host}:{args.port}/review.html")
    print(f"items loaded: {len(Handler.items)}   suggest proxy: POST /api/suggest")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)


if __name__ == "__main__":
    main()
