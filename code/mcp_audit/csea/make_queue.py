"""Build the annotation queue: stratified sample, machine-prefilled facts, and a
single-file review page.

The sample is stratified on the *verdict*, not on the old text band, because the
verdict is the construct the validation is about. Within each stratum 25% of the
items are marked ``blind``: the review page hides the machine prefill for those,
so anchoring can be measured rather than assumed.

    python make_queue.py [--n 600] [--blind-share 0.25]

Writes:
    annotation/queue.csv     one row per sampled unit, machine facts prefilled
    annotation/review.html   self-contained review page (no server needed)
    annotation/answers.csv   empty template + header for the annotator export
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import dispatch as D
import csea_core as C
from paths import ANN, OUT as OUT_DIR

OUT = ANN
SEED = 20261103

CLASSES = sorted(C.ALL_CLASSES)

#: Hand-adjudicated detector precision, Table 2 of the manuscript. Shown next to
#: each detected class so the annotator knows how much the hit is worth.
PRECISION = {
    "env_access": 100.0, "subprocess": 92.5, "network_egress": 87.5,
    "db_access": 75.0, "shell_exec": 62.5, "fs_destructive": 35.0,
    "credential_paths": 10.0, "arbitrary_code": 5.0, "unsafe_deserialize": 2.5,
}


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def load_verdicts(recs_path: Path) -> list[dict]:
    with recs_path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def stratify(rows: list[dict], n_total: int, blind_share: float, rng: random.Random):
    """Allocate equally across verdicts, then proportionally across reasons.

    Equal allocation is deliberate: the rare verdicts (SUPPORTED, UNRESOLVED) are
    exactly the ones whose precision the validation has to report, so a
    proportional sample would leave them unestimable. The sampling weight is
    recorded per item so the corpus-level rates can be recovered by reweighting.
    """
    by_v = defaultdict(list)
    for r in rows:
        by_v[r["verdict"]].append(r)
    present = [v for v in by_v if by_v[v]]
    per_v = n_total / max(1, len(present))

    picked = []
    for v in present:
        items = by_v[v]
        target = min(len(items), max(1, int(round(per_v))))
        by_r = defaultdict(list)
        for r in items:
            by_r[r["reason"]].append(r)
        for rk, ris in by_r.items():
            t = min(len(ris), max(1, int(round(target * len(ris) / len(items)))))
            picked.extend(rng.sample(ris, t))

    # sampling weight per (verdict, reason) stratum
    pop = Counter((r["verdict"], r["reason"]) for r in rows)
    got = Counter((r["verdict"], r["reason"]) for r in picked)
    for r in picked:
        k = (r["verdict"], r["reason"])
        r["_w"] = round(pop[k] / got[k], 4) if got[k] else None

    rng.shuffle(picked)
    n_blind = int(round(len(picked) * blind_share))
    blind_idx = set(rng.sample(range(len(picked)), n_blind))
    for i, r in enumerate(picked):
        r["_mode"] = "blind" if i in blind_idx else "verify"
    # Blind items first: the annotator should meet them before any prefill has been
    # seen, so the anchoring measurement is not contaminated by warm-up.
    picked.sort(key=lambda r: 0 if r["_mode"] == "blind" else 1)
    return picked


# --------------------------------------------------------------------------
# Evidence bundles
# --------------------------------------------------------------------------

def evidence_for(norm_url: str, ctx: dict, verdict_row: dict) -> dict:
    cp = ctx["cap"].get(norm_url) or {}
    co = ctx["cons"].get(norm_url) or {}
    rp = ctx["repl"].get(norm_url) or {}
    cl = ctx["clone"].get(norm_url)

    detected = sorted((cp.get("detected") or co.get("actual") or set()) & C.ALL_CLASSES)
    declared = sorted((co.get("declared") or set()) & C.ALL_CLASSES)
    examples = cp.get("examples") or {}

    per_source = {}
    for s, v in (co.get("per_source") or {}).items():
        per_source[s] = sorted(set(v or []) & C.ALL_CLASSES)

    return {
        "norm_url": norm_url,
        "sources": verdict_row.get("sources", ""),
        "verdict": verdict_row.get("verdict", ""),
        "reason": verdict_row.get("reason", ""),
        "band": verdict_row.get("band", ""),
        "clone_status": cl or "missing",
        "files_scanned": cp.get("files_scanned", 0),
        "manifest_kinds": cp.get("manifest_kinds") or [],
        "manifests": list(dict.fromkeys(cp.get("manifests") or []))[:6],
        "has_channel": bool(cp.get("has_channel")),
        "multi_id": bool(int(verdict_row.get("multi_id") or 0)),
        "detected": detected,
        "detected_examples": {k: examples.get(k) for k in detected},
        "declared": declared,
        "declared_per_source": per_source,
        "commit": cp.get("commit_sha") or rp.get("stored_sha"),
        "declared_version": rp.get("declared_version"),
        "head_sha": rp.get("head_sha"),
        "descriptions": {},
    }


def attach_descriptions(ev: dict, desc_rows: dict) -> None:
    d = desc_rows.get(ev["norm_url"]) or {}
    descs = d.get("descriptions") or {}
    ev["descriptions"] = {k: (v[0] if isinstance(v, list) and v else v)
                          for k, v in descs.items()}


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<title>CSEA fact review</title>
<style>
 :root{--bg:#f7f7f5;--fg:#1c1c1a;--mut:#6b6b66;--line:#d9d9d4;--acc:#1f6feb;--warn:#b45309}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.6 -apple-system,"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
 header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:10px 18px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header b{font-size:15px}
 .bar{flex:1;min-width:160px;height:8px;background:#e8e8e3;border-radius:4px;overflow:hidden}
 .bar i{display:block;height:100%;background:var(--acc)}
 main{max-width:1420px;margin:0 auto;padding:18px}
 .cols{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;align-items:start}
 .col-side{position:sticky;top:62px;max-height:calc(100vh - 80px);overflow:auto;padding-right:2px}
 .col-side .card{position:relative}
 @media (max-width:1080px){
   .cols{grid-template-columns:1fr}
   .col-side{position:static;max-height:none;overflow:visible}
 }
 .card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}
 .tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:#eceff8;color:#2b3a67;margin-right:6px}
 .tag.blind{background:#fdf2e3;color:var(--warn)}
 .mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
 .mut{color:var(--mut)}
 h3{margin:18px 0 8px;font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
 table{border-collapse:collapse;width:100%;font-size:13px}
 td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
 .q{margin:14px 0;padding:12px 14px;border:1px solid var(--line);border-radius:8px}
 .q p{margin:0 0 8px}
 .q .ask{font-weight:600}
 label{display:inline-flex;align-items:center;gap:5px;margin-right:14px;cursor:pointer}
 .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
 .chip{border:1px solid var(--line);border-radius:999px;padding:3px 10px;cursor:pointer;font-size:12.5px;background:#fff}
 .chip.on{background:#e8f0fe;border-color:#a9c3f5}
 nav{display:flex;gap:10px;justify-content:space-between;margin-top:20px}
 button{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer}
 button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
 button:disabled{opacity:.45;cursor:not-allowed}
 .desc{border-left:3px solid var(--line);padding-left:10px;margin:8px 0;white-space:pre-wrap}
 .sug{border:1px solid #cfe0ff;background:#f4f8ff;border-radius:8px;padding:10px 14px;margin:0 0 16px}
 .sug summary{cursor:pointer;font-weight:600;color:#1f4fa8}
 .sug p{margin:8px 0 0}
 .inp{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font:inherit}
 .row{display:flex;gap:10px;align-items:center;margin:6px 0}
 .row label{width:90px;color:var(--mut);margin:0}
 button.mini{padding:4px 12px;font-size:13px;border-radius:6px}
 .aibox{border:1px solid #d8e6ff;background:#fbfdff;border-radius:8px;padding:10px 12px;margin-top:10px}
 .aibox table{margin:6px 0}
 .badge-ai{background:#e6f4ea;color:#1a6b35}
 .badge-no{background:#fde8e8;color:#9b1c1c}
 .sidenav{padding:10px 14px}
 .batchbar{padding:10px 14px}
 .batchbar .row2{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .batchbar .row2 label{margin:0;width:auto;color:var(--fg)}
</style></head><body>
<header>
  <b>CSEA fact review</b>
  <span id="pos" class="mut"></span>
  <div class="bar"><i id="prog"></i></div>
  <span id="mode"></span>
  <button onclick="toggleAI()">设置</button>
  <button onclick="exportCSV()">导出 answers.csv</button>
</header>
<main id="app"></main>
<script>
const DATA = __DATA__;
const CLASSES = __CLASSES__;
const KEY = "csea_answers_v1";
let answers = {};
try { answers = JSON.parse(localStorage.getItem(KEY) || "{}"); }
catch (e) { answers = {}; }              // file:// without storage still works
let i = 0;
function persist(){ try { localStorage.setItem(KEY, JSON.stringify(answers)); } catch (e) {} }
function save(){ persist(); }
let aiOpen = false;
// Open state of the suggestion box per item. render() rebuilds the DOM, so the
// <details> would snap shut on every AI reply without this map.
let sugOpen = {};
function sugToggle(id, open){
  sugOpen[id] = open;
  /* opening the box records nothing */
}
function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function aiCfg(){ try { return JSON.parse(localStorage.getItem("csea_ai") || "{}"); } catch(e){ return {}; } }
function setCfg(k,v){
  const c = aiCfg(); c[k] = v;
  try{ localStorage.setItem("csea_ai", JSON.stringify(c)); }catch(e){}
  batch.error = "";            // a fixed setting should not stay blocked
}
function toggleAI(){ aiOpen = !aiOpen; render(); }

// ---------------------------------------------------------------- AI batch --
let autoAI = true;
try { const v = localStorage.getItem("csea_auto"); if (v !== null) autoAI = v === "1"; } catch(e){}
let batch = {busy: false, done: 0, total: 0, failed: 0, error: ""};
let dismissed = {};
let SUG = {};        // suggestion per item, never persisted
let SUGERR = {};

function aheadGap(k){
  let gap = 0;
  for (let j = 1; j <= k; j++){
    const d = DATA[(i + j) % DATA.length];
    if (!SUG[d.id] && !SUGERR[d.id] && !dismissed[d.id]) gap++;
  }
  return gap;
}

function toggleAuto(on){ autoAI = on; try{ localStorage.setItem("csea_auto", on ? "1" : "0"); }catch(e){} render(); }
function aiReady(){ const c = aiCfg(); return c.base_url && c.model && c.api_key; }

function nextIds(n){
  const out = [];
  for (let k = 0; k < DATA.length && out.length < n; k++){
    const d = DATA[(i + k) % DATA.length];
    const a = ans(d.id);
    if (!SUG[d.id] && !SUGERR[d.id] && !dismissed[d.id]) out.push(d.id);
  }
  return out;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
let workers = 16;
try { const v = localStorage.getItem("csea_workers"); if (v) workers = Math.max(1, Math.min(32, parseInt(v, 10) || 16)); } catch(e){}
function setWorkers(n){ workers = n; try { localStorage.setItem("csea_workers", String(n)); } catch(e){} render(); }

async function fetchOne(id){
  const c = aiCfg();
  let last;
  for (let attempt = 0; attempt < 3; attempt++){
    try {
      const r = await fetch("/api/suggest", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: id, base_url: c.base_url, model: c.model, api_key: c.api_key})
      });
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || "调用失败");
      SUG[id] = j.facts;      // memory only: answers is not touched at all
      delete SUGERR[id];
      return;
    } catch(e) {
      last = e;
      await sleep(700 * (attempt + 1));
    }
  }
  throw last;
}

let priming = {};
async function primeCurrent(){
  const id = cur().id;
  if (SUG[id] || SUGERR[id] || priming[id] || dismissed[id] || !aiReady()) return;
  priming[id] = true;
  try { await fetchOne(id); }
  catch(e){ SUGERR[id] = String(e.message || e).slice(0, 200); }
  finally { delete priming[id]; }
  try { render(); } catch(e) {}
}

function retryFailed(){
  Object.keys(SUGERR).forEach(k => delete SUGERR[k]);
  batch.error = "";
  runBatch(100);
}

async function runBatch(n){
  if (batch.busy) return;
  if (!aiReady()){ aiOpen = true; batch.error = "先填端点设置（base_url / model / api_key）"; render(); return; }
  const ids = nextIds(n);
  if (!ids.length) return;
  batch = {busy: true, done: 0, total: ids.length, failed: 0, error: ""};
  render();
  const t0 = Date.now();
  const tick = setInterval(() => {
    const st = document.getElementById("batchstate");
    if (st) st.textContent = `生成中 0/${ids.length} · ${Math.round((Date.now() - t0) / 1000)}s`;
  }, 1000);
  try {
    const c = aiCfg();
    const r = await fetch("/api/suggest_batch", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ids: ids, base_url: c.base_url, model: c.model,
                            api_key: c.api_key, concurrency: workers})
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "批次失败");
    for (const it of (j.results || [])){
      if (it.facts) SUG[it.id] = it.facts;
      else SUGERR[it.id] = String(it.error || "失败").slice(0, 200);
    }
    batch.done = (j.results || []).length;
    batch.failed = (j.results || []).filter(x => !x.facts).length;
  } catch(e) {
    ids.forEach(id => { SUGERR[id] = String(e.message || e).slice(0, 200); });
    batch.failed = ids.length;
    batch.error = "批次失败：" + (e.message || e);
  } finally {
    clearInterval(tick);
    batch.busy = false;
    batch.lastFailed = batch.failed;
    if (batch.total > 0 && batch.failed === batch.total && !batch.error){
      batch.error = "全部失败，检查端点与 key";
    }
    try { render(); } catch(e) {}
    save();
  }
}
window.onerror = function(m, s, l, c, e){
  const b = document.createElement("div");
  b.style.cssText = "background:#fee;border:1px solid #c00;padding:10px;margin:10px;font-family:monospace;white-space:pre-wrap";
  b.textContent = "页面脚本报错（请把这段发给我）：\n" + m + " @ " + l + ":" + c + "\n" + ((e && e.stack) || "");
  document.body.prepend(b);
  return false;
};
function cur(){ return DATA[i]; }
function ans(id){ return answers[id] || {}; }

function setVal(id, k, v){
  answers[id] = answers[id] || {};
  answers[id][k] = v;
  save();
}

async function askItem(id){
  const c = aiCfg();
  if(!c.base_url || !c.model || !c.api_key){
    aiOpen = true; render();
    alert("先在「设置」里填 base_url、model、api_key。");
    return;
  }
  const st = document.getElementById("aistate");
  if(st) st.textContent = "调用中…（最长 90 秒）";
  try{
    const r = await fetch("/api/suggest", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: id, base_url: c.base_url, model: c.model, api_key: c.api_key})
    });
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || "调用失败");
    answers[id] = answers[id] || {};
    SUG[id] = j.facts;
    delete SUGERR[id];
    render();
  }catch(e){
    const st2 = document.getElementById("aistate");
    if(st2) st2.textContent = "失败：" + e.message;
  }
}

function applyItem(id){
  const a = answers[id] || {};
  const f = SUG[id];
  if(!f) return;
  ["A1","A2","A3","A6","A7","A8"].forEach(k => { if(f[k]) a[k] = f[k]; });
  if(f.A4) a.A4 = f.A4.slice();
  if(f.A5) a.A5 = f.A5.slice();
  answers[id] = a; save(); render();
}

function dismissItem(id){
  const a = answers[id] || {};
  delete SUG[id];                 // withdrawn; nothing about it was ever stored
  dismissed[id] = true;           // do not fetch it again in this session
  render();
}

function chipGroup(id, key, opts, prefillReadonly){
  return `<div class="chips">` + opts.map(o=>{
    const on = (ans(id)[key]||[]).includes(o);
    return `<span class="chip ${on?'on':''}" onclick="toggle('${id}','${key}','${o}',${!!prefillReadonly})">${o}</span>`;
  }).join("") + `</div>`;
}
function toggle(id,key,val,readonly){
  if(readonly) return;
  const a = ans(id); const arr = a[key] || [];
  const j = arr.indexOf(val);
  if(j>=0) arr.splice(j,1); else arr.push(val);
  setVal(id,key,arr); render();
}
function radio(id,key,opts){
  return opts.map(o=>`<label><input type="radio" name="${id}_${key}" ${ans(id)[key]===o?"checked":""} onchange="setVal('${id}','${key}','${o}');render()"> ${o}</label>`).join("");
}

function aiBox(id){
  const f = SUG[id];
  if(!f && SUGERR[id]){
    return `<div class="aibox">
      <div class="mut" style="color:#b45309">请求失败：${esc(SUGERR[id])}</div>
      <button class="mini" style="margin-top:8px" onclick="askItem('${id}')">重试</button>
    </div>`;
  }
  if(!f) return "";
  const val = v => Array.isArray(v) ? (v.join(", ") || "（空）") : (v || "-");
  const row = (k, lab) => `<tr><td style="width:150px">${lab}</td><td>${esc(val(f[k]))}</td></tr>`;
  const btns = `<button class="mini pri" onclick="applyItem('${id}')">采纳</button>
    <button class="mini" style="margin-left:8px" onclick="dismissItem('${id}')">不采纳</button>`;
  return `<div class="aibox">
    <div><b>建议</b> <span class="mut">置信度 ${f.confidence==null?"-":f.confidence}</span></div>
    <div class="mut" style="margin:6px 0">${esc(f.rationale||"")}</div>
    <table>
      ${row("A1","身份唯一")}${row("A2","可比声明")}${row("A3","代码可得")}
      ${row("A4","记录声明")}${row("A5","代码可见")}${row("A6","版本固定")}
      ${row("A7","第二来源")}${row("A8","有判定程序")}
    </table>
    ${btns}
  </div>`;
}

function isFilled(id){
  const a = ans(id);
  return ["A1","A2","A3","A6","A7","A8"].some(k => a[k]) ||
         (a.A4 && a.A4.length) || (a.A5 && a.A5.length);
}
function jumpTo(v){
  const n = parseInt(v, 10);
  if (!n) return;
  i = Math.max(0, Math.min(DATA.length - 1, n - 1));
  render();
}
function firstUnanswered(){
  const k = DATA.findIndex(d => !isFilled(d.id));
  if (k >= 0) { i = k; render(); }
  else alert("全部已填。");
}
function navHtml(){
  const filled = DATA.filter(d => isFilled(d.id)).length;
  return `<div style="display:flex;gap:10px;align-items:center">
    <button onclick="go(-1)" ${i===0?"disabled":""}>← 上一条</button>
    <button class="pri" onclick="go(1)" ${i===DATA.length-1?"disabled":""}>下一条 →</button>
    <span class="mut" style="margin-left:auto">${i+1} / ${DATA.length}</span>
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
    <input id="jump" class="inp" style="width:92px" type="number" min="1" max="${DATA.length}"
           placeholder="题号" onkeydown="if(event.key==='Enter')jumpTo(this.value)">
    <button class="mini" onclick="jumpTo(document.getElementById('jump').value)">跳转</button>
    <button class="mini" onclick="firstUnanswered()">第一个未填</button>
    <span class="mut" style="margin-left:auto">已填 ${filled}</span>
  </div>
  <div class="mut" style="margin-top:8px;font-size:12px">← → 翻页 · a 采纳 · r 不采纳</div>`;
}

function batchBar(){
  const ready = Object.keys(SUG).length;
  const st = batch.busy ? `生成中 ${batch.done}/${batch.total}`
                        : `已备 ${ready} 条`;
  return `<div class="card batchbar">
    <div class="row2">
      <label><input type="checkbox" ${autoAI?"checked":""} onchange="toggleAuto(this.checked)"> 自动预填</label>
      <button class="mini" onclick="runBatch(100)" ${batch.busy?"disabled":""}>预填 100 条</button>
      <span id="batchstate" class="mut">${st}</span>
      ${Object.keys(SUGERR).length ? `<button class="mini" onclick="retryFailed()">重试失败 ${Object.keys(SUGERR).length} 条</button>` : ""}
    </div>
    ${batch.error?`<div class="mut" style="color:#b45309;margin-top:6px">${esc(batch.error)}</div>`:""}
    ${Object.keys(SUGERR).length?`<div class="mut" style="color:#b45309;margin-top:6px">有 ${Object.keys(SUGERR).length} 条请求失败；点上面的按钮可重试。</div>`:""}
  </div>`;
}

function render(){
  const d = cur();
  const blind = d.mode === "blind";
  document.getElementById("pos").textContent = `${i+1} / ${DATA.length}`;
  document.getElementById("prog").style.width = (100*(i+1)/DATA.length).toFixed(1)+"%";
  document.getElementById("mode").innerHTML = blind
    ? '<span class="tag blind">盲抽：预填已隐藏</span>'
    : '<span class="tag">核验：预填仅供参考</span>';

  const pre = blind ? {} : d.prefill;
  let h = "";
  // Side column: navigation, batch controls, settings, suggestion. All of it
  // stays put while the questions scroll.
  let side = `<div class="card sidenav">${navHtml()}</div>` + batchBar();
  if (aiOpen) {
    const c = aiCfg();
    side += `<div class="card" style="border-color:#cfe0ff">
      <h3 style="margin-top:0">端点</h3>
      <div class="row"><label>base_url</label><input id="ai_base" class="inp" value="${esc(c.base_url||"")}" placeholder="https://api.deepseek.com" oninput="setCfg('base_url',this.value)"></div>
      <div class="row"><label>model</label><input id="ai_model" class="inp" value="${esc(c.model||"")}" placeholder="deepseek-chat" oninput="setCfg('model',this.value)"></div>
      <div class="row"><label>api_key</label><input id="ai_key" type="password" class="inp" value="${esc(c.api_key||"")}" placeholder="sk-..." oninput="setCfg('api_key',this.value)"></div>
      <div class="row"><label>并发</label><select class="inp" onchange="setWorkers(+this.value)">
        ${[1,2,4,8,16,24,32].map(n => `<option value="${n}" ${workers===n?"selected":""}>${n}</option>`).join("")}
      </select></div>
      <div class="mut">任意 OpenAI 兼容端点。key 只存在本浏览器，只发往 127.0.0.1 的本地代理，不写盘、不进日志。</div>
    </div>`;
  }
  h += `<div class="card">
    <div><span class="tag">${d.verdict || "-"}</span>
    ${d.reason?`<span class="tag">${d.reason}</span>`:""}
    <span class="tag">${d.sources||""}</span>
    <span class="tag">band: ${d.band||"-"}</span>
    </div>
    <div class="mono" style="margin-top:8px">${d.norm_url}</div>
    <div class="mut" style="margin-top:6px">clone: <b>${d.clone_status}</b> ·
      files_scanned: <b>${d.files_scanned}</b> ·
      manifest kinds: <b>${(d.manifest_kinds||[]).join(", ")||"none"}</b> ·
      commit: <span class="mono">${(d.commit||"-").slice(0,10)}</span></div>`;

  const desks = d.descriptions || {};
  const keys = Object.keys(desks);
  h += `<h3>Record descriptions</h3>`;
  if(!keys.length) h += `<div class="mut">（无可比描述）</div>`;
  keys.forEach(k=>{ h += `<div class="desc"><b>${k}</b><br>${(desks[k]||"").slice(0,600)}</div>`; });

  h += `<h3>Declared capability classes (per source)</h3>`;
  const ps = d.declared_per_source || {};
  const psKeys = Object.keys(ps);
  if(!psKeys.length) h += `<div class="mut">（没有来源声明任何能力类）</div>`;
  psKeys.forEach(k=>{ h += `<div><b>${k}</b>: ${(ps[k]||[]).join(", ")||"∅"}</div>`; });

  h += `<h3>Detector output (verify, do not trust)</h3>`;
  const det = d.detected || [];
  if(!det.length) h += `<div class="mut">（未检出任何类别）</div>`;
  else {
    h += `<table><tr><th>class</th><th>precision</th><th>triggering file</th></tr>`;
    det.forEach(c=>{
      const p = (d.precision||{})[c];
      h += `<tr><td class="mono">${c}</td><td>${p!=null? p.toFixed(1)+"%":"-"}</td>`+
           `<td class="mono">${(d.detected_examples||{})[c]||"-"}</td></tr>`;
    });
    h += `</table>`;
    h += `<div class="mut" style="margin-top:6px">precision 为人工裁定的单类精度（Table 2）；低于 50% 的类别务必核对触发文件后再选 A5。</div>`;
  }
  h += `<div class="mut" style="margin-top:6px">manifests: ${(d.manifests||[]).join(", ")||"none"}</div>`;
  h += `</div>`;

  // Suggestion + AI live in the side column so they stay visible while the
  // questions scroll. The open state is kept in sugOpen because render() rebuilds
  // the DOM and would otherwise snap the box shut on every AI reply.
  const sug = d.suggestion;
  if (sug) {
    const isOpen = (d.id in sugOpen) ? sugOpen[d.id] : true;
    side += `<details class="sug" ${isOpen ? "open" : ""} ontoggle="sugToggle('${d.id}', this.open)">
      <summary>建议</summary>
      <p><b>${sug.verdict}</b> · ${sug.reason}</p>
      <p class="mut">${sug.text}</p>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="mini" onclick="askItem('${d.id}')">重新请求</button>
        <span id="aistate" class="mut"></span>
      </div>
      ${aiBox(d.id)}
      <p class="mut" style="margin-top:10px">只是意见，不是事实。你可以在左边全部不同意。</p>
    </details>`;
  }

  // questions
  h += `<div class="card">
    <div class="q"><p class="ask">A1 该记录的身份是否唯一指向一个仓库？</p>
      <div class="mut">预填：${d.multi_id? "多标识（entry>source）":"单标识"}${blind?"":"；歧义表："+(d.ambiguous?"命中":"未命中")}</div>
      ${radio(d.id,"A1",["是","否","不可见"])}</div>

    <div class="q"><p class="ask">A2 记录中是否存在可与代码比较的声明？</p>
      <div class="mut">预填：${pre.A2 ?? "-"}</div>
      ${radio(d.id,"A2",["是","否","无法判断"])}</div>

    <div class="q"><p class="ask">A3 代码是否可得且非空壳？</p>
      <div class="mut">预填：${pre.A3 ?? "-"}</div>
      ${radio(d.id,"A3",["是","否","无法判断"])}</div>

    <div class="q"><p class="ask">A4 记录声明了哪些能力类？（多选，按你读到的记录）</p>
      <div class="mut">预填：${(pre.A4||[]).join(", ")||"-"}</div>
      ${chipGroup(d.id,"A4",CLASSES,false)}</div>

    <div class="q"><p class="ask">A5 代码中"可见地"实现哪些能力类？（点开触发文件核对；不确定就不选）</p>
      <div class="mut">预填（检测器给出的）：${(pre.A5||[]).join(", ")||"-"}</div>
      ${chipGroup(d.id,"A5",CLASSES,false)}</div>

    <div class="q"><p class="ask">A6 版本是否固定到不可变修订？</p>
      <div class="mut">预填：${pre.A6 ?? "-"}</div>
      ${radio(d.id,"A6",["是","否","不可见"])}</div>

    <div class="q"><p class="ask">A7 是否有第二个来源对同一属性给出可比较的声明？</p>
      <div class="mut">预填：${pre.A7 ?? "-"}</div>
      ${radio(d.id,"A7",["是","否","无法判断"])}</div>

    <div class="q"><p class="ask">A8 该属性是否存在判定程序（能否据证据下结论）？</p>
      <div class="mut">预填：${pre.A8 ?? "-"}</div>
      ${radio(d.id,"A8",["是","否","不适用"])}</div>

    <div class="q"><p class="ask">备注（可选）</p>
      <textarea id="note" style="width:100%;min-height:56px" oninput="setVal('${d.id}','note',this.value)">${ans(d.id).note||""}</textarea></div>
  </div>`;

  // navigation lives in the side column (navHtml), so it never scrolls away

  document.getElementById("app").innerHTML =
    `<div class="cols"><div class="col-main">${h}</div>` +
    `<div class="col-side">${side}</div></div>`;
  // Blind arm: no machine value may be visible, including the identity hint.
  if (blind) {
    document.querySelectorAll("#app .mut").forEach(e => {
      if ((e.textContent || "").trim().startsWith("预填")) e.remove();
    });
  }
  // Auto mode: fetch the next 30 in the background as soon as the current item
  // has no suggestion yet. Guarded by batch.busy, so it can fire at most once.
  if (autoAI && aiReady()){
    // The visible item first: a batch can take tens of seconds, and a cursor that
    // moved during it would otherwise land on items its window never covered.
    primeCurrent();
    // Fill only when the buffer ahead is running thin, and never right after a
    // batch that had failures: that would hammer an endpoint which just refused.
    if (!batch.busy && !batch.error && SUG[d.id] &&
        (batch.lastFailed || 0) === 0 && aheadGap(5) > 0) runBatch(100);
  }
}

function go(d){ i = Math.max(0, Math.min(DATA.length-1, i+d)); window.scrollTo(0,0); render(); }

function exportCSV(){
  const head = ["id","norm_url","verdict","reason","mode","sources","A1","A2","A3","A4","A5","A6","A7","A8","note"];
  const rows = DATA.map(d=>{
    const a = answers[d.id] || {};
    return [d.id, d.norm_url, d.verdict, d.reason, d.mode, d.sources,
      a.A1||"", a.A2||"", a.A3||"", (a.A4||[]).join(";"), (a.A5||[]).join(";"),
      a.A6||"", a.A7||"", a.A8||"",
      (a.note||"").replace(/[\r\n]+/g," ")];
  });
  const csv = [head, ...rows].map(r=>r.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
  const blob = new Blob(["\ufeff"+csv], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "answers.csv"; a.click();
}

function acceptCurrent(){
  const id = cur().id;
  if (SUG[id]) applyItem(id);
}
function dismissCurrent(){
  const id = cur().id;
  if (SUG[id]) dismissItem(id);
}

document.addEventListener("keydown", e=>{
  const t = (e.target.tagName || "").toUpperCase();
  if (t === "TEXTAREA" || t === "INPUT" || t === "SELECT") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key){
    case "ArrowRight": go(1); break;
    case "ArrowLeft":  go(-1); break;
    case "a": case "A": acceptCurrent(); break;
    case "r": case "R": dismissCurrent(); break;
  }
});

render();
</script></body></html>
"""


