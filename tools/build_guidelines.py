#!/usr/bin/env python3
"""E군 경영지침 코퍼스 수집기 (빌드타임, Mac에서 실행)

알리오 '공공기관 법령/지침' 게시판(etcLawList, 코드 B1120) 78건을
HWP 다운로드 → 텍스트 추출 → 지침명 그룹핑(현행/연혁) → guidelines_corpus.json.

수집 경로 (2026-06-10 실측 확정 — HWP OLE 매직바이트까지 검증):
  목록  GET /etc/findEtcLawList.json?pageNo=N  → data.result[], data.totalCnt(=78)
  상세  GET /etc/findEtcLawDtl.json?boardNo=B  → data.fileList[{fileNm, fileNo}]
  파일  GET /download/download.json?fileNo=F   → HWP(OLE) 본문

런타임 비의존: 산출 JSON만 배포(직원 PC는 오프라인으로 읽음).
공개자료(재경부 고시 지침)라 클라우드 전송 무관.
"""
import json
import os
import re
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(os.path.dirname(HERE), "mcp")
sys.path.insert(0, MCP)

import alio_client                       # noqa: E402  (verify_ssl=False 기본 — 기존 검증 모듈)
from hwp_extract import (                # noqa: E402
    extract_hwp_text, extract_hwpx_text, extract_pdf_text, extract_docx_text,
    extract_hwpml_text)


def _extract_smart(path: str) -> str:
    """확장자가 아닌 매직바이트로 실제 포맷을 판별해 추출
    (게시판에 .hwp로 올라온 PDF·HWPX·HWPML 혼입 대응 — 회계기준 등)."""
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic[:4] == b"\xd0\xcf\x11\xe0":      # OLE = 구형 HWP
        return extract_hwp_text(path)
    if magic[:5] == b"<?xml":                  # HWPML (한글 XML)
        return extract_hwpml_text(path)
    if magic[:2] == b"PK":                     # zip = HWPX/DOCX
        try:
            return extract_hwpx_text(path)
        except Exception:
            return extract_docx_text(path)
    if magic[:4] == b"%PDF":
        return extract_pdf_text(path)
    raise ValueError(f"미지원 포맷 magic={magic[:8]!r}")

BASE = "https://www.alio.go.kr"
OUT = os.path.join(MCP, "data", "guidelines_corpus.json")
RAW_DIR = os.path.join(HERE, "cache", "guidelines_hwp")


def fetch_list(s) -> list:
    """findEtcLawList.json 전 페이지 순회 → 게시물 메타 목록."""
    items, page = [], 1
    while True:
        r = s.get(f"{BASE}/etc/findEtcLawList.json", params={"pageNo": page}, timeout=20)
        d = r.json().get("data") or {}
        result = d.get("result") or []
        items.extend(result)
        if len(items) >= d.get("totalCnt", 0) or not result:
            break
        page += 1
    return items


def fetch_files(s, board_no) -> list:
    """findEtcLawDtl.json?boardNo → fileList[{fileNm, fileNo}]."""
    r = s.get(f"{BASE}/etc/findEtcLawDtl.json", params={"boardNo": board_no}, timeout=20)
    return (r.json().get("data") or {}).get("fileList") or []


def download(s, file_no, dest) -> bool:
    r = s.get(f"{BASE}/download/download.json", params={"fileNo": file_no},
              timeout=60, stream=True)
    if r.status_code != 200:
        return False
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)
    return True


def norm_name(title: str) -> str:
    """표시용 지침명 — 괄호(개정일·시행)·꼬리 접미사 제거, 가운뎃점은 공백 통일.
    앞 연도(2026년도 등)는 본질적 구분이라 유지한다."""
    t = re.sub(r"\([^)]*\)", "", title)                             # 모든 괄호
    t = re.sub(r"(개정안|_?\s*부칙수정|수정|개정|제정|시행)\s*$", "", t)    # 꼬리 접미사
    for ch in "·∙ㆍ.":
        t = t.replace(ch, " ")
    return re.sub(r"\s+", " ", t).strip()


def group_key(norm: str) -> str:
    """그룹핑 키 — 공백·문장부호 모두 제거(가운뎃점·공백 표기차 흡수)."""
    return re.sub(r"[\s·∙ㆍ.]+", "", norm)


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    s = alio_client.create_session()
    items = fetch_list(s)
    print(f"목록 수신: {len(items)}건")

    records = []
    for i, it in enumerate(items, 1):
        board_no = it.get("boardNo") or it.get("seq")
        title = (it.get("rtitle") or "").strip()
        try:
            files = fetch_files(s, board_no)
        except Exception as e:
            print(f"  [{i}] 상세실패 {title[:34]} :: {e}")
            continue
        if not files:
            print(f"  [{i}] 첨부없음 {title[:34]}")
            continue
        f0 = files[0]
        ext = os.path.splitext(f0.get("fileNm", ""))[1].lower() or ".hwp"
        raw = os.path.join(RAW_DIR, f"{board_no}{ext}")
        if not os.path.exists(raw) and not download(s, f0.get("fileNo"), raw):
            print(f"  [{i}] 다운실패 {title[:34]}")
            continue
        try:
            text = re.sub(r"\n{3,}", "\n\n", _extract_smart(raw)).strip()
        except Exception as e:
            print(f"  [{i}] 추출실패 {title[:34]} :: {e}")
            text = ""
        records.append({
            "boardNo": str(board_no), "title": title, "norm": norm_name(title),
            "ministry": (it.get("pname") or "").strip(),
            "date": it.get("idate") or it.get("bdate") or "",
            "rn": it.get("rn"), "file": f0.get("fileNm", ""),
            "chars": len(text), "text": text,
        })
        print(f"  [{i}/{len(items)}] {title[:40]} ({len(text):,}자)")
        time.sleep(0.2)

    # 현행/연혁 분류 — 같은 norm 그룹에서 date 최신 1건만 '현행'
    groups = defaultdict(list)
    for r in records:
        groups[group_key(r["norm"])].append(r)
    for rs in groups.values():
        rs.sort(key=lambda x: x["date"], reverse=True)
        for j, r in enumerate(rs):
            r["status"] = "현행" if j == 0 else "연혁"
    records.sort(key=lambda x: (x["norm"], x["status"] != "현행", x["date"]))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)

    cur = [r for r in records if r["status"] == "현행"]
    print(f"\n완료: {len(records)}건 (현행 {len(cur)} / 연혁 {len(records)-len(cur)})")
    print(f"코퍼스: {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB)")
    print("=== 현행 지침 목록 ===")
    for r in cur:
        print(f"  · {r['norm']}  ({r['date']}, {r['chars']:,}자)")


if __name__ == "__main__":
    main()
