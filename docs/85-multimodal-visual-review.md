# Multimodal Visual Review

Status: implemented reusable visual-assessment runtime with hash-pinned image input, independent perspectives, targeted contract repair, synthesis, and independent final verification. It evaluates supplied images and produces scoped revision instructions; it does not generate or silently replace visual assets.

## Scope

`run-visual-review` serves academic figures, rendered manuscript pages, aesthetic concept alternatives, and other bounded visual comparisons. The Score declares the communication objective, medium, audience, exact assets, criteria, and two to six review perspectives. Asset roles distinguish the subject under review from a reference, source, or rendered candidate.

The runtime accepts PNG and JPEG inputs. A PDF, SVG, video, or other source must first be rendered to explicit page or frame images by an authorized project tool. Source and rendered images may be supplied together so the review can verify a bounded change without treating the reference as a replacement candidate.

## Execution and evidence contract

1. Validate the visual Score, model binding, finite deadline, asset roles, criteria, and independent perspectives before dispatch.
2. Read each source once, validate its media header and dimensions, enforce the configured aggregate image-byte limit, and publish the original bytes as an immutable `source_capture` with SHA-256, dimensions, role, and label.
3. Pass only the content-addressed object path, media type, and expected SHA-256 to the model worker. The model client re-reads and verifies the bytes immediately before building the request. Base64 request bodies are never written to context or result artifacts.
4. Send all images in declared order to independent perspective calls. Each perspective must execute every criterion exactly once and bind observations, issues, and strengths to named asset IDs and concrete visible locations.
5. Reject malformed or out-of-contract replies. A bounded retry receives only its own previous response and validation error; accepted perspectives are retained and are not regenerated.
6. Synthesize the retained perspectives while inspecting the same images. Each criterion carries the exact ordered `perspective_outcomes`, checked mechanically against the source reviews. Every reported issue has a purpose, location, impact, smallest sufficient `change_scope`, protected elements, and a ranked action with an explicit allowed-change list and rerender verification condition.
7. A fresh verifier sees the images, reviews, and exact synthesis. Acceptance requires all of `asset-grounding`, `criterion-coverage`, `issue-action-traceability`, `surgical-scope`, and `comparative-fidelity` to pass. Acceptance approves the assessment record, not the aesthetic quality of the subject or any external publication.

The same configured first-result, target, and hard elapsed limits used by other Scores apply. Image bytes are bounded separately from the complete JSON request. The default model-client ceilings are 7,000,000 combined image bytes, 10,000,000 request bytes, and sixteen images; a project may set smaller limits. Direct image input requires the OpenAI-compatible protocol.

## Configuration and operation

Copy [`config/visual-review.example.json`](../config/visual-review.example.json), set absolute asset paths and an authorized OpenAI-compatible multimodal model, then run:

```bash
python -m scisaurus.cli run-visual-review PROJECT_DIR --config CONFIG.json
```

The CLI also accepts `--first-result-seconds`, `--target-seconds`, and `--deadline-seconds`. A project directory is single-use, preserving the exact Score, image captures, model contexts, responses, validation failures, accepted assessment, and event chain.

| Output | Meaning |
|---|---|
| `output/visual-assessment.json` | Accepted structured criteria, exact perspective outcomes, issues, and scoped actions |
| `output/visual-assessment.md` | Readable assessment and prioritized actions |
| `output/run.json` | Status, accepted reference, asset/review/verification refs, timing, usage, and integrity state |

## Live execution evidence

Run `2480b3e77aea4de4a01c6538f4519520` evaluated the 1020×1320 rendered ResNet gap-report page through the configured external Qwen multimodal endpoint. Two perspective calls ran concurrently, followed by synthesis and independent verification. The assessment was accepted after 267.55 seconds, within the 360-second first-result and 600-second hard limits. The ledger records four model calls, 18,056 input tokens, and 12,652 output tokens; the event chain verifies as `ok`.

The accepted decision was `revise`. It found one bounded defect: stretched inter-word spacing on the first bibliography line. The action permits only reference-line spacing/tracking and an optional line break, while preserving the author string, year, title, DOI, all scientific wording, and limitation statement. The run is stored under `.runs/axion-visual-20260911T012926Z/`; its `run.json` SHA-256 is `386c11a5600a578844cce4da7dcf734754ddc8c258ac1b778fe476485af896bb`, and its structured assessment SHA-256 is `a94b1239fdc5e81c15b38dad51d7c65d3f03467423dcd4336d4fbadab22f64a0`.

Two earlier development executions were rejected without accepting an assessment. The first returned an extra response field. The second exposed that free-form agreement text could overstate reviewer consensus and that allowed changes can be plural. The current schema records the exact outcome from each perspective and requires a list of allowed changes; local regression tests also verify that only a rejected perspective is retried.

The paper builder now emits `\raggedright` only inside the bibliography environment. A fresh release candidate under `.runs/resnet-v3-paper-release-visual-fixed-20260911/` differs in generated LaTeX by that one directive; its claim index and `.bib` file are byte-identical to the prior candidate. The new PDF SHA-256 is `4415bfe4606b510821c6832845e78ef344dfa41409394fbc43a8aeb6dfe1132a`; the rendered page SHA-256 is `eed1d231548fda064eb674e6e4ad50bf691406c2d56e883481db0cef25a3e120`.

Run `f2eab9a8c73441ecb2a07522e23c3983` then inspected the before and after renders together. Two independent perspectives, synthesis, and verification completed in 67.28 seconds using four model calls, 18,848 input tokens, and 6,247 output tokens. The accepted decision was `accept`: both criteria passed, no issue or corrective action remained, and the report states that the only visible change is the repaired reference spacing. Its `run.json` SHA-256 is `49e486c9cce4b021f9122278276dc16ce10a31407308a1f77a74ec1774a61f51`; its assessment SHA-256 is `2873b9e0b654487e3a56dc871d8c81adc4c9381ba91a88c897a3507d20027a8b`.

These executions prove that the configured model received and assessed real image data, the workflow rejected schema drift, one visual finding drove a bounded repair, and a fresh comparative review accepted the rendered result without broadening the change. They do not establish calibrated aesthetic judgment across domains. A held-out corpus of expert-labeled visual cases remains necessary to measure missed defects, false alarms, and preference consistency.
