# AI Scientist-v2 benchmark integration

Sci-whale uses [SakanaAI/AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) as a design benchmark. The comparison is about research control, not code reuse. The benchmark repository's useful ideas are progressive tree search, structured idea proposals, staged experiment progression, multi-seed checks, retained alternatives, aggregated visual inspection, and a separate manuscript review pass.

The project adopts those ideas in bounded form:

- `scisaurus.runtime.research_tree` stores a versioned hypothesis tree. Every child names its parent, stage, seed, inputs, metrics, and evidence. A branch can be promoted only after an independent verification reference is recorded; proposed and verified alternatives remain visible after promotion. `render_tree_html` produces a deterministic audit view suitable for a checkpoint artifact.
- The experiment runtime treats repeated seeded observations, exact replay, independent metric recalculation, and multiple reviewer perspectives as acceptance prerequisites. The proper calibration case uses a fixed repeated split design and three review perspectives: scientific validity, methods/statistical validity, and an adversarial AI-smell/claims pass.
- Manuscript review is a separate gate from manuscript production. A reviewer may propose a location-specific repair, but a writer cannot silently rewrite the whole document. The existing paragraph and paper release contracts continue to enforce exact unit scope, evidence links, storyline order, and rendered-PDF inspection.
- Candidate branches are selected by evidence and an explicit Principal policy. Model self-scores do not establish progress, novelty, or truth.

The reusable manuscript gate runs the three roles and a final non-writing synthesis as four bounded model calls:

```bash
python3 scripts/run-manuscript-review.py \
  --manuscript /absolute/path/manuscript.json \
  --config /absolute/path/review-config.json \
  --output /absolute/path/review-package.json
```

The output records the exact manuscript hash, each role's checks and location-specific findings, and the final repair contract. A `needs_revision` result is a work order; it is not an approval. The paper runner must apply only those scoped repairs and then rerun the three roles plus the rendered-PDF check.

The project does not copy the benchmark's unrestricted execution path. AI Scientist-v2 itself warns that it executes model-written code and may install uncontrolled packages or use uncontrolled web/process access. Sci-whale keeps execution behind an allowlisted Operations Cell, pinned program sources, project-local workspaces, bounded deadlines, independent verification, and immutable artifacts. This preserves the useful search pattern without granting a generated proposal direct write or process authority.

The benchmark also reports that its v2 tree-search path is better suited to open-ended exploration while a template-driven path can be more reliable when the objective is already clear. Sci-whale therefore keeps both modes: a frozen, evidence-bound paper path for commitment and a bounded research tree for exploratory alternatives. The tree is a proposal and provenance layer; it cannot bypass the paper's literature, result, review, or Principal-approval gates.

## Human research-team benchmark

The runtime is also evaluated against observable practices from scientific-team research, not against an idealized org chart. The benchmark basis is the National Academies' *The Science and Practice of Team Science*, NIH's field guide derived from interviews with effective and unsuccessful research collaborations, NIH's rigor and reporting guidance, and NASA's project-scientist authority/accountability definitions. These sources emphasize a shared scientific objective, explicit responsibilities, coordination and information sharing, open disagreement with conflict resolution, and revisiting the plan when evidence or conditions change. NIH's reviewer criteria specifically separate research importance from rigor, design, analysis, interpretation, and reporting; the runtime must therefore repair the evidence-bearing method, not mistake a transport or schema defect for a scientific verdict. These sources do not claim that any one organizational template guarantees correct science.

Two concrete review mechanisms sharpen the workflow benchmark. NIH's first-level process assigns multiple reviewers to prepare independent critiques, then uses a managed review discussion and a recorded synthesis; repeated rounds should therefore retain the original critiques and focus new calls on material revisions, rather than silently re-running the same panel. Registered Reports provide a direct outcome-neutral experiment pattern: review the question, methods, and analysis plan before data collection, then review execution, protocol adherence, quality checks, and interpretation after results exist. Acceptance is not conditioned on result direction. Sci-whale adopts these as workflow principles, not as a claim that an AI panel is equivalent to institutional peer review.

