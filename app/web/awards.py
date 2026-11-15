"""조직 부문 시상의 시상 내용을 화면에 싣는 규칙 한 곳. 조직 부문 시상(/promo/org-awards)과 명예의전당(/perf/hall)이 함께 쓴다.

D-45 제안대로 둔다. 가정이며 2026-11-15 에 정했다.

| 무엇 | 규칙 |
| --- | --- |
| 순위별 시상 내용 | 설계사 모두에게 공개하는 상품표다. 역할과 상관없이 부문마다 순위별 시상표(prizes)를 싣는다 |
| 수상권 줄의 시상 | 그 순위의 시상 내용을 모든 역할에게 적는다. 상품표와 순위가 모두 공개라 줄에 적어도 더 알려지는 것이 없다 |
| 합계 | 시상 내용을 더하거나 사람 수를 곱한 합계는 만들지 않는다. 관리자에게 팀원이 받을 금액의 합계가 보이지 않게 한다. D-17 |
| 그 밖의 금액 | 수수료, 구간시상, 프로모션 예상 금액은 이 규칙과 상관없다. D-17 대로 관리자에게 팀원 것을 싣지 않는다 |

명예의전당은 확정한 결과(org_award_result)의 시상 글을, 조직 부문 시상은 지금 부문 설정(org_award_reward)의 시상 글을 쓴다.
확정한 뒤 부문 설정을 고쳐도 명예의전당에는 확정할 때의 글이 남는다.
"""

PUBLIC_NOTE = "순위별 시상 내용은 모두에게 공개하는 상품표입니다. 받을 금액의 합계는 적지 않습니다."


def prizes(rewards: dict, top_n: int | None = None) -> list:
    """순위별 시상표. rewards 는 {순위: 시상 글} 이다. 선발 인원을 넘는 순위는 뺀다."""
    out = [{"rank": int(k), "reward": v} for k, v in (rewards or {}).items() if v and (top_n is None or int(k) <= top_n)]
    return sorted(out, key=lambda x: x["rank"])


def confirmed_prizes(rows: list) -> list:
    """확정한 수상자 줄에서 순위별 시상표를 만든다. 보이는 줄로 거르기 전의 모든 줄을 넘긴다."""
    seen = {}
    for r in rows:
        if r.get("reward") and r["rank"] not in seen:
            seen[r["rank"]] = r["reward"]
    return prizes(seen)


def label_rows(rows: list) -> list:
    """보이는 줄에 시상 글을 남긴다. 수상권 밖 줄은 지운다. 역할로 가리지 않는다(D-45 가정)."""
    for r in rows:
        if "winner" in r and not r["winner"]:
            r["reward"] = None
    return rows
