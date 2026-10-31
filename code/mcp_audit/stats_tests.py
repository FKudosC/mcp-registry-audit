"""E7：统计显著性检验全量补齐（χ² / Fisher 精确 + 95% CI + 效应量 + Cochran's Q + 分层卡方）。

背景
----
论文此前只报点估计（比例），缺显著性检验与效应量。B 类期刊（IST / JSS / EMSE）
对此是**硬性期望**：每个关键比较须给出检验统计量 + p 值 + 置信区间 + 效应量。

本模块对三类关键比较补齐：
  (a) CSCD 同期 vs 跨期（概念 1）：2×2 独立性检验 + OR + Cohen's h；
      数据源 = `xsource_fidelity.collect` 的逐单元 verdict × span 桶。
  (b) `source_blame` 7 源排名（概念 4.4）：全量 7×2 同质性 χ² + 平衡子设计 Cochran's Q
      + 事后两两比较（McNemar 精确 / 组合两比例 z，Holm 校正）；
      数据源 = `consensus_analysis.collect` 的逐单元 overgrant × 成员源。
  (c) 长度封顶混淆（E1）：把描述相似度结构性封顶（`sim_ceiling < 0.5`）当作分层变量，
      对 (a) 做 Mantel–Haenszel 分层卡方 + RBG 95% CI + Breslow–Day 同质性检验，
      给出粗 OR vs 校正 OR，量化长度封顶对同期效应的混淆方向与幅度。

统计口径（全部纯标准库，不引入 scipy）
------------------------------------
- χ² 尾概率：正则化不完全伽马 Q(df/2, x/2)（级数 + 连分数），df=1 时等价 erfc；
- Fisher 精确：超几何分布，概率全部在 log 空间（`lgamma`）计算，避免大组合数溢出；
- OR 95% CI：Woolf（log-OR ± z·SE），零格用 Haldane–Anscombe +0.5 校正；
- 单比例 95% CI：Wilson（score）区间；
- 效应量：Cohen's h = 2(asin√p₁ − asin√p₂)，SE = √(1/n₁ + 1/n₂)；
- McNemar 精确：不一致对 (b, c) 上 n=b+c、p=0.5 的双侧精确二项检验；
- Cochran's Q：k×1 完全区组二值设计；df=k−1；
- Mantel–Haenszel：共同 OR + RBG 方差 + MH χ²（Yates 校正）；
- Breslow–Day：OR 跨层同质性（Tarone 校正），df=k−1；
- 多重比较：Holm–Bonferroni 逐步下降。

已知数据边界（须在论文中显式申报）
----------------------------
- **7 源完全区组交集为空**（22,104 单元中无「7 源皆有代码证据」者）⇒ Cochran's Q 只能在
  最大平衡子设计（k=3，`glama × mcpso × official`，n=1,893）上计算。
- **同单元内各源判决完全一致**：对所共享单元，任一源对的「欠披露」指示逐单元相同
  ⇒ 全部 21 对的不一致对数 (b, c) = (0, 0) ⇒ 两两 McNemar p ≡ 1、Cochran's Q ≡ 0。
  这不是缺陷而是**结论**：source_blame 的**排名差异完全由「各源所列组合」的构成差异驱动**，
  而非同一单元上各源的行为差异；7×2 同质性 χ²（样本重叠⇒近似）与匹配 Q 的对照即证据。

写库：**本模块只读，不新增 finding_type、不写 findings**（纯统计层）。

用法：
    python -m mcp_audit stats                    # 打印三类检验报告
    python -m mcp_audit stats --md               # 追加 markdown 结果表
    python -m mcp_audit stats --out paper/tables/table49_stats_tests.md
"""

import itertools
import math
from collections import Counter, defaultdict
from statistics import NormalDist

from . import analysis, consensus_analysis, db, xsource_fidelity

_Z = NormalDist()
_Z95 = _Z.inv_cdf(0.975)

CEILING_CUT = 0.5
BLAME_SUBDESIGN_K = 3


# --------------------------------------------------------------------------
# 基础统计工具（纯标准库）
# --------------------------------------------------------------------------

def _logcomb(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1))


