#!/usr/bin/env python3
"""부패영향평가 AI MCP — 도구 13종 self-check (서버 함수 직접 호출)

A·B군은 오프라인, C군은 알리오 실호출, D군은 법제처 프록시 실호출.
LAW_PROXY_TOKEN 미설정 시 open-law .env에서 자동 로드 시도.
"""
import os
import sys
import traceback

# 토큰 폴백 (로컬 개발 편의): ~/.claude.json의 open-law MCP env에서 차용
if not os.environ.get("LAW_PROXY_TOKEN"):
    try:
        import json as _json
        with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as _f:
            _cfg = _json.load(_f)
        _tok = (_cfg.get("mcpServers", {}).get("open-law", {})
                .get("env", {}).get("LAW_PROXY_TOKEN"))
        if _tok:
            os.environ["LAW_PROXY_TOKEN"] = _tok
    except (OSError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

PASS, FAIL = [], []


def check(name, fn, *args, validate=None, **kwargs):
    try:
        r = fn(*args, **kwargs)
        assert isinstance(r, dict), f"dict 아님: {type(r)}"
        assert "error" not in r, f"error 반환: {r.get('error')}"
        if validate:
            validate(r)
        PASS.append(name)
        print(f"  PASS {name}")
        return r
    except Exception as e:
        FAIL.append((name, str(e)))
        print(f"  FAIL {name}: {e}")
        traceback.print_exc(limit=1)
        return {}


print("── A군 도메인지식 ──")
check("get_assessment_guide", server.get_assessment_guide,
      validate=lambda r: (r["meta"]["최종개정"] == "2024-03-18",
                          len(r["평가절차"]) == 6,
                          len(r["평가결과_유형"]) == 3))
check("get_target_gate", server.get_target_gate,
      validate=lambda r: len(r["확인기준"]["items"]) == 4)
check("get_criteria(전체)", server.get_criteria,
      validate=lambda r: len(r["평가기준"]) == 11)
check("get_criteria(부패통제)", server.get_criteria, "부패통제",
      validate=lambda r: len(r["평가기준"]) == 2)
check("get_checklist(재량)", server.get_checklist, criterion="재량",
      validate=lambda r: len(r["체크리스트"][0]["items"]) == 11)
check("get_checklist(공개성·공백변형)", server.get_checklist, criterion="공개성",
      validate=lambda r: r["체크리스트"][0]["no"] == 8)
check("get_checklist(계약)", server.get_checklist, worktype="계약",
      validate=lambda r: len(r["체크리스트"][0]["items"]) == 11)
check("get_checklist(목록안내)", server.get_checklist)
check("get_form_template(세부평가서)", server.get_form_template, "세부평가서",
      validate=lambda r: "평가대상 조문" in r["template"])

print("── B군 자체규정 ──")
check("list_internal_rules", server.list_internal_rules,
      validate=lambda r: r["건수"] == 137)
check("list_internal_rules(보수)", server.list_internal_rules, "보수",
      validate=lambda r: 0 < r["건수"] < 20)
check("get_internal_rule(부패영향평가)", server.get_internal_rule, "부패영향평가 지침",
      validate=lambda r: "제1조(목적)" in r["본문"])
check("get_internal_rule(페이징)", server.get_internal_rule, "계약규정",
      offset=0, max_chars=500)
check("search_internal_rules(수의계약)", server.search_internal_rules, "수의계약",
      validate=lambda r: r["일치_규정수"] >= 1)

r = server.get_internal_rule("규정")  # 복수 매칭 → 후보 안내 동작 확인
print(f"  INFO 복수매칭 안내: {'안내' in r} (후보 {len(r.get('후보', []))}건)")

print("── C군 타기관 (알리오 실호출) ──")
peer = check("search_peer_rules(여비, 단일기관)", server.search_peer_rules,
             "여비규정", "대한무역투자진흥공사",
             validate=lambda r: r["결과"] and r["결과"][0]["동종규정"])
if peer:
    hit = peer["결과"][0]["동종규정"][0]
    print(f"  INFO 발견: {hit['규정명']} (seq {hit['seq']})")
    check("fetch_peer_rule", server.fetch_peer_rule,
          "대한무역투자진흥공사", hit["seq"], max_chars=800,
          validate=lambda r: r["전체_글자수"] > 500)
    check("fetch_peer_rule(캐시 재호출)", server.fetch_peer_rule,
          "대한무역투자진흥공사", hit["seq"], max_chars=300,
          validate=lambda r: r["캐시"] is True)

print("── 신규: 문서 추출·조문 단위·행정규칙 ──")
_draft = os.path.expanduser(
    "~/Documents/04_법률_규정/임직원_행동강령_검토/"
    "2. 한국산업단지공단 임직원 행동강령 개정(안) 전문.hwpx")
if os.path.exists(_draft):
    check("extract_document(개정안 HWPX)", server.extract_document, _draft,
          validate=lambda r: r["전체_글자수"] > 30000)
else:
    print("  SKIP extract_document — 검토용 로컬 문서 없음")

check("get_internal_rule(article=제43조의2)", server.get_internal_rule,
      "임직원 행동강령", "제43조의2",
      validate=lambda r: "자진신고" in r["본문"] and r["구간_글자수"] < 600)
check("get_internal_rule(article=별표3의2)", server.get_internal_rule,
      "임직원 행동강령", "별표3의2",
      validate=lambda r: "외부강의" in r["본문"])
check("search_law(admrul)", server.search_law,
      "공직자 행동강령 운영지침", 3, False, "admrul",
      validate=lambda r: r.get("AdmRulSearch"))
check("get_law_text(admrul summary)", server.get_law_text,
      "2100000247036", "", "summary", "admrul",
      validate=lambda r: r["행정규칙기본정보"]["행정규칙종류"] == "예규")

print("── D군 법령 (법제처 프록시 실호출) ──")
law = check("search_law(부패방지권익위법, 약칭)", server.search_law,
            "부패방지권익위법", 5, True,
            validate=lambda r: r.get("LawSearch"))
if law:
    laws = law["LawSearch"].get("law", [])
    if isinstance(laws, dict):
        laws = [laws]
    # 시행령(대통령령) 찾기 — 지침 근거인 제30조 조회용
    mst = None
    for x in laws:
        if "시행령" in (x.get("법령명한글") or ""):
            mst = x.get("법령일련번호")
            break
    print(f"  INFO 시행령 MST: {mst}")
    if mst:
        check("get_law_text(시행령 제30조)", server.get_law_text, str(mst), "제30조",
              validate=lambda r: "법령" in r)
    check("get_law_text(summary)", server.get_law_text,
          str(laws[0].get("법령일련번호")), "", "summary",
          validate=lambda r: r.get("조문수", 0) > 0)

print("\n" + "=" * 50)
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
for n, e in FAIL:
    print(f"  ✗ {n}: {e}")
sys.exit(1 if FAIL else 0)
