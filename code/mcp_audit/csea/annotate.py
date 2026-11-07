"""Terminal annotator: no browser, no server, resumable.

    python mcp_audit\\csea\\annotate.py --mode blind
    python mcp_audit\\csea\\annotate.py --mode verify --limit 20

Answers go to paper/csea/annotation/answers.csv after every item, so stopping
with q or Ctrl-C loses nothing and the run resumes where it left off.

The annotator answers eight facts and never sees a verdict prompt: the verdict is
computed from the facts afterwards, which is what makes this a test of the rule
rather than a test of agreement between two people.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from paths import ANN

ITEMS = ANN / "items.json"
ANS = ANN / "answers.csv"

CLASSES = ["arbitrary_code", "credential_paths", "db_access", "env_access",
           "fs_destructive", "network_egress", "subprocess", "shell_exec",
           "unsafe_deserialize"]

HEADER = ["id", "norm_url", "verdict", "reason", "mode", "sources",
          "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8",
          "revealed", "note"]

W = 78


def rule(t: str = "") -> None:
    print(t.ljust(W, "-") if t else "-" * W)


def load_done() -> dict:
    if not ANS.exists():
        return {}
    with ANS.open(encoding="utf-8-sig", newline="") as fh:
        return {r["id"]: r for r in csv.DictReader(fh) if r.get("id")}


def write_all(rows: dict) -> None:
    ANS.parent.mkdir(parents=True, exist_ok=True)
    with ANS.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for k in sorted(rows):
            w.writerow({h: rows[k].get(h, "") for h in HEADER})


def ask_choice(prompt: str, opts: list[str], default: str | None) -> str:
    while True:
        tail = f" [{'/'.join(opts)}]"
        if default:
            tail += f" (回车 = {default})"
        s = input(prompt + tail + ": ").strip()
        if not s and default:
            return default
        if s in opts:
            return s
        if s.isdigit() and 1 <= int(s) <= len(opts):
            return opts[int(s) - 1]
        print("   ! 请输入选项之一" + ("，或直接回车" if default else ""))


def ask_multi(prompt: str, default: list[str] | None) -> list[str]:
    for i, c in enumerate(CLASSES, 1):
        print(f"   {i:2d}  {c}")
    if default is not None:
        print(f"   预填: {', '.join(default) if default else '（无）'}")
    while True:
        hint = "采纳预填" if default is not None else "全不选"
        s = input(prompt + f" 编号（空格分隔；回车 = {hint}）: ").strip()
        if not s:
            return list(default or [])
        if s.lower() in {"none", "n", "-"}:
            return []
        try:
            idx = [int(x) for x in s.replace(",", " ").split()]
        except ValueError:
            print("   ! 只输编号")
            continue
        if all(1 <= k <= len(CLASSES) for k in idx):
            return sorted({CLASSES[k - 1] for k in idx})
        print("   ! 编号超出范围")


def show(it: dict, n: int, total: int) -> None:
    blind = it["mode"] == "blind"
    rule(f"[{n}/{total}]  {it['id']}  {it['mode'].upper()}"
         + ("  预填已隐藏" if blind else ""))
    print(f"判定     : {it['verdict']} / {it['reason']}")
    print(f"来源     : {it['sources']}      band: {it['band']}")
    print(f"仓库     : {it['norm_url']}")
    print(f"克隆     : {it['clone_status']}   files={it['files_scanned']}   "
          f"manifest={','.join(it['manifest_kinds']) or 'none'}")
    if it.get("commit"):
        print(f"commit   : {it['commit'][:12]}")
    if it.get("multi_id") or it.get("ambiguous"):
        print(f"标识     : multi_id={it.get('multi_id')} ambiguous={it.get('ambiguous')}")

    descs = it.get("descriptions") or {}
    print("\n-- 记录描述 " + "-" * (W - 7))
    if not descs:
        print("(无可比描述)")
    for k, v in descs.items():
        print(f"[{k}] {(v or '')[:320]}")

    ps = it.get("declared_per_source") or {}
    print("\n-- 各来源声明的能力类 " + "-" * (W - 13))
    if not ps:
        print("(没有任何来源声明能力类)")
    for k, v in ps.items():
        print(f"[{k}] {', '.join(v) if v else '(空)'}")

    det = it.get("detected") or []
    print("\n-- 检测器输出（要核对，不要直接信） " + "-" * (W - 32))
    if not det:
        print("(未检出)")
    prec = it.get("precision") or {}
    for c in det:
        p = prec.get(c)
        pstr = f"{p:5.1f}%" if isinstance(p, (int, float)) else "  -  "
        print(f"   {c:<22} 精度 {pstr}  触发文件: "
              f"{(it.get('detected_examples') or {}).get(c) or '-'}")
    if det:
        print("   低于 50% 的类别务必先核对触发文件再选 A5")
    mf = it.get("manifests") or []
    if mf:
        print("   manifests: " + ", ".join(mf[:5]))
    print()


def collect(it: dict) -> dict:
    blind = it["mode"] == "blind"
    pre = {} if blind else (it.get("prefill") or {})
    a: dict = {}

    print("A1 该记录的身份是否唯一指向一个仓库？")
    a["A1"] = ask_choice(
        "   ", ["是", "否", "不可见"],
        None if blind else ("否" if (it.get("multi_id") or it.get("ambiguous")) else "是"))

    print("A2 记录中是否存在可与代码比较的声明？")
    a["A2"] = ask_choice("   ", ["是", "否", "无法判断"], pre.get("A2"))

    print("A3 代码是否可得且非空壳？（看 clone 与 files_scanned）")
    a["A3"] = ask_choice("   ", ["是", "否", "无法判断"], pre.get("A3"))

    print("A4 记录声明了哪些能力类？")
    a["A4"] = ask_multi("   ", pre.get("A4") if not blind else None)

    print("A5 代码中『可见地』实现了哪些能力类？（不确定就别选）")
    a["A5"] = ask_multi("   ", pre.get("A5") if not blind else None)

    print("A6 版本是否固定到不可变修订？")
    a["A6"] = ask_choice("   ", ["是", "否", "不可见"], pre.get("A6"))

    print("A7 是否有第二个来源对同一属性给出可比较的声明？")
    a["A7"] = ask_choice("   ", ["是", "否", "无法判断"], pre.get("A7"))

    print("A8 该属性是否存在判定程序（能否据证据下结论）？")
    a["A8"] = ask_choice("   ", ["是", "否", "不适用"], pre.get("A8"))

    a["note"] = input("备注（可空，回车跳过）: ").strip()
    return a


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["blind", "verify", "all"], default="blind")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--suggest", choices=["auto", "off", "on"], default="auto",
                    help="显示机器建议；auto = 盲抽关、核验开")
    args = ap.parse_args()

    if not ITEMS.exists():
        sys.exit("找不到 paper/csea/annotation/items.json —— 先运行 make_queue.py")

    items = json.loads(ITEMS.read_text(encoding="utf-8"))
    if args.mode != "all":
        items = [i for i in items if i["mode"] == args.mode]

    done = load_done()
    todo = items if args.redo else [i for i in items if i["id"] not in done]
    if args.limit:
        todo = todo[: args.limit]

    print(f"\n模式 {args.mode}：共 {len(items)} 条，已完成 {len(items) - len(todo)}，"
          f"本次待做 {len(todo)}")
    print("提示：随时 Ctrl-C 退出，答案每做完一条立即落盘。\n")

    rows = dict(done)
    for n, it in enumerate(todo, 1):
        show(it, n, len(todo))
        reveal = (args.suggest == "on" or
                  (args.suggest == "auto" and it["mode"] != "blind"))
        if reveal and it.get("suggestion"):
            s = it["suggestion"]
            print("-- 机器建议（规则意见，不是事实） " + "-" * (W - 25))
            print(f"   {s['verdict']} · {s['reason']}")
            print(f"   {s['text']}")
            print("   你可以在下面全部不同意。\n")
        try:
            a = collect(it)
        except (KeyboardInterrupt, EOFError):
            print("\n已保存，退出。")
            break

        rows[it["id"]] = {
            "id": it["id"], "norm_url": it["norm_url"], "verdict": it["verdict"],
            "reason": it["reason"], "mode": it["mode"], "sources": it["sources"],
            "A4": ";".join(a["A4"]), "A5": ";".join(a["A5"]),
            "revealed": "suggest" if reveal else "",
            **{k: a[k] for k in ("A1", "A2", "A3", "A6", "A7", "A8", "note")},
        }
        write_all(rows)
        print(f"   ✓ 已保存（剩 {len(todo) - n} 条，累计 {len(rows)} 条）\n")

    print(f"\n完成。答案在 {ANS}")


if __name__ == "__main__":
    main()
