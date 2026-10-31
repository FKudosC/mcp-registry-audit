from . import (agcm_annotate_gui, agcm_annotate_me_gui,
               agcm_audit, analysis, annotate_gui, annotation, artifact_demo,
               c1_reclone, cap_extract, cloner, code_analysis,
               consensus_analysis, consistency_analysis, defense_baseline,
               desc_conflict, drift_analysis, e1_ablation, fork_analysis,
               stats_tests, xsource_cli, xsource_fidelity)
from .crawler import main as crawl_main


def main() -> None:
    import sys

    # 行缓冲：输出被重定向到文件/管道时 Python 默认块缓冲（8KB），
    # 会让长任务几十分钟没有任何回显。这里强制每行立即落盘，终端/日志实时可见。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    if len(sys.argv) > 1 and sys.argv[1] == "clone":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        cloner.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "c1-reclone":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        c1_reclone.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "compare-desc":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        analysis.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "entity-ambiguity":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        analysis.ambiguity_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "code-scan":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        code_analysis.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "drift":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        drift_analysis.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "fork":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        fork_analysis.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "consistency":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        consistency_analysis.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "consensus":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        consensus_analysis.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "xsource":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        xsource_fidelity.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "xsource-cli":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        xsource_cli.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "defense":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        defense_baseline.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "cap-extract":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        cap_extract.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "e1-ablation":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        e1_ablation.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        stats_tests.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "desc-conflict":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        desc_conflict.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "demo":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        artifact_demo.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "annotate":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        annotation.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "gui":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        annotate_gui.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "agcm-audit":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        agcm_audit.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "agcm-annotate-me":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        agcm_annotate_me_gui.main()
    elif len(sys.argv) > 1 and sys.argv[1] == "agcm-annotate-gui":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        agcm_annotate_gui.main()
    else:
        crawl_main()


if __name__ == "__main__":
    main()
