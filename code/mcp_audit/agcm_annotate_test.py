"""M2 标注前端测试：人工前端（agcm_annotate_me_gui）与 LLM 控制台（agcm_annotate_gui）。

覆盖（里程碑 §八.2 M2；计划 §三/§五）：
  1. sheet 解析：读 sheet.csv → 条目（item_id / repo_key / category / 类中文名 / 片段），保行序
  2. 三值归一：中文/大小写别名 → present/absent/unsure；认不出点名报错（不静默丢弃）
  3. 原子回写：整卡按 sheet 行序落 label_<人>.csv；未填留空；拒绝 LLM 卡
  4. 载荷：bundle 只含盲标字段（不泄露 AGCM/声明）；codebook 9 类；三值域
  5. LLM 建议：用 M2_SYSTEM_PROMPT + _m2_llm_prompt，解析三值
  6. LLM 控制台：M2 status（三值归一）；验证探针；未验证拒绝开跑

运行（在 mcp_audit/ 上级目录下）：py -3 mcp_audit/agcm_annotate_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mcp_audit import agcm_audit as m2  # noqa: E402
from mcp_audit import agcm_annotate_me_gui as me  # noqa: E402
from mcp_audit import agcm_annotate_gui as gui  # noqa: E402

_FAILURES: list[str] = []
_PASSED = 0


def check(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
    else:
        _FAILURES.append(msg)
        print(f"  FAIL {msg}")


def _fixture() -> Path:
    """构造最小 M2 标注包：2 仓 × 2 类 = 4 判定行 + 隐藏键 + 空卡。"""
    out = Path(tempfile.mkdtemp(prefix="m2_gui_"))
    cats = ("shell_true", "db_access")
    rows: list[dict] = []
    for repo in ("acme__a", "acme__b"):
        for c in cats:
            rows.append({"item_id": f"{repo}#{c}", "repo_key": repo, "category": c,
                         "category_zh": m2.CATEGORY_ZH[c],
                         "file_list": "a.py;b.js",
                         "snippet": "--- a.py ---\nsubprocess.run(cmd, shell=True)"})
    m2._write_csv(out / m2.SHEET_CSV, rows)
    m2._write_csv(out / m2.KEY_CSV, [
        {"item_id": r["item_id"], "repo_key": r["repo_key"], "category": r["category"],
         "agcm_present": 1, "declared": 0} for r in rows])
    blank = [{"item_id": r["item_id"], "label": "", "note": ""} for r in rows]
    for name in ("Me", "other"):
        m2._write_csv(out / f"{m2.LABEL_PREFIX}{name}.csv", blank)
    return out


def test_me_sheet_items() -> None:
    out = _fixture()
    items = me._sheet_items(out)
    check(len(items) == 4, "sheet_items 读 4 条")
    check(items[0]["item_id"] == "acme__a#shell_true", "sheet_items 保行序")
    check(items[0]["repo_key"] == "acme__a", "sheet_items 含 repo_key")
    check(items[0]["category"] == "shell_true", "sheet_items 含 category")
    check(items[0]["category_zh"] == m2.CATEGORY_ZH["shell_true"], "sheet_items 含类中文名")
    check("shell=True" in items[0]["snippet"], "sheet_items 含源码片段")
    check(items[0]["file_list"] == "a.py;b.js", "sheet_items 含文件清单")
    check(me._sheet_items(Path(tempfile.mkdtemp())) == [], "无 sheet → 空列表")


def test_me_save_and_norm() -> None:
    out = _fixture()
    r = me._save_one(out, {"card": "Me", "item_id": "acme__a#shell_true",
                           "label": "存在", "note": "有 shell"})
    check(r["ok"] and r["label"] == "present", "save 中文别名 → present")
    rows = {x["item_id"]: x for x in m2._read_csv(out / (m2.LABEL_PREFIX + "Me.csv"))}
    check(rows["acme__a#shell_true"]["label"] == "present", "save 落卡 label")
    check(rows["acme__a#shell_true"]["note"] == "有 shell", "save 落卡 note")
    check(rows["acme__b#db_access"]["label"] == "", "save 未填留空")
    check(r["filled"] == 1 and r["n_items"] == 4, "save 计已填/总数")

    bad = me._save_one(out, {"card": "Me", "item_id": "acme__a#db_access", "label": "bogus"})
    check(not bad["ok"], "save 非法 label 点名报错")
    miss = me._save_one(out, {"card": "Me", "item_id": "nope#x", "label": "present"})
    check(not miss["ok"], "save 不在 sheet 的 item 报错")
    llm = me._save_one(out, {"card": "DeepSeek", "item_id": "acme__a#shell_true",
                             "label": "present"})
    check(not llm["ok"], "save 拒绝 LLM 卡")


def test_me_bundle_payload() -> None:
    out = _fixture()
    p = me._bundle_payload(out, "Me")
    check(p["ok"] and p["n_items"] == 4, "bundle 4 条")
    check(list(p["labels"]) == ["present", "absent", "unsure"], "bundle 三值域")
    check(len(p["codebook"]) == 9, "bundle 9 类 codebook")
    check(any(c["category"] == "shell_true" for c in p["codebook"]), "codebook 含 shell_true")
    check("shell=True" in json.dumps(p["items"], ensure_ascii=False), "bundle 含片段")
    check("agcm" not in json.dumps(p["items"], ensure_ascii=False).lower(),
          "bundle items 不泄露 AGCM")
    check(any(c["name"] == "Me" for c in p["cards"]), "bundle 卡片列表含 Me")

    m2._write_csv(out / (m2.LABEL_PREFIX + "other.csv"),
                  [{"item_id": "acme__a#shell_true", "label": "???", "note": ""}])
    p2 = me._bundle_payload(out, "other")
    check(len(p2["invalid"]) == 1, "bundle 标记认不出的卡值")
    bad = me._bundle_payload(out, "DeepSeek")
    check(not bad["ok"], "bundle 拒绝 LLM 卡")


def test_me_parse_translations() -> None:
    t = me._parse_translations("[1] 你好\n[2] 世界", [1, 2])
    check(t.get("1") == "你好" and t.get("2") == "世界", "parse_translations 多行")
    t2 = me._parse_translations("单条无序号", [1])
    check(t2.get("1") == "单条无序号", "parse_translations 单条兜底")


def test_me_norm_url() -> None:
    check(me.norm_url("https://h") == "https://h/v1/chat/completions", "norm_url 补 /v1")
    check(me.norm_url("https://h/v1") == "https://h/v1/chat/completions", "norm_url 补后缀")
    check(me.norm_url("https://h/v1/chat/completions") == "https://h/v1/chat/completions",
          "norm_url 原样")
    check(me.norm_url("") == "", "norm_url 空")


def test_me_suggest() -> None:
    out = _fixture()
    seen: dict = {}

    def fake_call(cfg, system, user):
        seen["system"] = system
        seen["user"] = user
        return '{"label": "present", "note": "n"}', False, ""

    orig = me._call
    me._call = fake_call
    try:
        item = {"item_id": "acme__a#shell_true", "repo_key": "acme__a",
                "category": "shell_true", "category_zh": m2.CATEGORY_ZH["shell_true"],
                "file_list": "a.py", "snippet": "subprocess.run(cmd, shell=True)"}
        r = me._do_suggest(out, {"url": "https://x/v1", "model": "m", "key": "k", "item": item})
        check(r["ok"] and r["label"] == "present", "suggest 解析三值")
        check(seen["system"] == m2.M2_SYSTEM_PROMPT, "suggest 用 M2_SYSTEM_PROMPT")
        check("shell_true" in seen["user"] and "shell=True" in seen["user"],
              "suggest prompt 含类名与片段")
        check("agcm" not in seen["user"].lower(), "suggest prompt 不泄露 AGCM")
    finally:
        me._call = orig


def test_gui_status() -> None:
    out = _fixture()
    m2._write_csv(out / (m2.LABEL_PREFIX + "DeepSeek.csv"),
                  [{"item_id": "acme__a#shell_true", "label": "present", "note": ""}])
    st = gui._state_payload(out)
    check(st["n_items"] == 4, "status n_items")
    check(st["annotators"]["DeepSeek"]["done"] == 1, "status LLM 已填 1")
    check(st["annotators"]["DeepSeek"]["kind"] == "LLM", "status kind = LLM")
    check(list(st["labels"]) == ["present", "absent", "unsure"], "status 三值域")
    check(st["sheet_exists"], "status sheet 存在")
    check(st["running"] is False, "status 未在跑")
    check(st["parallel"] is False, "status 带 parallel 开关（默认关）")


def test_gui_verify() -> None:
    out = _fixture()
    orig = m2._m2_llm_call
    m2._m2_llm_call = lambda *a, **k: '{"label": "absent", "note": "no"}'
    try:
        r = gui._do_verify(out, {"provider": "DeepSeek", "url": "https://x/v1",
                                 "model": "m", "key": "k"})
        check(r["ok"] and r["label"] == "absent", "verify 解析三值")
        check(r.get("provider", {}).get("model") == "m", "verify 回传 provider")
    finally:
        m2._m2_llm_call = orig
    miss = gui._do_verify(out, {"provider": "DeepSeek", "url": "https://x/v1",
                                "model": "m", "key": ""})
    check(not miss["ok"], "verify 无 key 报错")


def test_gui_run_guard() -> None:
    out = _fixture()
    r = gui._do_run(out, {"models": ["DeepSeek"],
                          "providers": {"DeepSeek": {"url": "u", "model": "m", "key": "k"}}})
    check(not r["ok"], "run 未验证拒绝开跑")
    r2 = gui._do_run(out, {"models": [], "providers": {}})
    check(not r2["ok"], "run 未选模型拒绝")


def test_gui_run_parallel() -> None:
    """_do_run 把「模型并行」开关透传到 m2.llm(parallel=...)。"""
    import time
    out = _fixture()
    with gui._LOCK:
        gui._STATE["verified"]["DeepSeek"] = {"ok": True, "url": "u", "model": "m"}
        gui._STATE["verified"]["Claude"] = {"ok": True, "url": "u2", "model": "m2"}
    captured: dict = {}

    def fake_llm(out_dir, models, limit, force, timeout, sleep, **kw):
        captured["models"] = list(models)
        captured["parallel"] = kw.get("parallel")
        return {"items": 0, "models": {}, "reps": 1, "workers": 1,
                "dir": str(out_dir), "stopped": False}

    orig = m2.llm
    m2.llm = fake_llm
    try:
        r = gui._do_run(out, {
            "models": ["DeepSeek", "Claude"], "parallel": True,
            "providers": {"DeepSeek": {"url": "u", "model": "m", "key": "k"},
                          "Claude": {"url": "u2", "model": "m2", "key": "k2"}}})
        check(r["ok"], "run 并行开跑成功")
        for _ in range(200):
            if not gui._STATE["running"]:
                break
            time.sleep(0.02)
        check(captured.get("parallel") is True, "_do_run 透传 parallel=True 给 m2.llm")
        check(len(captured.get("models") or []) == 2, "两家模型都传给 m2.llm")
    finally:
        m2.llm = orig
        with gui._LOCK:
            gui._STATE["verified"].clear()
            gui._STATE["running"] = False


def test_gui_config_relay() -> None:
    out = _fixture()
    (out / "providers.json").write_text(json.dumps({
        "default": {"url": "https://relay/v1/chat/completions", "key": "sk-x"},
        "DeepSeek": {"model": "ds-chat"},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = gui._load_ui_config(out)
    check(cfg["relay"]["url"] == "https://relay/v1/chat/completions",
          "config 读 providers.json 的 default.url 作共享中转站")
    check(cfg["providers"]["DeepSeek"]["model"] == "ds-chat",
          "config 读 providers.json 具名 model")
    check(cfg["providers"]["DeepSeek"]["url"] == "https://relay/v1/chat/completions",
          "providers.json default.url 合入具名模型")
    check("key" not in cfg["providers"].get("DeepSeek", {}),
          "config 不带 key（凭据不落盘）")

    gui._save_ui_config(out, {"relay": {"url": "https://ui/v1/chat/completions"},
                              "providers": {"Claude": {"url": "https://ui-claude", "model": "k"}}})
    cfg2 = gui._load_ui_config(out)
    check(cfg2["relay"]["url"] == "https://ui/v1/chat/completions", "save relay 落盘且优先")
    check(cfg2["providers"]["Claude"]["url"] == "https://ui-claude", "save 具名 url 落盘")
    check(cfg2["providers"]["DeepSeek"]["url"] == "https://relay/v1/chat/completions",
          "save 不丢 providers.json 基线")


def test_gui_config_keys() -> None:
    """Key 只在**本机内存**流转：页面刷新能回填，但绝不落盘。"""
    out = _fixture()
    gui._reset_session_keys()
    gui._save_ui_config(out, {
        "providers": {"Claude": {"url": "https://ui", "model": "m"}},
        "keys": {"relay": "sk-relay", "providers": {"Claude": "sk-claude"}},
    })
    cfg = gui._load_ui_config(out)
    check(cfg["keys"]["relay"] == "sk-relay", "config 回填共享中转站 Key（内存）")
    check(cfg["keys"]["providers"]["Claude"] == "sk-claude", "config 回填具名 Key（内存）")
    check("key" not in cfg["providers"].get("Claude", {}), "providers 字段仍不带 key")
    disk = (out / "llm_ui.json").read_text("utf-8")
    check("sk-relay" not in disk and "sk-claude" not in disk, "Key 绝不落盘（llm_ui.json 无凭据）")
    gui._reset_session_keys()


def test_gui_route2b_profile() -> None:
    """路线2b 厚层重标模式：默认落点 m2b、层标签 thick、提示点明「仅分歧项」。"""
    out = _fixture()
    st_thin = gui._state_payload(out)
    check(st_thin["profile"] == gui.PROFILE_M2, "默认 profile = m2")
    check(st_thin["layer"] == "thin", "默认 layer = thin")
    st_thick = gui._state_payload(out, profile=gui.PROFILE_R2B)
    check(st_thick["profile"] == gui.PROFILE_R2B, "route2b profile")
    check(st_thick["layer"] == "thick", "route2b layer = thick")
    check("厚层" in st_thick["notice"] and "分歧" in st_thick["notice"],
          "route2b notice 点明厚层/仅分歧项")
    check(st_thin["notice"] == "", "薄层模式无 banner notice")
    check(gui._out_dir(None, gui.PROFILE_R2B) == m2.M2B_DIR, "route2b 默认落点 m2b")
    check(gui._out_dir(None) == m2.M2_DIR, "默认落点 m2")
    check(gui._out_dir("D:/tmp/x", gui.PROFILE_R2B) == Path("D:/tmp/x"), "显式 --dir 优先")
    check(gui.PORT_R2B == 8776 and gui.PORT_R2B != gui.DEFAULT_PORT, "route2b 独立端口")


def test_gui_stepfun_channel() -> None:
    """第 4 家 StepFun（step-5-preview）接入：M2 注册表含之、route2b 通道含之、验证放行。"""
    reg = m2.llm_providers()
    check("StepFun" in reg, "M2 注册表含 StepFun")
    check(reg["StepFun"]["model"] == "step-5-preview", "StepFun 预置模型 = step-5-preview")
    check(m2.MODEL4_DEFAULT == "StepFun", "第 4 家默认 = StepFun（口径同步 §7.8）")
    check(m2.MODEL4_DEFAULT in reg, "第 4 家默认已在 M2 注册表中")
    check("StepFun" not in gui.annotation.LLM_PROVIDERS, "不改第一篇 LLM_PROVIDERS")

    out = _fixture()
    check("StepFun" not in gui._state_payload(out)["llm_names"], "薄层通道不含 StepFun")
    check("StepFun" in gui._state_payload(out, profile=gui.PROFILE_R2B)["llm_names"],
          "route2b 通道含 StepFun")
    check(gui._state_payload(out, profile=gui.PROFILE_R2B)["llm_default"] == list(m2.LLM_DEFAULT),
          "state 暴露前三家（残余模式据此把前三家排除出放行闸）")
    cfg = gui._load_ui_config(out)
    check(cfg["presets"].get("StepFun", {}).get("model") == "step-5-preview",
          "StepFun 预设随 config 下发")

    orig = m2._m2_llm_call
    m2._m2_llm_call = lambda *a, **k: '{"label": "present", "note": "ok"}'
    try:
        r = gui._do_verify(out, {"provider": "StepFun", "url": "https://api.stepfun.com/v1/chat/completions",
                                 "model": "step-5-preview", "key": "sk-x"})
        check(r["ok"] and r["label"] == "present", "StepFun 通过白名单验证")
    finally:
        m2._m2_llm_call = orig


def test_gui_residual_scope() -> None:
    """残余子集 scope：仅第 4 家可跑（对三家报错），透传 sheet_residual.csv 给 m2.llm。"""
    import time
    out = _fixture()
    with gui._LOCK:
        gui._STATE["verified"]["DeepSeek"] = {"ok": True, "url": "u", "model": "m"}
        gui._STATE["verified"]["StepFun"] = {"ok": True, "url": "su", "model": "sm"}

    # (1) 对前三家跑残余 → 拒绝（会缩小其满量卡）
    r_bad = gui._do_run(out, {
        "models": ["DeepSeek"], "scope": "residual",
        "providers": {"DeepSeek": {"url": "u", "model": "m", "key": "k"}}})
    check(not r_bad["ok"] and "第 4 家" in r_bad["error"], "残余 scope 对三家 → 拒绝")

    # (2) 对第 4 家跑残余 → 生成残余子集并透传 sheet_name
    captured: dict = {}
    orig_llm, orig_resid = m2.llm, m2.residual_sheet

    def fake_resid(out_dir, force=False):
        captured["resid_force"] = force
        return {"total": 10, "residual": 4, "consensus": 6,
                "path": out / m2.RESIDUAL_SHEET_CSV}

    m2.residual_sheet = fake_resid

    def fake_llm(out_dir, models, limit, force, timeout, sleep, **kw):
        captured["sheet_name"] = kw.get("sheet_name")
        captured["models"] = list(models)
        return {"items": 0, "models": {}, "reps": 1, "workers": 1,
                "dir": str(out_dir), "stopped": False}

    m2.llm = fake_llm
    try:
        r_ok = gui._do_run(out, {
            "models": ["StepFun"], "scope": "residual",
            "providers": {"StepFun": {"url": "su", "model": "sm", "key": "k"}}})
        check(r_ok["ok"], "残余 scope 对第 4 家开跑成功")
        for _ in range(200):
            if not gui._STATE["running"]:
                break
            time.sleep(0.02)
        check(captured.get("sheet_name") == m2.RESIDUAL_SHEET_CSV,
              "残余 scope 透传 sheet_residual.csv")
        check(captured.get("resid_force") is True,
              "残余 scope 每次重算残余子集（force，避免「已存在」报错）")
    finally:
        m2.llm, m2.residual_sheet = orig_llm, orig_resid
        with gui._LOCK:
            gui._STATE["verified"].clear()
            gui._STATE["running"] = False

    # (3) 无残余分歧项 → 拒绝（三家已全一致）
    orig_resid = m2.residual_sheet
    m2.residual_sheet = lambda *a, **k: {"total": 10, "residual": 0, "consensus": 10,
                                         "path": out / m2.RESIDUAL_SHEET_CSV}
    try:
        with gui._LOCK:
            gui._STATE["verified"]["StepFun"] = {"ok": True, "url": "su", "model": "sm"}
        r_none = gui._do_run(out, {
            "models": ["StepFun"], "scope": "residual",
            "providers": {"StepFun": {"url": "su", "model": "sm", "key": "k"}}})
        check(not r_none["ok"] and "无残余" in r_none["error"], "无残余项 → 拒绝")
    finally:
        m2.residual_sheet = orig_resid
        with gui._LOCK:
            gui._STATE["verified"].clear()
            gui._STATE["running"] = False


def test_me_auditb_mode() -> None:
    """跨层盲审模式（--route2b）：读 audit_b.csv（带 layer）、写 label_audit_b_<人>.csv、
    默认落点 m2b、独立端口、拒绝裸卡名，且不泄露 LLM 标签。"""
    out = Path(tempfile.mkdtemp(prefix="m2b_gui_"))
    rows = [
        {"item_id": "acme__a#shell_true", "layer": "thin", "repo_key": "acme__a",
         "category": "shell_true", "category_zh": m2.CATEGORY_ZH["shell_true"],
         "file_list": "a.py", "snippet": "subprocess.run(cmd, shell=True)"},
        {"item_id": "acme__b#db_access", "layer": "thin", "repo_key": "acme__b",
         "category": "db_access", "category_zh": m2.CATEGORY_ZH["db_access"],
         "file_list": "b.py", "snippet": "cursor.execute(sql)"},
        {"item_id": "acme__c#shell_true", "layer": "thick", "repo_key": "acme__c",
         "category": "shell_true", "category_zh": m2.CATEGORY_ZH["shell_true"],
         "file_list": "c.py;d.js", "snippet": "os.system(cmd)"},
        {"item_id": "acme__d#db_access", "layer": "thick", "repo_key": "acme__d",
         "category": "db_access", "category_zh": m2.CATEGORY_ZH["db_access"],
         "file_list": "e.py", "snippet": "conn.query(sql)"},
    ]
    m2._write_csv(out / m2.AUDIT_B_CSV, rows)
    blank = [{"item_id": r["item_id"], "label": "", "note": ""} for r in rows]
    for name in ("audit_b_Me", "audit_b_other"):
        m2._write_csv(out / f"{m2.LABEL_PREFIX}{name}.csv", blank)

    # 1) 模式常量：文件名 / 卡名 / 落点 / 端口
    check(me._sheet_name(me.PROFILE_AUDITB) == m2.AUDIT_B_CSV, "auditb 读 audit_b.csv")
    check(me._human_cards(me.PROFILE_AUDITB) == m2._audit_b_humans(m2.HUMANS_DEFAULT),
          "auditb 卡名 = audit_b_<人>")
    check(me._out_dir(None, me.PROFILE_AUDITB) == m2.M2B_DIR, "auditb 默认落点 m2b")
    check(me._out_dir(None) == m2.M2_DIR, "默认落点 m2")
    check(me.PORT_AUDITB != me.DEFAULT_PORT, "auditb 独立端口")

    # 2) 条目带 layer/layer_zh，且不含 LLM 标签
    items = me._sheet_items(out, me.PROFILE_AUDITB)
    check(len(items) == 4, "auditb sheet_items 读 4 条")
    check(items[0]["layer"] == "thin" and items[2]["layer"] == "thick", "auditb 带 layer")
    check(items[0]["layer_zh"] == me.LAYER_ZH["thin"], "auditb 带 layer 中文名")
    dumped = json.dumps(items, ensure_ascii=False).lower()
    check("agcm" not in dumped and "consensus" not in dumped and "adj4" not in dumped,
          "auditb items 不泄露 LLM/金标来源")

    # 3) bundle：profile/notice/卡列表
    p = me._bundle_payload(out, "audit_b_Me", me.PROFILE_AUDITB)
    check(p["ok"] and p["profile"] == me.PROFILE_AUDITB, "auditb bundle profile")
    check(p["notice"], "auditb bundle 带模式 notice")
    check([c["name"] for c in p["cards"]] == list(m2._audit_b_humans(m2.HUMANS_DEFAULT)),
          "auditb bundle 卡列表 = audit_b_<人>")
    check(p["card_file"] == "label_audit_b_Me.csv", "auditb 卡文件名")
    check(not me._bundle_payload(out, "Me", me.PROFILE_AUDITB)["ok"],
          "auditb 拒绝裸卡名 Me")
    check(me._bundle_payload(_fixture(), "Me")["ok"], "全量模式仍接受 Me")

    # 4) 落卡到 label_audit_b_Me.csv，不动全量卡
    r = me._save_one(out, {"card": "audit_b_Me", "item_id": "acme__c#shell_true",
                           "label": "存在", "note": "厚层有 shell"}, me.PROFILE_AUDITB)
    check(r["ok"] and r["label"] == "present" and r["file"] == "label_audit_b_Me.csv",
          "auditb save 落 label_audit_b_Me.csv")
    saved = {x["item_id"]: x for x in m2._read_csv(out / "label_audit_b_Me.csv")}
    check(saved["acme__c#shell_true"]["label"] == "present", "auditb save label")
    check(not (out / (m2.LABEL_PREFIX + "Me.csv")).exists(), "auditb 不写全量卡")
    check(not me._save_one(out, {"card": "audit_b_Me", "item_id": "nope#x",
                                 "label": "present"}, me.PROFILE_AUDITB)["ok"],
          "auditb save 拒绝不在包内的 item")

    # 5) 页面：占位符替换为 audit_b_Me，默认页为 Me
    html_ab = me._html_for(me.PROFILE_AUDITB)
    check("%%BOOT_CARD%%" not in html_ab and 'boot("audit_b_Me")' in html_ab,
          "auditb 页面 boot 卡 = audit_b_Me")
    check("%%BOOT_CARD%%" not in me._html_for() and 'boot("Me")' in me._html_for(),
          "默认页面 boot 卡 = Me")


def test_me_enrich_mode() -> None:
    """C-lite 富集补样盲审模式（--enrich）：读 enrich_sheet.csv（带 layer）、写
    label_enrich_<人>.csv、默认落点 m2r2、独立端口、拒绝裸卡名，且不泄露 LLM 标签。"""
    out = Path(tempfile.mkdtemp(prefix="m2r2_gui_"))
    rows = [
        {"item_id": "acme__a#shell_true", "layer": "thin", "repo_key": "acme__a",
         "category": "shell_true", "category_zh": m2.CATEGORY_ZH["shell_true"],
         "file_list": "a.py", "snippet": "subprocess.run(cmd, shell=True)"},
        {"item_id": "acme__c#unsafe_deserialize", "layer": "thick", "repo_key": "acme__c",
         "category": "unsafe_deserialize",
         "category_zh": m2.CATEGORY_ZH["unsafe_deserialize"],
         "file_list": "c.py", "snippet": "pickle.loads(data)"},
    ]
    m2._write_csv(out / m2.ENRICH_SHEET_CSV, rows)
    blank = [{"item_id": r["item_id"], "label": "", "note": ""} for r in rows]
    for name in ("enrich_Me", "enrich_other"):
        m2._write_csv(out / f"{m2.LABEL_PREFIX}{name}.csv", blank)

    # 1) 模式常量：文件名 / 卡名 / 落点 / 端口
    check(me._sheet_name(me.PROFILE_ENRICH) == m2.ENRICH_SHEET_CSV,
          "enrich 读 enrich_sheet.csv")
    check(me._human_cards(me.PROFILE_ENRICH) == m2._enrich_humans(m2.HUMANS_DEFAULT),
          "enrich 卡名 = enrich_<人>")
    check(me._out_dir(None, me.PROFILE_ENRICH) == m2.M2R2_DIR, "enrich 默认落点 m2r2")
    check(me.PORT_ENRICH != me.DEFAULT_PORT and me.PORT_ENRICH != me.PORT_AUDITB,
          "enrich 独立端口")

    # 2) 条目带 layer/layer_zh，且不含 LLM 标签
    items = me._sheet_items(out, me.PROFILE_ENRICH)
    check(len(items) == 2, "enrich sheet_items 读 2 条")
    check(items[0]["layer"] == "thin" and items[1]["layer"] == "thick", "enrich 带 layer")
    dumped = json.dumps(items, ensure_ascii=False).lower()
    check("agcm" not in dumped and "consensus" not in dumped,
          "enrich items 不泄露 LLM/金标来源")

    # 3) bundle：profile / notice / 卡列表
    p = me._bundle_payload(out, "enrich_Me", me.PROFILE_ENRICH)
    check(p["ok"] and p["profile"] == me.PROFILE_ENRICH, "enrich bundle profile")
    check(p["notice"], "enrich bundle 带模式 notice")
    check([c["name"] for c in p["cards"]] == list(m2._enrich_humans(m2.HUMANS_DEFAULT)),
          "enrich bundle 卡列表 = enrich_<人>")
    check(p["card_file"] == "label_enrich_Me.csv", "enrich 卡文件名")
    check(not me._bundle_payload(out, "Me", me.PROFILE_ENRICH)["ok"],
          "enrich 拒绝裸卡名 Me")

    # 4) 落卡到 label_enrich_Me.csv，不动全量卡
    r = me._save_one(out, {"card": "enrich_Me", "item_id": "acme__a#shell_true",
                           "label": "存在", "note": "富集正例"}, me.PROFILE_ENRICH)
    check(r["ok"] and r["label"] == "present" and r["file"] == "label_enrich_Me.csv",
          "enrich save 落 label_enrich_Me.csv")
    check(not (out / (m2.LABEL_PREFIX + "Me.csv")).exists(), "enrich 不写全量卡")
    check(not me._save_one(out, {"card": "enrich_Me", "item_id": "nope#x",
                                 "label": "present"}, me.PROFILE_ENRICH)["ok"],
          "enrich save 拒绝不在包内的 item")

    # 5) 页面：占位符替换为 enrich_Me
    html_en = me._html_for(me.PROFILE_ENRICH)
    check("%%BOOT_CARD%%" not in html_en and 'boot("enrich_Me")' in html_en,
          "enrich 页面 boot 卡 = enrich_Me")


def main() -> int:
    test_me_sheet_items()
    test_me_save_and_norm()
    test_me_bundle_payload()
    test_me_parse_translations()
    test_me_norm_url()
    test_me_suggest()
    test_me_auditb_mode()
    test_me_enrich_mode()
    test_gui_status()
    test_gui_verify()
    test_gui_run_guard()
    test_gui_run_parallel()
    test_gui_config_relay()
    test_gui_config_keys()
    test_gui_route2b_profile()
    test_gui_stepfun_channel()
    test_gui_residual_scope()
    print(f"\n{_PASSED} passed, {len(_FAILURES)} failed")
    if _FAILURES:
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("M2 标注前端测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
