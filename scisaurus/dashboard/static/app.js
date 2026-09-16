(() => {
  "use strict";

  const state = { snapshot: null, projects: [], projectRef: null, filter: "all", query: "", inventoryPage: 0 };
  const INVENTORY_PAGE_SIZE = 24;
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function number(value) {
    return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : "—";
  }

  function projectQuery() {
    return state.projectRef ? `&project=${encodeURIComponent(state.projectRef)}` : "";
  }

  function bytes(value) {
    const size = Number(value);
    if (!Number.isFinite(size)) return "—";
    if (size < 1024) return `${size} B`;
    if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
    return `${(size / 1024 ** 3).toFixed(1)} GB`;
  }

  function duration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return "—";
    if (value < 60) return `${Math.round(value)}s`;
    const minutes = Math.floor(value / 60);
    if (minutes < 60) return `${minutes}m ${Math.floor(value % 60)}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }

  function relativeDate(value) {
    if (!value) return "—";
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return text(value);
    const delta = Math.max(0, Date.now() - timestamp) / 1000;
    if (delta < 10) return "just now";
    if (delta < 60) return `${Math.floor(delta)}s ago`;
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return new Date(timestamp).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function statusClass(value) {
    return String(value || "unknown").toLowerCase().replaceAll(/[^a-z0-9_-]/g, "_");
  }

  function statusPill(value) {
    const status = text(value, "unknown").replaceAll("_", " ");
    return `<span class="state-pill state-pill--${statusClass(value)}">${escapeHtml(status)}</span>`;
  }

  function stageName(snapshot, id) {
    const stage = (snapshot?.pipeline?.stages || []).find((item) => item.id === id);
    return stage?.label || text(id);
  }

  function prettyPhase(snapshot, phase) {
    if (!phase) return "—";
    const [stage, detail] = String(phase).split(":", 2);
    const label = stageName(snapshot, stage);
    if (!detail) return label;
    return `${label} · ${detail.replaceAll("_", " ")}`;
  }

  function usageEntries(usage) {
    if (!usage || typeof usage !== "object") return [];
    return Object.entries(usage)
      .filter(([, value]) => value !== null && value !== undefined && typeof value !== "object")
      .slice(0, 7);
  }

  function renderHeader(snapshot) {
    const project = snapshot.project || {};
    const live = snapshot.live || {};
    const status = live.status || "unknown";
    $("#project-name").textContent = text(project.name, "Sci-saurus");
    $("#breadcrumb-name").textContent = text(project.name, "project");
    $("#objective").textContent = text(project.objective, "No objective recorded in the workflow.");
    $("#project-path").textContent = text(project.path);
    $("#hero-status").outerHTML = `<span class="state-pill state-pill--${statusClass(status)}" id="hero-status">${escapeHtml(status.replaceAll("_", " "))}</span>`;
    $("#hero-source").textContent = live.source_ref ? "LIVE CHECKPOINT" : "NO CHECKPOINT";
    $("#current-phase").textContent = prettyPhase(snapshot, live.phase || live.last_phase);
    $("#elapsed").textContent = duration(live.elapsed_seconds);
    $("#remaining").textContent = live.remaining_seconds === null || live.remaining_seconds === undefined
      ? "No deadline" : `${duration(live.remaining_seconds)} left`;
    const processes = snapshot.runtime?.processes || [];
    $("#process-state").textContent = processes.length ? `${processes.map((item) => `PID ${item.pid}`).join(" · ")}` : "not detected";
    $("#connection-status").textContent = "Connected";
    $("#connection-dot").classList.remove("is-error");
    $("#last-updated").textContent = `Updated ${relativeDate(snapshot.generated_at)}`;
  }

  function renderResearch(snapshot) {
    const research = snapshot.research || {};
    const topic = research.topic || {};
    const selection = research.selection || {};
    const experiment = research.experiment || {};
    const stages = research.stage_progress || [];
    const currentStage = snapshot.live?.current_stage;
    const currentStageLabel = currentStage ? stageName(snapshot, currentStage) : "no stage running";
    const title = topic.title || "Research direction not recorded";
    const question = topic.question || "The accepted research question has not been recorded in the current checkpoint.";
    $("#research-title").textContent = title;
    $("#research-question").textContent = question;
    $("#research-caption").textContent = `${text(topic.domain, "domain not recorded")} · ${currentStageLabel}`;

    const facts = [
      ["Phenomenon", topic.phenomenon],
      ["Mechanism", topic.mechanism],
      ["Scope", topic.scope],
      ["Selection", selection.status ? `${selection.status}${selection.maturity_score !== null && selection.maturity_score !== undefined ? ` · maturity ${selection.maturity_score}` : ""}` : null],
    ].filter(([, value]) => value !== null && value !== undefined && value !== "");
    $("#research-facts").innerHTML = facts.length ? facts.map(([label, value]) => `<div class="research-fact"><div class="research-fact-label">${escapeHtml(label)}</div><div class="research-fact-value">${escapeHtml(text(value))}</div></div>`).join("") : '<div class="tree-empty">No research brief recorded.</div>';

    const designRows = [
      ["Comparison", experiment.comparison],
      ["Measurement", experiment.measurement],
      ["Data regime", experiment.data_regime],
      ["Disconfirmation", experiment.disconfirmation_test],
    ].filter(([, value]) => value !== null && value !== undefined && value !== "");
    const experimentStatus = experiment.status === "unknown" ? "planned" : text(experiment.status, "not started");
    $("#experiment-status").textContent = experimentStatus.replaceAll("_", " ");
    $("#experiment-design").innerHTML = designRows.length ? designRows.map(([label, value]) => `<div class="research-row"><div class="research-row-label">${escapeHtml(label)}</div><div class="research-row-value">${escapeHtml(text(value))}</div></div>`).join("") : '<div class="tree-empty">No executable design recorded yet.</div>';

    const survey = research.survey || {};
    const coverage = survey.coverage || {};
    const priorSurvey = survey.last_result_status && survey.last_result_status !== survey.current_status
      ? `prior ${String(survey.last_result_status).replaceAll("_", " ")}` : "latest checkpoint";
    const gapState = survey.current ? text(survey.gap_state, "not assessed") : priorSurvey;
    const evidenceItems = [
      ["SURVEY GATE", text(survey.current_status, "not recorded").replaceAll("_", " "), priorSurvey],
      ["LAST RECORDED WORKS", number(coverage.unique_works), "literature map"],
      ["VERIFIED FULL TEXTS", number(coverage.verified_full_texts), "source spans"],
      ["GAP STATE", gapState, survey.current ? "current assessment" : "current survey is rebuilding"],
      ["RELEASE GATE", text(survey.release_status, "not recorded").replaceAll("_", " "), "human release remains required"],
    ];
    $("#research-evidence").innerHTML = evidenceItems.map(([label, value, note]) => `<div class="research-evidence-cell"><div class="research-evidence-label">${escapeHtml(label)}</div><strong>${escapeHtml(value)}</strong><div class="research-evidence-note">${escapeHtml(note)}</div></div>`).join("");

    $("#research-progress-caption").textContent = `${number(stages.filter((item) => item.status === "completed").length)} of ${number(stages.length)} gates complete`;
    if (!stages.length) {
      $("#research-progress").innerHTML = '<div class="loading-block">No stage progress recorded.</div>';
      return;
    }
    setGridColumns("#research-progress", "--research-progress-columns", stages.length, layoutPreference("research"));
    $("#research-progress").innerHTML = stages.map((stage, index) => {
      const currentStatus = stage.status || "unknown";
      const displayStatus = currentStatus === "unknown" ? "planned" : currentStatus;
      const lastResult = stage.last_result_status && stage.last_result_status !== currentStatus
        ? `<div class="research-progress-note">last result: ${escapeHtml(String(stage.last_result_status).replaceAll("_", " "))}</div>` : "";
      return `<article class="research-progress-card research-progress-card--${statusClass(currentStatus)}">
        <div class="research-progress-number">0${index + 1}</div>
        <div class="research-progress-name">${escapeHtml(text(stage.label, stage.id))}</div>
        <div class="research-progress-deliverable">${escapeHtml(text(stage.deliverable, "Stage result"))}</div>
        ${lastResult}
        <div class="research-progress-footer"><span>${statusPill(displayStatus)}</span><span>${stage.current ? "current gate" : escapeHtml(text(stage.gate, "acceptance gate"))}</span></div>
      </article>`;
    }).join("");
  }

  function renderMetrics(snapshot) {
    const pipeline = snapshot.pipeline || {};
    const counts = snapshot.counts || {};
    const execution = snapshot.execution || {};
    const roleAssignments = execution.role_assignments || {};
    const provider = execution.provider || {};
    $("#metric-pipeline").textContent = `${number(pipeline.completed)} / ${number(pipeline.total)}`;
    $("#metric-pipeline-note").textContent = `${Math.round((pipeline.completion_ratio || 0) * 100)}% of stages complete`;
    const activeSpecialists = snapshot.specialists_active ?? (snapshot.specialists || []).filter((item) => item.engaged || ["running", "queued", "awaiting_review", "started"].includes(item.status)).length;
    $("#metric-specialists").textContent = roleAssignments.limit
      ? `${number(roleAssignments.running)} / ${number(roleAssignments.limit)}` : number(activeSpecialists);
    const providerCapacity = provider.worker_capacity ?? provider.durable_window_capacity ?? provider.configured_capacity;
    $("#metric-specialists-note").textContent = providerCapacity
      ? `${number(provider.running_tasks)} / ${number(providerCapacity)} provider calls · ${number(roleAssignments.queued)} queued`
      : "logical assignments · provider capacity unavailable";
    $("#metric-tasks").textContent = number(counts.tasks ?? snapshot.tasks?.length);
    $("#metric-tasks-note").textContent = `${number(counts.active_tasks)} active · ledger records`;
    $("#metric-artifacts").textContent = number(counts.artifacts ?? snapshot.artifacts?.length);
    const integrity = snapshot.integrity?.databases || [];
    const statuses = integrity.map((item) => item.status);
    const integrityStatus = !integrity.length ? "not available" : statuses.every((item) => item === "ok") ? "verified" : statuses.some((item) => item === "failed") ? "failed" : "review needed";
    $("#metric-integrity").textContent = integrityStatus;
    $("#metric-integrity-note").textContent = integrity.length ? `${integrity.length} database${integrity.length === 1 ? "" : "s"} checked` : "no SQLite ledger discovered";
    $("#pipeline-caption").textContent = `${number(pipeline.completed)} of ${number(pipeline.total)} stages complete`;
  }

  function renderPipeline(snapshot) {
    const stages = snapshot.pipeline?.stages || [];
    if (!stages.length) {
      $("#pipeline-grid").innerHTML = '<div class="loading-block">No stage definitions found in the workflow.</div>';
      return;
    }
    setGridColumns("#pipeline-grid", "--pipeline-columns", stages.length, layoutPreference("pipeline"));
    $("#pipeline-grid").innerHTML = stages.map((stage, index) => {
      const active = stage.active_agents || [];
      const researchStage = (snapshot.research?.stage_progress || []).find((item) => item.id === stage.id) || {};
      const roles = active.length ? `${active.length} role slot${active.length === 1 ? "" : "s"}` : "no active roles";
      return `<article class="stage-card stage-card--${statusClass(stage.status)}">
        <div class="stage-number">0${index + 1}</div>
        <div class="stage-name">${escapeHtml(text(stage.label, stage.id))}</div>
        <div class="stage-deliverable">${escapeHtml(text(researchStage.deliverable, "stage result"))}</div>
        <div class="stage-status">${statusPill(stage.status)}</div>
        <div class="stage-footer"><span>${escapeHtml(roles)}</span><span>${number(stage.attempts)} attempt${stage.attempts === 1 ? "" : "s"}</span></div>
      </article>`;
    }).join("");
  }

  function qualifyRole(department, role) {
    if (!role) return "—";
    return String(role).includes(".") ? String(role) : `${department}.${role}`;
  }

  function compactRole(role) {
    const value = text(role, "—");
    return value.includes(".") ? value.split(".").pop() : value;
  }

  function balancedColumns(count, preferred) {
    const total = Math.max(1, Number(count) || 1);
    const maximum = Math.max(1, Math.min(total, Number(preferred) || 1));
    for (let columns = maximum; columns > 1; columns -= 1) {
      if (total % columns === 0) return columns;
    }
    return 1;
  }

  function setGridColumns(selector, property, count, preferred) {
    const element = $(selector);
    if (element) element.style.setProperty(property, balancedColumns(count, preferred));
  }

  function layoutPreference(kind) {
    const width = window.innerWidth;
    if (kind === "department") return width > 1180 ? 5 : 1;
    if (kind === "pipeline") return width >= 1181 ? 6 : width >= 590 ? 3 : 2;
    if (kind === "research") return width >= 1181 ? 6 : width >= 861 ? 3 : 2;
    return width >= 861 ? 2 : 1;
  }

  function renderDepartments(snapshot) {
    const organization = snapshot.organization || {};
    const agents = snapshot.specialists || [];
    let departments = Array.isArray(organization.departments) ? organization.departments : [];
    if (!departments.length) {
      const names = [...new Set(agents.map((item) => item.department).filter(Boolean))];
      departments = names.map((id) => ({ id, label: id }));
    }
    if (!departments.length) {
      $("#department-grid").innerHTML = '<div class="loading-block">No department manifest found.</div>';
      return;
    }
    setGridColumns("#department-grid", "--department-columns", departments.length, layoutPreference("department"));
    $("#department-grid").innerHTML = departments.map((department) => {
      const items = agents.filter((item) => item.department === department.id);
      const engaged = items.filter((item) => item.engaged).length;
      const failures = items.filter((item) => ["failed", "blocked", "rejected"].includes(item.status)).length;
      const chief = qualifyRole(department.id, department.chief);
      const adversary = qualifyRole(department.id, department.adversary);
      const rows = items.length ? items.map((item) => `<div class="agent-row agent-row--clickable" data-agent-role="${escapeHtml(item.role)}" tabindex="0" role="button" title="${escapeHtml(item.role)}">
        <span class="status-dot status-dot--${statusClass(item.status)}"></span>
        <span class="agent-row-name">${escapeHtml(text(item.label, compactRole(item.role)))}</span>
        <span class="agent-row-state">${escapeHtml(text(item.status, "idle"))}</span>
      </div>`).join("") : '<div class="tree-empty">No roster entries.</div>';
      return `<article class="department-card"><div class="department-header"><div><div class="department-name">${escapeHtml(text(department.label, department.id))}</div><div class="department-id mono">${escapeHtml(department.id)}</div></div><div class="department-stats"><strong>${number(engaged)}</strong> active<br><span>${number(failures)} flagged</span></div></div><div class="department-leads"><span>CHIEF <b>${escapeHtml(chief)}</b></span><span>ADVERSARY <b>${escapeHtml(adversary)}</b></span></div><div class="agent-list">${rows}</div></article>`;
    }).join("");
  }

  function renderSpecialists(snapshot) {
    const items = snapshot.specialists || [];
    const work = snapshot.recent_work || [];
    if (!work.length) {
      $("#specialist-body").innerHTML = '<tr><td colspan="5" class="empty-cell">No roster or assignment records found.</td></tr>';
      $("#specialist-caption").textContent = `${number(items.length)} roles · no recent assignment work`;
      return;
    }
    const active = items.filter((item) => item.engaged).length;
    const meta = snapshot.recent_work_meta || {};
    const suffix = meta.truncated ? " · bounded" : "";
    const provider = snapshot.execution?.provider || {};
    const providerCapacity = provider.worker_capacity ?? provider.durable_window_capacity ?? provider.configured_capacity;
    const providerSummary = providerCapacity ? ` · ${number(provider.running_tasks)} / ${number(providerCapacity)} provider calls` : "";
    $("#specialist-caption").textContent = `${number(items.length)} roles · ${number(active)} engaged${providerSummary} · ${number(work.length)} recent work${suffix}`;
    $("#specialist-body").innerHTML = work.map((item) => `<tr class="agent-table-row" data-agent-role="${escapeHtml(item.role)}" data-agent-task="${escapeHtml(item.task_id || "")}" tabindex="0" role="button" title="Inspect ${escapeHtml(item.role)} · ${escapeHtml(item.task_id || "task")}">
      <td><div class="table-role"><span class="status-dot status-dot--${statusClass(item.status)}"></span><span>${escapeHtml(text(item.label, item.role))}</span></div><div class="table-sub mono">${escapeHtml(text(item.role))}</div></td>
      <td><div>${escapeHtml(item.stage_id ? stageName(snapshot, item.stage_id) : "unassigned")}</div><div class="table-sub">${escapeHtml(text(item.focus, item.kind || "assignment"))}</div></td>
      <td><span class="task-id">${escapeHtml(text(item.task_id, "unassigned"))}</span><div class="table-sub">${item.attempt_number ? `attempt ${escapeHtml(item.attempt_number)}` : ""}${item.updated_at ? ` · ${escapeHtml(relativeDate(item.updated_at))}` : ""}</div></td>
      <td><div>${statusPill(item.status)}</div><div class="table-sub work-action">${escapeHtml(text(item.action, "work recorded"))}</div></td>
      <td><div class="response-state response-state--${statusClass(item.response_status)}">${escapeHtml(text(item.response_status))}</div><div class="table-sub">${escapeHtml(text(item.verifier || item.reviewer, "no independent reviewer"))}</div></td>
    </tr>`).join("");
  }

  function renderActivity(snapshot) {
    const items = (snapshot.logs || snapshot.activity || []).slice(0, 60);
    if (!items.length) {
      $("#activity-list").innerHTML = '<div class="loading-block">No durable activity recorded yet.</div>';
      return;
    }
    $("#activity-list").innerHTML = items.map((item) => `<div class="activity-item activity-item--${statusClass(item.status)}">
      <div class="activity-rail"></div>
      <div><div class="activity-title">${escapeHtml(text(item.title, "activity"))}</div><div class="activity-kicker">${escapeHtml(text(item.kind))}${item.stage_id ? ` · ${escapeHtml(stageName(snapshot, item.stage_id))}` : ""}${item.role ? ` · ${escapeHtml(item.role)}` : ""}</div><div class="activity-detail">${escapeHtml(text(item.detail, "No detail recorded."))}</div></div>
      <div class="activity-time">${escapeHtml(relativeDate(item.ts))}</div>
    </div>`).join("");
  }

  function resourceRows(entries) {
    if (!entries.length) return '<div class="resource-row"><span class="resource-key">state</span><span class="resource-value">not recorded</span></div>';
    return entries.map(([key, value]) => `<div class="resource-row"><span class="resource-key">${escapeHtml(key.replaceAll("_", " "))}</span><span class="resource-value">${escapeHtml(text(value))}</span></div>`).join("");
  }

  function renderResources(snapshot) {
    const resources = snapshot.resources || {};
    const usage = resources.usage || snapshot.live?.usage || {};
    const pools = resources.pools || [];
    const databases = snapshot.integrity?.databases || [];
    const execution = snapshot.execution || {};
    const roleAssignments = execution.role_assignments || {};
    const provider = execution.provider || {};
    const gates = databases.length ? databases.map((item) => `<div class="gate-row"><span class="status-dot status-dot--${item.status === "ok" ? "pass" : "warn"}"></span><span>${escapeHtml(item.root_key)} ledger · ${escapeHtml(item.status)}</span></div>`).join("") : '<div class="gate-row"><span class="status-dot status-dot--warn"></span><span>No ledger verification available</span></div>';
    const currentPool = provider.source_root ? pools.filter((pool) => pool.root_key === provider.source_root) : pools;
    const poolBlock = currentPool.slice(0, 1).map((pool) => `<div class="resource-card"><div class="resource-card-heading"><span>CURRENT RESOURCE POOL</span><strong>${escapeHtml(text(pool.root_key))}</strong></div><div class="resource-rows">${resourceRows(usageEntries(pool.usage))}</div></div>`).join("");
    const totalCapacity = provider.durable_window_capacity ?? provider.configured_capacity;
    const workerCapacity = provider.worker_capacity ?? provider.configured_worker_concurrency;
    const dispatchRows = [
      ["role assignments", `${number(roleAssignments.running)} running · ${number(roleAssignments.queued)} queued`],
      ["stage role limit", roleAssignments.limit === null || roleAssignments.limit === undefined ? "not recorded" : number(roleAssignments.limit)],
      ["provider calls", `${number(provider.running_tasks)} running · ${number(provider.awaiting_review_tasks)} awaiting review`],
      ["worker capacity", workerCapacity === null || workerCapacity === undefined ? "not recorded" : number(workerCapacity)],
      ["total call cap", totalCapacity === null || totalCapacity === undefined ? "not recorded" : number(totalCapacity)],
      ["capacity guard", provider.within_capacity === false ? "OVER CAPACITY" : "within capacity"],
    ];
    $("#resource-stack").innerHTML = `<div class="resource-card"><div class="resource-card-heading"><span>MISSION USAGE</span><strong>${usageEntries(usage).length ? "live" : "not recorded"}</strong></div><div class="resource-rows">${resourceRows(usageEntries(usage))}</div></div><div class="resource-card"><div class="resource-card-heading"><span>DISPATCH SEMANTICS</span><strong>${escapeHtml(text(provider.capacity_source, "not recorded"))}</strong></div><div class="resource-rows">${resourceRows(dispatchRows)}</div></div>${poolBlock}<div class="resource-card"><div class="resource-card-heading"><span>INTEGRITY GATES</span><strong>read-only</strong></div><div class="resource-rows">${gates}</div></div>`;
  }

  function renderStructure(snapshot) {
    const structure = snapshot.structure || {};
    const roots = structure.roots || [];
    const directories = structure.directories || [];
    const fileTotal = snapshot.files_meta?.total || 0;
    const visibleRoots = roots.filter((root) => !root.covered_by || root.files || root.directories);
    const displayRoots = visibleRoots.length ? visibleRoots : roots;
    $("#structure-caption").textContent = `${number(directories.length)} directories · ${number(fileTotal)} files indexed`;
    const coveredCount = Math.max(0, roots.length - displayRoots.length);
    const omitted = (structure.skipped_directories || []).map((item) => `/${item}`).join("  ");
    $("#structure-note").textContent = `${coveredCount ? `${number(coveredCount)} nested roots collapsed into the owning scan · ` : ""}${omitted} omitted from recursive file scan · previews and inventory are bounded for live polling.`;
    if (!roots.length) {
      $("#structure-grid").innerHTML = '<div class="loading-block">No project roots found.</div>';
      return;
    }
    setGridColumns("#structure-grid", "--structure-columns", displayRoots.length, layoutPreference("structure"));
    $("#structure-grid").innerHTML = displayRoots.map((root) => {
      const entries = directories.filter((item) => item.root_key === root.root_key).slice(0, 180);
      const tree = entries.length ? entries.map((item) => `<div class="tree-row" style="--depth:${Math.min(Math.max(Number(item.depth) || 0, 0), 8)}"><span class="tree-marker">/</span><span>${escapeHtml(item.path)}</span></div>`).join("") : `<div class="tree-empty">${root.covered_by ? `covered by ${escapeHtml(root.covered_by)} root scan` : "no directories discovered"}</div>`;
      return `<article class="structure-card"><div class="structure-card-header"><div><div class="structure-root">${escapeHtml(root.root_key)}</div><div class="structure-path mono">${escapeHtml(root.path)}</div></div><div class="structure-count">${number(root.files)} files<br>${number(root.directories)} dirs</div></div><div class="tree-list">${tree}</div></article>`;
    }).join("");
  }

  function inventoryItems(snapshot) {
    const checkpoints = (snapshot.checkpoints || []).map((item) => ({ ...item, kind: "checkpoint", displayKind: "checkpoint" }));
    const artifacts = (snapshot.artifacts || []).map((item) => ({ ...item, kind: "artifact", displayKind: item.artifact_type || "artifact", path: item.artifact_ref || item.logical_id, name: item.logical_id || item.artifact_ref, owner: item.owner || item.author }));
    const files = (snapshot.files || []).map((item) => ({ ...item, kind: "file", displayKind: item.kind || "file" }));
    return [...checkpoints, ...artifacts, ...files].sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")));
  }

  function renderInventory(snapshot) {
    const all = inventoryItems(snapshot);
    const query = state.query.trim().toLowerCase();
    const matching = all.filter((item) => {
      const matchesType = state.filter === "all" || item.kind === state.filter;
      const haystack = `${item.name || ""} ${item.path || ""} ${item.ref || ""} ${item.owner || ""}`.toLowerCase();
      return matchesType && (!query || haystack.includes(query));
    });
    const pageCount = Math.max(1, Math.ceil(matching.length / INVENTORY_PAGE_SIZE));
    state.inventoryPage = Math.min(state.inventoryPage, pageCount - 1);
    const start = state.inventoryPage * INVENTORY_PAGE_SIZE;
    const filtered = matching.slice(start, start + INVENTORY_PAGE_SIZE);
    $("#inventory-meta").textContent = `${number(matching.length)} matching records${snapshot.files_meta?.truncated ? " · file list capped" : ""}`;
    $("#inventory-page-meta").textContent = matching.length ? `${number(start + 1)}–${number(Math.min(start + filtered.length, matching.length))} of ${number(matching.length)}` : "0 records";
    $("#inventory-prev").disabled = state.inventoryPage === 0;
    $("#inventory-next").disabled = state.inventoryPage >= pageCount - 1;
    if (!filtered.length) {
      $("#inventory-list").innerHTML = '<div class="loading-block">No records match this filter.</div>';
      return;
    }
    $("#inventory-list").innerHTML = filtered.map((item, index) => `<div class="inventory-row">
      <div class="inventory-kind">${escapeHtml(text(item.displayKind, item.kind))}</div>
      <div class="inventory-main"><div class="inventory-name">${escapeHtml(text(item.name, item.logical_id || "unnamed"))}</div><div class="file-path">${escapeHtml(text(item.path, item.ref))}</div></div>
      <div class="inventory-owner">${escapeHtml(text(item.owner || item.author, "—"))}</div>
      <div class="inventory-date">${escapeHtml(relativeDate(item.updated_at || item.created_at))}</div>
      <div class="inventory-size">${escapeHtml(bytes(item.size ?? item.body_size_bytes))}</div>
      <button class="inventory-open" type="button" data-ref="${escapeHtml(item.ref || item.file_ref || "")}" data-index="${index}" ${item.ref || item.file_ref ? "" : "disabled"}>Inspect</button>
    </div>`).join("");
    $$(".inventory-open").forEach((button) => button.addEventListener("click", () => {
      const ref = button.dataset.ref;
      if (ref) openInspector(ref);
    }));
  }

  function render(snapshot) {
    state.snapshot = snapshot;
    renderHeader(snapshot);
    renderResearch(snapshot);
    renderMetrics(snapshot);
    renderPipeline(snapshot);
    renderDepartments(snapshot);
    renderSpecialists(snapshot);
    renderActivity(snapshot);
    renderResources(snapshot);
    renderStructure(snapshot);
    renderInventory(snapshot);
    $$("[data-agent-role]").forEach((element) => {
      const inspect = () => openAgentInspector(element.dataset.agentRole, element.dataset.agentTask || null);
      element.addEventListener("click", inspect);
      element.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          inspect();
        }
      });
    });
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 5000);
  }

  function renderProjects(data) {
    state.projects = Array.isArray(data?.projects) ? data.projects : [];
    const selectedRef = state.projectRef || ".";
    const selected = state.projects.find((item) => item.ref === selectedRef);
    $("#project-manager-status").textContent = `${number(state.projects.length)} project${state.projects.length === 1 ? "" : "s"} · ${text(selected?.name, "none")} selected`;
    $("#project-list").innerHTML = state.projects.length ? state.projects.map((project) => `<button class="project-item${project.ref === selectedRef ? " is-selected" : ""}" type="button" data-project-ref="${escapeHtml(project.ref)}">
      <span><strong>${escapeHtml(text(project.name, project.ref))}</strong><small>${escapeHtml(text(project.ref))}</small></span>
      <span class="project-item-status">${statusPill(project.status)}${project.pid ? `<small>PID ${escapeHtml(project.pid)}</small>` : ""}</span>
    </button>`).join("") : '<div class="loading-block">No managed Composer projects found.</div>';
    $$(".project-item").forEach((button) => button.addEventListener("click", () => {
      state.projectRef = button.dataset.projectRef === "." ? null : button.dataset.projectRef;
      closeProjectDialog();
      fetchSnapshot();
    }));
    const draft = selected?.status === "draft";
    $("#start-current").disabled = !draft;
    $("#resume-current").disabled = !selected || draft || selected.initialized !== true;
  }

  async function fetchProjects() {
    try {
      const response = await fetch(`/api/projects?ts=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`project list request failed (${response.status})`);
      renderProjects(await response.json());
    } catch (error) {
      $("#project-manager-status").textContent = "project list unavailable";
      $("#project-list").innerHTML = `<div class="loading-block">${escapeHtml(error.message)}</div>`;
    }
  }

  function closeProjectDialog() {
    const dialog = $("#project-dialog");
    if (dialog.open) dialog.close();
  }

  async function postAction(payload) {
    const response = await fetch("/api/actions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), cache: "no-store",
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error || `action failed (${response.status})`);
    return value;
  }

  async function runProject(resume) {
    try {
      const result = await postAction({ action: "start_composer", project: state.projectRef || ".", resume });
      showToast(result.status === "already_running" ? "This Composer is already running." : `Composer started · PID ${result.pid}`);
      await fetchProjects();
      await fetchSnapshot();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function createProject(startNow) {
    const form = $("#project-form");
    if (!form.reportValidity()) return;
    const formData = new FormData(form);
    try {
      const result = await postAction({
        action: "create_project", template: state.projectRef || ".",
        slug: formData.get("slug"), objective: formData.get("objective"),
        hard_seconds: Number(formData.get("hard_seconds")), start_now: startNow,
      });
      state.projectRef = result.project;
      form.reset();
      closeProjectDialog();
      showToast(startNow && result.start?.pid ? `Project created and started · PID ${result.start.pid}` : `Project created · ${result.project}`);
      await fetchProjects();
      await fetchSnapshot();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function fetchSnapshot() {
    try {
      const response = await fetch(`/api/snapshot?ts=${Date.now()}${projectQuery()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`snapshot request failed (${response.status})`);
      render(await response.json());
    } catch (error) {
      $("#connection-status").textContent = "Unavailable";
      $("#connection-dot").classList.add("is-error");
      $("#last-updated").textContent = "Waiting for the local server";
      if (state.snapshot) showToast(error.message);
      else $("#objective").textContent = `Unable to read the project snapshot: ${error.message}`;
    }
  }

  async function openInspector(ref) {
    const dialog = $("#inspector");
    $("#inspector-title").textContent = "Loading file";
    $("#inspector-meta").textContent = ref;
    $("#inspector-content").textContent = "Loading…";
    $("#inspector-raw").classList.add("is-disabled");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      const response = await fetch(`/api/file?ref=${encodeURIComponent(ref)}${projectQuery()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`file request failed (${response.status})`);
      const file = await response.json();
      $("#inspector-title").textContent = text(file.name, "File");
      $("#inspector-meta").textContent = `${text(file.root_key)} · ${text(file.path)} · ${bytes(file.size)} · ${relativeDate(file.updated_at)}`;
      $("#inspector-content").textContent = text(file.text, "No preview available.");
      $("#inspector-note").textContent = file.truncated ? "Preview truncated at 800 KB." : "Preview is bounded and read-only.";
      const raw = $("#inspector-raw");
      raw.href = `/api/raw?ref=${encodeURIComponent(ref)}${projectQuery()}`;
      raw.classList.remove("is-disabled");
    } catch (error) {
      $("#inspector-content").textContent = error.message;
      $("#inspector-note").textContent = "The file could not be read.";
    }
  }

  function inspectorRecord(file) {
    if (!file?.is_text) return null;
    try {
      const value = JSON.parse(file.text);
      return value && typeof value === "object" ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function assignmentRecord(record) {
    return record?.assignment_phase === "specialist" || record?.assignment_phase === "verifier";
  }

  function outputFileRef(snapshot, record) {
    const outputRef = record?.output_ref;
    if (typeof outputRef !== "string" || !outputRef) return null;
    if (outputRef.includes("::")) return outputRef;
    const linkedArtifact = (snapshot.artifacts || []).find((item) =>
      item.artifact_ref === outputRef || item.logical_id === outputRef);
    if (linkedArtifact?.file_ref) return linkedArtifact.file_ref;
    for (const root of snapshot.project?.roots || []) {
      const rootPath = String(root.path || "").replace(/\/+$/, "");
      if (!rootPath) continue;
      if (outputRef === rootPath) return `${root.key}::`;
      if (outputRef.startsWith(`${rootPath}/`)) {
        return `${root.key}::${outputRef.slice(rootPath.length + 1)}`;
      }
    }
    return null;
  }

  function recordedResponse(record) {
    if (!assignmentRecord(record)) return true;
    return record.outcome !== null && record.outcome !== undefined;
  }

  async function readInspectorFile(ref) {
    const response = await fetch(`/api/file?ref=${encodeURIComponent(ref)}${projectQuery()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`response artifact request failed (${response.status})`);
    return response.json();
  }

  async function openAgentInspector(role, taskId = null) {
    const snapshot = state.snapshot || {};
    const agent = (snapshot.specialists || []).find((item) => item.role === role) || { role };
    const roleTasks = (snapshot.tasks || []).filter((item) => item.role === role || item.payload?.assigned_role === role || item.payload?.role === role);
    const selectedTask = taskId ? roleTasks.find((item) => item.task_id === taskId) : roleTasks[0];
    const tasks = selectedTask ? [selectedTask, ...roleTasks.filter((item) => item !== selectedTask)] : roleTasks;
    const taskIds = new Set(tasks.map((item) => item.task_id));
    const attempts = (snapshot.attempts || []).filter((item) => taskIds.has(item.task_id) || item.lease_owner === role || item.payload?.assigned_role === role);
    const selectedLogicalId = selectedTask?.payload?.assignment_logical_id;
    const artifacts = (snapshot.artifacts || [])
      .filter((item) => item.author === role || item.owner === role || String(item.logical_id || "").includes(role))
      .filter((item) => item.file_ref)
      .sort((left, right) => {
        const leftMatch = selectedLogicalId && left.logical_id === selectedLogicalId ? 1 : 0;
        const rightMatch = selectedLogicalId && right.logical_id === selectedLogicalId ? 1 : 0;
        return rightMatch - leftMatch || String(right.created_at || "").localeCompare(String(left.created_at || ""));
      })
      .slice(0, 16);
    const dialog = $("#inspector");
    $("#inspector-title").textContent = text(agent.role, "Agent");
    $("#inspector-meta").textContent = `${text(agent.department)} · ${text(agent.appointment, "agent")} · ${text(agent.status, "idle")} · ${selectedTask?.payload?.stage_id ? stageName(snapshot, selectedTask.payload.stage_id) : (agent.stage_id ? stageName(snapshot, agent.stage_id) : "not assigned")} · ${text(selectedTask?.task_id, "no task selected")}`;
    $("#inspector-content").textContent = "Loading agent record…";
    $("#inspector-note").textContent = "Task, attempt, and response artifact are read-only.";
    const raw = $("#inspector-raw");
    raw.classList.add("is-disabled");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      let assignment = null;
      let selected = null;
      for (const artifact of artifacts) {
        let file;
        try {
          file = await readInspectorFile(artifact.file_ref);
        } catch (_error) {
          continue;
        }
        const record = inspectorRecord(file);
        if (assignmentRecord(record) && !assignment) assignment = { artifact, file, record };
        const linkedRef = outputFileRef(snapshot, record);
        if (linkedRef && linkedRef !== artifact.file_ref) {
          try {
            const responseFile = await readInspectorFile(linkedRef);
            selected = { artifact, file, record, responseFile, responseRef: linkedRef };
            break;
          } catch (_error) {
            // Keep inspecting other bounded artifact candidates.
          }
        }
        if (file.is_text && recordedResponse(record)) {
          selected = { artifact, file, record };
          break;
        }
      }

      let responseText;
      if (selected) {
        const displayFile = selected.responseFile || selected.file;
        if (selected.responseFile) {
          const parsedResponse = inspectorRecord(selected.responseFile);
          responseText = JSON.stringify({
            role: agent.role,
            assignment: selected.record,
            response_ref: selected.responseRef,
            response: parsedResponse || selected.responseFile.text,
          }, null, 2);
          raw.href = `/api/raw?ref=${encodeURIComponent(selected.responseRef)}${projectQuery()}`;
          $("#inspector-note").textContent = `Response file · ${text(selected.responseRef)}${displayFile.truncated ? " · preview truncated" : ""}`;
        } else {
          responseText = displayFile.text;
          raw.href = `/api/raw?ref=${encodeURIComponent(selected.artifact.file_ref)}${projectQuery()}`;
          $("#inspector-note").textContent = `${assignmentRecord(selected.record) ? "Latest result record" : "Latest response artifact"} · ${text(selected.artifact.artifact_ref)}${displayFile.truncated ? " · preview truncated" : ""}`;
        }
        raw.classList.remove("is-disabled");
      } else {
        responseText = JSON.stringify({
          role: agent.role,
          status: agent.status,
          task: tasks[0] || null,
          attempt: attempts[0] || null,
          assignment_record: assignment ? {
            artifact_ref: assignment.artifact.artifact_ref,
            artifact_type: assignment.artifact.artifact_type,
            file_ref: assignment.artifact.file_ref,
            record: assignment.record,
          } : null,
          response: "No response artifact has been recorded for this agent yet.",
        }, null, 2);
        $("#inspector-note").textContent = "No response artifact recorded yet; current task and assignment state are shown.";
      }
      $("#inspector-content").textContent = responseText || "No response content.";
    } catch (error) {
      $("#inspector-content").textContent = error.message;
      $("#inspector-note").textContent = "The response record could not be read.";
    }
  }

  function bindControls() {
    $("#refresh-button").addEventListener("click", fetchSnapshot);
    $("#projects-button").addEventListener("click", () => {
      const dialog = $("#project-dialog");
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      fetchProjects();
    });
    $("#project-dialog-close").addEventListener("click", closeProjectDialog);
    $("#project-dialog").addEventListener("click", (event) => {
      if (event.target === $("#project-dialog")) closeProjectDialog();
    });
    $("#start-current").addEventListener("click", () => runProject(false));
    $("#resume-current").addEventListener("click", () => runProject(true));
    $("#project-form").addEventListener("submit", (event) => {
      event.preventDefault();
      createProject(event.submitter?.dataset.start === "true");
    });
    $("#inventory-search").addEventListener("input", (event) => {
      state.query = event.target.value;
      state.inventoryPage = 0;
      if (state.snapshot) renderInventory(state.snapshot);
    });
    $$(".filter-button").forEach((button) => button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      state.inventoryPage = 0;
      $$(".filter-button").forEach((item) => item.classList.toggle("is-active", item === button));
      if (state.snapshot) renderInventory(state.snapshot);
    }));
    $("#inventory-prev").addEventListener("click", () => {
      state.inventoryPage = Math.max(0, state.inventoryPage - 1);
      if (state.snapshot) renderInventory(state.snapshot);
    });
    $("#inventory-next").addEventListener("click", () => {
      state.inventoryPage += 1;
      if (state.snapshot) renderInventory(state.snapshot);
    });
    $("#inspector-close").addEventListener("click", () => $("#inspector").close());
    $("#inspector").addEventListener("click", (event) => {
      if (event.target === $("#inspector")) $("#inspector").close();
    });
    $$(".nav-link").forEach((link) => link.addEventListener("click", () => {
      $$(".nav-link").forEach((item) => item.classList.toggle("is-active", item === link));
    }));
  }

  bindControls();
  fetchProjects();
  fetchSnapshot();
  window.setInterval(fetchSnapshot, 5000);
  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (state.snapshot) render(state.snapshot);
    }, 120);
  });
})();