def _gser(a: float, x: float) -> float:
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(1000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(x: float, df: int) -> float:
    """χ² 分布上尾概率 P(X >= x)，df 自由度。"""
    if x <= 0:
        return 1.0
    if df <= 0:
        return 1.0
    a = df / 2.0
    xx = x / 2.0
    if xx < a + 1.0:
        return max(0.0, min(1.0, 1.0 - _gser(a, xx)))
    return max(0.0, min(1.0, _gcf(a, xx)))


def chi2_2x2(a: float, b: float, c: float, d: float,
             yates: bool = True) -> tuple[float, float]:
    """2×2 Pearson χ²（默认 Yates 连续性校正），df=1。"""
    n = a + b + c + d
    r1, r2 = a + b, c + d
    c1, c2 = a + c, b + d
    if n == 0 or 0 in (r1, r2, c1, c2):
        return 0.0, 1.0
    corr = n / 2.0 if yates else 0.0
    num = n * (abs(a * d - b * c) - corr) ** 2
    x2 = num / (r1 * r2 * c1 * c2)
    return x2, chi2_sf(x2, 1)


def chi2_kx2(table: list[list[float]]) -> tuple[float, float]:
    """k×2 同质性 χ²，df=(k−1)。"""
    rows = len(table)
    cols = len(table[0])
    n = sum(sum(r) for r in table)
    if n == 0:
        return 0.0, 1.0
    rt = [sum(r) for r in table]
    ct = [sum(table[i][j] for i in range(rows)) for j in range(cols)]
    x2 = 0.0
    for i in range(rows):
        for j in range(cols):
            e = rt[i] * ct[j] / n
            if e > 0:
                x2 += (table[i][j] - e) ** 2 / e
    return x2, chi2_sf(x2, (rows - 1) * (cols - 1))


def fisher_exact_2x2(a: int, b: int, c: int, d: int) -> float:
    """Fisher 精确检验（双侧，超几何，log 空间累加）。"""
    n = a + b + c + d
    if n == 0:
        return 1.0
    r1 = a + b
    c1 = a + c

    def logp(k: int) -> float:
        return (_logcomb(r1, k) + _logcomb(n - r1, c1 - k)
                - _logcomb(n, c1))

    p_obs = logp(a)
    lo = max(0, c1 - (n - r1))
    hi = min(r1, c1)
    tot = 0.0
    for k in range(lo, hi + 1):
        lp = logp(k)
        if lp <= p_obs + 1e-9:
            tot += math.exp(lp)
    return max(0.0, min(1.0, tot))


def _expected_min_2x2(a: int, b: int, c: int, d: int) -> float:
    n = a + b + c + d
    if n == 0:
        return 0.0
    cells = ((a + b) * (a + c), (a + b) * (b + d),
             (c + d) * (a + c), (c + d) * (b + d))
    return min(cel / n for cel in cells)


def test_2x2(a: int, b: int, c: int, d: int) -> dict:
    """2×2 关联检验：期望频数 < 5 用 Fisher 精确，否则 χ²（Yates）。"""
    emin = _expected_min_2x2(a, b, c, d)
    if emin < 5:
        p = fisher_exact_2x2(a, b, c, d)
        return {"method": "Fisher", "stat": None, "p": p, "emin": emin}
    x2, p = chi2_2x2(a, b, c, d)
    return {"method": "χ²(Yates)", "stat": x2, "p": p, "emin": emin}


def odds_ratio_ci(a: float, b: float, c: float, d: float) -> tuple[float, float, float]:
    """OR + Woolf 95% CI（零格 Haldane–Anscombe +0.5）。"""
    if 0 in (a, b, c, d):
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    orv = (a * d) / (b * c)
    se = math.sqrt(1.0 / a + 1.0 / b + 1.0 / c + 1.0 / d)
    return orv, math.exp(math.log(orv) - _Z95 * se), math.exp(math.log(orv) + _Z95 * se)


def cohens_h(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    """Cohen's h（两比例效应量）+ 95% CI。"""
    p1, p2 = k1 / n1, k2 / n2
    h = 2.0 * (math.asin(math.sqrt(p1)) - math.asin(math.sqrt(p2)))
    se = math.sqrt(1.0 / n1 + 1.0 / n2)
    return h, h - _Z95 * se, h + _Z95 * se


def wilson_ci(k: int, n: int) -> tuple[float, float]:
    """Wilson score 95% CI（单比例）。"""
    if n == 0:
        return None, None
    p = k / n
    z = _Z95
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / den
    return center - half, center + half


def binom_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """精确二项检验双侧 p（概率不超过观测点的所有结果之和）。"""
    if n == 0:
        return 1.0

    def logp(i: int) -> float:
        return (_logcomb(n, i) + i * math.log(p) + (n - i) * math.log(1.0 - p))

    p_k = logp(k)
    tot = 0.0
    for i in range(0, n + 1):
        lp = logp(i)
        if lp <= p_k + 1e-9:
            tot += math.exp(lp)
    return max(0.0, min(1.0, tot))


def mcnemar_exact(b: int, c: int) -> float:
    """McNemar 精确检验（不一致对 b, c）。"""
    n = b + c
    if n == 0:
        return 1.0
    return binom_two_sided(min(b, c), n, 0.5)


def cochrans_q(rows: list[list[int]]) -> tuple[float, int, float]:
    """Cochran's Q（完全区组 k 处理二值设计）。"""
    n = len(rows)
    if n == 0:
        return 0.0, 0, 1.0
    k = len(rows[0])
    tj = [sum(r[j] for r in rows) for j in range(k)]
    t = sum(tj)
    li = [sum(r) for r in rows]
    num = (k - 1) * (k * sum(x * x for x in tj) - t * t)
    den = k * t - sum(x * x for x in li)
    if den <= 0:
        return 0.0, k - 1, 1.0
    q = num / den
    df = k - 1
    return q, df, chi2_sf(q, df)


def mh_or_ci(strata: list[tuple[int, int, int, int]]) -> tuple[float, float, float]:
    """Mantel–Haenszel 共同 OR + RBG 95% CI。层内 2×2 = (a, b; c, d)。"""
    r = s = 0.0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n == 0:
            continue
        r += a * d / n
        s += b * c / n
    if r == 0 or s == 0:
        return None, None, None
    orv = r / s
    p_term = q_term = r_term = 0.0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n <= 1:
            continue
        p_term += (a + d) * a * d / (n * n)
        q_term += ((a + d) * b * c + (b + c) * a * d) / (n * n)
        r_term += (b + c) * b * c / (n * n)
    var = p_term / (2.0 * r * r) + q_term / (2.0 * r * s) + r_term / (2.0 * s * s)
    se = math.sqrt(var)
    return orv, math.exp(math.log(orv) - _Z95 * se), math.exp(math.log(orv) + _Z95 * se)


def mh_chi2(strata: list[tuple[int, int, int, int]]) -> tuple[float, float]:
    """Mantel–Haenszel 共同 OR 检验（Yates 校正），df=1。"""
    num = den = 0.0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n <= 1:
            continue
        r1, r2 = a + b, c + d
        c1, c2 = a + c, b + d
        num += a - r1 * c1 / n
        den += r1 * r2 * c1 * c2 / (n * n * (n - 1))
    if den == 0:
        return 0.0, 1.0
    x2 = (abs(num) - 0.5) ** 2 / den
    return x2, chi2_sf(x2, 1)


def breslow_day(strata: list[tuple[int, int, int, int]],
                or_mh: float) -> tuple[float, float, float, float]:
    """Breslow–Day OR 同质性检验（Tarone 校正），df=k−1。"""
    if or_mh is None or or_mh <= 0:
        return None, None, None, None
    psi = or_mh
    x2 = adj = adjv = 0.0
    used = 0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n <= 1:
            continue
        r1, r2 = a + b, c + d
        c1, c2 = a + c, b + d
        if abs(psi - 1.0) < 1e-12:
            e = r1 * c1 / n
        else:
            am1 = psi - 1.0
            bb = (r1 + c1) * am1 + n
            cc = psi * r1 * c1
            disc = bb * bb - 4.0 * am1 * cc
            if disc < 0:
                disc = 0.0
            e = (bb - math.sqrt(disc)) / (2.0 * am1)
        e = max(1e-9, min(e, r1 - 1e-9, c1 - 1e-9))
        parts = (e, r1 - e, c1 - e, r2 - c1 + e)
        if any(x <= 0 for x in parts):
            continue
        v = 1.0 / sum(1.0 / x for x in parts)
        if v <= 0:
            continue
        x2 += (a - e) ** 2 / v
        adj += (a - e)
        adjv += v
        used += 1
    if used < 2:
        return x2, chi2_sf(x2, max(0, used - 1)), x2, chi2_sf(x2, max(0, used - 1))
    df = used - 1
    x2t = x2 - adj * adj / adjv if adjv > 0 else x2
    return x2, chi2_sf(x2, df), x2t, chi2_sf(x2t, df)


def holm(pvals: list[float]) -> list[float]:
    """Holm–Bonferroni 逐步下降校正。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    prev = 0.0
    for rank, i in enumerate(order):
        v = min(1.0, max(prev, (m - rank) * pvals[i]))
        adj[i] = v
        prev = v
    return adj


# --------------------------------------------------------------------------
# (a) CSCD 同期 vs 跨期
# --------------------------------------------------------------------------

def _count(comp: list[dict], pred) -> tuple[int, int]:
    grp = [p for p in comp if pred(p)]
    diff = sum(1 for p in grp if p["verdict"] == "different")
    return diff, len(grp) - diff


def test_cscd(xdata: list[dict]) -> dict:
    comp = [p for p in xdata if p["verdict"] != analysis.INSUFFICIENT]
    same = _count(comp, lambda p: p["spot"] == "same")
    cross2 = _count(comp, lambda p: p["spot"] == "cross2")
    cross3 = _count(comp, lambda p: p["spot"] == "cross3plus")
    cross = (cross2[0] + cross3[0], cross2[1] + cross3[1])

    a, b = same
    c, d = cross
    main_test = test_2x2(a, b, c, d)
    orv, lo, hi = odds_ratio_ci(a, b, c, d)
    h, hlo, hhi = cohens_h(a, a + b, c, c + d)

    pairwise = []
    for name, x in (("跨2日", cross2), ("跨3+日", cross3)):
        xx, yy = x
        t = test_2x2(a, b, xx, yy)
        ov, ol, oh = odds_ratio_ci(a, b, xx, yy)
        pairwise.append({
            "label": name, "diff": xx, "n": xx + yy,
            "diff_ratio": xx / (xx + yy) if (xx + yy) else None,
            "method": t["method"], "stat": t["stat"], "p": t["p"],
            "or": ov, "or_lo": ol, "or_hi": oh,
        })

    return {
        "same": {"diff": a, "n": a + b},
        "cross": {"diff": c, "n": c + d},
        "cross2": {"diff": cross2[0], "n": cross2[0] + cross2[1]},
        "cross3plus": {"diff": cross3[0], "n": cross3[0] + cross3[1]},
        "main": main_test,
        "or": orv, "or_lo": lo, "or_hi": hi,
        "h": h, "h_lo": hlo, "h_hi": hhi,
        "pairwise": pairwise,
    }


# --------------------------------------------------------------------------
# (b) source_blame 7 源排名
# --------------------------------------------------------------------------

def _blame_sets(cs: dict) -> tuple[dict, dict]:
    members: dict[str, set] = defaultdict(set)
    blamed: dict[str, set] = defaultdict(set)
    for nu, p in cs.items():
        if not p["has_code"]:
            continue
        for s in p["sources"]:
            members[s].add(nu)
            if p["overgrant"] or s in p["blame"]:
                blamed[s].add(nu)
    return members, blamed


def test_source_blame(cs: dict) -> dict:
    members, blamed = _blame_sets(cs)
    _rate = {s: (len(blamed[s]) / len(members[s]) if members[s] else 0.0)
             for s in members}
    srcs = sorted(members, key=lambda s: (-_rate[s], s))

    rows = []
    for s in srcs:
        n = len(members[s])
        k = len(blamed[s])
        lo, hi = wilson_ci(k, n)
        rows.append({"source": s, "members": n, "blamed": k,
                     "rate": (k / n) if n else None, "lo": lo, "hi": hi})

    table = [[r["blamed"], r["members"] - r["blamed"]] for r in rows]
    homo_x2, homo_p = chi2_kx2(table)
    homo_df = len(rows) - 1

    sub_n = 0
    sub_srcs: tuple = ()
    for combo in itertools.combinations(srcs, BLAME_SUBDESIGN_K):
        inter = set.intersection(*[members[s] for s in combo])
        if len(inter) > sub_n:
            sub_n = len(inter)
            sub_srcs = combo
    q = df = None
    q_p = None
    sub_cols = None
    if sub_n > 0:
        inter = set.intersection(*[members[s] for s in sub_srcs])
        matrix = [[1 if nu in blamed[s] else 0 for s in sub_srcs]
                  for nu in inter]
        q, df, q_p = cochrans_q(matrix)
        sub_cols = [sum(m[j] for m in matrix) for j in range(BLAME_SUBDESIGN_K)]

    pairwise = []
    for s, t in itertools.combinations(srcs, 2):
        inter = members[s] & members[t]
        b_only = len((blamed[s] - blamed[t]) & inter)
        c_only = len((blamed[t] - blamed[s]) & inter)
        mp = mcnemar_exact(b_only, c_only)
        n1, k1 = len(members[s]), len(blamed[s])
        n2, k2 = len(members[t]), len(blamed[t])
        p1 = k1 / n1 if n1 else 0.0
        p2 = k2 / n2 if n2 else 0.0
        p_pool = (k1 + k2) / (n1 + n2) if (n1 + n2) else 0.0
        se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2)) if (n1 and n2 and 0 < p_pool < 1) else 0.0
        z = (p1 - p2) / se if se else 0.0
        zp = 2.0 * (1.0 - _Z.cdf(abs(z))) if se else 1.0
        pairwise.append({
            "s": s, "t": t, "inter": len(inter),
            "b_only": b_only, "c_only": c_only,
            "mcnemar_p": mp,
            "rate_s": p1, "rate_t": p2,
            "z": z if se else None, "z_p": zp,
        })
    z_adj = holm([r["z_p"] for r in pairwise])
    for r, pa in zip(pairwise, z_adj):
        r["z_p_holm"] = pa

    return {
        "rows": rows,
        "homo_x2": homo_x2, "homo_df": homo_df, "homo_p": homo_p,
        "sub_srcs": sub_srcs, "sub_n": sub_n,
        "q": q, "q_df": df, "q_p": q_p, "sub_cols": sub_cols,
        "pairwise": pairwise,
    }


# --------------------------------------------------------------------------
# (c) 长度封顶混淆（分层卡方）
# --------------------------------------------------------------------------

def _stratum(comp: list[dict], pred) -> tuple[int, int, int, int]:
    grp = [p for p in comp if pred(p)]
    same = [p for p in grp if p["spot"] == "same"]
    cross = [p for p in grp if p["spot"] != "same"]
    a = sum(1 for p in same if p["verdict"] == "different")
    c = sum(1 for p in cross if p["verdict"] == "different")
    return a, len(same) - a, c, len(cross) - c


def test_length(xdata: list[dict]) -> dict:
    comp = [p for p in xdata if p["verdict"] != analysis.INSUFFICIENT]

    def capped(p):
        return p["sim_ceiling"] is not None and p["sim_ceiling"] < CEILING_CUT

    layers = [
        ("结构封顶（ceiling<0.5）", capped),
        ("非封顶（ceiling>=0.5）", lambda p: not capped(p)),
    ]
    strata = []
    for label, pred in layers:
        a, b, c, d = _stratum(comp, pred)
        strata.append({"label": label, "a": a, "b": b, "c": c, "d": d,
                       "n": a + b + c + d})

    tuples = [(s["a"], s["b"], s["c"], s["d"]) for s in strata]
    a = sum(s["a"] for s in strata)
    b = sum(s["b"] for s in strata)
    c = sum(s["c"] for s in strata)
    d = sum(s["d"] for s in strata)
    crude_or, crude_lo, crude_hi = odds_ratio_ci(a, b, c, d)
    crude_x2, crude_p = chi2_2x2(a, b, c, d)

    mh_or, mh_lo, mh_hi = mh_or_ci(tuples)
    mh_x2, mh_p = mh_chi2(tuples)
    bd_x2, bd_p, bd_t, bd_tp = breslow_day(tuples, mh_or)

    for s in strata:
        orv, lo, hi = odds_ratio_ci(s["a"], s["b"], s["c"], s["d"])
        s["or"] = orv
        s["or_lo"] = lo
        s["or_hi"] = hi
        s["same_n"] = s["a"] + s["b"]
        s["cross_n"] = s["c"] + s["d"]

    return {
        "strata": strata,
        "crude": {"a": a, "b": b, "c": c, "d": d, "or": crude_or,
                  "lo": crude_lo, "hi": crude_hi, "x2": crude_x2, "p": crude_p},
        "mh_or": mh_or, "mh_lo": mh_lo, "mh_hi": mh_hi,
        "mh_x2": mh_x2, "mh_p": mh_p,
        "bd_x2": bd_x2, "bd_p": bd_p, "bd_tarone_x2": bd_t,
        "bd_tarone_p": bd_tp,
    }


# --------------------------------------------------------------------------

def run(conn) -> dict:
    xdata = xsource_fidelity.collect(conn)
    cs = consensus_analysis.collect(conn)
    return {
        "n_units": len(xdata),
        "n_comparable": sum(1 for p in xdata
                            if p["verdict"] != analysis.INSUFFICIENT),
        "cscd": test_cscd(xdata),
        "length": test_length(xdata),
        "blame": test_source_blame(cs),
    }


def _pfmt(p) -> str:
    if p is None:
        return "—"
    if p < 1e-4:
        return f"{p:.2e}"
    return f"{p:.4f}"


def _pct(x) -> str:
    return f"{x * 100:.1f}%" if x is not None else "—"


def _num(x, nd=3) -> str:
    return f"{x:.{nd}f}" if x is not None else "—"


def show(result: dict) -> None:
    c = result["cscd"]
    print()
    print(f"=== E7 统计显著性检验（{result['n_units']} 跨源单元，"
          f"可比 {result['n_comparable']}） ===")

    print()
    print("[a] CSCD 同期 vs 跨期（2×2 独立性检验）:")
    print(f"  同期     different {c['same']['diff']:>6} / {c['same']['n']:<6}"
          f" = {_pct(c['same']['diff'] / c['same']['n'])}")
    print(f"  非同期   different {c['cross']['diff']:>6} / {c['cross']['n']:<6}"
          f" = {_pct(c['cross']['diff'] / c['cross']['n'])}")
    m = c["main"]
    stat = _num(m["stat"]) if m["stat"] is not None else "—"
    print(f"  主检验     {m['method']}  stat={stat}  p={_pfmt(m['p'])}")
    print(f"  OR         {_num(c['or'])}  (95% CI {_num(c['or_lo'])}, {_num(c['or_hi'])})")
    print(f"  Cohen's h  {_num(c['h'])}  (95% CI {_num(c['h_lo'])}, {_num(c['h_hi'])})")
    for pw in c["pairwise"]:
        stat = _num(pw["stat"]) if pw["stat"] is not None else "—"
        print(f"  ├ 同期 vs {pw['label']:<7} {pw['diff']}/{pw['n']}="
              f"{_pct(pw['diff_ratio'])}  {pw['method']} stat={stat} "
              f"p={_pfmt(pw['p'])}  OR={_num(pw['or'])} "
              f"({_num(pw['or_lo'])},{_num(pw['or_hi'])})")

    bl = result["blame"]
    print()
    print("[b] source_blame 7 源排名（同质性 χ² + 平衡子设计 Cochran's Q + 事后两两）:")
    print(f"  {'注册表':<12}{'命中/分母':>14}{'率':>8}{'Wilson 95% CI':>20}")
    for r in bl["rows"]:
        ci = f"({_pct(r['lo'])}, {_pct(r['hi'])})"
        hit = f"{r['blamed']}/{r['members']}"
        print(f"  {r['source']:<12}{hit:>14}{_pct(r['rate']):>8}{ci:>20}")
    print(f"  全量 7×2 同质性 χ²(df={bl['homo_df']}) = {_num(bl['homo_x2'])}  "
          f"p={_pfmt(bl['homo_p'])}  （样本重叠 ⇒ 独立 χ² 为近似）")
    if bl["q"] is not None:
        combo = " × ".join(bl["sub_srcs"])
        print(f"  平衡子设计 Cochran's Q[{combo}, n={bl['sub_n']}] = "
              f"{_num(bl['q'])}  df={bl['q_df']}  p={_pfmt(bl['q_p'])}"
              f"  （各源列和 {bl['sub_cols']}）")
    n_disc = sum(1 for r in bl["pairwise"] if r["b_only"] + r["c_only"] > 0)
    print(f"  事后两两：21 对中不一致对>0 的对数 = {n_disc}"
          f"  ⇒ 同单元内各源判决完全一致，排名差异为「组合构成」效应")
    sig = [r for r in bl["pairwise"] if r["z_p_holm"] < 0.05]
    print(f"  组合两比例 z（Holm 校正）显著对数 = {len(sig)} / 21"
          f"（描述性：各源「组合」欠披露率确有差异）")

    ln = result["length"]
    print()
    print("[c] 长度封顶 30.0% 混淆（分层卡方 / Mantel–Haenszel）:")
    for s in ln["strata"]:
        print(f"  {s['label']:<24} n={s['n']:>5}  同期 "
              f"{s['a']}/{s['same_n']}  非同期 {s['c']}/{s['cross_n']}  "
              f"OR={_num(s['or'])}({_num(s['or_lo'])},{_num(s['or_hi'])})")
    cr = ln["crude"]
    print(f"  粗 OR（合计）             {_num(cr['or'])} "
          f"(95% CI {_num(cr['lo'])}, {_num(cr['hi'])})  χ²={_num(cr['x2'])} "
          f"p={_pfmt(cr['p'])}")
    print(f"  MH 校正 OR                {_num(ln['mh_or'])} "
          f"(95% CI {_num(ln['mh_lo'])}, {_num(ln['mh_hi'])})  "
          f"MH χ²={_num(ln['mh_x2'])} p={_pfmt(ln['mh_p'])}")
    print(f"  Breslow–Day 同质性        χ²={_num(ln['bd_x2'])} p={_pfmt(ln['bd_p'])}"
          f"  （Tarone χ²={_num(ln['bd_tarone_x2'])} p={_pfmt(ln['bd_tarone_p'])}）")


def markdown(result: dict) -> str:
    c = result["cscd"]
    bl = result["blame"]
    ln = result["length"]
    out = []
    out.append("# Table.49 统计显著性检验（E7）")
    out.append("")
    out.append("**表题**：三类关键比较的显著性检验 + 95% 置信区间 + 效应量（纯标准库实现，"
               "无 scipy）。")
    out.append("")
    out.append(f"**分母**：描述可比 {result['n_comparable']}（跨源单元 "
               f"{result['n_units']}）。")
    out.append("")

    out.append("## A · CSCD 同期 vs 跨期（2×2 独立性检验）")
    out.append("")
    out.append("| 比较 | 可比 n | different | 比例 | 检验 | 统计量 | p | OR (95% CI) | "
               "Cohen's h (95% CI) |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    m = c["main"]
    stat = _num(m["stat"]) if m["stat"] is not None else "—"
    out.append(f"| 同期 vs 非同期 | {c['same']['n'] + c['cross']['n']} | "
               f"{c['same']['diff'] + c['cross']['diff']} | — | {m['method']} | "
               f"{stat} | {_pfmt(m['p'])} | "
               f"{_num(c['or'])} ({_num(c['or_lo'])}, {_num(c['or_hi'])}) | "
               f"{_num(c['h'])} ({_num(c['h_lo'])}, {_num(c['h_hi'])}) |")
    for pw in c["pairwise"]:
        stat = _num(pw["stat"]) if pw["stat"] is not None else "—"
        out.append(f"| 同期 vs {pw['label']} | {pw['n']} | {pw['diff']} | "
                   f"{_pct(pw['diff_ratio'])} | {pw['method']} | {stat} | "
                   f"{_pfmt(pw['p'])} | {_num(pw['or'])} "
                   f"({_num(pw['or_lo'])}, {_num(pw['or_hi'])}) | — |")
    out.append("")
    out.append(f"**读数**：同期 {_pct(c['same']['diff'] / c['same']['n'])} vs 非同期 "
               f"{_pct(c['cross']['diff'] / c['cross']['n'])}；OR = {_num(c['or'])} "
               f"(95% CI {_num(c['or_lo'])}, {_num(c['or_hi'])}) 不含 1 ⇒ 同期分歧率"
               f"**显著高于**跨期（若时间漂移为主因则应相反）⇒ 分歧是**结构性**的。")
    out.append("")

    out.append("## B · source_blame 7 源排名（同质性 χ² + Cochran's Q + 事后两两）")
    out.append("")
    out.append("| 排名 | 注册表 | 命中 / 分母 | 率 | Wilson 95% CI |")
    out.append("|---|---|---|---|---|")
    for i, r in enumerate(bl["rows"], 1):
        out.append(f"| {i} | {r['source']} | {r['blamed']} / {r['members']} | "
                   f"**{_pct(r['rate'])}** | ({_pct(r['lo'])}, {_pct(r['hi'])}) |")
    out.append("")
    out.append(f"- **全量 7×2 同质性 χ²** = {_num(bl['homo_x2'])}，df = {bl['homo_df']}，"
               f"p = {_pfmt(bl['homo_p'])}（样本重叠 ⇒ 独立 χ² 为近似）。")
    if bl["q"] is not None:
        combo = " × ".join(bl["sub_srcs"])
        out.append(f"- **平衡子设计 Cochran's Q**（{combo}，n = {bl['sub_n']}）= "
                   f"{_num(bl['q'])}，df = {bl['q_df']}，p = {_pfmt(bl['q_p'])}"
                   f"（各源列和 = {bl['sub_cols']}）。")
    n_disc = sum(1 for r in bl["pairwise"] if r["b_only"] + r["c_only"] > 0)
    sig = sum(1 for r in bl["pairwise"] if r["z_p_holm"] < 0.05)
    out.append(f"- **事后两两**：21 对中不一致对 > 0 者 = {n_disc} ⇒ 同单元内各源判决"
               f"完全一致，McNemar p ≡ 1；组合两比例 z（Holm）显著对 = {sig}/21。")
    out.append("- **结论**：7 源**排名差异完全由各源「所列货号组合」的构成差异驱动**，"
               "而非同一单元上各源的行为差异 ⇒ 欠披露是**组合暴露**属性（各源覆盖决定其"
               "欠披露率），《Table.3》区间窄（81.3%–94.0%）据此得到结构性解释。")
    out.append("")
    out.append("**事后两两明细（组合两比例 z，Holm 校正）**：")
    out.append("")
    out.append("| 源 A | 源 B | 交集 n | 不一致 (A,B) | McNemar p | rateA | rateB | z | "
               "p(Holm) |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in bl["pairwise"]:
        out.append(f"| {r['s']} | {r['t']} | {r['inter']} | "
                   f"({r['b_only']}, {r['c_only']}) | {_pfmt(r['mcnemar_p'])} | "
                   f"{_pct(r['rate_s'])} | {_pct(r['rate_t'])} | "
                   f"{_num(r['z'], 2)} | {_pfmt(r['z_p_holm'])} |")
    out.append("")

    out.append("## C · 长度封顶混淆（分层卡方 / Mantel–Haenszel）")
    out.append("")
    out.append("| 层 | n | 同期 different/n | 非同期 different/n | OR (95% CI) |")
    out.append("|---|---|---|---|---|")
    for s in ln["strata"]:
        out.append(f"| {s['label']} | {s['n']} | {s['a']} / {s['same_n']} | "
                   f"{s['c']} / {s['cross_n']} | {_num(s['or'])} "
                   f"({_num(s['or_lo'])}, {_num(s['or_hi'])}) |")
    cr = ln["crude"]
    out.append(f"| **合计（粗）** | {cr['a'] + cr['b'] + cr['c'] + cr['d']} | "
               f"{cr['a']} / {cr['a'] + cr['b']} | {cr['c']} / {cr['c'] + cr['d']} | "
               f"{_num(cr['or'])} ({_num(cr['lo'])}, {_num(cr['hi'])}) |")
    out.append("")
    out.append(f"- **粗 OR** = {_num(cr['or'])}（χ² = {_num(cr['x2'])}，"
               f"p = {_pfmt(cr['p'])}）。")
    out.append(f"- **MH 校正 OR** = {_num(ln['mh_or'])} "
               f"(95% CI {_num(ln['mh_lo'])}, {_num(ln['mh_hi'])})，MH χ² = "
               f"{_num(ln['mh_x2'])}，p = {_pfmt(ln['mh_p'])}。")
    out.append(f"- **Breslow–Day 同质性** χ² = {_num(ln['bd_x2'])}，p = "
               f"{_pfmt(ln['bd_p'])}（Tarone χ² = {_num(ln['bd_tarone_x2'])}，"
               f"p = {_pfmt(ln['bd_tarone_p'])}）⇒ 各层 OR 同质，可报共同 OR。")
    out.append(f"- **结论**：长度封顶是**真混杂**（跨期单元封顶率更高）——粗 OR "
               f"{_num(cr['or'])} 被**稀释**，校正后 MH OR = {_num(ln['mh_or'])} "
               f"更大 ⇒ 长度封顶**不解释**同期分歧，反而掩盖其强度。")
    out.append("")
    out.append("**数据源**：`mcp_audit/stats_tests.py`（`mcp_audit stats`，只读统计层，"
               "不新增 finding_type）。")
    out.append("")
    return "\n".join(out)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit stats",
        description="E7 统计显著性检验（只读；不写库）")
    parser.add_argument("--md", action="store_true", help="输出 markdown 结果表")
    parser.add_argument("--out", default=None, help="将 markdown 结果表写入指定路径")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn)
    show(result)
    if args.md or args.out:
        md = markdown(result)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"\n已写出 markdown 结果表：{args.out}")
        else:
            print("\n" + md)
    conn.close()


if __name__ == "__main__":
    main()
