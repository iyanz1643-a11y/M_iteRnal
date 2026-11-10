"""보험사·데이터 유형별 컬럼 매핑(YAML) 로드. 대상 월에 유효한 버전을 고른다."""
import pathlib

import yaml

MAPPING_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "mapping"


def load(source: str, data_type: str, period_ym: str) -> dict:
    path = MAPPING_DIR / f"{source}_{data_type}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"매핑 파일 없음: {path.name}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    candidates = [
        v for v in doc["versions"]
        if str(v["effective_from"]) <= period_ym and (not v.get("effective_to") or str(v["effective_to"]) >= period_ym)
    ]
    if not candidates:
        raise LookupError(f"{path.name}: {period_ym} 에 유효한 매핑 버전이 없음")
    best = max(candidates, key=lambda v: str(v["effective_from"]))
    best = dict(best)
    best["mapping_version"] = f"{path.stem}@v{best['version']}"
    return best