def suggestion_for(ev: dict, verdict: str, reason: str) -> dict:
    """The rule's own opinion, stated so the annotator can disagree with it."""
    det = ev["detected"]
    decl = ev["declared"]
    kinds = ev["manifest_kinds"]
    det_decl = sorted(set(det) & C.DECLARABLE_CLASSES)
    missing = sorted(set(det_decl) - set(decl))

    head = (f"检出 {len(det)} 类" + (f"（{', '.join(det)}）" if det else "") +
            f"；记录声明 {len(decl)} 类" + (f"（{', '.join(decl)}）" if decl else ""))

    why = {
        "CHANNEL_ABSENT": (f"该制品只带 {', '.join(kinds) or '无 manifest'}，没有 "
                           f"server.json，{len(det_decl)} 个可声明类无处可写。"),
        "NO_DECLARING_CHANNEL": "检出的类别全部没有声明字段（schema 缺位）。",
        "UNDECLARED_CAPABILITY": f"可声明类 {', '.join(det_decl)} 全部未在记录中声明。",
        "DECLARATION_SUBSET": (f"可声明类 {', '.join(det_decl)} 中缺 "
                               f"{', '.join(missing)}。"),
        "FULL_MATCH": "可声明类均已声明。",
        "CODE_OPAQUE": "拿到克隆但没扫到可分析内容。",
        "CODE_GONE": "索引 URL 已不可解析。",
        "CODE_UNAVAILABLE": "克隆失败，没有代码证据。",
        "CODE_TOO_LARGE": "仓库超过体积上限，未克隆。",
        "NO_DECLARATION": "记录里没有任何可比较的声明。",
        "IDENTITY_AMBIGUOUS": "该名字映射到多个仓库，身份无法唯一确定。",
        "SOURCE_DISAGREEMENT": "两个来源对同一属性给出互斥值。",
    }.get(reason, reason)

    return {"verdict": verdict, "reason": reason, "text": f"{head}。{why}"}


