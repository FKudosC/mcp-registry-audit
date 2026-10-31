"""爬虫入口（多源：official / smithery）。

用法：
    python -m mcp_audit crawl                      # 爬全部源
    python -m mcp_audit crawl --source smithery   # 只爬 Smithery
    python -m mcp_audit crawl --source smithery --pages 5   # 只跑 5 个查询词（测试）
    python -m mcp_audit crawl --fresh             # 丢弃断点开新轮（造新时间点）
    python -m mcp_audit crawl --resume            # 仅续未完成轮；无则跳过（可反复安全重跑）
    python -m mcp_audit crawl --no-details        # Smithery 不拉详情（快）
    python -m mcp_audit stats                     # 查看统计
"""

import argparse
import sys

from . import config, db
from .sources import get_source


def crawl(source_name: str, pages: int | None, fresh: bool,
          with_details: bool = True, detail_concurrency: int | None = None,
          resume: bool = False) -> None:
    conn = db.connect()
    db.init_db(conn)

    round_row = None if fresh else db.get_open_round(conn, source_name)
    existing_names: set[str] = set()
    if round_row is None and resume:
        print(f"[{source_name}] 无未完成轮，跳过（--resume 不新建轮）。")
        conn.close()
        return
    if round_row is None:
        round_id = db.start_round(conn, source_name)
        cursor, seen0 = None, 0
        print(f"[{source_name}] 新一轮爬取 round #{round_id}")
    else:
        round_id = round_row["id"]
        cursor, seen0 = round_row["last_cursor"], round_row["servers_seen"]
        print(f"[{source_name}] 续爬 round #{round_id}（已有 {seen0} 条，"
              f"cursor={cursor}）")
        # 断点续爬：预载本轮已有 server 名（跳过重复详情请求）
        existing_names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM snapshots WHERE round_id = ? AND source = ?",
                (round_id, source_name),
            )
        }

    src = get_source(
        source_name,
        with_details=with_details,
        existing_names=existing_names,
        detail_concurrency=detail_concurrency,
    )

    # 进度落库回调（断点）
    def save_progress(cur, s, p):
        db.update_round_progress(conn, round_id, cur, s, p)

    src.save_progress = save_progress

    total_new = 0
    try:
        for entries in src.iterate_entries(cursor, pages):
            written = db.save_entries(conn, round_id, source_name, entries)
            total_new += written
            print(f"  [{source_name}] 本批 {len(entries)} 条"
                  f"（新写入 {written}，累计 {seen0 + total_new}）")
        db.mark_round_done(conn, round_id)
        print(f"[{source_name}] round #{round_id} 完成（本轮共写入 {total_new}）。")
    except KeyboardInterrupt:
        print("\n手动中断，进度已保存（重跑即续爬）。")
    except RuntimeError as exc:
        print(f"[error] {exc} 进度已保存，重跑即续爬。")
        sys.exit(1)
    finally:
        src.close()
        conn.close()


def show_stats() -> None:
    conn = db.connect()
    db.init_db(conn)
    s = db.stats(conn)
    print("=== rounds ===")
    for r in s["rounds"]:
        print(f"  #{r['id']} {r['source']:9s} 开始={r['started_at'][:19]} "
              f"完成={'是' if r['done'] else '否'} servers={r['servers_seen']} "
              f"pages={r['pages_fetched']}")
    print("=== servers（按源） ===")
    for row in s["by_source"]:
        print(f"  {row['source']:9s}: {row['c']}")
    print(f"  合计     : {s['total_servers']}")
    print(f"snapshots 总数: {s['total_snapshots']}")
    print("=== 声明了 tools 的（按源） ===")
    for row in s["with_tools_declared"]:
        print(f"  {row['source']:9s}: {row['c']}")
    print(f"findings 总数  : {s['total_findings']}")
    print("=== clone 状态 ===")
    for row in s["clone_status"]:
        print(f"  {row['status']}: {row['c']}")
    print("=== clone 状态（按源 × 状态） ===")
    for line in db.format_clone_by_source(conn):
        print(line)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_audit",
                                     description="MCP 生态安全测量工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="爬取数据源")
    p_crawl.add_argument("--source", default="all",
                         choices=["all", "official", "smithery", "modelscope",
                                  "mcpso", "glama", "cursor", "aibase"])
    p_crawl.add_argument("--pages", type=int, default=None,
                         help="最多页数/查询词数（测试）")
    grp = p_crawl.add_mutually_exclusive_group()
    grp.add_argument("--fresh", action="store_true",
                     help="丢弃断点、强制新开一轮（造新时间点）")
    grp.add_argument("--resume", action="store_true",
                     help="仅续跑已有未完成轮；无则跳过（不新建轮），可反复安全重跑")
    p_crawl.add_argument("--no-details", action="store_true",
                         help="Smithery 跳过详情（不抓 tools 声明）")
    p_crawl.add_argument("--detail-concurrency", type=int, default=None,
                         help="Smithery 详情并发数（默认 48）")

    sub.add_parser("stats", help="查看数据库统计")

    args = parser.parse_args()
    if args.command == "crawl":
        sources = (["official", "smithery", "modelscope", "mcpso", "glama",
                    "aibase"]
                   if args.source == "all" else [args.source])
        for i, sname in enumerate(sources):
            if i:
                print()
            crawl(sname, args.pages, args.fresh, not args.no_details,
                  args.detail_concurrency, args.resume)
    elif args.command == "stats":
        show_stats()


if __name__ == "__main__":
    main()
