(function (root) {
  "use strict";
  const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label = (key) => String(key).replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const technical = /^(schema_version|budget|usage|.*_sha256|.*_ref|.*_path|sampling.*|generation_seed|candidate_attempt_trace|candidate_sampling_trace|frontier_seed_plan|rejected_topic_history)$/;
  const priority = ["question", "selected_id", "selection_rationale", "decision", "outcome", "status", "summary", "rationale", "topic", "source_challenge", "required_revisions", "blocking_findings", "deferred_obligations", "item", "title", "abstract", "analysis", "review", "branches", "candidates"];
  function render(value, depth = 0) {
    if (value === null || value === undefined) return '<span class="output-empty">Not recorded</span>';
    if (typeof value !== "object") {
      if (typeof value === "string" && /^[\[{]/.test(value.trim())) {
        try { return render(JSON.parse(value), depth); } catch (_) { /* Text is not a complete JSON document. */ }
      }
      return `<p class="output-text">${escape(value)}</p>`;
    }
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="output-empty">None recorded</span>';
      return `<ol class="output-list">${value.map((item) => `<li>${render(item, depth + 1)}</li>`).join("")}</ol>`;
    }
    const entries = Object.entries(value).sort(([a], [b]) => {
      const rank = (key) => { const index = priority.indexOf(key); return index < 0 ? priority.length : index; };
      return rank(a) - rank(b);
    });
    if (!entries.length) return '<span class="output-empty">No fields recorded</span>';
    return entries.map(([key, item]) => {
      const title = escape(label(key));
      const content = render(item, depth + 1);
      return technical.test(key) || depth > 5
        ? `<details class="output-field output-field--details"><summary>${title}</summary><div>${content}</div></details>`
        : `<section class="output-field"><h3>${title}</h3>${content}</section>`;
    }).join("");
  }
  function document(value) {
    const result = value?.report?.response ?? value?.response ?? value?.item;
    if (result !== undefined && result !== null) {
      return render(result) + `<details class="output-field output-field--details"><summary>Provenance and execution record</summary><div>${render(value, 6)}</div></details>`;
    }
    return render(value);
  }
  root.ScisaurusOutputView = { render, document, label };
})(typeof window === "undefined" ? globalThis : window);
