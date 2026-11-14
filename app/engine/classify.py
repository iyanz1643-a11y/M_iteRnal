"""계약의 구분과 업권을 읽는 한 곳.

실제 계약현황은 상품 마스터와 잇지 않고, 계약에 구분(contract.product_category)과 업권(contract.sector)을 직접 싣는다.
샘플 계약은 상품 마스터(product)와 원수사 마스터(insurer)로 읽는다.
그래서 계약에 붙은 값을 먼저 읽고, 비어 있으면 마스터를 읽는다. 엔진은 모두 이 조각을 쓴다.
뷰 v_contract_extract 도 같은 식이다. 식을 바꾸면 schema/schema.sql 끝의 그 뷰도 함께 고친다.

조각은 별칭을 받는다. 기본 별칭은 계약 c, 상품 pr, 원수사 i 다.
"""


def category(c="c", p="pr") -> str:
    """구분 코드 식. 계약의 구분이 먼저, 없으면 상품 마스터의 구분이다."""
    return f"COALESCE({c}.product_category, {p}.product_category)"


def sector(c="c", i="i") -> str:
    """업권 식. 계약의 업권이 먼저, 없으면 원수사 마스터의 업권이다."""
    return f"COALESCE({c}.sector, {i}.sector)"


def joins(c="c", p="pr", i="i") -> str:
    """위 두 식이 읽는 마스터를 잇는다. 원수사는 계약에 늘 있고 상품은 없을 수 있다."""
    return f"JOIN insurer {i} ON {i}.insurer_id={c}.insurer_id LEFT JOIN product {p} ON {p}.product_id={c}.product_id"


CATEGORY = category()
SECTOR = sector()
JOINS = joins()


def of_contract(con, contract_id) -> tuple:
    """(구분, 업권). 시험과 점검용."""
    r = con.execute(f"SELECT {CATEGORY} cat, {SECTOR} sec FROM contract c {JOINS} WHERE c.contract_id=?", (contract_id,)).fetchone()
    return (r["cat"], r["sec"]) if r else (None, None)
