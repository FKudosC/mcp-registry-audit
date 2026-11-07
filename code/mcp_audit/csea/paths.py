"""Where the CSEA code lives and where its artifacts go.

    code       mcp_audit/csea/    <- this package
    artifacts  paper/csea/        <- out/, annotation/, notes

Keeping the two apart is deliberate: the analysis code belongs with the other
analysis code, and everything the manuscript cites belongs under paper/.
"""

from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = CODE.parents[1]                  # mcp-audit/
ARTIFACTS = ROOT / "paper" / "csea"
OUT = ARTIFACTS / "out"
ANN = ARTIFACTS / "annotation"

DEFAULT_DB = r"D:\Data\mcp-audit\mcp_audit.db"
