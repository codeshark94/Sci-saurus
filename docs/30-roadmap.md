# Sci-saurus — 구현 로드맵 (Roadmap)

> `00-SSOT.md` 파생 문서. 페이즈 완료조건은 SSOT의 결정(D1~D8)과 아키텍처(`20-architecture-v0.md`)를 준수함을 의미한다.

---

## 1. 페이즈 개관

| 페이즈 | 이름 | 산출물 | 완료조건 | 상태 |
|---|---|---|---|---|
| **P0** | 설계 확정 | SSOT · survey · architecture · roadmap 4종 | 문서 승인(열린 질문 Q1~Q5 결정) | **본 세션 초안 완료** |
| **P1** | 저장 코어 | `scisaurus` 파이썬 패키지: 프로젝트 스토어 + 원장 + CLI | 아티팩트 불변성·원장 append-only·릴리스 태그 테스트 통과 | 대기 |
| **P2** | MVP 파이프라인 | 논문 악보 v0 실행(브리프→조사→스토리라인→아웃라인→드래프트) | 실제 브리프 1건으로 초고 + KB 스냅샷 + 컴파일 PDF(LaTeX) + 릴리스 산출 | 대기 |
| **P3** | 자유 교환 완성 | 메시지 버스·이의제기/중재·편집 루프 캡·전체 감사 리포트 | 부서 간 크로스 리뷰 시나리오 3종 통과 | 대기 |
| **P4** | 범용 확장 | 커스텀 악보 SDK · 리소스 자각 · 실험 실행 슬롯 · A2A/MCP 어댑터 | 논문 외 산출물 1종(예: 리포트) 악보 교체만으로 생성 | 대기 |

## 2. P1 — 저장 코어 상세 (다음 착수 대상)

### 2.1 디렉터리/모듈 계획
```
scisaurus/
  core/
    schema.py        # dataclass + JSON 검증: Project, Mission, Task, Artifact, Message, Event
    store.py         # 프로젝트 생성 / 아티팩트 쓰기(불변, version+1, parents) / 읽기 / HEAD / diff
    ledger.py        # events.jsonl append-only / replay / project별 필터
    release.py       # 마일스톤 스냅샷 + git 태그 + 릴리스 노트 생성
  org/
    manifest.py      # 부서·에이전트 매니페스트 로더/검증
    bus.py           # 메시지 큐(JSONL) 송수신 + 원장 기록
  agents/
    base.py          # Agent 추상(run 인터페이스), LocalAgentRunner, 모델 라우팅
  scores/
    paper.yaml       # 논문 악보 v0 (P2에서 채움)
  cli.py             # scisaurus new|status|show|diff|release
  tests/
    test_store.py    # 불변성·부모링크·동시분기
    test_ledger.py   # append-only·replay 재현
    test_bus.py      # 봉투 검증·기록
```

### 2.2 CLI (v0 스펙)
```bash
scisaurus new <project_id> --brief brief.md       # 프로젝트 + 저장소 생성, 브리프 불변 저장
scisaurus status <project_id>                     # 아티팩트 트리·게이트 상태·최근 원장
scisaurus show <project_id> <artifact_id>[@ver]   # 아티팩트 열람(+provenance)
scisaurus diff <project_id> <artifact_id> v1 v3   # 버전 비교
scisaurus release <project_id> v0.2-survey        # 스냅샷 + 태그 + 릴리스 노트
scisaurus run <project_id> --score paper          # (P2) 파이프라인 실행
```

### 2.3 P1 테스트 기준(완료조건)
1. 같은 id 3회 저장 → 버전 3개, 본문 3개 모두 보존, parents 체인 정확.
2. 원장 파일을 임의 편집 시도 → replay 검증으로 탐지(체크섬 체인).
3. 릴리스 태그 → 스냅샷 재현 가능(동일 체크섬).
4. 권한 매트릭스 위반 쓰기 → 거부(원장에 `gate.failed` 기록).

## 3. P2 — MVP 파이프라인 세부

1. `scores/paper.yaml` 선언(§2 아키텍처의 S0~S6, 게이트 G0~G5b).
2. 도구 어댑터: 웹검색 / arXiv / Semantic Scholar(선택) / 로컬 파일. 각 조회는 레퍼런스 카드 강제 생성.
3. 조사부 실행: 스카우트(병렬 검색) → 계보 분석 → 사서 정규화 → 검증관 QA → **KB 스냅샷**.
4. 전략부 실행: 스토리라인 → 레드팀 → (재조사 요청 루프) → 아웃라인 → 섹션 병렬 집필.
5. 편집실 루프: 리뷰 → 수정 → 최대 3회 → 편집장 판정.
6. 조립·렌더링: 섹션 → LaTeX 조립(포맷 편집자) → 컴파일(**G4b**) → PDF.
7. 릴리스: `v1.0-final`(LaTeX 소스 + PDF) + 조사 아카이브 + 결정 요약.

## 4. 리스크 & 대응

| 리스크 | 영향 | 대응 | 근거 |
|---|---|---|---|
| 인용 환각 | 논문 신뢰성 붕괴 | 검증관 에이전트 + G1/G2 게이트(식별자 강제) | Deep Research 선례 |
| 무한 리뷰 루프 | 비용·지연 | 루프 캡 3회 + 편집장 종료권 + 중재 | CycleResearcher 교훈 |
| 비용 폭주 | 운영 | 부서별 모델 라우팅 + 프로젝트 예산 게이트 | — |
| 부서 간 무질서 교류 | 품질·추적성 저하 | 봉투 스키마 강제 + 전면 원장 기록 + 권한 매트릭스 | 블랙보드 계열 교훈 |
| 실행 상태 유실(중단) | 재시작 비용 | 원장 replay + git 체크포인트(계층 분리) | LangGraph 선례 |
| 프레임워크 종속 | 확장 제약 | 부서 실행기 어댑터 인터페이스 고정 | SSOT D2/D6 |
| 데이터 재현 불가 | 검증 불능 | KB 스냅샷 아티팩트화 + 조회 로그 | §5-E 선례 |

## 5. 즉시 착수 가능 작업 (결정 후 바로 실행)

- [x] Q1(하이브리드 하네스)·Q2(LaTeX 렌더링) 결정 반영 완료 — Q3~Q5 잔여
- [ ] 사용자 문서 검토 의견 반영
- [ ] `git init` + P1 스켈레톤 생성
- [ ] `core/schema.py`, `core/store.py`, `core/ledger.py` 구현 + 테스트
- [ ] `scores/paper.yaml` 초안 작성
- [ ] 도구 어댑터 1종(웹검색) 검증 — 레퍼런스 카드 자동 생성 확인