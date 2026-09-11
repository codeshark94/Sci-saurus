# AI Scientist-v2 benchmark integration

Sci-saurus uses [SakanaAI/AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) as a design benchmark. The comparison is about research control, not code reuse. The benchmark repository's useful ideas are progressive tree search, structured idea proposals, staged experiment progression, multi-seed checks, retained alternatives, aggregated visual inspection, and a separate manuscript review pass.

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

The project does not copy the benchmark's unrestricted execution path. AI Scientist-v2 itself warns that it executes model-written code and may install uncontrolled packages or use uncontrolled web/process access. Sci-saurus keeps execution behind an allowlisted Operations Cell, pinned program sources, project-local workspaces, bounded deadlines, independent verification, and immutable artifacts. This preserves the useful search pattern without granting a generated proposal direct write or process authority.

The benchmark also reports that its v2 tree-search path is better suited to open-ended exploration while a template-driven path can be more reliable when the objective is already clear. Sci-saurus therefore keeps both modes: a frozen, evidence-bound paper path for commitment and a bounded research tree for exploratory alternatives. The tree is a proposal and provenance layer; it cannot bypass the paper's literature, result, review, or Principal-approval gates.