| Team-science practice | Required Sci-whale behavior | Regression evidence |
|---|---|---|
| Shared, revisitable research purpose | Keep the admitted topic and experimental intent attached to repair work; change them only from a typed, source-backed scientific decision. | `test_technical_failure_evidence_cannot_open_topic_pivot`; `test_zero_observation_failure_reopens_same_topic_source_repair` |
| Clear ownership and handoff | Every repair order identifies its owner, exact evidence, dependencies, acceptance checks, and the independent role that verifies the result. | `test_experiment_repair_reconciles_unknown_attempts_before_execution`; department work-order and assignment lifecycle tests |
| Coordinated specialist work | Specialists may investigate distinct evidence or methods questions; the accountable lead must reconcile their findings into one recorded decision and next work order. Role count or parallel-call count alone is not progress. | Composer specialist-pool, synthesis, and independent-verifier tests |
| Productive scientific disagreement | Reviewers cite the exact candidate/result and distinguish reproducible defects from hypotheses; a technical parse/API failure is not a scientific rejection. | `test_truncated_patch_preserves_scientific_review_without_reclassifying_it_as_a_review_failure`; `test_author_format_failure_uses_bulk_route_before_premium_fallback` |
| Independent critiques followed by accountable synthesis | Keep each reviewer report immutable; the methods lead resolves disagreements into one bounded, evidence-linked work order. A plan-only revision reuses unchanged critiques and asks the independent verifier to review the revised plan, rather than dispatching a fresh full panel. | `test_rejected_plan_reuses_reviewers_and_revises_from_verifier_feedback`; `test_prior_result_history_skips_empty_attempts_and_separates_related_question` |
| Stage-specific admission, not premature rejection | A pre-execution panel judges the proposed question, estimand, and design. Missing generated source, observations, and recalculation are explicit later gates; they do not count against a plan, but source integrity and independent recalculation must pass before execution results can be admitted. | `test_pre_execution_repair_verifier_reviews_the_plan_not_missing_results`; `test_preexecution_verifier_dissent_blocks_capability_admission` |
| Outcome-neutral plan review | Preserve the admitted question, predicted direction, primary estimand, and acceptance criteria before execution. A contrary result is handled as disconfirmation and reported; protocol changes are versioned and justified without tuning to the observed direction. | `test_repair_adjudication_prompt_receives_exact_lineage_and_recent_external_evidence`; `test_capability_repair_packet_compares_bounded_prior_measurement_history` |
| Evidence-led repair, not binary rejection | A rejected pre-execution candidate retains exact review evidence and routes to a methods repair; technical failure cannot erase the selected question or silently turn into a topic pivot. | `test_interrupted_foundry_rejection_restores_same_topic_repair_order_once`; `test_technical_failure_evidence_cannot_open_topic_pivot` |
| Output-limit resilience without lost critique | A complete schema-valid response is judged even when the provider reports `length`; incomplete JSON resumes from its exact durable prefix, and an unknown continuation is never replayed. Review evidence is not rejected or clipped by a separate arbitrary word-count ceiling. | `test_complete_review_json_is_accepted_when_finish_reason_is_length`; `test_specialist_continues_provider_truncation_until_stop_within_call_quota`; `test_review_response_contracts_preserve_complete_evidence_without_word_ceilings`; `test_unknown_review_continuation_is_not_replayed_after_resume` |
| Adaptation without losing provenance | An interrupted response resumes from its durable prefix, an unknown external operation is reconciled before equivalent execution, and every accepted repair is independently rerun. | `test_length_limited_author_response_continues_after_resume`; `test_experiment_repair_reconciles_unknown_attempts_before_execution` |

These are behavioral checks, not a certificate that the current mission has completed valid research. The normal CI acceptance suite runs them together with the full runtime tests. Reassess the benchmark whenever a recovery, pivot, assignment, reviewer, or completion path changes; add a regression test for any newly observed way technical failure can erase scientific intent or strand a deliverable.

### Sources

- [National Academies, *The Science and Practice of Team Science*](https://www.ncbi.nlm.nih.gov/books/NBK615760/)
- [Bennett et al., *Collaboration and Team Science: From Theory to Practice*](https://pmc.ncbi.nlm.nih.gov/articles/PMC3652225/)
- [NIH, Enhancing Reproducibility through Rigor and Transparency](https://grants.nih.gov/policy-and-compliance/policy-topics/reproducibility)
- [NIH, Principles and Guidelines for Reporting Preclinical Research](https://www.grants.nih.gov/policy-and-compliance/policy-topics/reproducibility/principles-guidelines-reporting-preclinical-research)
- [NIH, First-Level Peer Review](https://grants.nih.gov/grants-process/review/first-level)
- [NIH Grants Policy Statement, Initial Review](https://grants.nih.gov/grants/policy/nihgps/HTML5/section_2/2.4.1_initial_review.htm)
- [Scientific Reports, Registered Reports](https://www.nature.com/srep/journal-policies/registered-reports)
- [NASA Science Workforce Study: project-scientist roles, responsibilities, authorities, and accountability](https://science.nasa.gov/wp-content/uploads/2023/06/FINAL_Agency_Science_Workforce_Study_03.02-2.pdf)
