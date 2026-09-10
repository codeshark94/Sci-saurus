# Sci-saurus — Structural Precursor Survey

> Purpose: verify that the design decisions (D1–D12) in `00-SSOT.md` line up with real systems, research, and standards, and distill the patterns we adopt or reject. Every item carries its sources. This document derives from `00-SSOT.md`.

---

## 1. Category A — End-to-end automated scientific research systems

### 1.1 The AI Scientist (Sakana AI)
- [The AI Scientist — published in Nature](https://sakana.ai/ai-scientist-nature/) · [GitHub: SakanaAI/AI-Scientist](https://github.com/sakanaai/ai-scientist) · [AI-Scientist-v2](https://github.com/sakanaai/ai-scientist-v2) · [v2 paper PDF (Agentic Tree Search)](https://pub.sakana.ai/ai-scientist-v2/paper/paper.pdf)
- Idea generation → experiment execution → code writing → paper writing → automated review, as a single pipeline. v2 explores the experiment space with **agentic tree search**.
- **Implications:** (adopt) proves idea→write→review full-cycle automation is feasible; tree search is a reference model for the v2 "experiment execution" slot. (distance) a single loop without role separation and template dependence differ from Sci-saurus's "departments + free exchange"; we treat organization metaphor and artifact version control far more strictly.

### 1.2 Agent Laboratory (AMD + Johns Hopkins)
- [GitHub: SamuelSchmidgall/AgentLaboratory](https://github.com/SamuelSchmidgall/AgentLaboratory) · [Site](https://agentlaboratory.github.io/) · [Paper (arXiv 2501.04227)](https://arxiv.org/html/2501.04227v2) · [EMNLP 2025 Findings](https://aclanthology.org/2025.findings-emnlp.320/) · [InfoQ summary](https://www.infoq.com/news/2025/01/amd-jhu-ai-lab-research-agent/)
- Stage structure: **literature review → plan → experiments → report writing**; role structure: PI · postdoc · PhD · reviewer. Each stage's output feeds the next stage.
- **Implications:** structurally the closest precedent to our pipeline (survey → strategy → writing → editing) — validates **stage-wise artifact handoff + hierarchical roles**. Weak on free exchange (async inter-department messaging) — the point we reinforce.

### 1.3 CycleResearcher / CycleReviewer (WestlakeNLP)
- [Paper PDF (arXiv 2411.00816)](https://arxiv.org/pdf/2411.00816) · [ICLR 2025 proceedings page](https://proceedings.iclr.cc/paper_files/paper/2025/hash/0a48036026dc7946ef6033ae14719cc5-Abstract-Conference.html)
- A **researcher–reviewer iterative loop**: reviewer agents' feedback improves researcher agents' outputs.
- **Implication:** grounds the Editorial ↔ Strategy revision loop. Without a **loop cap (default 3)**, endless iteration and cost blowup — Sci-saurus enforces termination via gates + arbitration.

## 2. Category B — Deep research / knowledge synthesis (precedents for the Research department)

### 2.1 STORM (Stanford OVAL)
- [GitHub: stanford-oval/storm](https://github.com/stanford-oval/storm/) · [Co-STORM paper (EMNLP 2024, arXiv 2408.15232)](https://arxiv.org/pdf/2408.15232)
- Synthesizes outlines via **multi-perspective question asking** and produces cited articles; Co-STORM extends to multi-agent collaborative knowledge curation.
- **Implications:** direct precedent for the Research department's "perspective-decomposed survey" and the **outline+sources** artifact format handed to Strategy. Adding citation-network perspectives completes genealogy/trend work.

### 2.2 Open Deep Research family
- [GitHub: langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research/) · [HuggingFace blog: Open Deep Research](https://github.com/huggingface/blog/blob/main/open-deep-research.md) · [DXD-LABS/open-deep-research](https://github.com/DXD-LABS/open-deep-research) · [Commercial comparison (Perplexity vs OpenAI vs Gemini)](https://anthemcreation.com/en/artificial-intelligence/deep-research-perplexity-openai-gemini-who-is-the-best/)
- The standard deep-research loop: search → select → synthesize → report. Commercial systems (Gemini/OpenAI/Perplexity) set the quality baseline.
- **Implications:** reference implementation for Research's "Literature Scout + Cataloger" split; the rule that all survey outputs carry **source metadata (DOI/URL, access time)** grounds gate G1.

## 3. Category C — Orchestration frameworks

### 3.1 Overview & comparisons
- [Agentic AI Frameworks: Architectures, Protocols, and Design Challenges (survey, arXiv 2508.10146)](https://arxiv.org/html/2508.10146) · [Framework landscape overview](https://www.softwareseni.com/navigating-the-multi-agent-framework-landscape-from-crewai-to-langgraph-to-autogen-and-beyond/) · [CrewAI vs MetaGPT vs AutoGen](https://www.agentframeworkhub.com/compare/multi/crewai-vs-metagpt-vs-autogen) · [Benchmark: LangGraph vs CrewAI vs AutoGen (JATIR)](https://jatir.org/publishedpapers/140332_PAPER.pdf)

### 3.2 MetaGPT — the "SOP company" archetype
- [Paper (arXiv 2308.00352)](https://arxiv.org/html/2308.00352v7) · [GitHub README](https://github.com/geekan/MetaGPT/blob/main/README.md) · [Standard development roles](https://deepwiki.com/FoundationAgents/MetaGPT/5.1-standard-development-roles)
- Software-company metaphor + **SOP-based role division**, with **structured document artifacts** passed between roles. The closest precedent to Sci-saurus's "departments + artifacts" concept.
- **Implications:** (adopt) the SOP pattern of role → artifact → next role. (distance) MetaGPT fixes the pipeline to software development → Sci-saurus generalizes by making the pipeline itself swappable via scores (D6). Free inter-department messaging and version control are absent.

### 3.3 AutoGen 0.4 (Microsoft) — event-driven actor model
- [MSR article](https://www.microsoft.com/en-us/research/articles/autogen-v0-4-reimagining-the-foundation-of-agentic-ai-for-scale-extensibility-and-robustness/) · [Dev blog](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/) · [Docs](https://microsoft.github.io/autogen/0.4.0/index.html)
- v0.4 redesigned into an event-driven, extensible **actor architecture**; async inter-agent messaging is a first-class citizen.
- **Implications:** the validated implementation model for D3's "free exchange bus". Lacks artifact version control and the department metaphor → our store layer supplements.

### 3.4 LangGraph — state machines + checkpointing (persistence)
- [Checkpointers docs](https://docs.langchain.com/oss/python/langgraph/checkpointers) · [Persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence) · [GitHub](https://github.com/langchain-ai/langgraph)
- Graph state machine + **checkpointer for state persistence/resume**, built-in human-in-the-loop.
- **Implications:** validates pipeline robustness (interrupt–resume). But LangGraph checkpoints persist **execution state**, not **document artifacts** → Sci-saurus separates the two layers: execution state (ledger) / outputs (git content store).

### 3.5 Blackboard architecture — the classic archetype of "free exchange"
- [Hearsay-II original paper](http://faculty.chas.uni.edu/~wallingf/teaching/162/readings/hearsay-ii.pdf) · [Stanford CS-TR-86-1123 (Blackboard Systems)](http://i.stanford.edu/pub/cstr/reports/cs/tr/86/1123/CS-TR-86-1123.pdf) · [Blackboard for LLM multi-agent systems (arXiv 2507.01701)](https://arxiv.org/html/2507.01701)
- Knowledge sources read a **shared workspace (blackboard)** and contribute conditionally — Hearsay-II is the archetype.
- **Implications:** theoretical grounding for D3's blackboard+bus hybrid. Recently re-examined for LLM multi-agent systems; our "shared files + permission model + recording" combo is its modernization.

## 4. Category D — Interoperability standards (future expansion)

- **A2A (Agent2Agent, Google):** [Site](https://a2a-protocol.org/latest/) · [Announcement blog](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/) · [Spec](https://github.com/google/A2A/blob/7b900e77/docs/specification.md) · [SAP: A2A+MCP](https://architecture.learning.sap.com/docs/ref-arch/76ec36)
- **MCP (Model Context Protocol):** [official site](https://modelcontextprotocol.io) — tool/context standard.
- **Implications:** v1 ships its own internal protocol for speed; after v2, A2A for exposing departments as external agents and MCP for tool connections, via adapters. Designing message envelopes with A2A-like fields (role/task/artifact refs) now minimizes migration cost.

## 5. Category E — Storage · version control · knowledge bases

- LangGraph checkpoints (execution-state persistence): §3.4 above.
- git itself (content addressing, immutable commits, tags) — Sci-saurus default storage engine.
- DVC (data version control, [https://dvc.org](https://dvc.org)) — v2 slot for large data files/datasets.
- KB versioning attempts: [kbvc (PyPI)](https://pypi.org/project/kbvc/0.1.3/) · [ContextSync Protocol](https://github.com/metisos/contextsync-protocol) · [KB change tracking for agents overview](https://callsphere.ai/blog/data-versioning-ai-agents-tracking-knowledge-base-changes.md)
- **Implications:** dedicated tools are early-stage. Sci-saurus satisfies "artifact versions + action audit" with **git + JSONL ledger (event sourcing)** (D5). The knowledge base is archived as **snapshots** (survey-time freezes) as artifacts, securing reproducibility.

## 6. Consolidated Comparison

| System/Framework | Organization metaphor | Artifact versioning | Free inter-dept exchange | Generality | Relation to Sci-saurus |
|---|---|---|---|---|---|
| AI Scientist v1/v2 | ✗ (single pipeline) | Partial (logs) | ✗ | Low (paper-specific) | Reference for end-to-end feasibility; experiment slot |
| Agent Laboratory | △ (hierarchical roles) | △ | ✗ | Medium | Precedent for stage-structured pipeline |
| CycleResearcher | △ (researcher–reviewer loop) | ✗ | Loop-shaped | Medium | Grounds the review loop (cap needed) |
| STORM/Co-STORM | △ (perspective agents) | △ | Collaborative curation | Medium | Precedent for Research dept patterns |
| MetaGPT | ◎ (SOP company) | △ (document artifacts) | Mostly sequential | Low (software-fixed) | Archetype of dept+artifact metaphor → generalized |
| AutoGen 0.4 | △ (actor network) | ✗ | ◎ (event bus) | ◎ | Reference implementation for comms layer |
| LangGraph | ✗ (graph) | △ (checkpoint = exec state) | Graph-topology constrained | ◎ | Robustness option for pipeline |
| Blackboard (Hearsay-II) | ◎ (knowledge sources + shared space) | ✗ | ◎ | ◎ | Theoretical basis for blackboard+bus mix |
| **Sci-saurus (target)** | **Departments + Composer + Archivist** | **◎ (immutable versions + ledger + releases)** | **◎ (bus + blackboard + arbitration)** | **◎ (score swap)** | — |

## 7. Patterns Adopted into the Design

1. **Stage–artifact pipeline** (Agent Laboratory, MetaGPT) → the backbone of scores.
2. **Role hierarchy + chief quality verdict** (Agent Laboratory's PI) → each department's chief.
3. **Iterative review loop with a cap** (CycleResearcher) → editorial loop, default cap 3.
4. **Multi-perspective survey + mandatory source citations** (STORM, Deep Research) → Research patterns, gate G1.
5. **Event bus + async actors** (AutoGen 0.4) → message bus design.
6. **Blackboard shared workspace + permissions** (Hearsay-II, arXiv 2507.01701) → project workspace permission model.
7. **State persistence** (LangGraph checkpoints) → execution resumability; artifact versioning stays separate on git content addressing.
8. **Tree-search experiments** (AI Scientist v2) → v2 experiment-execution slot.
9. **Standard-compatible fields** (A2A/MCP) → future-compatible message envelope fields.
10. **Event sourcing + git combined** → dual record: ledger (audit) + immutable artifacts (versions).

## 8. Differentiation (vs precedents)

1. **Department generality**: precedents mostly fix a single pipeline (papers, or software). Sci-saurus: departments = general functions; pipeline = swappable score (D2, D6).
2. **The Archivist as a standing cross-cutting organ**: elevating version control & audit to a first-class organizational member is rare among precedents.
3. **Disciplined free exchange**: the "anytime, freely" requirement is disciplined with **full recording + gates + arbitration**, not disorder.
4. **Provenance as a first-class citizen**: every artifact carries parent links, input evidence, and the owning role — the genealogy of artifacts across Research–Strategy–Editorial becomes an asset itself.

## 9. Reference Link Index

Inline above. Primary sources: [AI Scientist-v2](https://github.com/sakanaai/ai-scientist-v2), [Agent Laboratory](https://agentlaboratory.github.io/), [CycleResearcher](https://arxiv.org/pdf/2411.00816), [STORM](https://github.com/stanford-oval/storm/), [MetaGPT](https://arxiv.org/html/2308.00352v7), [AutoGen 0.4](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/), [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Hearsay-II](http://faculty.chas.uni.edu/~wallingf/teaching/162/readings/hearsay-ii.pdf), [A2A](https://a2a-protocol.org/latest/), [MCP](https://modelcontextprotocol.io).