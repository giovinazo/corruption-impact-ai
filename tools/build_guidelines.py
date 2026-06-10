#!/usr/bin/env python3
"""E군 경영지침 코퍼스 — 전체 수집 (관리자용 빌드타임).

런타임 증분 갱신(server.refresh_corpus)과 **동일 엔진**(mcp/corpus_refresh.py)을
빈 코퍼스에서 호출해 전량 수집한다. 빌드/런타임 로직 단일화로 결과 일관 보장.

산출:
  mcp/data/guidelines_corpus.json       78건 본문 (현행/연혁 분류)
  mcp/data/guidelines_corpus.meta.json  기준일·건수·최신공시일 (도구 표기·신선도 비교용)
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(os.path.dirname(HERE), "mcp")
sys.path.insert(0, MCP)

import alio_client          # noqa: E402
import corpus_refresh       # noqa: E402

OUT = os.path.join(MCP, "data", "guidelines_corpus.json")
META = os.path.join(MCP, "data", "guidelines_corpus.meta.json")
RAW = os.path.join(HERE, "cache", "guidelines_hwp")


def main():
    s = alio_client.create_session()
    print("알리오 게시판 수집 중…")
    records, summary = corpus_refresh.refresh_guidelines([], s, RAW, max_new=200)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    meta = {
        "built_at": datetime.now().strftime("%Y-%m-%d"),
        "count": len(records),
        "현행": summary["현행"],
        "alio_latest": summary["알리오_최신공시"],
        "source": "알리오 공공기관 법령/지침 게시판 (etcLawList B1120)",
    }
    with open(META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    print(f"완료: {len(records)}건 (현행 {summary['현행']} / 연혁 {len(records)-summary['현행']})"
          f" · 수집실패 {summary['수집_실패']}")
    print(f"코퍼스: {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB)")
    print(f"메타  : {meta}")


if __name__ == "__main__":
    main()
