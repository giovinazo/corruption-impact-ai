# 부패영향평가 AI (corruption-impact-ai)

한국산업단지공단 사규 제·개정안의 **부패영향평가 사전검토**를 보조하는 Claude 플러그인.
원클릭 자동평가기가 아니라 — 담당자가 Claude와 **대화하며** 검토하고, 판단은 사람이 내린다.

| 주체 | 역할 |
|---|---|
| 담당자(사람) | 검토 방향 결정, 조문별 쟁점 질의, 최종 판단(원안동의/개선권고/철회의견) |
| Claude | 체크리스트 항목별 추론, 조문 대조 분석, 세부평가서 초안 |
| MCP(이 플러그인) | 평가기준·체크리스트 공급, 자체규정 검색, 타 기관 규정 조달, 법령 조문 조달 |

## 왜 MCP 결합인가

「부패영향평가 지침」 체크리스트는 **비교**를 요구하지만 수작업으로는 사실상 불가능했다:

| 지침 요구 | 종전 | 이 플러그인 |
|---|---|---|
| "유사 법령의 준수부담·제재정도와 **비교**" | 기관별 HWP 수동 다운로드 | **알리오 결합** — 동종 규정 검색→본문 즉시 조달 |
| "**상위법령**에서 정한 범주 내인가" | 법제처 사이트 별도 검색 | **법제처 결합** — 조문 단위 즉시 조회 (중계 프록시로 IP 제약 없음) |
| "동일 사안 **이중 부담**·타 규정 **저촉**" | 규정집 137건 기억에 의존 | **전문 코퍼스** — 키워드 전문검색 |

## 구성

```
corruption-impact-ai/
├── .claude-plugin/plugin.json   # 플러그인 매니페스트
├── .mcp.json                    # MCP 서버 등록 (서버명 cia)
├── commands/사규검토.md          # /사규검토 — 대화형 검토 세션 시작
├── skills/assessment-writer/    # 세부평가서·결과통보서 작성 규칙
└── mcp/
    ├── server.py                # FastMCP 도구 14종
    ├── alio_client.py           # 알리오 (alio-crawler에서 이식)
    ├── law_client.py            # 법제처 (open-law에서 이식, NAS 프록시 경유)
    ├── hwp_extract.py           # HWP 스펙준수 파서 + HWPX/PDF/DOCX
    ├── self_check.py            # 도구 검증 (PASS 25)
    └── data/
        ├── assessment_kb.json   # 지침 구조화 — 기준 11·체크리스트 164항목·서식 4종
        ├── rules_corpus.json    # 산단공 내부규정 137건 전문 (알리오 공시, 200만 자)
        └── peer_cache/          # 타 기관 규정 캐시 (자동 생성)
```

## 도구 14종

| 군 | 도구 | 기능 |
|---|---|---|
| A 도메인지식 | `get_assessment_guide` | 제도 개요·절차 6단계·권장 워크플로우 |
| | `get_target_gate` | 평가대상 게이트(별표2) 4항목+판정규칙 |
| | `get_criteria` | 평가기준(별표1) 4분야 11개 |
| | `get_checklist` | 기준별(별표3, 11종)·업무유형별(별표4, 8종) 체크리스트 |
| | `get_form_template` | 별지 서식 4종 템플릿 |
| 문서 | `extract_document` | 로컬 제·개정안(HWP/HWPX/PDF/DOCX) 텍스트 추출 — 조문·별표 단위(`article`) 지원 |
| B 자체규정 | `list_internal_rules` | 코퍼스 137건 목록 |
| | `get_internal_rule` | 규정 전문 (페이징·조문/별표 단위 `article`) |
| | `search_internal_rules` | 전문(全文) 키워드 검색 — 충돌·이중부담 탐지 |
| C 타기관 | `search_peer_rules` | 타 기관 동종 규정 검색 (기본 비교군 5개, 병렬) |
| | `fetch_peer_rule` | 다운로드+텍스트 추출 원스톱 (HWP/HWPX/PDF/DOCX, 캐시) |
| | `survey_peer_rules` | **전국 전수 통계** — 동종 규정에서 문구 보유율 집계 (예: 행동강령 내 음주운전 자진신고 1.1%) |
| D 법령 | `search_law` | 법령·행정규칙(`target=admrul` — 권익위 예규 등) 검색 (약칭 14종) |
| | `get_law_text` | 법령·행정규칙 본문/조문 (summary 모드 기본) |

## 설치

```bash
# 로컬 개발 로드 (Claude Code)
claude --plugin-dir /path/to/corruption-impact-ai
```

요구사항: Python 3.10+ / `pip install mcp requests olefile PyMuPDF python-docx`

### 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `LAW_PROXY_TOKEN` | △ | 법제처 NAS 프록시 토큰. 미설정 시 `~/.claude.json`의 open-law 등록값 자동 차용 |
| `LAW_PROXY_URL` | — | 프록시 주소 (기본 내장) |
| `CIA_DEFAULT_PEERS` | — | 타 기관 기본 비교군 오버라이드 (쉼표구분) |
| `ALIO_VERIFY_SSL` | — | `1`이면 알리오 SSL 검증 활성화 (기본 비활성 — SSL 검사 장비 환경 대응) |

## 사용

```
/사규검토 계약규정 개정안입니다. 수의계약 한도를 상향하려 합니다. [개정안 붙여넣기]
```

## 데이터 갱신

규정 개정 시 코퍼스 재빌드: `28_워크플로우경진대회3/03_데이터/`의
`download_rules.py` → `build_corpus.py` 실행 후 `mcp/data/rules_corpus.json` 교체.

## 데이터 출처·주의

- 내부규정 코퍼스·타 기관 규정: **알리오 공개 공시 데이터** (비공개 사규 미포함)
- 「부패영향평가 지침」: 알리오 공시 최신본(2024.3.18 개정) 구조화
- 산출 문서는 **AI 보조 검토 초안** — 최종 판단·확정은 평가담당자 결재로 한다
