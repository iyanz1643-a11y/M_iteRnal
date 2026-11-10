"""기간 키(YYYYMM, 2026Q4, 2026H2, 2026) 계산."""
from datetime import date


def ym_to_tuple(ym: str):
    return int(ym[:4]), int(ym[4:6])


def ym_add(ym: str, n: int) -> str:
    y, m = ym_to_tuple(ym)
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}{idx % 12 + 1:02d}"


def ym_diff(a: str, b: str) -> int:
    """a - b (개월)."""
    ya, ma = ym_to_tuple(a)
    yb, mb = ym_to_tuple(b)
    return (ya * 12 + ma) - (yb * 12 + mb)


def ym_range(start: str, end: str):
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = ym_add(cur, 1)
    return out


def month_bounds(ym: str):
    y, m = ym_to_tuple(ym)
    first = date(y, m, 1)
    last = date(y + (m == 12), (m % 12) + 1, 1)
    return first.isoformat(), last.isoformat()  # [first, last)


def period_key(period_type: str, ym: str) -> str:
    y, m = ym_to_tuple(ym)
    if period_type == "month":
        return ym
    if period_type == "quarter":
        return f"{y}Q{(m - 1) // 3 + 1}"
    if period_type == "half":
        return f"{y}H{1 if m <= 6 else 2}"
    if period_type == "year":
        return f"{y}"
    raise ValueError(period_type)


def period_start(period_type: str, ym: str) -> str:
    y, m = ym_to_tuple(ym)
    if period_type == "month":
        return ym
    if period_type == "quarter":
        return f"{y}{((m - 1) // 3) * 3 + 1:02d}"
    if period_type == "half":
        return f"{y}{1 if m <= 6 else 7:02d}"
    if period_type == "year":
        return f"{y}01"
    raise ValueError(period_type)


def period_end(period_type: str, ym: str) -> str:
    y, m = ym_to_tuple(ym)
    if period_type == "month":
        return ym
    if period_type == "quarter":
        return f"{y}{((m - 1) // 3) * 3 + 3:02d}"
    if period_type == "half":
        return f"{y}{6 if m <= 6 else 12:02d}"
    if period_type == "year":
        return f"{y}12"
    raise ValueError(period_type)


def date_ym(d: str) -> str:
    return d[:4] + d[5:7]


def months_between(d_from: str, d_to: str) -> int:
    """두 날짜(YYYY-MM-DD) 사이 개월 수 (내림)."""
    y1, m1, d1 = int(d_from[:4]), int(d_from[5:7]), int(d_from[8:10])
    y2, m2, d2 = int(d_to[:4]), int(d_to[5:7]), int(d_to[8:10])
    n = (y2 - y1) * 12 + (m2 - m1)
    if d2 < d1:
        n -= 1
    return max(n, 0)
