#!/usr/bin/env python3
"""부패영향평가 AI MCP 서버 (corruption-impact-ai, 서버명 cia)

산단공 「부패영향평가 지침」(2024.3.18)의 기준·체크리스트를 도메인 지식으로 내장하고,
알리오(타 기관 형평성·자체규정 충돌)와 법제처(상위법령 정합성)를 결합한
사규 사전검토 **대화형 보조도구**. 판단은 담당자가, 추론은 Claude가, 자료는 MCP가.

도구 14종 — A군 도메인지식(5) / 문서(1) / B군 자체규정(3) / C군 타기관(3) / D군 법령(2)
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from mcp.server.fastmcp import FastMCP

import alio_client
import law_client
from hwp_extract import extract_any

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
PEER_CACHE = os.path.join(DATA, "peer_cache")

# 기본 비교군: 산업통상자원부 산하 등 유사 위탁집행형 준정부기관
# (환경변수 CIA_DEFAULT_PEERS="기관1,기관2"로 오버라이드)
DEFAULT_PEERS = [
    "대한무역투자진흥공사",
    "중소벤처기업진흥공단",
    "한국산업기술진흥원",
    "한국에너지공단",
    "한국가스안전공사",
]

mcp = FastMCP("cia")

_kb = None
_corpus = None
_guidelines = None
_session = None


def kb() -> dict:
    global _kb
    if _kb is None:
        with open(os.path.join(DATA, "assessment_kb.json"), encoding="utf-8") as f:
            _kb = json.load(f)
    return _kb


def corpus() -> list:
    global _corpus
    if _corpus is None:
        with open(os.path.join(DATA, "rules_corpus.json"), encoding="utf-8") as f:
            _corpus = json.load(f)
    return _corpus


def guidelines() -> list:
    global _guidelines
    if _guidelines is None:
        path = os.path.join(DATA, "guidelines_corpus.json")
        try:
            with open(path, encoding="utf-8") as f:
                _guidelines = json.load(f)
        except (OSError, ValueError):
            _guidelines = []
    return _guidelines


def session():
    global _session
    if _session is None:
        _session = alio_client.create_session()
    return _session


def _norm(s: str) -> str:
    """매칭용 정규화 — 공백·중점 제거 (예: '공 개 성'↔'공개성', '위탁･대행'↔'위탁대행')"""
    return re.sub(r"[\s·･‧»·]+", "", s or "")


def _list_sections(text: str) -> dict:
    """규정 전문에 실재하는 별표 번호·조문 정의 수를 반환 (부재 확정 진단용)."""
    bps = sorted(set(re.findall(r"별\s*표\s*([0-9]+(?:의[0-9]+|[-‑][0-9]+)?)", text)),
                 key=lambda x: (int(re.match(r"\d+", x).group()), x))
    bps = [b.replace("-", "의").replace("‑", "의") for b in bps]
    # 중복 제거(3-2와 3의2가 같은 별표) — 순서 보존
    seen, norm_bps = set(), []
    for b in bps:
        if b not in seen:
            seen.add(b); norm_bps.append(b)
    n_art = len(re.findall(r"제\s*\d+\s*조(?:의\d+)?\s*\(", text))
    return {"본문에_존재하는_별표": norm_bps, "조문_정의_수": n_art}


def _missing_section_diag(out: dict, text: str, article: str) -> dict:
    """조문·별표 부재 시 존재 목록과 비고를 붙여 반환 (get_internal_rule·extract_document 공용)."""
    return {**out,
            "결과": f"'{article}' 구간 없음 — 이 규정 본문에 해당 조문·별표가 존재하지 않습니다",
            **_list_sections(text),
            "비고": "개정안이 이 구간을 인용한다면 '존재하지 않는 구간 인용'에 해당 — 동시 신설 필요 여부 검토"}


def _slice_section(text: str, article: str):
    """규정 전문에서 조문('제43조의2') 또는 별표('별표3의2'/'별표 3-2') 구간만 추출.

    조문은 정의부 패턴 '제N조(…)'(괄호 제목)로 식별하므로 본문 중 인용("제36조와
    관련하여")은 걸리지 않는다. 단 '제N조 삭제'(괄호 없는 삭제 조문)도 존재로 인정한다.
    별표는 본문 블록이 가장 큰 매칭(각주·참조가 아닌 실제 표 영역)을 사용한다.
    """
    key = re.sub(r"[\s\[\]〔〕]", "", article).replace("-", "의").replace("‑", "의")
    if key.startswith("별표"):
        pat = re.compile(r"[\[〔]?\s*별\s*표\s*([0-9]+(?:\s*(?:의|[-‑])\s*[0-9]+)?)")
        heads = [(m.start(),
                  "별표" + re.sub(r"[\s]", "", m.group(1)).replace("-", "의").replace("‑", "의"))
                 for m in pat.finditer(text)]
        idx = [i for i, (_, n) in enumerate(heads) if n == key]
        if not idx:
            return None
        # 각주·참조('별표3 참조')가 아닌 실제 표 본문 = 다음 헤드까지 길이가 가장 긴 매칭
        def span_len(i):
            s = heads[i][0]
            e = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            return e - s
        k = max(idx, key=span_len)
    else:
        # '제N조(...' 정의부 또는 '제N조 삭제'(괄호 없는 삭제 조문)
        pat = re.compile(r"제\s*([0-9]+)\s*조(?:\s*의\s*([0-9]+))?\s*(?:\(|삭\s*제)")
        heads = [(m.start(),
                  f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else ""))
                 for m in pat.finditer(text)]
        idx = [i for i, (_, n) in enumerate(heads) if n == key]
        if not idx:
            return None
        k = idx[0]
    start = heads[k][0]
    end = heads[k + 1][0] if k + 1 < len(heads) else len(text)
    return text[start:end].strip()


def _safe_seq(seq) -> bool:
    """알리오 seq는 숫자 문자열 — 경로 주입(../ 등) 차단."""
    return bool(re.fullmatch(r"[0-9]+", str(seq)))


def _page(text: str, offset: int, max_chars: int) -> dict:
    chunk = text[offset:offset + max_chars]
    return {
        "본문": chunk,
        "전체_글자수": len(text),
        "offset": offset,
        "다음_offset": offset + len(chunk) if offset + len(chunk) < len(text) else None,
    }


# ════════════════════════════════════════════════════════════
# A군 — 부패영향평가 도메인 지식 (지침 내장)
# ════════════════════════════════════════════════════════════

@mcp.tool()
def get_assessment_guide() -> dict:
    """부패영향평가 제도 개요·절차·결과유형과 이 MCP의 권장 사용 순서를 반환한다.

    검토 세션을 시작할 때 가장 먼저 호출하라. 근거 규정(「부패영향평가 지침」
    2024.3.18 개정), 평가부서(감사실), 평가기한(15일), 절차 6단계,
    평가결과 3유형(원안동의/개선권고/철회의견)을 제공한다.
    """
    k = kb()
    return {
        "meta": k["meta"],
        "평가절차": k["procedure"],
        "평가결과_유형": k["result_types"],
        "권장_검토_워크플로우": [
            "1) get_target_gate → 평가대상 여부 게이트 확인 (해당 없으면 평가 생략 가능)",
            "2) get_criteria → 11개 평가기준 개관, 개정안 성격에 맞는 중점 기준 선정",
            "3) get_checklist(worktype=...) → 업무유형별 체크리스트로 조문별 점검",
            "4) [상위법령] search_law + get_law_text → 개정안이 상위법령 범주 내인지 대조",
            "4-2) [상위지침] list_mgmt_guidelines + get_mgmt_guideline → 정원·보수·예산·재무 사항은 재정경제부 경영지침(공운법 제50조, 법제처 미등재)을 상위법령과 함께 대조",
            "5) [자체규정 충돌] search_internal_rules → 동일 사안 이중규정·저촉 탐지",
            "6) [타기관 형평성] search_peer_rules + fetch_peer_rule → 동종 규정 수준 비교",
            "7) get_form_template('세부평가서') → 검토 결과를 서식에 정리",
        ],
    }


@mcp.tool()
def get_target_gate() -> dict:
    """평가대상 여부 확인 기준(별표2)을 반환한다.

    규정 제·개정안이 부패영향평가 대상인지 4개 항목으로 우선 판정한다 (제5조의2①).
    판정규칙: ①·③·④ 또는 ②·③·④ 모두 '예'면 평가 생략, 그 외 평가대상.
    """
    k = kb()
    return {
        "확인기준": k["target_gate"],
        "법정_제외사유": "법령·정관·정부방침·상위규정 변경으로 개정을 요하는 사항, 기관 설치·조직운영·업무분장·문서관리 등 국민생활·기업활동과 무관해 부패발생요인이 없다고 판단되는 규정 (제3조②)",
        "유의": "제외 여부는 소관(주관)부서가 판단하되, 평가부서 요청 시 평가 필수 (제3조③)",
    }


@mcp.tool()
def get_criteria(field: str = "") -> dict:
    """평가기준(별표1) 4분야 11개를 반환한다.

    Args:
        field: 분야 필터 — '준수'(3개)·'집행'(3개)·'행정절차'(3개)·'부패통제'(2개).
               빈 값이면 전체 11개.
    """
    items = kb()["criteria"]
    if field:
        nf = _norm(field)
        items = [c for c in items if nf in _norm(c["field"])]
        if not items:
            return {"error": f"분야 '{field}' 없음. 사용 가능: 준수/집행/행정절차/부패통제"}
    return {"평가기준": items,
            "비고": "각 기준의 상세 검토항목은 get_checklist(criterion=기준명)으로 조회"}


@mcp.tool()
def get_checklist(criterion: str = "", worktype: str = "") -> dict:
    """평가기준별(별표3) 또는 업무유형별(별표4) 체크리스트를 반환한다.

    Args:
        criterion: 평가기준명 부분일치 (예: '재량', '이해충돌', '준수부담').
                   11종: 준수부담의 합리성/제재규정의 적정성/특혜발생 가능성/
                   재량규정의 구체성·객관성/위탁·대행의 투명성·책임성/재정누수 가능성/
                   접근의 용이성/공개성/예측 가능성/이해충돌 가능성/부패방지장치의 체계성
        worktype: 업무유형 부분일치 (예: '계약', '인사', '위원회').
                  8종: 회계/계약/자산관리/인증·지정/조사/인사/감사/심의·의결 위원회

    둘 다 비우면 사용 가능한 체크리스트 목록을 안내한다.
    """
    k = kb()
    if criterion:
        nc = _norm(criterion)
        hits = [c for c in k["criteria_checklists"] if nc in _norm(c["name"])]
        if not hits:
            return {"error": f"기준 '{criterion}' 없음",
                    "사용가능": [c["name"] for c in k["criteria_checklists"]]}
        return {"체크리스트": hits, "출처": "별표3 평가기준별 체크리스트"}
    if worktype:
        nw = _norm(worktype)
        hits = [w for w in k["worktype_checklists"] if nw in _norm(w["name"])]
        if not hits:
            return {"error": f"유형 '{worktype}' 없음",
                    "사용가능": [w["name"] for w in k["worktype_checklists"]]}
        return {"체크리스트": hits, "출처": "별표4 업무유형별 체크리스트"}
    return {
        "안내": "criterion 또는 worktype 중 하나를 지정하세요",
        "평가기준별(별표3)": [f"({c['no']}) {c['name']} — {len(c['items'])}항목"
                          for c in k["criteria_checklists"]],
        "업무유형별(별표4)": [f"({w['no']}) {w['name']} — {len(w['items'])}항목"
                          for w in k["worktype_checklists"]],
    }


@mcp.tool()
def get_form_template(form: str = "") -> dict:
    """평가 서식 템플릿(별지 1~4호)을 반환한다.

    Args:
        form: '기초자료'(별지1, 소관부서 의뢰용) / '결과통보서'(별지2) /
              '세부평가서'(별지3, 조문 단위 평가) / '관리카드'(별지4).
              빈 값이면 서식 목록.
    """
    forms = kb()["forms"]
    if not form:
        return {"서식목록": {k: v["용도"] for k, v in forms.items()}}
    nf = _norm(form)
    for key, v in forms.items():
        if nf in _norm(key) or nf in _norm(v["name"]):
            return v
    return {"error": f"서식 '{form}' 없음", "사용가능": list(forms.keys())}


# ════════════════════════════════════════════════════════════
# B군 — 자체규정 정합성 (산단공 137건 코퍼스)
# ════════════════════════════════════════════════════════════

@mcp.tool()
def list_internal_rules(divis: str = "") -> dict:
    """한국산업단지공단 내부규정 코퍼스(137건) 목록을 반환한다.

    Args:
        divis: 분류 필터 부분일치 — '정관'/'인사·복무·징계'/'보수'/'직제'/'기타'.
               빈 값이면 전체.
    """
    items = corpus()
    nd = _norm(divis)
    out = [{"seq": r["seq"], "규정명": r["title"], "분류": r["divis"],
            "글자수": r["chars"]}
           for r in items if not nd or nd in _norm(r["divis"])]
    return {"건수": len(out), "규정": out,
            "출처": "알리오 공시 내부규정 최신본 (build_corpus.py로 갱신)"}


@mcp.tool()
def get_internal_rule(title: str, article: str = "", offset: int = 0,
                      max_chars: int = 20000) -> dict:
    """산단공 내부규정 전문 또는 특정 조문·별표를 조회한다 (제목 부분일치).

    Args:
        title: 규정명 (부분일치 — 예: '계약규정', '여비'). 복수 매칭 시 후보 목록 반환.
        article: 조문·별표 단위 추출 — '제43조의2', '별표3의2', '별표 3-2' 형식.
                 지정 시 해당 구간만 반환 (긴 규정에서 컨텍스트 절약).
        offset: 본문 시작 위치 (긴 규정 페이징용, article 미지정 시).
        max_chars: 1회 반환 최대 글자수 (기본 20,000).
    """
    nt = _norm(title)
    hits = [r for r in corpus() if nt in _norm(r["title"])]
    exact = [r for r in hits if _norm(r["title"]) == nt]
    if exact:
        hits = exact
    if not hits:
        return {"error": f"'{title}' 규정 없음 — list_internal_rules로 목록 확인"}
    if len(hits) > 1:
        return {"안내": f"'{title}' 매칭 {len(hits)}건 — 정확한 규정명으로 재호출",
                "후보": [r["title"] for r in hits]}
    r = hits[0]
    out = {"규정명": r["title"], "분류": r["divis"], "원본파일": r["file_name"]}
    if article.strip():
        sec = _slice_section(r["text"], article)
        if sec is None:
            return _missing_section_diag(out, r["text"], article)
        out.update({"구간": article, "본문": sec[:max_chars],
                    "구간_글자수": len(sec)})
        return out
    out.update(_page(r["text"], offset, max_chars))
    return out


@mcp.tool()
def search_internal_rules(keyword: str, context: int = 100, max_rules: int = 20) -> dict:
    """산단공 내부규정 137건 전문(全文)에서 키워드를 검색한다.

    용도: 동일 사안 이중 부담·타 규정과의 저촉·중복 탐지 (제규정관리규정 검토항목),
    개정안 키워드가 등장하는 모든 규정 식별.

    Args:
        keyword: 검색어 (예: '수의계약', '위원회 구성', '재량').
        context: 일치 위치 전후로 포함할 문맥 글자수 (기본 100).
        max_rules: 반환할 최대 규정 수 (기본 20).
    """
    if not keyword.strip():
        return {"error": "keyword 필수"}
    results = []
    for r in corpus():
        text = r["text"]
        positions = [m.start() for m in re.finditer(re.escape(keyword), text)]
        if not positions:
            continue
        snippets = []
        for p in positions[:3]:  # 규정당 최대 3개 스니펫
            s = max(0, p - context)
            snippets.append("…" + text[s:p + len(keyword) + context].replace("\n", " ") + "…")
        results.append({"규정명": r["title"], "분류": r["divis"],
                        "일치수": len(positions), "스니펫": snippets})
    results.sort(key=lambda x: -x["일치수"])
    return {"검색어": keyword, "일치_규정수": len(results),
            "결과": results[:max_rules],
            "비고": "특정 규정 전문은 get_internal_rule(title)로 조회"}


@mcp.tool()
def extract_document(path: str, article: str = "", offset: int = 0,
                     max_chars: int = 20000) -> dict:
    """로컬 문서(HWP/HWPX/PDF/DOCX)에서 텍스트를 추출한다.

    용도: 검토 대상 제·개정안 파일을 대화에 직접 읽혀 검토를 시작한다 —
    공시 전 내부 초안은 알리오에 없으므로 이 도구로 조달한다.

    Args:
        path: 문서 경로 (~ 확장 지원). 심볼릭 링크는 거부한다.
        article: 조문·별표 단위 추출 — '제48조', '별표3의3' 형식 (선택).
        offset / max_chars: 본문 페이징 (기본 20,000자).

    보안: 심볼릭 링크와 디렉터리는 거부. 단일 사용자 로컬 도구이나, 프롬프트
    인젝션으로 임의 경로가 주입되는 것을 차단하기 위해 실경로를 검사한다.
    """
    p = os.path.realpath(os.path.expanduser(path))
    if os.path.islink(os.path.expanduser(path)):
        return {"error": "심볼릭 링크는 허용하지 않습니다"}
    if not os.path.isfile(p):
        return {"error": f"파일 없음: {p}"}
    ext = os.path.splitext(p)[1].lower()
    if ext not in (".hwp", ".hwpx", ".pdf", ".docx"):
        return {"error": f"미지원 형식 {ext} — hwp/hwpx/pdf/docx 지원"}
    try:
        text = extract_any(p)
    except Exception as e:
        return {"error": f"추출 실패: {e}"}
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out = {"파일": os.path.basename(p), "형식": ext}
    if article.strip():
        sec = _slice_section(text, article)
        if sec is None:
            return _missing_section_diag(out, text, article)
        out.update({"구간": article, "본문": sec[:max_chars], "구간_글자수": len(sec)})
        return out
    out.update(_page(text, offset, max_chars))
    return out


# ════════════════════════════════════════════════════════════
# C군 — 타 기관 형평성 (알리오 결합)
# ════════════════════════════════════════════════════════════

_MAX_TITLE_PAGES = 50  # 전국 규정명 검색 페이지 상한 (10건/페이지 — 행동강령 403건도 수집 가능)


def _search_rules_by_title(keyword: str) -> tuple:
    """알리오 전국 규정명 검색(type=title) — 페이지 병렬 순회.

    기관별 전체 목록 순회(수십 페이지×기관 수) 대비 수십 배 빠르다.
    반환: (total_cnt, [{seq, title, divis, pname, ruleStDa}, ...])
    """
    s = session()
    url = f"{alio_client.BASE_URL}/occasional/findRuleList.json"

    def page(p):
        resp = alio_client.retry_request(
            s, "GET", url, params={"type": "title", "word": keyword,
                                   "pageNo": p, "divis": ""}, timeout=30)
        d = resp.json().get("data") or {}
        return d.get("totalCnt", 0), d.get("result") or []

    total, first = page(1)
    items = list(first)
    n_pages = min((total + 9) // 10, _MAX_TITLE_PAGES)
    if n_pages > 1:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for fut in as_completed([ex.submit(page, p) for p in range(2, n_pages + 1)]):
                items.extend(fut.result()[1])
    return total, items


@mcp.tool()
def search_peer_rules(rule_keyword: str, inst_names: str = "") -> dict:
    """타 공공기관의 동종 내부규정을 알리오에서 검색한다 (형평성 비교용).

    지침 체크리스트의 "유사 법령·유사 사례와 비교" 요구에 대응 — 다른 기관이
    같은 사안을 어떤 수준으로 규정하는지 비교 증거를 수집한다.
    알리오 전국 규정명 검색을 사용하므로 구체적 키워드일수록 빠르고 정확하다.

    Args:
        rule_keyword: 규정명 키워드 (예: '계약규정', '여비규정', '수의계약').
        inst_names: 쉼표구분 기관명 목록 (부분일치 필터).
                    빈 값 → 기본 비교군(산업부 산하 등 유사 위탁집행형 5개).
                    '*' → 전국 모드 (기관 필터 없이 상위 결과 반환).

    Returns:
        기관별 동종 규정 목록 — fetch_peer_rule(inst_name, seq)로 본문 조달.
    """
    total, items = _search_rules_by_title(rule_keyword.strip())
    rows = [{"seq": r.get("seq"), "규정명": (r.get("title") or "").strip(),
             "분류": r.get("insdRuleDivis", ""), "기관": (r.get("pname") or "").strip(),
             "공시일": r.get("ruleStDa", "")} for r in items]

    if inst_names.strip() == "*":
        return {"키워드": rule_keyword, "전국_일치건수": total,
                "결과": rows[:100],
                "비고": f"전국 모드 — 상한 {_MAX_TITLE_PAGES * 10}건 수집" if total > len(rows) else "전국 모드"}

    peers = ([p.strip() for p in inst_names.split(",") if p.strip()]
             if inst_names.strip()
             else [p.strip() for p in os.environ.get(
                 "CIA_DEFAULT_PEERS", ",".join(DEFAULT_PEERS)).split(",")])
    out = []
    for peer in peers:
        np_ = _norm(peer)
        hits = [r for r in rows if np_ in _norm(r["기관"])]
        out.append({"기관": peer, "동종규정": [
            {k: v for k, v in h.items() if k != "기관"} for h in hits]})
    out.sort(key=lambda x: -len(x["동종규정"]))
    return {"키워드": rule_keyword, "비교기관": peers,
            "전국_일치건수": total, "결과": out,
            "비고": "0건 기관은 규정 명칭이 다를 수 있음 — 키워드를 바꾸거나 inst_names='*'로 전국 명칭 분포 확인"}


def _get_peer_text(inst_name: str, seq: str):
    """타 기관 규정 본문 조달 (캐시 우선 → 다운로드+추출). (text, meta, cached) 반환."""
    if not _safe_seq(seq):
        raise ValueError(f"잘못된 seq: {seq!r} — 숫자만 허용 (경로 주입 차단)")
    os.makedirs(PEER_CACHE, exist_ok=True)
    txt_cache = os.path.join(PEER_CACHE, f"{seq}.txt")
    meta_cache = os.path.join(PEER_CACHE, f"{seq}.meta.json")

    if os.path.exists(txt_cache) and os.path.exists(meta_cache):
        with open(meta_cache, encoding="utf-8") as f:
            meta = json.load(f)
        with open(txt_cache, encoding="utf-8") as f:
            return f.read(), meta, True

    s = session()
    detail = alio_client.fetch_rule_detail(s, seq)
    latest = detail.get("latest")
    if not latest:
        raise RuntimeError(f"seq {seq} 파일정보 없음: {detail.get('error', 'latest 없음')}")

    file_name = latest["file_name"]
    ext = os.path.splitext(file_name)[1].lower() or ".hwp"
    raw_path = os.path.join(PEER_CACHE, f"{seq}{ext}")
    ok, _, msg = alio_client.download_rule_file_to_path(s, latest["file_no"], raw_path)
    if not ok:
        raise RuntimeError(f"다운로드 실패: {msg}")
    text = extract_any(raw_path)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    meta = {"기관": inst_name, "seq": seq, "원본파일": file_name}
    with open(txt_cache, "w", encoding="utf-8") as f:
        f.write(text)
    with open(meta_cache, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return text, meta, False


@mcp.tool()
def fetch_peer_rule(inst_name: str, seq: str, offset: int = 0,
                    max_chars: int = 20000) -> dict:
    """타 기관 규정 최신본을 다운로드하고 텍스트로 추출해 반환한다 (원스톱).

    알리오 단독으로는 HWP 파일 저장까지만 가능 — 이 도구는 다운로드+HWP/HWPX
    텍스트 추출을 묶어 본문을 바로 읽을 수 있게 한다. 결과는 디스크에 캐시.

    Args:
        inst_name: 기관명 (캐시 키·출처 표기용).
        seq: search_peer_rules 결과의 규정 seq.
        offset / max_chars: 본문 페이징 (기본 20,000자).
    """
    try:
        text, meta, cached = _get_peer_text(inst_name, seq)
    except Exception as e:
        return {"error": str(e)}
    out = {**meta, "캐시": cached}
    out.update(_page(text, offset, max_chars))
    return out


@mcp.tool()
def survey_peer_rules(rule_keyword: str, contain: str, match: str = "tokens",
                      max_fetch: int = 450, sample: int = 3, context: int = 70) -> dict:
    """전국 동종 규정 전수에서 특정 문구의 보유 현황을 집계한다 (형평성 통계).

    예: survey_peer_rules('행동강령', '음주운전 자진신고') → "보유 N/M개 기관(x%)" +
    대표 발췌. 수작업으로 반나절 걸리던 전수 비교를 한 번의 호출로 수행한다.

    주의: 첫 실행은 기관별 본문 다운로드로 수 분이 걸릴 수 있다(이후 캐시로 즉답).
    시연·대량 조사 전에는 한 번 미리 실행해 캐시를 적재해 둘 것.

    Args:
        rule_keyword: 규정명 키워드 (예: '행동강령'). 기관당 첫 매칭 1건만 조사.
        contain: 본문에서 찾을 문구. 공백으로 나뉜 여러 토큰을 주면 **모두 포함**
                 (AND·순서무관)하는 기관을 센다 — 예 '음주운전 자진신고'는
                 '음주운전'과 '자진신고'가 (표제든 문장형이든) 함께 나오는 기관을 집계.
                 정확한 연속 문구만 세려면 match='phrase' 지정.
        match: 'tokens'(기본, 공백분리 AND) | 'phrase'(연속 문자열 그대로).
        max_fetch: 조사할 최대 기관 수 (기본 450).
        sample: 대표 발췌 수 (기본 3).
        context: 발췌 전후 문맥 글자수.
    """
    total, items = _search_rules_by_title(rule_keyword.strip())
    by_org = {}
    for it in items:
        org = (it.get("pname") or "").strip()
        if org and org not in by_org:
            by_org[org] = it
    rows = list(by_org.items())[:max_fetch]

    tokens = contain.split() if match == "tokens" else [contain]

    def hit_pos(text):
        """매칭 성립 시 첫 토큰 위치 반환, 아니면 -1. tokens 모드는 모든 토큰 포함 필요."""
        if match == "tokens":
            if all(t in text for t in tokens):
                return min(text.find(t) for t in tokens)
            return -1
        return text.find(contain)

    results, fails = {}, []  # results[org] = (it, pos, text) — seq 순 결정적 집계

    def one(org, it):
        text, _, _ = _get_peer_text(org, it["seq"])
        return org, it, hit_pos(text), text

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(one, org, it): org for org, it in rows}
        for f in as_completed(futs):
            try:
                org, it, pos, text = f.result()
                results[org] = (it, pos, text)
            except Exception as e:
                fails.append({"기관": futs[f], "오류": str(e)[:80]})

    # 동시성 무관 결정적 결과: org 정렬 순회로 보유·발췌 수집
    have, samples = [], []
    for org in sorted(results):
        it, pos, text = results[org]
        if pos < 0:
            continue
        have.append(org)
        if len(samples) < sample:
            s0 = max(0, pos - context)
            end = pos + (len(contain) if match == "phrase" else len(tokens[0])) + context
            samples.append({"기관": org, "규정명": it.get("title", "").strip(),
                            "발췌": "…" + text[s0:end].replace("\n", " ") + "…"})

    surveyed = len(rows) - len(fails)
    if surveyed <= 0:
        status = "본문_추출_전부실패" if rows else "비교가능_규정_없음"
        return {"규정_키워드": rule_keyword, "검색_문구": contain, "전국_매칭": total,
                "조사_기관수": surveyed, "보유율_퍼센트": None, "상태": status,
                "조사_실패": len(fails),
                "비고": "조사 대상이 0 — 보유율을 0%로 해석하지 말 것"}
    rate = round(len(have) / surveyed * 100, 1)
    return {
        "규정_키워드": rule_keyword, "검색_문구": contain, "매칭방식": match,
        "전국_매칭": total, "조사_기관수": surveyed,
        "보유_기관수": len(have), "보유율_퍼센트": rate,
        "대표_발췌": samples,
        "조사_실패": len(fails),
        "보유_기관_목록_일부": sorted(have)[:30],
        "비고": ("기관당 동명 규정 1건 기준 · 본문 추출 실패 기관은 분모에서 제외 · "
               + ("토큰 AND 매칭(표제·문장형 모두 포착)" if match == "tokens"
                  else "연속 문구 정확매칭")),
    }


# ════════════════════════════════════════════════════════════
# D군 — 상위법령 정합성 (법제처 결합)
# ════════════════════════════════════════════════════════════

@mcp.tool()
def search_law(query: str, display: int = 10, include_abbreviation: bool = False,
               target: str = "law") -> dict:
    """법제처 국가법령정보에서 현행 법령 또는 행정규칙을 검색한다.

    용도: 개정안의 상위법령 식별 — "적용 대상·범위가 상위법령에서 정한 범주 내인가",
    "상위규정 근거 없이 재량권을 부여하지 않는가" 검토의 출발점.

    Args:
        query: 법령·규칙명 (예: '공무원 행동강령', '공직자 행동강령 운영지침').
        display: 최대 결과 수 (기본 10).
        include_abbreviation: True면 약칭 매핑 (공운법·청탁금지법·부패방지권익위법·
                              이해충돌방지법·국가계약법·산집법 등 14종).
        target: 'law'(법령, 기본) | 'admrul'(행정규칙 — 권익위 운영지침·예규·고시 등.
                사규의 실무 기준은 예규에 있는 경우가 많음).

    Returns:
        law: {"LawSearch": {"law": [{법령일련번호(MST), 법령명한글, ...}]}}
        admrul: {"AdmRulSearch": {"admrul": [{행정규칙일련번호(ID), 행정규칙명, ...}]}}
    """
    q = law_client.LAW_ABBR.get(query, query) if include_abbreviation else query
    return law_client.call("lawSearch.do", {"target": target, "query": q,
                                            "display": display})


@mcp.tool()
def get_law_text(mst: str, jo: str = "", mode: str = "summary",
                 target: str = "law") -> dict:
    """법령 또는 행정규칙의 본문·특정 조문을 조회한다.

    권장 패턴: mode='summary'로 조문 목록 확인 → 필요한 조문만 jo로 재호출
    (큰 법령 전체는 수백 KB라 컨텍스트 낭비).

    Args:
        mst: search_law 결과의 법령일련번호(MST). target='admrul'이면 행정규칙일련번호(ID).
        jo: 조문 지정 — '제30조' 또는 6자리 코드 '003000'. 지정 시 해당 조문만.
            (admrul도 '제15조' 형식 지원 — 조문내용에서 해당 조 구간 추출)
        mode: jo 미지정 시 응답 크기 — 'summary'(기본: 기본정보+조문목록) /
              'articles_only'(조문 전체) / 'full'(부칙·개정문 포함).
        target: 'law'(법령, 기본) | 'admrul'(행정규칙 — 예규·고시·훈령).
    """
    if target == "admrul":
        raw = law_client.call("lawService.do", {"target": "admrul", "ID": mst})
        body = raw.get("AdmRulService", {})
        info = body.get("행정규칙기본정보", {})
        arts = body.get("조문내용", [])
        if isinstance(arts, str):
            arts = [arts]
        text = "\n".join(str(a) for a in arts)
        out = {"행정규칙기본정보": {k: info.get(k) for k in
                                ("행정규칙명", "행정규칙종류", "발령일자", "발령번호", "소관부처명")}}
        if jo:
            sec = _slice_section(text, jo)
            out["조문"] = sec if sec else f"'{jo}' 구간을 찾지 못함"
        elif mode == "summary":
            out["조문수_추정"] = len(re.findall(r"제\s*\d+\s*조(?:의\d+)?\s*\(", text))
            out["본문_글자수"] = len(text)
            out["안내"] = "본문은 jo='제N조' 지정 또는 mode='full'로 조회"
        else:
            out["조문내용"] = text
            buchik = body.get("부칙")
            if buchik and mode == "full":
                out["부칙"] = buchik
        return out

    params = {"target": "law", "MST": mst}
    if jo:
        m = re.match(r"^제?(\d+)조(?:의(\d+))?$", jo.strip())
        if m:
            jo = f"{int(m.group(1)):04d}{int(m.group(2) or 0):02d}"
        params["JO"] = jo
    raw = law_client.call("lawService.do", params)

    if jo or mode == "full":
        body = raw.get("법령")
        if isinstance(body, dict) and body.get("부칙"):
            body["부칙_flat"] = law_client.normalize_buchik(body["부칙"])
        return raw

    body = raw.get("법령", {})
    units = body.get("조문", {}).get("조문단위", [])
    if isinstance(units, dict):
        units = [units]
    if mode == "summary":
        return {"기본정보": body.get("기본정보"), "조문수": len(units),
                "조문목록": [{"조문번호": u.get("조문번호"),
                          "조문제목": u.get("조문제목"),
                          "조문여부": u.get("조문여부")} for u in units]}
    return {"기본정보": body.get("기본정보"), "조문": body.get("조문")}


# ════════════════════════════════════════════════════════════
# E군 — 상위 경영지침 정합성 (공운법 제50조 / 재정경제부)
# ════════════════════════════════════════════════════════════

@mcp.tool()
def list_mgmt_guidelines(status: str = "현행", keyword: str = "") -> dict:
    """재정경제부 경영지침(공운법 제50조) 목록을 반환한다.

    공운법 제50조에 따라 재정경제부장관이 정하고 공기업·준정부기관에 통보하는
    경영지침(① 조직·정원·인사 ② 예산·자금 ③ 재무건전성)은 법제처 국가법령정보에
    등재되지 않고 알리오에만 공시된다. 사규 개정안이 정원·보수·복리후생·예산을
    건드릴 때 상위법령(D군)과 함께 대조해야 하는 1차 기준이다.

    Args:
        status: '현행'(기본)·'연혁'·'전체'. 같은 지침명은 최신 공시본만 현행.
        keyword: 지침명 부분일치 필터 (예: '예산', '혁신', '경영', '안전').
    """
    gs = guidelines()
    if not gs:
        return {"error": "경영지침 코퍼스 미적재 — tools/build_guidelines.py로 수집 필요"}
    nk = _norm(keyword)
    out = []
    for g in gs:
        if status != "전체" and g.get("status") != status:
            continue
        if nk and nk not in _norm(g["norm"]):
            continue
        out.append({"지침명": g["norm"], "제목": g["title"], "소관": g["ministry"],
                    "공시일": g["date"], "상태": g["status"], "글자수": g["chars"]})
    return {"기준": "공운법 제50조 경영지침 (재정경제부 통보, 알리오 공시)",
            "상태필터": status, "건수": len(out), "지침": out,
            "비고": "본문·조문은 get_mgmt_guideline(title)로 조회"}


@mcp.tool()
def get_mgmt_guideline(title: str, article: str = "", offset: int = 0,
                       max_chars: int = 20000) -> dict:
    """경영지침 전문 또는 특정 조문을 조회한다 (지침명 부분일치, 현행 우선).

    Args:
        title: 지침명 (부분일치 — 예: '경영에 관한 지침', '예산운용', '혁신').
               서로 다른 지침 복수 매칭 시 후보 목록을 반환한다.
        article: 조문 단위 추출 — '제15조'·'제15조의2' 형식 (선택).
        offset / max_chars: 본문 페이징 (기본 20,000자, article 미지정 시).
    """
    gs = guidelines()
    if not gs:
        return {"error": "경영지침 코퍼스 미적재 — tools/build_guidelines.py로 수집 필요"}
    nt = _norm(title)
    hits = [g for g in gs if nt in _norm(g["norm"]) or nt in _norm(g["title"])]
    if not hits:
        return {"error": f"'{title}' 경영지침 없음 — list_mgmt_guidelines로 목록 확인"}
    norm_keys = {_norm(g["norm"]) for g in hits}          # 공백·가운뎃점 표기차 흡수
    if len(norm_keys) > 1:
        cur = sorted({g["norm"] for g in hits if g.get("status") == "현행"})
        return {"안내": f"'{title}' 매칭 {len(norm_keys)}종 — 정확한 지침명으로 재호출",
                "후보": cur or sorted({g["norm"] for g in hits})}
    # 동일 지침의 현행·연혁 중 현행(최신) 우선
    hits.sort(key=lambda g: (g.get("status") != "현행", g.get("date", "")), reverse=False)
    cur = [g for g in hits if g.get("status") == "현행"]
    g = cur[0] if cur else hits[-1]
    out = {"지침명": g["norm"], "제목": g["title"], "소관": g["ministry"],
           "공시일": g["date"], "상태": g["status"],
           "출처": "공운법 제50조 / 알리오 공시 (법제처 미등재)"}
    if article.strip():
        sec = _slice_section(g["text"], article)
        if sec is None:
            return _missing_section_diag(out, g["text"], article)
        out.update({"구간": article, "본문": sec[:max_chars], "구간_글자수": len(sec)})
        return out
    out.update(_page(g["text"], offset, max_chars))
    return out


if __name__ == "__main__":
    mcp.run()
