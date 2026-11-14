"""실제 양식 엑셀을 읽는 공통 도우미: 머리글 검사, 민감 열 버리기, 행 읽기.

민감 열은 머리글만 보고 위치를 정한 뒤 그 칸의 값은 읽지 않는다. 그래서 버린 칸의 값은
원본 행(raw_row), 표준 행, 오류 메시지 어디에도 들어가지 않는다.
"""
import openpyxl

from app.loader.transforms import _blank, apply, normalize_header


class HeaderMismatch(Exception):
    """머리글이 매핑과 달라 적재 전에 막았다. reasons 에 사유 목록이 있다."""

    def __init__(self, reasons, file_name=""):
        self.reasons = list(reasons)
        super().__init__(f"머리글이 매핑과 달라 적재하지 않았습니다{' (' + file_name + ')' if file_name else ''}: " + " / ".join(self.reasons))


def is_sensitive(header: str, keywords) -> bool:
    return any(k in header for k in keywords or [])


def open_sheet(path, sheet=None, header_row=None, must_have=()):
    """시트와 머리글 행을 고른다. header_row 가 없으면 앞 10행에서 must_have 가 모두 있는 행을 찾는다.
    돌려주는 것: (workbook, worksheet, header_row, headers). headers 는 정규화한 머리글이고 끝의 빈 칸은 뺀다."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = [wb[sheet]] if sheet and sheet in wb.sheetnames else list(wb.worksheets)
    for ws in sheets:
        rows = list(ws.iter_rows(min_row=1, max_row=header_row or 10, values_only=True))
        candidates = [header_row] if header_row else range(1, len(rows) + 1)
        for r in candidates:
            if r is None or r > len(rows):
                continue
            headers = [normalize_header(v) for v in rows[r - 1]]
            while headers and headers[-1] == "":
                headers.pop()
            if header_row or all(normalize_header(h) in headers for h in must_have):
                return wb, ws, r, headers
    wb.close()
    raise HeaderMismatch([f"머리글 행을 찾지 못함: {', '.join(must_have)} 이 있는 행이 없습니다"])


def check_headers(headers, expected=None, required=None, drop_keywords=None):
    """머리글을 검사한다. 돌려주는 것: (막는 사유 목록, 버린 열 이름 목록).
    expected 가 있으면 이름과 순서가 모두 같아야 한다. required 만 있으면 그 열이 있기만 하면 된다.
    민감 열은 검사에서 빼고 버린다."""
    dropped = [h for h in headers if h and is_sensitive(h, drop_keywords)]
    kept = [h for h in headers if not (h and is_sensitive(h, drop_keywords))]
    reasons = []
    blanks = [i + 1 for i, h in enumerate(kept) if h == ""]
    if blanks:
        reasons.append(f"이름 없는 열: {', '.join(map(str, blanks[:5]))}번째")
    dups = sorted({h for h in kept if h and kept.count(h) > 1})
    if dups:
        reasons.append(f"이름이 겹치는 열: {', '.join(dups)}")
    if expected is not None:
        exp = [normalize_header(h) for h in expected]
        missing = [h for h in exp if h not in kept]
        extra = [h for h in kept if h and h not in exp]
        if missing:
            reasons.append(f"없는 열 {len(missing)}개: {', '.join(missing)}")
        if extra:
            reasons.append(f"매핑에 없는 열 {len(extra)}개: {', '.join(extra)}")
        if not missing and not extra and not dups and kept != exp:
            moved = [f"{i + 1}번째 '{e}' 자리에 '{k}'" for i, (e, k) in enumerate(zip(exp, kept)) if e != k]
            reasons.append(f"열 순서가 바뀜: {', '.join(moved[:5])}{' 외' if len(moved) > 5 else ''}")
    if required:
        missing = [normalize_header(h) for h in required if normalize_header(h) not in kept]
        if missing:
            reasons.append(f"필수 열 없음: {', '.join(missing)}")
    return reasons, dropped


def iter_raw(ws, header_row, headers, drop_keywords=None):
    """(행 번호, {머리글: 값}) 을 차례로 돌려준다. 민감 열과 빈 행은 건너뛴다. 민감 칸의 값은 꺼내지 않는다."""
    keep = [(i, h) for i, h in enumerate(headers) if h and not is_sensitive(h, drop_keywords)]
    for r_idx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
        raw = {h: (row[i] if i < len(row) else None) for i, h in keep}
        if all(_blank(v) for v in raw.values()):
            continue
        yield r_idx, raw


def to_standard(raw, columns, transforms, required=()):
    """원본 행 하나를 표준 이름으로 옮긴다. 돌려주는 것: (표준 행, 막는 문제 목록, 경고 목록).
    메시지에는 칸 이름만 넣고 값은 넣지 않는다."""
    std, blocks, warns = {}, [], []
    norm_cols = {normalize_header(src): dst for src, dst in columns.items()}
    for src, dst in norm_cols.items():
        try:
            std[dst] = apply(raw.get(src), transforms.get(dst, []))
        except (ValueError, TypeError):
            std[dst] = None
            (blocks if dst in required else warns).append(f"'{src}' 칸을 읽을 수 없습니다{'' if dst in required else '. 비워 둡니다'}")
    blank = [src for src, dst in norm_cols.items() if dst in required and std.get(dst) in (None, "")]
    already = {b.split("'")[1] for b in blocks}
    blank = [s for s in blank if s not in already]
    if blank:
        blocks.append(f"필수 값 없음: {', '.join(blank)}")
    return std, blocks, warns
