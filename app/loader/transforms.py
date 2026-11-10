"""셀 값 변환 규칙. 매핑 YAML의 transforms 에서 이름으로 호출한다."""
import re
from datetime import date, datetime, timedelta

DATE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d")


def _blank(v):
    return v is None or (isinstance(v, str) and v.strip() in ("", "-"))


def as_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def parse_money(v):
    """'1,234,000' / '(1,000)' / '150,000원' / 숫자 → 숫자. 빈 값은 None."""
    if _blank(v):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    t = str(v).strip().replace(",", "").replace("원", "").replace(" ", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if t.startswith("-"):
        neg, t = True, t[1:]
    n = float(t)
    n = int(n) if n.is_integer() else n
    return -n if neg else n


def parse_percent(v):
    """'850%' → 8.5, '30%' → 0.3, 숫자는 그대로."""
    if _blank(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    if t.endswith("%"):
        return float(t[:-1].replace(",", "")) / 100
    return float(t)


def parse_date(v):
    """여러 날짜 표기 → 'YYYY-MM-DD'. 빈 값은 None."""
    if _blank(v):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)):  # Excel 일련번호 또는 20261005
        n = int(v)
        if n > 19000101:
            return datetime.strptime(str(n), "%Y%m%d").date().isoformat()
        return (date(1899, 12, 30) + timedelta(days=n)).isoformat()
    t = str(v).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"날짜 형식 인식 불가: {v!r}")


def to_int(v):
    return None if _blank(v) else int(float(str(v).replace(",", "")))


def to_float(v):
    return None if _blank(v) else float(str(v).replace(",", ""))


SIMPLE = {
    "strip": lambda v: v.strip() if isinstance(v, str) else v,
    "upper": lambda v: v.upper() if isinstance(v, str) else v,
    "as_text": as_text,
    "to_int": to_int,
    "to_float": to_float,
    "parse_money": parse_money,
    "parse_percent": parse_percent,
    "parse_date": parse_date,
}


def apply(value, steps):
    for step in steps or []:
        if isinstance(step, str):
            value = SIMPLE[step](value)
        elif "remove" in step:
            if isinstance(value, str):
                for ch in step["remove"]:
                    value = value.replace(ch, "")
        elif "map" in step:
            key = value.strip() if isinstance(value, str) else value
            value = step["map"].get(key, step.get("default", value))
        elif "multiply" in step:
            value = None if value is None else value * step["multiply"]
        else:
            raise ValueError(f"알 수 없는 변환: {step}")
    return value


def normalize_policy_no(v):
    """증권번호 정규화: 공백·하이픈·점 제거, 대문자."""
    t = as_text(v).strip().upper()
    return re.sub(r"[\s\-.]", "", t) or None
