# Sci-saurus — 구조 선행조사 (Precedent & Framework Survey)

> 목적: SSOT의 설계 결정(D1~D8)이 실재하는 시스템·연구·표준과 어떻게 맞닿아 있는지 확인하고, 채택/반영할 패턴을 추린다. 모든 항목은 출처 링크를 포함한다. 본 문서는 `00-SSOT.md`의 파생물이다.

---

## 1. 카테고리 A — 종단간(End-to-End) 과학 연구 자동화 시스템

### 1.1 The AI Scientist (Sakana AI)
- [The AI Scientist — Nature 출판 소식](https://sakana.ai/ai-scientist-nature/) · [GitHub: SakanaAI/AI-Scientist](https://github.com/sakanaai/ai-scientist) · [AI-Scientist-v2](https://github.com/sakanaai/ai-scientist-v2) · [v2 논문 PDF (Agentic Tree Search)](https://pub.sakana.ai/ai-scientist-v2/paper/paper.pdf)
- 아이디어 생성 → 실험 실행 → 코드 작성 → 논문 집필 → 자동 리뷰까지 단일 파이프라인으로 수행. v2는 **agentic tree search**로 실험 공간을 탐색.
- **시사점:** (채택) 아이디어→집필→리뷰의 전 주기 자동화 가능성 입증, 트리 탐색은 v2 "실험 실행" 확장 슬롯의 참고 모델. (거리) 역할 구분 없는 단일 루프 + 템플릿 종속은 Sci-saurus의 "부서+자유교환"과 다름. Sci-saurus는 조직 은유와 산출물 버전관리를 더 엄격히 다룸.

### 1.2 Agent Laboratory (AMD + Johns Hopkins)
- [GitHub: SamuelSchmidgall/AgentLaboratory](https://github.com/SamuelSchmidgall/AgentLaboratory) · [홈페이지](https://agentlaboratory.github.io/) · [논문 (arXiv 2501.04227)](https://arxiv.org/html/2501.04227v2) · [EMNLP 2025 Findings](https://aclanthology.org/2025.findings-emnlp.320/) · [InfoQ 요약](https://www.infoq.com/news/2025/01/amd-jhu-ai-lab-research-agent/)
- 단계 구조: **문헌 리뷰 → 계획 → 실험 → 보고서 작성**, 역할 구조: PI · 포닥 · 박사 · 리뷰어. 각 단계의 산출물이 다음 단계의 입력이 됨.
- **시사점:** Sci-saurus 파이프라인(조사→전략→집필→편집)과 가장 구조적으로 유사 — **단계별 산출물 전달 + 위계적 역할** 패턴은 검증된 선례. 단, 자유 교환(부서 간 비동기 통신)은 약함 → 우리가 보강할 지점.

### 1.3 CycleResearcher / CycleReviewer (WestlakeNLP)
- [논문 PDF (arXiv 2411.00816)](https://arxiv.org/pdf/2411.00816) · [ICLR 2025 Proceedings 페이지](https://proceedings.iclr.cc/paper_files/paper/2025/hash/0a48036026dc7946ef6033ae14719cc5-Abstract-Conference.html)
- **연구자-리뷰자 반복 루프**: 리뷰어 에이전트의 피드백으로 리서처 에이전트가 개선하는 순환.
- **시사점:** 편집실 ↔ 전략부의 수정 루프 설계의 근거. 단 **루프 상한(기본 3회)**이 없으면 무한 반복·비용 폭주 — Sci-saurus는 게이트+중재로 강제 종료 조건을 둠.

## 2. 카테고리 B — 딥 리서치 / 지식 합성 (조사부의 선례)

### 2.1 STORM (Stanford OVAL)
- [GitHub: stanford-oval/storm](https://github.com/stanford-oval/storm/) · [Co-STORM 논문 (EMNLP 2024, arXiv 2408.15232)](https://arxiv.org/pdf/2408.15232)
- **다관점 질문 생성(multi-perspective question asking)**으로 개요를 합성하고, 인용 가능한 출처를 붙인 아티클을 생성. Co-STORM은 다중 에이전트 협업 지식 큐레이션으로 확장.
- **시사점:** 조사부의 "관점 분해 조사" 패턴과 전략부로 넘길 **개요+출처** 산출물 형식의 직접 선례. "계보·동향" 정리에 인용 네트워크 관점을 추가하면 완성.

### 2.2 Open Deep Research 계열
- [GitHub: langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research/) · [HuggingFace 블로그: Open Deep Research](https://github.com/huggingface/blog/blob/main/open-deep-research.md) · [DXD-LABS/open-deep-research](https://github.com/DXD-LABS/open-deep-research) · [상용 비교 (Perplexity vs OpenAI vs Gemini)](https://anthemcreation.com/en/artificial-intelligence/deep-research-perplexity-openai-gemini-who-is-the-best/)
- 검색→선별→합성→보고서의 표준 딥리서치 루프. 상용(Gemini/OpenAI/Perplexity)은 품질 기준선 제공.
- **시사점:** 조사부 "문헌 스카우트 + 사서" 역할 분리의 참조 구현. 모든 조사 산출물에 **출처 메타데이터(DOI/URL, 접근 시각)**를 강제하는 근거: 게이트 G1의 근거.

## 3. 카테고리 C — 오케스트레이션 프레임워크

### 3.1 비교 개관
- [Agentic AI Frameworks: Architectures, Protocols, and Design Challenges (서베이, arXiv 2508.10146)](https://arxiv.org/html/2508.10146) · [프레임워크 랜드스케이프 개관](https://www.softwareseni.com/navigating-the-multi-agent-framework-landscape-from-crewai-to-langgraph-to-autogen-and-beyond/) · [CrewAI vs MetaGPT vs AutoGen 비교](https://www.agentframeworkhub.com/compare/multi/crewai-vs-metagpt-vs-autogen) · [벤치마크: LangGraph vs CrewAI vs AutoGen (JATIR)](https://jatir.org/publishedpapers/140332_PAPER.pdf)

### 3.2 MetaGPT — "SOP 회사" 은유의 원형
- [논문 (arXiv 2308.00352)](https://arxiv.org/html/2308.00352v7) · [GitHub README](https://github.com/geekan/MetaGPT/blob/main/README.md) · [표준 개발 역할 문서](https://deepwiki.com/FoundationAgents/MetaGPT/5.1-standard-development-roles)
- 소프트웨어 회사 은유 + **SOP(표준운영절차)** 기반 역할 분업, 역할 간 **구조화된 산출물(문서)** 전달. Sci-saurus의 "부서+아티팩트" 개념과 가장 가까운 선례.
- **시사점:** (채택) 역할→산출물→다음 역할의 SOP 패턴. (거리) MetaGPT는 소프트웨어 개발 파이프라인에 고정 → Sci-saurus는 악보(score)로 파이프라인 자체를 교체 가능하게 일반화(D6). 부서 간 자유 통신·버전관리는 MetaGPT에 없음.

### 3.3 AutoGen 0.4 (Microsoft) — 이벤트 기반 actor 모델
- [MSR 아티클](https://www.microsoft.com/en-us/research/articles/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/) · [개발자 블로그](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/) · [공식 문서](https://microsoft.github.io/autogen/0.4.0/index.html)
- v0.4에서 이벤트 기반, 확장 가능한 **actor 아키텍처**로 재설계. 에이전트 간 비동기 메시징이 1급 시민.
- **시사점:** D3 "자유 교환 버스"의 검증된 구현 모델. 단, 산출물 버전관리·부서 은유는 없음 → 우리 스토어 레이어가 보완.

### 3.4 LangGraph — 상태머신 + 체크포인트(영속성)
- [Checkpointers 문서](https://docs.langchain.com/oss/python/langgraph/checkpointers) · [Persistence 문서](https://docs.langchain.com/oss/python/langgraph/persistence) · [GitHub](https://github.com/langchain-ai/langgraph)
- 그래프 상태머신 + **체크포인터로 상태 영속화/재개**, 휴먼 인 더 루프 내장.
- **시사점:** 파이프라인 견고성(중단-재개) 검증. 다만 LangGraph 체크포인트는 **실행 상태**의 저장이지 **문서 산출물의 버전관리**가 아님 → Sci-saurus는 두 계층을 분리: 실행 상태(원장) / 산출물(git 콘텐츠 저장소).

### 3.5 Blackboard 아키텍처 — "자유 교환"의 고전적 원형
- [Hearsay-II 원문](http://faculty.chas.uni.edu/~wallingf/teaching/162/readings/hearsay-ii.pdf) · [Stanford CS-TR-86-1123 (Blackboard Systems)](http://i.stanford.edu/pub/cstr/reports/cs/tr/86/1123/CS-TR-86-1123.pdf) · [LLM 다중 에이전트에의 블랙보드 적용 (arXiv 2507.01701)](https://arxiv.org/html/2507.01701)
- 지식 소스(knowledge sources)들이 **공유 작업공간(blackboard)**을 읽고 조건부로 기여하는 고전 아키텍처. Hearsay-II가 원형.
- **시사점:** D3(블랙보드+버스 혼합)의 이론적 근거. 최근 LLM 멀티에이전트에서도 블랙보드 재조명 — 우리의 "공유 파일+권한 모델+기록" 조합은 이 라인의 현대화.

## 4. 카테고리 D — 상호운용 표준 (미래 확장용)

- **A2A (Agent2Agent, Google):** [공식 사이트](https://a2a-protocol.org/latest/) · [발표 블로그](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/) · [스펙](https://github.com/google/A2A/blob/7b900e77/docs/specification.md) · [SAP: A2A+MCP 비교](https://architecture.learning.sap.com/docs/ref-arch/76ec36)
- **MCP (Model Context Protocol):** [공식 사이트](https://modelcontextprotocol.io) — 도구/컨텍스트 표준.
- **시사점:** v1은 내부 자체 프로토콜로 빠르게; v2 이후 "부서를 외부 에이전트로 노출"할 때 A2A, "도구 연결"에 MCP를 어댑터로 채택. 지금부터 메시지 봉투를 A2A 유사 필드(role/task/artifact refs)로 설계하면 전환 비용 최소화.

## 5. 카테고리 E — 저장 · 버전관리 · 지식베이스

- LangGraph 체크포인트(실행 상태 영속): 위 §3.4.
- git 자체(산출물 콘텐츠 어드레싱·불변 커밋·태그) — Sci-saurus 기본 저장 엔진.
- DVC(데이터 버전관리, [https://dvc.org](https://dvc.org)) — 큰 데이터 파일/데이터셋 버전관리 필요 시 v2 슬롯.
- KB 버전관리 시도들: [kbvc (PyPI)](https://pypi.org/project/kbvc/0.1.3/) · [ContextSync Protocol](https://github.com/metisos/contextsync-protocol) · [에이전트 지식베이스 변경 추적 개관](https://callsphere.ai/blog/data-versioning-ai-agents-tracking-knowledge-base-changes.md)
- **시사점:** 전용 도구는 초기 단계. Sci-saurus는 **git + JSONL 원장(이벤트 소싱)** 조합으로 "산출물 버전 + 행위 감사"를 동시에 충족(D5). 지식베이스는 스냅샷(조사 시점 고정)을 아티팩트로 저장해 재현성 확보.

## 6. 종합 비교표

| 시스템/프레임워크 | 조직 은유 | 산출물 버전관리 | 부서 간 자유 교환 | 범용성 | Sci-saurus와의 관계 |
|---|---|---|---|---|---|
| AI Scientist v1/v2 | ✗ (단일 파이프라인) | 부분 (로그) | ✗ | 낮음(논문 특화) | 종단간 가능성 참고, 실험 확장 슬롯 |
| Agent Laboratory | △ (위계적 역할) | △ | ✗ | 중간 | 파이프라인 단계 구조 선례 |
| CycleResearcher | △ (리서처-리뷰어 루프) | ✗ | 루프형 | 중간 | 리뷰 루프 설계 근거(상한 필요) |
| STORM/Co-STORM | △ (관점 에이전트) | △ | 협업 큐레이션 | 중간 | 조사부 패턴 선례 |
| MetaGPT | ◎ (SOP 회사) | △ (문서 산출물) | 순차 중심 | 낮음(소프트웨어 고정) | 부서+아티팩트 은유 원형 → 일반화 |
| AutoGen 0.4 | △ (actor 네트워크) | ✗ | ◎ (이벤트 버스) | ◎ | 통신 계층 참고 구현 |
| LangGraph | ✗ (그래프) | △ (체크포인트=실행상태) | 그래프 위상 제한 | ◎ | 파이프라인 견고성 옵션 |
| Blackboard (Hearsay-II) | ◎ (지식소+공유공간) | ✗ | ◎ | ◎ | 블랙보드+버스 혼합의 이론 근거 |
| **Sci-saurus (목표)** | **부서+Composer+기록부** | **◎ (불변 버전+원장+릴리스)** | **◎ (버스+블랙보드+중재)** | **◎ (악보 교체)** | — |

## 7. 설계에 반영한 패턴 (채택 목록)

1. **단계-산출물 파이프라인**(Agent Laboratory, MetaGPT) → 악보의 뼈대.
2. **역할 위계 + 부장 품질 판정**(Agent Laboratory PI) → 각 부서의 부장 역할.
3. **리뷰 반복 루프 + 상한**(CycleResearcher) → 편집실 루프, 기본 3회 캡.
4. **다관점 조사 + 인용 출처 강제**(STORM, Deep Research) → 조사부 패턴, 게이트 G1.
5. **이벤트 버스 + 비동기 actor**(AutoGen 0.4) → 메시지 버스 설계.
6. **블랙보드 공유 작업공간 + 권한**(Hearsay-II, arXiv 2507.01701) → 프로젝트 워크스페이스 권한 모델.
7. **상태 영속화**(LangGraph checkpoint) → 실행 재개성. 단, 산출물 버전관리는 git 콘텐츠 어드레싱으로 분리.
8. **트리 탐색 실험**(AI Scientist v2) → v2 실험 실행 확장 슬롯.
9. **표준 호환 필드**(A2A/MCP) → 메시지 봉투에 미래 호환 필드 포함.
10. **이벤트 소싱 + git 결합** → 원장(감사) + 불변 아티팩트(버전)의 이중 기록.

## 8. 차별화 포인트 (선례 대비 Sci-saurus의 신규성)

1. **부서의 범용성**: 선행 시스템들은 대부분 "논문(또는 소프트웨어) 전용 고정 파이프라인". Sci-saurus는 부서=범용 기능, 파이프라인=교체 가능한 악보(D2, D6).
2. **기록부라는 상설 횡단 조직**: 버전관리·감사를 1급 조직 구성원으로 격상한 선례는 드묾.
3. **자유 교환의 규율**: "언제든지 자유롭게"라는 요구를, 무질서가 아닌 **전면 기록 + 게이트 + 중재**로 규율화.
4. **provenance 1급 시민**: 모든 아티팩트가 부모 링크·입력 근거·담당 역할을 가짐 → 조사-전략-편집 간 데이터 계보(genealogy of artifacts) 자체가 자산.

## 9. 참고 링크 총목

위 각 절에 인라인 표기. 주요 1차 출처: [AI Scientist-v2](https://github.com/sakanaai/ai-scientist-v2), [Agent Laboratory](https://agentlaboratory.github.io/), [CycleResearcher](https://arxiv.org/pdf/2411.00816), [STORM](https://github.com/stanford-oval/storm/), [MetaGPT](https://arxiv.org/html/2308.00352v7), [AutoGen 0.4](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/), [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Hearsay-II](http://faculty.chas.uni.edu/~wallingf/teaching/162/readings/hearsay-ii.pdf), [A2A](https://a2a-protocol.org/latest/), [MCP](https://modelcontextprotocol.io).