def build_page(items: list[dict]) -> str:
    payload = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    page = PAGE.replace("__DATA__", payload)
    return page.replace("__CLASSES__", json.dumps(CLASSES, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--blind-share", type=float, default=0.25)
    ap.add_argument("--db", default=D.DEFAULT_DB)
    ap.add_argument("--out", default=str(ANN))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    recs = load_verdicts(OUT_DIR / "records.csv")
    # attach the claim reason so stratification can use it
    reasons = {}
    with (OUT_DIR / "verdicts.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["claim_type"] == "DECLARATION_COMPLETE":
                reasons[r["norm_url"]] = r["reason"]
    for r in recs:
        r["reason"] = reasons.get(r["norm_url"], "")

    rng = random.Random(SEED)
    picked = stratify(recs, args.n, args.blind_share, rng)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ctx = D.load(con)

    desc_rows = {}
    cur = con.cursor()
    cur.execute("SELECT data_json FROM findings WHERE finding_type='desc_diff'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        desc_rows[o["norm_url"]] = o

    # detector examples live in code_capability
    cur.execute("SELECT data_json FROM findings WHERE finding_type='code_capability'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        if o["norm_url"] in ctx["cap"]:
            ctx["cap"][o["norm_url"]]["examples"] = o.get("danger_examples") or {}

    items, rows = [], []
    for k, r in enumerate(picked):
        ev = evidence_for(r["norm_url"], ctx, r)
        attach_descriptions(ev, desc_rows)
        ev["ambiguous"] = r["norm_url"] in ctx["ambig"]
        uid = f"{k:04d}"
        det_decl = sorted(set(ev["detected"]) & C.DECLARABLE_CLASSES)

        prefill = {
            "A2": "是" if (ev["declared"] or ev["manifests"]) else "否",
            "A3": "是" if (ev["clone_status"] == "ok" and ev["files_scanned"] > 0) else "否",
            "A4": ev["declared"],
            "A5": ev["detected"],
            "A6": "是" if ev["declared_version"] else "否",
            "A7": "是" if len([1 for v in ev["declared_per_source"].values() if v]) >= 2 else "否",
            "A8": "是" if (det_decl and ev["has_channel"]) else "否",
        }

        items.append({"id": uid, "mode": r["_mode"], "norm_url": ev["norm_url"],
                      "verdict": r["verdict"], "reason": r["reason"],
                      "sources": ev["sources"], "band": ev["band"],
                      "clone_status": ev["clone_status"],
                      "files_scanned": ev["files_scanned"],
                      "manifest_kinds": ev["manifest_kinds"],
                      "manifests": ev["manifests"], "commit": ev["commit"],
                      "detected": ev["detected"],
                      "detected_examples": ev["detected_examples"],
                      "precision": PRECISION,
                      "declared_per_source": ev["declared_per_source"],
                      "descriptions": ev["descriptions"],
                      "multi_id": ev["multi_id"], "ambiguous": ev["ambiguous"],
                      "suggestion": suggestion_for(ev, r["verdict"], r["reason"]),
                      "prefill": prefill})

        rows.append({"id": uid, "mode": r["_mode"], "weight": r["_w"],
                     "norm_url": ev["norm_url"],
                     "verdict": r["verdict"], "reason": r["reason"],
                     "sources": ev["sources"], "band": ev["band"],
                     **{f"pre_{k2}": (",".join(v) if isinstance(v, list) else v)
                        for k2, v in prefill.items()}})

    with (out / "queue.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    (out / "review.html").write_text(build_page(items), encoding="utf-8")
    (out / "items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    # Never clobber collected answers: this script is re-run whenever the page is
    # regenerated, and an annotator's work is not reproducible.
    ans_path = out / "answers.csv"
    if not ans_path.exists():
        with ans_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "norm_url", "verdict", "reason", "mode", "sources",
                        "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "note"])
    else:
        print(f"  answers.csv 已存在（{ans_path.name}），未覆盖")

    cnt = Counter((r["verdict"], r["_mode"]) for r in picked)
    print(f"sampled {len(picked)} units -> {out}")
    for (v, m), k in sorted(cnt.items()):
        print(f"  {v:13s} {m:7s} {k}")
    print(f"  blind share: {sum(1 for r in picked if r['_mode']=='blind')}/{len(picked)}")
    print("  open annotation/review.html in a browser")


if __name__ == "__main__":
    main()
