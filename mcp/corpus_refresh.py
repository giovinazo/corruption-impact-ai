#!/usr/bin/env python3
"""코퍼스 증분 갱신 엔진 — 검토 시점의 '현행' 규범을 직원 PC가 스스로 보장한다.

빌드타임(관리자, 전체 수집)과 런타임(직원 PC, 증분 갱신)이 공유하는 단일 진실:
  - tools/build_guidelines.py → 빈 코퍼스에서 refresh_guidelines() 전량 호출
  - server.refresh_corpus()    → 알리오와 대조해 '바뀐 항목만' 수집·재분류

알리오가 유일 출처인 경영지침(E군)을 다룬다. (내부규정 B군은 후속 단계에서 추가)
서버·빌더가 함께 import하므로 표준 라이브러리 + alio_client + hwp_extract만 의존.
"""
import os
import re
from collections import defaultdict

import alio_client
from hwp_extract import (extract_hwp_text, extract_hwpx_text,
                         extract_pdf_text, extract_docx_text, extract_hwpml_text)

ALIO = "https://www.alio.go.kr"


def extract_smart(path: str) -> str:
    """확장자가 아닌 매직바이트로 실제 포맷 판별 (HWP/HWPX/PDF/HWPML 혼입 대응)."""
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic[:4] == b"\xd0\xcf\x11\xe0":
        return extract_hwp_text(path)
    if magic[:5] == b"<?xml":
        return extract_hwpml_text(path)
    if magic[:2] == b"PK":
        try:
            return extract_hwpx_text(path)
        except Exception:
            return extract_docx_text(path)
    if magic[:4] == b"%PDF":
        return extract_pdf_text(path)
    raise ValueError(f"미지원 포맷 magic={magic[:8]!r}")


def norm_name(title: str) -> str:
    """표시용 지침명 — 괄호·꼬리 접미사 제거, 가운뎃점은 공백 통일. 앞 연도는 유지."""
    t = re.sub(r"\([^)]*\)", "", title)
    t = re.sub(r"(개정안|_?\s*부칙수정|수정|개정|제정|시행)\s*$", "", t)
    for ch in "·∙ㆍ.":
        t = t.replace(ch, " ")
    return re.sub(r"\s+", " ", t).strip()


def _group_key(norm: str) -> str:
    return re.sub(r"[\s·∙ㆍ.]+", "", norm)


def classify(records: list) -> list:
    """현행/연혁 status 부여 — 같은 group_key에서 date 최신 1건만 '현행'."""
    groups = defaultdict(list)
    for r in records:
        groups[_group_key(r["norm"])].append(r)
    for rs in groups.values():
        rs.sort(key=lambda x: x.get("date", ""), reverse=True)
        for j, r in enumerate(rs):
            r["status"] = "현행" if j == 0 else "연혁"
    records.sort(key=lambda x: (x["norm"], x.get("status") != "현행", x.get("date", "")))
    return records


# ── 경영지침 (E군, 알리오 etcLawList 게시판 B1120) ──────────────────

def guideline_postings(session) -> list:
    """알리오 게시판 전체 목록 → [{boardNo, title, date, ministry, rn}] (본문 미수집)."""
    items, page = [], 1
    while True:
        r = session.get(f"{ALIO}/etc/findEtcLawList.json",
                        params={"pageNo": page}, timeout=20)
        d = r.json().get("data") or {}
        res = d.get("result") or []
        for x in res:
            items.append({
                "boardNo": str(x.get("boardNo") or x.get("seq")),
                "title": (x.get("rtitle") or "").strip(),
                "date": x.get("idate") or x.get("bdate") or "",
                "ministry": (x.get("pname") or "").strip(),
                "rn": x.get("rn"),
            })
        if len(items) >= d.get("totalCnt", 0) or not res:
            break
        page += 1
    return items


def _fetch_one(session, posting: dict, cache_dir: str):
    """게시물 1건 본문 조달 (상세→파일→다운로드→추출). 실패 시 None."""
    bn = posting["boardNo"]
    try:
        r = session.get(f"{ALIO}/etc/findEtcLawDtl.json",
                        params={"boardNo": bn}, timeout=20)
        files = (r.json().get("data") or {}).get("fileList") or []
    except Exception:
        return None
    if not files:
        return None
    f0 = files[0]
    ext = os.path.splitext(f0.get("fileNm", ""))[1].lower() or ".hwp"
    raw = os.path.join(cache_dir, f"{bn}{ext}")
    if not os.path.exists(raw):
        try:
            rr = session.get(f"{ALIO}/download/download.json",
                             params={"fileNo": f0.get("fileNo")},
                             timeout=60, stream=True)
            if rr.status_code != 200:
                return None
            with open(raw, "wb") as fh:
                for c in rr.iter_content(8192):
                    if c:
                        fh.write(c)
        except Exception:
            return None
    try:
        text = re.sub(r"\n{3,}", "\n\n", extract_smart(raw)).strip()
    except Exception:
        return None
    return {"boardNo": bn, "title": posting["title"], "norm": norm_name(posting["title"]),
            "ministry": posting["ministry"], "date": posting["date"], "rn": posting.get("rn"),
            "file": f0.get("fileNm", ""), "chars": len(text), "text": text}


def diff_guidelines(corpus: list, session) -> dict:
    """코퍼스 vs 알리오 대조 → 신규/삭제 게시물·최신 공시일 (본문 미수집, 빠름)."""
    have = {str(r.get("boardNo")) for r in corpus}
    postings = guideline_postings(session)
    live = {p["boardNo"] for p in postings}
    new = [p for p in postings if p["boardNo"] not in have]
    return {
        "postings": postings,
        "new": new,
        "removed": sorted(have - live),
        "alio_total": len(postings),
        "alio_latest": max((p["date"] for p in postings), default=""),
        "corpus_count": len(corpus),
        "corpus_latest": max((str(r.get("date", "")) for r in corpus), default=""),
    }


def refresh_guidelines(corpus: list, session, cache_dir: str, max_new: int = 30):
    """증분 갱신 — 신규 게시물만 수집해 병합·재분류. (records, summary) 반환.
    corpus=[] 이면 전량 수집(빌드타임)."""
    os.makedirs(cache_dir, exist_ok=True)
    d = diff_guidelines(corpus, session)
    fetched, failed = [], []
    for p in d["new"][:max_new]:
        rec = _fetch_one(session, p, cache_dir)
        if rec:
            fetched.append(rec)
        else:
            failed.append(p["title"][:30])
    merged = [dict(r) for r in corpus] + fetched
    merged = classify(merged)
    summary = {
        "신규_수집": len(fetched),
        "수집_실패": len(failed),
        "삭제_감지": d["removed"],
        "총건수": len(merged),
        "현행": sum(1 for r in merged if r.get("status") == "현행"),
        "알리오_최신공시": d["alio_latest"],
        "상한_초과_미수집": max(0, len(d["new"]) - max_new),
    }
    return merged, summary
