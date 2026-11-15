"""조직·설정 묶음의 마스터 /org/masters. 보험사, 상품, 구분을 보고 고친다. 메인 관리자만 쓴다.

- 고친 일은 audit_log 에 master_create, master_update 로 남긴다. before_json 과 after_json 에 바뀐 칸의 전과 뒤가 있다.
  사람 이름과 금액은 마스터에 없다. 인정률은 비율이다.
- 상품의 인정률과 구분의 시상 대상 기본값은 계산 기준에 닿는다. 바꿀 때는 확인 칸을 골라야 하고, 이미 낸 업적 run 은 다시 돌려야 반영된다.
- 보험사 이름은 계약현황의 보험사명과 맞춰 찾는다(app/loader/eternal_contract.py). 적재기가 임시로 넣은 보험사(origin=loader)는
  이름과 업권을 확인한 뒤 마스터로 확정한다.
- 상품종류 분류표(config/mapping/product_category_map.yaml, D-04 확정)는 보기만 한다. 고치는 일은 그 파일을 고친 뒤 적재 이력에서
  계약현황을 재적재하는 것이다. 원장에서 분류표에 없는 상품종류의 건수를 함께 보여 준다.
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import db
from app.loader import eternal_contract as ec
from app.web.deps import get_con, guard, page

router = APIRouter()
PATH = "/org/masters"
TABS = {"insurer": "보험사", "product": "상품", "category": "구분", "map": "상품종류 분류표", "history": "변경 이력"}
SECTOR_KO = {"life": "생명보험", "nonlife": "손해보험"}
ORIGIN_KO = {"master": "마스터", "loader": "적재기가 임시 등록"}
CODE = re.compile(r"^[A-Z0-9_]{1,16}$")
YM = re.compile(r"^20\d\d(0[1-9]|1[0-2])$")
FIELD_KO = {"code": "코드", "name": "이름", "sector": "업권", "active": "사용", "origin": "출처", "product_code": "상품 코드",
            "product_name": "상품명", "product_category": "구분", "conversion_rate": "인정률", "effective_from": "적용 시작",
            "effective_to": "적용 끝", "insurer_id": "보험사", "incentive_eligible": "시상 대상 기본값", "sort_order": "순서"}
MONEY_FIELDS = {"conversion_rate", "incentive_eligible"}       # 계산 기준에 닿는 칸. 바꿀 때 확인 칸이 있어야 한다


class Invalid(ValueError):
    pass


def _rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def _audit(con, actor, action, target, before, after):
    con.execute("INSERT INTO audit_log(at, actor, action, target, before_json, after_json) VALUES(?,?,?,?,?,?)",
                (db.now(), actor, action, target, json.dumps(before, ensure_ascii=False) if before is not None else None,
                 json.dumps(after, ensure_ascii=False)))


def _diff(old: dict, new: dict) -> tuple:
    keys = [k for k in new if str(old.get(k)) != str(new[k])]
    return {k: old.get(k) for k in keys}, {k: new[k] for k in keys}


# ---------------------------------------------------------------- 보기
def map_view(con) -> dict:
    """분류표와, 원장에서 분류표에 없는 상품종류의 건수. 상품종류는 분류 값이라 사람과 금액이 없다."""
    try:
        cmap = ec.load_category_map()
    except Exception as e:                                  # 분류표 파일이 깨져도 화면은 열린다
        return {"meta": {}, "rows": [], "err": f"분류표를 읽지 못했습니다: {type(e).__name__}", "missing": []}
    meta = cmap.pop("_meta")
    cats = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM product_category")}
    used = {r[0]: r[1] for r in con.execute("SELECT product_type_raw, COUNT(*) FROM contract WHERE product_type_raw IS NOT NULL GROUP BY 1")}
    rows = [{"type": k, "category": cats.get(v["category"], v["category"]), "sector": v["sector"] or "-", "confirmed": v["confirmed"],
             "confidence": v["confidence"], "n": used.get(k, 0)} for k, v in cmap.items()]
    missing = _rows(con, """SELECT product_type_raw t, COUNT(*) n FROM contract WHERE product_type_raw IS NOT NULL AND product_category IS NULL
                            GROUP BY 1 ORDER BY 2 DESC LIMIT 50""")
    return {"meta": meta, "rows": rows, "err": None, "missing": missing}


def history(con, limit=100) -> list:
    out = []
    for r in _rows(con, "SELECT at, actor, action, target, before_json, after_json FROM audit_log WHERE action IN ('master_create','master_update') "
                        "ORDER BY id DESC LIMIT ?", (limit,)):
        before = json.loads(r["before_json"]) if r["before_json"] else {}
        after = json.loads(r["after_json"]) if r["after_json"] else {}
        r["changes"] = [{"field": FIELD_KO.get(k, k), "before": before.get(k), "after": v} for k, v in after.items()]
        r["action_ko"] = "새로 만듦" if r["action"] == "master_create" else "고침"
        out.append(r)
    return out


def _page(request, ctx, con, tab="insurer", status_code=200, **kw):
    tab = tab if tab in TABS else "insurer"
    insurers = _rows(con, """SELECT i.*, (SELECT COUNT(*) FROM product p WHERE p.insurer_id=i.insurer_id) products,
                                    (SELECT COUNT(*) FROM contract c WHERE c.insurer_id=i.insurer_id) contracts
                             FROM insurer i ORDER BY i.origin='loader' DESC, i.active DESC, i.code""")
    cats = _rows(con, """SELECT c.*, (SELECT COUNT(*) FROM product p WHERE p.product_category=c.code) products
                         FROM product_category c ORDER BY c.sort_order, c.code""")
    data = {"insurers": insurers, "cats": cats, "loader_n": sum(1 for i in insurers if i["origin"] == "loader")}
    if tab == "product":
        ins = request.query_params.get("ins") or ""
        where, args = ("WHERE p.insurer_id=?", [int(ins)]) if ins.isdigit() else ("", [])
        data["products"] = _rows(con, f"""SELECT p.*, i.code insurer_code, i.name insurer_name, c.name category_name
                                         FROM product p JOIN insurer i ON i.insurer_id=p.insurer_id
                                         LEFT JOIN product_category c ON c.code=p.product_category {where}
                                         ORDER BY i.code, p.product_code, p.effective_from LIMIT 500""", args)
        data["ins"] = ins
    if tab == "map":
        data["map"] = map_view(con)
    if tab == "history":
        data["history"] = history(con)
    qp = request.query_params
    edit = kw.pop("edit", None)
    if edit is None and (qp.get("edit") or "").strip():
        edit = _edit_row(con, tab, qp["edit"].strip())
    return page(request, ctx, "org_masters.html", status_code=status_code, tab=tab, TABS=TABS, SECTOR_KO=SECTOR_KO, ORIGIN_KO=ORIGIN_KO,
                edit=edit, new=qp.get("new") == "1" or kw.pop("new", False), **data, **{"msg": None, "err": None, "form": {}, **kw})


def _edit_row(con, tab, key):
    if tab == "insurer" and key.isdigit():
        r = con.execute("SELECT * FROM insurer WHERE insurer_id=?", (int(key),)).fetchone()
    elif tab == "product" and key.isdigit():
        r = con.execute("SELECT * FROM product WHERE product_id=?", (int(key),)).fetchone()
    elif tab == "category":
        r = con.execute("SELECT * FROM product_category WHERE code=?", (key,)).fetchone()
    else:
        r = None
    return dict(r) if r else None


@router.get("/org/masters", response_class=HTMLResponse)
def masters_page(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    return _page(request, ctx, con, request.query_params.get("tab") or "insurer", msg=request.query_params.get("msg"))


# ---------------------------------------------------------------- 고치기
def _text(form, key, limit=80, required=True):
    v = str(form.get(key) or "").strip()
    if required and not v:
        raise Invalid(f"{FIELD_KO.get(key, key)}을 넣으세요.")
    return v[:limit]


def _insurer_values(form) -> dict:
    sector = str(form.get("sector") or "")
    if sector not in SECTOR_KO:
        raise Invalid("업권은 생명보험이나 손해보험입니다.")
    return {"name": _text(form, "name", 60), "sector": sector, "active": 1 if form.get("active") == "1" else 0}


def _product_values(con, form) -> dict:
    ins = str(form.get("insurer_id") or "")
    if not ins.isdigit() or not con.execute("SELECT 1 FROM insurer WHERE insurer_id=?", (int(ins),)).fetchone():
        raise Invalid("보험사를 고르세요.")
    cat = str(form.get("product_category") or "")
    if not con.execute("SELECT 1 FROM product_category WHERE code=?", (cat,)).fetchone():
        raise Invalid("구분을 고르세요.")
    try:
        rate = round(float(str(form.get("conversion_rate") or "").strip()), 6)
    except ValueError:
        raise Invalid("인정률은 숫자로 넣으세요. 예: 0.8") from None
    if not 0 < rate <= 5:
        raise Invalid("인정률은 0보다 크고 5 이하입니다.")
    frm, to = _text(form, "effective_from", 6), _text(form, "effective_to", 6, required=False)
    if not YM.match(frm) or (to and (not YM.match(to) or to < frm)):
        raise Invalid("적용 시작과 끝은 YYYYMM 이고 끝이 시작보다 앞설 수 없습니다.")
    return {"insurer_id": int(ins), "product_code": _text(form, "product_code", 40), "product_name": _text(form, "product_name", 120),
            "product_category": cat, "conversion_rate": rate, "effective_from": frm, "effective_to": to or None}


def _category_values(form) -> dict:
    try:
        order = int(str(form.get("sort_order") or "0"))
    except ValueError:
        raise Invalid("순서는 정수입니다.") from None
    return {"incentive_eligible": 1 if form.get("incentive_eligible") == "1" else 0, "sort_order": order}


def _need_ack(form, before, after):
    touched = [FIELD_KO[k] for k in after if k in MONEY_FIELDS and (before is None or str(before.get(k)) != str(after[k]))]
    if before is None:
        touched = []                                 # 새로 만드는 상품은 이미 낸 계산에 닿지 않는다
    if touched and form.get("ack") != "1":
        raise Invalid(f"{', '.join(touched)}은 계산 기준입니다. 확인 칸을 고른 뒤 저장하세요.")


@router.post("/org/masters", response_class=HTMLResponse)
async def masters_action(request: Request, ctx: dict = Depends(guard), con=Depends(get_con)):
    form = await request.form()
    action = str(form.get("action") or "")
    by = ctx["login_id"]
    tab = {"insurer_save": "insurer", "insurer_confirm": "insurer", "product_save": "product", "category_save": "category"}.get(action)
    if tab is None:
        raise HTTPException(400, "처리할 수 없는 요청입니다.")
    key = str(form.get("id") or "").strip()
    try:
        msg = _save(con, action, key, form, by)
    except Invalid as e:
        con.rollback()
        edit = {**(_edit_row(con, tab, key) or {}), **{k: v for k, v in form.items() if k not in ("csrf_token", "action")}} if key else None
        return _page(request, ctx, con, tab, err=str(e), edit=edit, new=not key, form=dict(form), status_code=400)
    con.commit()
    return _page(request, ctx, con, tab, msg=msg)


def _save(con, action, key, form, by) -> str:
    if action == "insurer_confirm":
        r = _edit_row(con, "insurer", key)
        if not r or r["origin"] != "loader":
            raise Invalid("확정할 임시 보험사가 없습니다.")
        con.execute("UPDATE insurer SET origin='master' WHERE insurer_id=?", (r["insurer_id"],))
        _audit(con, by, "master_update", f"insurer:{r['code']}", {"origin": r["origin"]}, {"origin": "master"})
        return f"보험사 {r['name']} 을 마스터로 확정했습니다."
    if action == "insurer_save":
        vals = _insurer_values(form)
        if key:
            old = _edit_row(con, "insurer", key)
            if not old:
                raise Invalid("없는 보험사입니다.")
            before, after = _diff(old, vals)
            if not after:
                return "바뀐 칸이 없습니다."
            con.execute(f"UPDATE insurer SET {', '.join(f'{k}=?' for k in after)} WHERE insurer_id=?", (*after.values(), old["insurer_id"]))
            _audit(con, by, "master_update", f"insurer:{old['code']}", before, after)
            return f"보험사 {vals['name']} 을 고쳤습니다."
        code = _text(form, "code", 16).upper()
        if not CODE.match(code):
            raise Invalid("코드는 영문 대문자, 숫자, 밑줄로 16자까지입니다.")
        if con.execute("SELECT 1 FROM insurer WHERE code=?", (code,)).fetchone():
            raise Invalid("같은 코드의 보험사가 있습니다.")
        new = {"code": code, **vals, "origin": "master"}
        con.execute("INSERT INTO insurer(code, name, sector, active, origin) VALUES(?,?,?,?,?)", tuple(new.values()))
        _audit(con, by, "master_create", f"insurer:{code}", None, new)
        return f"보험사 {vals['name']} 을 만들었습니다."
    if action == "product_save":
        vals = _product_values(con, form)
        old = _edit_row(con, "product", key) if key else None
        if key and not old:
            raise Invalid("없는 상품입니다.")
        _need_ack(form, old, vals)
        dup = con.execute("SELECT product_id FROM product WHERE insurer_id=? AND product_code=? AND effective_from=?",
                          (vals["insurer_id"], vals["product_code"], vals["effective_from"])).fetchone()
        if dup and (not old or dup[0] != old["product_id"]):
            raise Invalid("같은 보험사, 상품 코드, 적용 시작의 상품이 있습니다.")
        code = con.execute("SELECT code FROM insurer WHERE insurer_id=?", (vals["insurer_id"],)).fetchone()[0]
        if old:
            before, after = _diff(old, vals)
            if not after:
                return "바뀐 칸이 없습니다."
            con.execute(f"UPDATE product SET {', '.join(f'{k}=?' for k in after)} WHERE product_id=?", (*after.values(), old["product_id"]))
            _audit(con, by, "master_update", f"product:{code}/{vals['product_code']}", before, after)
            tail = " 인정률은 업적 run 을 다시 돌려야 반영됩니다." if "conversion_rate" in after else ""
            return f"상품 {vals['product_name']} 을 고쳤습니다.{tail}"
        con.execute(f"INSERT INTO product({', '.join(vals)}) VALUES({', '.join('?' * len(vals))})", tuple(vals.values()))
        _audit(con, by, "master_create", f"product:{code}/{vals['product_code']}", None, vals)
        return f"상품 {vals['product_name']} 을 만들었습니다."
    # category_save
    old = _edit_row(con, "category", key)
    if not old:
        raise Invalid("없는 구분입니다.")
    vals = _category_values(form)
    _need_ack(form, old, vals)
    before, after = _diff(old, vals)
    if not after:
        return "바뀐 칸이 없습니다."
    con.execute(f"UPDATE product_category SET {', '.join(f'{k}=?' for k in after)} WHERE code=?", (*after.values(), old["code"]))
    _audit(con, by, "master_update", f"category:{old['code']}", before, after)
    return f"구분 {old['name']} 을 고쳤습니다."
