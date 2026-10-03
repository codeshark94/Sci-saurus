(() => {
  "use strict";

  const initialProject = new URLSearchParams(window.location.search).get("project");
  const state = {
    view: initialProject === null ? "workspace" : "project",
    inspectorRequest: 0,
    snapshot: null,
    workspace: null,
    projects: [],
    projectRef: initialProject === null ? null : (initialProject || "."),
    filter: "all",
    query: "",
    inventoryPage: 0,
    stageId: new URLSearchParams(window.location.search).get("stage"),
    topicPaperQuery: "",
    literatureQuery: "", literatureEvidence: "all", literatureOffset: 0, literatureRequest: 0,
    run: null, runBusy: false, runRequest: 0,
  };
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
    return state.view === "project" && state.projectRef
      ? `&project=${encodeURIComponent(state.projectRef)}` : "";
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
    const labels = {candidate_needs_review: "Review pending", research_expansion_required: "Research needed", awaiting_review: "Review pending", review_rejected: "Review rejected", waiting_for_provider: "Provider wait", deadline_reached: "Deadline reached"};
    const status = labels[value] || text(value, "unknown").replaceAll("_", " ");
    return `<span class="state-pill state-pill--${statusClass(value)}" title="${escapeHtml(text(value).replaceAll("_", " "))}">${escapeHtml(status)}</span>`;
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

  function projectStage(project) {
    const stage = (project?.stages || []).find((item) => item.current) ||
      (project?.stages || []).find((item) => item.status === "running");
    return stage?.label || (project?.current_stage ? text(project.current_stage).replaceAll("_", " ") : "not started");
  }

  function projectPhase(project) {
    const phase = project?.phase;
    if (!phase) return "no phase recorded";
    return String(phase).replaceAll("_", " ").replaceAll(":", " · ");
  }

  function projectName(ref) {
    const item = state.projects.find((project) => project.ref === ref) ||
      (state.workspace?.projects || []).find((project) => project.ref === ref);
    return item?.name || (ref === "." ? "autolab" : ref || "project");
  }

  function selectedProject() {
    return state.projects.find((project) => project.ref === state.projectRef) ||
      (state.workspace?.projects || []).find((project) => project.ref === state.projectRef) || null;
  }

  function renderCurrentProject() {
    const project = selectedProject();
    $("#current-project-name").textContent = projectName(state.projectRef);
    $("#current-project-meta").textContent = project
      ? `${text(project.status, "unknown")} · ${projectStage(project)}`
      : "loading project state";
  }

  const OPERATION_PAGES = {
    calls: ["mission-metrics", "calls", "activity"],
    specialists: ["specialists"],
    resources: ["resources"],
    inventory: ["structure", "inventory"],
  };

  function renderProjectPage() {
    const route = window.location.hash.replace(/^#/, "");
    const operation = Object.hasOwn(OPERATION_PAGES, route) ? route : null;
    const stagePage = !operation;
    $("#research-stage-view").hidden = !stagePage;
    $("#overview").hidden = !stagePage;
    $("#operations-view").hidden = stagePage;
    const visible = new Set(operation ? OPERATION_PAGES[operation] : []);
    new Set(Object.values(OPERATION_PAGES).flat()).forEach((id) => {
      document.getElementById(id).hidden = !visible.has(id);
    });
    $("#operations-status").hidden = !visible.has("activity") && !visible.has("resources");
    $$("#stage-navigation .stage-nav-item").forEach((button) => {
      const active = stagePage && button.dataset.stageId === state.stageId;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    $$("#project-nav .nav-link").forEach((link) => {
      const active = link.getAttribute("href") === `#${operation}`;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function navigateOperation(id) {
    if (!Object.hasOwn(OPERATION_PAGES, id)) return;
    closeInspector();
    const url = new URL(window.location.href);
    url.hash = id;
    window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
    renderProjectPage();
    window.scrollTo(0, 0);
  }

  function setView(view, projectRef = null) {
    state.view = view;
    state.projectRef = view === "project" ? (projectRef || ".") : null;
    const workspace = view === "workspace";
    $("#workspace-view").hidden = !workspace;
    $("#project-view").hidden = workspace;
    $("#project-nav").hidden = workspace;
    $("#sidebar-project-label").hidden = !workspace;
    $("#sidebar-project-list").hidden = !workspace;
    $("#workspace-link").classList.toggle("is-active", workspace);
    if (workspace) {
      $("#breadcrumb-context").textContent = "WORKSPACE";
      $("#breadcrumb-name").textContent = "Sci-saurus";
    } else {
      $("#breadcrumb-context").textContent = "PROJECT";
      $("#breadcrumb-name").textContent = projectName(state.projectRef);
    }
    $("#project-divider").hidden = workspace;
    const activeHash = window.location.hash.replace(/^#/, "") || "research";
    $$("#project-nav .nav-link").forEach((link) => {
      link.classList.toggle("is-active", view === "project" && link.getAttribute("href") === `#${activeHash}`);
    });
    renderCurrentProject();
    renderSidebar(state.workspace?.projects || state.projects);
    renderProjectPage();
  }

  function navigateWorkspace() {
    closeInspector();
    const url = new URL(window.location.href);
    url.searchParams.delete("project");
    url.hash = "workspace";
    window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
    setView("workspace");
    window.scrollTo(0, 0);
    fetchWorkspace();
  }

  function navigateProject(projectRef) {
    closeInspector();
    const ref = projectRef || ".";
    const url = new URL(window.location.href);
    url.searchParams.set("project", ref);
    url.searchParams.delete("stage");
    url.hash = "research";
    window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
    state.snapshot = null;
    state.run = null;
    renderRunControls();
    state.stageId = null; state.literatureOffset = 0; state.literatureQuery = "";
    state.literatureEvidence = "all";
    $("#literature-search").value = ""; $("#literature-evidence").value = "all";
    setView("project", ref);
    window.scrollTo(0, 0);
    fetchSnapshot();
    fetchProjects();
    fetchRun();
  }

  function renderSidebar(projects) {
    const list = $("#sidebar-project-list");
    if (!list) return;
    const items = Array.isArray(projects) ? projects : [];
    const selectedRef = state.view === "project" ? state.projectRef : null;
    list.innerHTML = items.length ? items.map((project) => `<button class="sidebar-project${project.ref === selectedRef ? " is-selected" : ""}" type="button" data-sidebar-project-ref="${escapeHtml(project.ref)}">
      <span class="sidebar-project-main"><span class="sidebar-project-name" title="${escapeHtml(text(project.name, project.ref))}">${escapeHtml(text(project.name, project.ref))}</span><span class="sidebar-project-meta" title="${escapeHtml(text(project.workflow_id, project.ref))}">${escapeHtml(text(project.workflow_id, project.ref === "." ? "current mission" : project.ref))}</span></span>
      <span class="sidebar-project-state">${statusPill(project.status)}</span>
    </button>`).join("") : '<div class="sidebar-empty">No projects</div>';
    $$("[data-sidebar-project-ref]").forEach((button) => button.addEventListener("click", () => navigateProject(button.dataset.sidebarProjectRef)));
  }

  function renderWorkspace(data) {
    state.workspace = data;
    const workspace = data.workspace || {};
    const summary = data.summary || {};
    const projects = Array.isArray(data.projects) ? data.projects : [];
    state.projects = projects;
    renderCurrentProject();
    $("#workspace-path").textContent = text(workspace.path);
    $("#workspace-managed-root").textContent = text(workspace.managed_root);
    $("#workspace-scope").textContent = `${number(summary.total_projects)} project${summary.total_projects === 1 ? "" : "s"} · ${number(summary.running_projects)} active`;
    $("#workspace-project-count").textContent = number(summary.total_projects);
    $("#workspace-running-count").textContent = number(summary.running_projects);
    $("#workspace-running-note").textContent = `${number(summary.processes)} Composer process${summary.processes === 1 ? "" : "es"}`;
    $("#workspace-draft-count").textContent = number(summary.draft_projects);
    $("#workspace-stage-count").textContent = number(summary.active_stages);
    $("#workspace-project-caption").textContent = `${number(summary.total_projects)} managed · ${number(summary.stale_projects)} stale · ${number(summary.failed_projects + summary.blocked_projects)} need attention`;
    const attention = Number(summary.stale_projects || 0) + Number(summary.failed_projects || 0) + Number(summary.blocked_projects || 0);
    const health = $("#workspace-health");
    health.className = `state-pill state-pill--${attention ? "pending" : "ok"}`;
    health.textContent = attention ? `${attention} NEED ATTENTION` : "CONNECTED";
    $("#workspace-updated").textContent = `Updated ${relativeDate(data.generated_at)}`;
    renderSidebar(projects);

    $("#workspace-project-list").innerHTML = projects.length ? projects.map((project) => `<button class="workspace-project-row" type="button" data-workspace-project-ref="${escapeHtml(project.ref)}">
      <span class="workspace-project-main"><span class="workspace-project-name">${escapeHtml(text(project.name, project.ref))}</span><span class="workspace-project-id mono">${escapeHtml(text(project.workflow_id, project.ref))}</span><span class="workspace-project-objective">${escapeHtml(text(project.objective, "No objective recorded."))}</span></span>
      <span class="workspace-project-stage"><span class="workspace-row-label">PHASE</span><strong>${escapeHtml(projectStage(project))}</strong><small>${escapeHtml(projectPhase(project))}</small></span>
      <span class="workspace-project-progress"><span class="workspace-row-label">SCHEDULED STAGES</span><strong>${number(project.completed_stages)} / ${number(project.total_stages)}</strong><span class="workspace-progress-bar"><i style="width:${Math.round((Number(project.progress_ratio) || 0) * 100)}%"></i></span><small>${relativeDate(project.last_updated)}</small></span>
      <span class="workspace-project-state">${statusPill(project.status)}${project.pid ? `<small>PID ${escapeHtml(project.pid)}</small>` : `<small>${escapeHtml(text(project.source, "not initialized"))}</small>`}</span>
    </button>`).join("") : '<div class="loading-block">No managed projects found.</div>';
    $("#workspace-run-list").innerHTML = data.active_runs?.length ? data.active_runs.map((project) => `<button class="workspace-run-card" type="button" data-workspace-project-ref="${escapeHtml(project.ref)}">
      <span class="workspace-run-heading"><span class="workspace-project-name">${escapeHtml(text(project.name, project.ref))}</span>${statusPill(project.status)}</span>
      <span class="workspace-run-phase"><strong>${escapeHtml(projectStage(project))}</strong><span>${escapeHtml(projectPhase(project))}</span></span>
      <span class="workspace-run-meta"><span>PID ${escapeHtml(project.pid || "not detected")}</span><span>${escapeHtml(duration(project.elapsed_seconds))} elapsed</span><span>${project.remaining_seconds === null || project.remaining_seconds === undefined ? "No deadline" : `${escapeHtml(duration(project.remaining_seconds))} left`}</span><span>${escapeHtml(text(project.source))}</span></span>
    </button>`).join("") : '<div class="workspace-empty">No active Composer runs. Draft projects remain available in the project list.</div>';
    const recent = Array.isArray(data.recent_projects) ? data.recent_projects : [];
    $("#workspace-recent-list").innerHTML = recent.length ? recent.map((project) => `<button class="workspace-recent-row" type="button" data-workspace-project-ref="${escapeHtml(project.ref)}">
      <span class="workspace-recent-main"><strong>${escapeHtml(text(project.name, project.ref))}</strong><span>${escapeHtml(text(project.workflow_id, project.ref))}</span></span>
      <span>${escapeHtml(projectStage(project))}</span><span>${statusPill(project.status)}</span><span>${escapeHtml(relativeDate(project.last_updated))}</span>
    </button>`).join("") : '<div class="workspace-empty">No checkpoint or workflow updates recorded.</div>';
    $$('[data-workspace-project-ref]').forEach((button) => button.addEventListener("click", () => navigateProject(button.dataset.workspaceProjectRef)));
  }

  function renderHeader(snapshot) {
    const project = snapshot.project || {};
    const live = snapshot.live || {};
    const status = live.status || "unknown";
    $("#project-name").textContent = text(project.name, "Sci-saurus");
    $("#breadcrumb-context").textContent = "PROJECT";
    $("#breadcrumb-name").textContent = text(project.name, "project");
    renderCurrentProject();
    $("#objective").textContent = text(project.objective, "No objective recorded in the workflow.");
    $("#project-path").textContent = text(project.path);
    $("#hero-status").outerHTML = `<span class="state-pill state-pill--${statusClass(status)}" id="hero-status">${escapeHtml(status.replaceAll("_", " "))}</span>`;
    $("#hero-source").textContent = live.source_ref ? "LIVE CHECKPOINT" : "NO CHECKPOINT";
    const phaseText = prettyPhase(snapshot, live.phase || live.last_phase);
    const currentActivity = live.current_activity;
    $("#current-phase").textContent = currentActivity?.label && !phaseText.includes(currentActivity.label)
      ? `${phaseText} · ${currentActivity.label}` : phaseText;
    $("#elapsed").textContent = duration(live.elapsed_seconds);
    $("#remaining").textContent = live.remaining_seconds === null || live.remaining_seconds === undefined
      ? "No deadline" : `${duration(live.remaining_seconds)} left`;
    const processes = (snapshot.runtime?.processes || []).filter((item) => item.owns_execution === true);
    $("#process-state").textContent = processes.length ? `${processes.map((item) => `PID ${item.pid}`).join(" · ")}` : "not detected";
    $("#connection-status").textContent = "Connected";
    $("#connection-dot").classList.remove("is-error");
    $("#last-updated").textContent = `Updated ${relativeDate(snapshot.generated_at)}`;
  }

  const STAGE_LABELS = {topic_discovery: "Topic discovery", survey: "Literature", experiment: "Experiment", interpretation: "Interpretation", argument: "Argument", paper: "Paper and PDF"};
  const EVIDENCE_LABELS = {full_text: "Verified full text", abstract_only: "Abstract only", no_abstract: "No abstract", unknown: "Unconfirmed"};
  const STAGE_STATUS = {completed: "Complete", running: "Running", retrying: "Repairing", paused: "Paused", blocked: "Blocked", candidate_needs_review: "Review held", review_rejected: "Rejected", research_expansion_required: "More research required", not_started: "Not started", not_scheduled: "Not scheduled", unknown: "Unknown", planned: "Not started"};

  function stageLabel(stage) { return STAGE_LABELS[stage.kind] || stage.label || stage.id; }
  function selectedStage(snapshot) {
    const stages = snapshot.stage_results || snapshot.pipeline?.stages || [];
    return stages.find((stage) => stage.id === state.stageId) ||
      stages.find((stage) => stage.id === snapshot.live?.current_stage) || stages[0];
  }
  function inspectionButton(label, output) {
    const ref = typeof output === "string" ? output : output?.file_ref;
    return ref?.includes("::") ? `<button class="button button--quiet" type="button" data-output-title="${escapeHtml(typeof output === "object" && output?.label ? window.ScisaurusOutputView.label(output.label.replace(/\.json$/, "")) : label)}" data-output-ref="${escapeHtml(ref)}">${escapeHtml(label)}</button>` : "";
  }
  function bindOutputInspection(container) {
    container.querySelectorAll("[data-output-ref]").forEach((button) => button.addEventListener("click", () => openInspector(button.dataset.outputRef, button.dataset.outputTitle)));
  }
  function selectStage(id, updateUrl = true) {
    state.stageId = id;
    if (updateUrl) {
      const url = new URL(window.location.href); url.searchParams.set("stage", id); url.hash = "stage-workspace";
      window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
    if (state.snapshot) renderStageResults(state.snapshot);
    renderProjectPage();
    window.scrollTo(0, 0);
  }
  function renderStageResults(snapshot) {
    const stages = snapshot.stage_results || snapshot.pipeline?.stages || [];
    const stage = selectedStage(snapshot);
    if (!stage) return;
    state.stageId = stage.id;
    $("#stage-navigation").innerHTML = stages.map((item,index) => `<button type="button" class="stage-nav-item${item.id === stage.id ? " is-active" : ""}" data-stage-id="${escapeHtml(item.id)}"><span class="nav-index">${String(index + 1).padStart(2, "0")}</span><span>${escapeHtml(stageLabel(item))}</span><span class="stage-nav-mark ${item.status === "completed" ? "is-complete" : ""}" aria-label="${escapeHtml(STAGE_STATUS[item.status] || text(item.status))}"></span></button>`).join("");
    $$('[data-stage-id]').forEach((button) => button.addEventListener("click", () => selectStage(button.dataset.stageId)));
    $("#stage-heading").textContent = stageLabel(stage);
    $("#stage-caption").textContent = stage.kind === "topic_discovery" && stage.status === "completed" ? "Candidate selected" : STAGE_STATUS[stage.status] || text(stage.status);
    const review = stage.review || {};
    const decision = review.decision;
    const decisions = {accept: "Accepted", pass: "Passed", hold: "Held", repair: "Repair required", reject: "Rejected"};
    $("#stage-result-summary").innerHTML = `<article class="stage-summary-card"><div class="research-label">Execution result</div><strong>${escapeHtml(STAGE_STATUS[stage.status] || text(stage.status))}</strong><p>${escapeHtml(text(stage.deliverable, "Inspect the actual outputs and independent verdict for this stage."))}</p></article><article class="stage-summary-card"><div class="research-label">Independent review</div><strong>${escapeHtml((stage.kind === "topic_discovery" && decision === "accept" ? "Admitted to literature survey" : decisions[decision]) || (decision ? text(decision) : "Not reviewed"))}</strong><p>${escapeHtml(text(review.summary || review.rationale, "The review will record its evidence and outstanding requirements."))}</p>${inspectionButton("Read review", review)}</article>`;
    bindOutputInspection($("#stage-result-summary"));
    const outputs = stage.outputs || [];
    $("#stage-result-outputs").innerHTML = `<div class="section-heading"><h3>Stage outputs</h3><span class="section-caption">${outputs.length} outputs</span></div>` + (outputs.length ? `<div class="stage-output-grid">${outputs.map((output) => `<article class="stage-output-card"><div><strong>${escapeHtml(window.ScisaurusOutputView.label((output.label || "Output").replace(/\.json$/, "")))}</strong><span>${output.current === false ? "Historical output · not current evidence" : escapeHtml(text(output.status, "Produced"))}</span></div>${inspectionButton("Open",output)}</article>`).join("")}</div>` : '<div class="stage-empty">No output has been produced for this stage yet.</div>');
    bindOutputInspection($("#stage-result-outputs"));
    const obligations = stage.open_obligations || [];
    $("#stage-obligations").hidden = !obligations.length;
    $("#stage-obligation-count").textContent = `${obligations.length}`;
    $("#stage-obligation-list").innerHTML = obligations.map((item) => {
      const requirement = typeof item === "string" ? item : item.requirement || item.objective;
      const owner = stages.find((candidate) => candidate.id === item.target_stage_id);
      return `<article class="stage-obligation"><span>${escapeHtml(owner ? stageLabel(owner) : text(item.target_stage_id, "This stage"))}</span><p>${escapeHtml(text(requirement))}</p>${item.completion_check || item.success_condition ? `<small>Completion check: ${escapeHtml(item.completion_check || item.success_condition)}</small>` : ""}</article>`;
    }).join("");
    $("#research").hidden = stage.kind !== "topic_discovery";
    $("#topic-papers").hidden = stage.kind !== "topic_discovery";
    if (stage.kind === "topic_discovery") renderTopicPapers(snapshot.topic_papers || {});
    const argument = $("#argument-defense-panel");
    if (argument.parentElement !== $("#stage-argument-context")) $("#stage-argument-context").appendChild(argument);
    $("#stage-argument-context").hidden = stage.kind !== "argument";
    $("#literature-results").hidden = stage.kind !== "survey";
    if (stage.kind === "survey") {
      renderLiteratureOverview(snapshot.literature || {});
      fetchLiterature();
    }
  }
  function renderTopicPapers(data) {
    const query = state.topicPaperQuery.toLowerCase();
    const papers = (data.items || []).filter((item)=>[item.title,item.doi,item.work_id].some((value)=>String(value || "").toLowerCase().includes(query)));
    const summary = data.search_summary || {};
    $("#topic-papers-count").textContent = `${papers.length} of ${data.total || 0} retained sources`;
    $("#topic-search-summary").textContent = summary.query_count !== undefined ? `${summary.query_count} searches · ${summary.returned_work_count} returned records · ${summary.keyword_matched_work_count} keyword matches · ${data.total} retained excerpts` : "Discovery excerpts retained in the stage output";
    $("#topic-paper-list").innerHTML = papers.length ? papers.map((item)=>`<article class="literature-paper"><div class="literature-paper-heading"><div><h3>${escapeHtml(text(item.title,item.work_id))}</h3><div class="literature-paper-meta">${escapeHtml([item.year,item.work_id,item.doi].filter(Boolean).join(" · "))}</div></div><span class="evidence-badge">${item.abstract ? "Abstract excerpt" : "Metadata only"}</span></div><div class="literature-paper-status"><span>${escapeHtml((item.origins || []).join(" · ").replaceAll("_"," "))}</span></div>${item.abstract ? `<details class="paper-abstract"><summary>Read abstract excerpt</summary><p>${escapeHtml(item.abstract)}</p><small>Stored discovery excerpt; completeness is not certified.</small></details>` : ""}<div class="literature-paper-actions"><button class="button button--quiet" type="button" data-topic-paper-id="${escapeHtml(item.work_id)}">Details and provenance</button>${inspectionButton("Discovery output",item.output_file_ref)}</div></article>`).join("") : '<div class="stage-empty">No discovery papers match this filter.</div>';
    bindOutputInspection($("#topic-paper-list"));
    $$('[data-topic-paper-id]').forEach((button)=>button.addEventListener("click",()=>{
      beginInspector();
      const item = (data.items || []).find((paper)=>paper.work_id === button.dataset.topicPaperId);
      $("#inspector-title").textContent = text(item.title,item.work_id); $("#inspector-meta").textContent = "Topic discovery source";
      setInspectorContent(JSON.stringify(item,null,2)); $("#inspector-note").textContent = "Discovery metadata and stored excerpt; not a full-text or literature-review verdict.";
      setInspectorRaw(null); $("#inspector").showModal();
    }));
  }
  function renderLiteratureOverview(data) {
    $("#literature-source").textContent = data.status === "not_started" ? "Survey not started" : text(data.source_root, "Source repository not recorded");
    const counts = data.counts || {};
    $("#literature-counts").innerHTML = [["Captured papers",data.total ?? 0],...Object.entries(EVIDENCE_LABELS).map(([key,label]) => [label,counts[key] ?? 0])].map(([label,value]) => `<div class="literature-count"><span>${escapeHtml(label)}</span><strong>${number(value)}</strong></div>`).join("");
    const outputs = [["Literature synthesis",data.survey_output || data.survey_ref,data.survey_current],["Gap assessment",data.gap_output || data.gap_ref,data.gap_current]];
    $("#literature-synthesis").innerHTML = outputs.map(([label,output,current]) => `<div class="literature-synthesis-card"><strong>${label}</strong><span>${output?.file_ref || output?.artifact_ref ? (current ? "Current output" : "Historical output · review required") : "Not produced"}</span>${inspectionButton("Open result",output)}</div>`).join("") + (data.notices || []).map((notice) => `<p class="stage-notice">${escapeHtml(typeof notice === "string" ? notice : notice.message || JSON.stringify(notice))}</p>`).join("");
    bindOutputInspection($("#literature-synthesis"));
  }
  async function fetchLiterature() {
    if (state.view !== "project" || selectedStage(state.snapshot || {})?.kind !== "survey") return;
    const request = ++state.literatureRequest;
    const params = new URLSearchParams({project:state.projectRef || ".",q:state.literatureQuery,evidence:state.literatureEvidence,offset:String(state.literatureOffset),limit:"25"});
    try {
      const response = await fetch(`/api/literature?${params}`,{cache:"no-store"});
      if (!response.ok) throw new Error(`Unable to read the literature list (${response.status})`);
      const data = await response.json();
      if (request !== state.literatureRequest || selectedStage(state.snapshot || {})?.kind !== "survey") return;
      renderLiteratureOverview(data);
      const items = data.items || [];
      $("#discovery-source-link").hidden = !(state.snapshot?.topic_papers?.total > 0 && data.status === "not_started");
      $("#discovery-source-link").textContent = `View ${state.snapshot?.topic_papers?.total || 0} discovery sources`;
      $("#literature-list").innerHTML = items.length ? items.map((item) => `<article class="literature-paper"><div class="literature-paper-heading"><div><h3>${escapeHtml(text(item.title,item.work_id))}</h3><div class="literature-paper-meta">${escapeHtml([item.year,item.work_id,item.doi].filter(Boolean).join(" · "))}</div></div><span class="evidence-badge evidence-badge--${statusClass(item.evidence_status)}">${escapeHtml(EVIDENCE_LABELS[item.evidence_status] || "Unconfirmed")}</span></div><div class="literature-paper-status"><span>Access: ${escapeHtml(text(item.access_status,"Not recorded"))}</span><span>Evidence review: ${escapeHtml(text(item.review?.outcome || item.review?.status,"Not reviewed"))}</span><span>Source fidelity: ${item.review?.source_fidelity_checks?.length ? `${item.review.source_fidelity_checks.length} checks recorded` : "Not reviewed"}</span><span>Question relevance: ${item.review?.question_relevance_checks?.length ? `${item.review.question_relevance_checks.length} checks recorded` : "Not reviewed"}</span></div><div class="literature-paper-actions">${inspectionButton("Abstract",(item.abstracts || []).find((entry)=>entry.file_ref) || item.abstract_ref)}${inspectionButton("Full text",(item.full_texts || []).find((entry)=>entry.identity_verified && entry.file_ref) || item.full_text_ref)}${inspectionButton("Analysis",item.analysis_output || item.analysis_ref)}${inspectionButton("Evidence review",item.review_output || item.review_ref)}<button class="button button--quiet" type="button" data-literature-id="${escapeHtml(item.work_id)}">Details and lineage</button></div></article>`).join("") : `<div class="stage-empty">${data.status === "not_started" ? "The literature survey has not started. Captured papers, abstracts, full texts, and reviews will appear here." : "No papers match these filters."}</div>`;
      bindOutputInspection($("#literature-list"));
      $$('[data-literature-id]').forEach((button)=>button.addEventListener("click",()=>openLiteratureDetail(button.dataset.literatureId)));
      $("#literature-page-meta").textContent = items.length ? `${data.offset+1}–${data.offset+items.length} / ${number(data.filtered_total)} papers` : "0 papers";
      $("#literature-prev").disabled = data.offset <= 0;
      $("#literature-next").disabled = !data.has_more;
    } catch(error) {
      if (request !== state.literatureRequest) return;
      $("#literature-list").innerHTML = `<div class="stage-empty stage-error">${escapeHtml(error.message)}</div>`;
      $("#literature-prev").disabled = true; $("#literature-next").disabled = true;
    }
  }
  async function openLiteratureDetail(workId) {
    const request = beginInspector();
    const dialog = $("#inspector");
    $("#inspector-title").textContent = workId; $("#inspector-meta").textContent = "Paper and evidence lineage";
    setInspectorContent("Loading…"); $("#inspector-note").textContent = "Captured evidence and review records";
    setInspectorRaw(null); dialog.showModal();
    try {
      const response = await fetch(`/api/literature/detail?work_id=${encodeURIComponent(workId)}${projectQuery()}`,{cache:"no-store"});
      if (!response.ok) throw new Error(`Unable to read the paper record (${response.status})`);
      const record = await response.json();
      if (request !== state.inspectorRequest) return;
      $("#inspector-title").textContent = text(record.item?.title,workId);
      setInspectorContent(JSON.stringify(record,null,2));
    } catch(error) { if (request === state.inspectorRequest) setInspectorContent(error.message); }
  }

  function renderResearch(snapshot) {
    const research = snapshot.research || {};
    const topic = research.topic || {};
    const selection = research.selection || {};
    const experiment = research.experiment || {};
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

    $("#research-logic-caption").textContent = research.research_program || research.argument_defense
      ? "provisional routing · evidence posture · reviewer tests"
      : "awaiting topic and argument artifacts";
    renderResearchProgram(research.research_program);
    renderArgumentDefense(research.argument_defense);

  }

  function renderResearchProgram(program) {
    const status = $("#research-program-status");
    const summary = $("#research-program-summary");
    const list = $("#research-branch-list");
    if (!program) {
      status.textContent = "not recorded";
      summary.innerHTML = '<div class="research-empty-state">No provisional research program is attached to this checkpoint.</div>';
      list.innerHTML = "";
      return;
    }
    const branches = Array.isArray(program.branches) ? program.branches : [];
    const visibleBranches = branches.slice(0, 8);
    const branchTotal = Number.isFinite(Number(program.branch_count)) ? Number(program.branch_count) : branches.length;
    const selected = program.selected_branch || branches.find((item) => item.id === program.selected_id);
    const outcomes = Array.isArray(selected?.paper_if) ? selected.paper_if : [];
    status.textContent = number(program.branch_count ?? branches.length) + " branches · " + text(program.selection_mode, "provisional");
    summary.innerHTML = selected
      ? '<div class="research-logic-kicker">SELECTED ROUTE · ' + escapeHtml(text(selected.id)) + '</div>' +
        '<strong class="research-logic-title">' + escapeHtml(text(selected.title, "Selected branch")) + '</strong>' +
        '<p class="research-logic-question">' + escapeHtml(text(selected.question)) + '</p>' +
        '<div class="research-outcome-grid">' + outcomes.map((outcome) =>
          '<div class="research-outcome">' +
            '<span class="outcome-chip outcome-chip--' + statusClass(outcome.id) + '">' +
              escapeHtml(String(outcome.id || "outcome").replaceAll("_", " ")) +
            '</span>' +
            '<p>' + escapeHtml(text(outcome.condition)) + '</p>' +
          '</div>'
        ).join("") + '</div>' +
        '<div class="research-kill"><span>KILL CONDITION</span><p>' +
          escapeHtml(text(selected.kill_if)) + '</p></div>'
      : '<div class="research-empty-state">Program has no selected branch.</div>';
    list.innerHTML = branches.length
      ? '<div class="research-branch-heading"><span>RETAINED / SELECTED BRANCHES</span><span>' +
        number(program.retained_count ?? 0) + ' retained</span></div>' +
        visibleBranches.map((branch) => {
          const selectedClass = branch.id === program.selected_id ? " research-branch--selected" : "";
          return '<div class="research-branch' + selectedClass + '">' +
            '<div class="research-branch-top"><span class="research-branch-id">' +
              escapeHtml(text(branch.id)) + '</span>' + statusPill(branch.status) + '</div>' +
            '<strong>' + escapeHtml(text(branch.title, "Untitled branch")) + '</strong>' +
            '<p>' + escapeHtml(text(branch.question)) + '</p>' +
            '<div class="research-branch-meta"><span>' + escapeHtml(text(branch.research_form)) +
              '</span><span>' + escapeHtml(text(branch.comparison_type)) + '</span></div>' +
          '</div>';
        }).join("") + '</div>' +
        (branchTotal > visibleBranches.length
          ? '<div class="research-more">Showing ' + number(visibleBranches.length) + ' of ' + number(branchTotal) + ' branches · additional records remain in the artifact inspector.</div>'
          : "")
      : '<div class="research-empty-state">No branches recorded.</div>';
  }

  function renderArgumentDefense(defense) {
    const status = $("#argument-defense-status");
    const summary = $("#argument-defense-summary");
    const claimsList = $("#argument-claim-list");
    const weakList = $("#argument-weak-list");
    if (!defense) {
      status.textContent = "not recorded";
      summary.innerHTML = '<div class="research-empty-state">No claim posture ledger is attached yet. It is created with the argument stage.</div>';
      claimsList.innerHTML = "";
      weakList.innerHTML = "";
      return;
    }
    const claims = Array.isArray(defense.claim_postures) ? defense.claim_postures : [];
    const weakPoints = Array.isArray(defense.weak_points) ? defense.weak_points : [];
    const visibleClaims = claims.slice(0, 8);
    const visibleWeakPoints = weakPoints.slice(0, 6);
    const claimTotal = Number.isFinite(Number(defense.claim_count)) ? Number(defense.claim_count) : claims.length;
    const weakPointTotal = Number.isFinite(Number(defense.weak_point_count)) ? Number(defense.weak_point_count) : weakPoints.length;
    const postureCounts = claims.reduce((counts, item) => {
      const key = item.posture || "unknown";
      counts[key] = (counts[key] || 0) + 1;
      return counts;
    }, {});
    status.textContent = number(defense.claim_count ?? claims.length) + " claims · " +
      number(defense.weak_point_count ?? weakPoints.length) + " weak points";
    summary.innerHTML = '<div class="defense-counts">' +
      Object.entries(postureCounts).map(([key, value]) =>
        '<span><b>' + number(value) + '</b>' + escapeHtml(key.replaceAll("_", " ")) + '</span>'
      ).join("") + '</div>' +
      '<p class="research-logic-note">Results accepts observed claims only. Interpretive claims stay bounded to Discussion, Limitations, or Future Work.</p>';
    claimsList.innerHTML = claims.length
      ? '<div class="research-branch-heading"><span>CLAIM POSTURES</span><span>evidence-bound</span></div>' +
        visibleClaims.map((claim) =>
          '<div class="research-claim">' +
            '<div class="research-claim-top"><span class="outcome-chip outcome-chip--' +
              statusClass(claim.posture) + '">' + escapeHtml(String(claim.posture || "unknown").replaceAll("_", " ")) +
              '</span><span class="research-claim-id">' + escapeHtml(text(claim.id)) + '</span></div>' +
            '<strong>' + escapeHtml(text(claim.claim, "Claim not recorded")) + '</strong>' +
            '<div class="research-claim-meta"><span>evidence: ' +
              escapeHtml((claim.evidence_ids || []).join(", ") || "none") + '</span><span>allowed: ' +
              escapeHtml((claim.allowed_sections || []).join(", ") || "none") + '</span></div>' +
            '<p>' + escapeHtml(text(claim.caveat, "Caveat not recorded")) + '</p>' +
          '</div>'
        ).join("") +
        (claimTotal > visibleClaims.length
          ? '<div class="research-more">Showing ' + number(visibleClaims.length) + ' of ' + number(claimTotal) + ' claims · additional records remain in the artifact inspector.</div>'
          : "")
      : '<div class="research-empty-state">No claims recorded.</div>';
    weakList.innerHTML = weakPoints.length
      ? '<div class="research-branch-heading"><span>OPEN WEAK POINTS</span><span>reviewer tests</span></div>' +
        visibleWeakPoints.map((item) =>
          '<div class="research-weak-point">' +
            '<div class="research-claim-top"><span class="outcome-chip outcome-chip--' +
              statusClass(item.defense_strategy) + '">' +
              escapeHtml(String(item.defense_strategy || "review").replaceAll("_", " ")) +
              '</span><span class="research-claim-id">' + escapeHtml(text(item.id)) + '</span></div>' +
            '<strong>' + escapeHtml(text(item.weak_point, "Weak point not recorded")) + '</strong>' +
            '<p>' + escapeHtml(text(item.reviewer_test, "Reviewer test not recorded")) + '</p>' +
          '</div>'
        ).join("") +
        (weakPointTotal > visibleWeakPoints.length
          ? '<div class="research-more">Showing ' + number(visibleWeakPoints.length) + ' of ' + number(weakPointTotal) + ' weak points · additional records remain in the artifact inspector.</div>'
          : "")
      : '<div class="research-empty-state research-empty-state--clear">No open weak points recorded.</div>';
  }

  function renderMetrics(snapshot) {
    const counts = snapshot.counts || {};
    const execution = snapshot.execution || {};
    const provider = execution.provider || {};
    const modelCalls = snapshot.model_calls || {};
    const recordedCalls = modelCalls.total ?? modelCalls.items?.length ?? 0;
    const activeCalls = modelCalls.active ?? (modelCalls.items || []).filter((item) => ["running", "queued", "started"].includes(item.state)).length;
    const providerCapacity = provider.worker_capacity ?? provider.durable_window_capacity ?? provider.configured_capacity;
    $("#metric-calls").textContent = number(activeCalls);
    $("#metric-calls-note").textContent = providerCapacity
      ? `${number(providerCapacity)} worker slots · ${number(recordedCalls)} recorded`
      : `${number(recordedCalls)} recorded · capacity unavailable`;
    $("#metric-tasks").textContent = number(counts.tasks ?? snapshot.tasks?.length);
    $("#metric-tasks-note").textContent = `${number(counts.active_tasks)} active · ledger records`;
    $("#metric-artifacts").textContent = number(counts.artifacts ?? snapshot.artifacts?.length);
    const integrity = snapshot.integrity?.databases || [];
    const statuses = integrity.map((item) => item.status);
    const integrityStatus = !integrity.length ? "not available" : statuses.every((item) => item === "ok") ? "verified" : statuses.some((item) => item === "failed") ? "failed" : "review needed";
    $("#metric-integrity").textContent = integrityStatus;
    $("#metric-integrity-note").textContent = integrity.length ? `${integrity.length} database${integrity.length === 1 ? "" : "s"} checked` : "no SQLite ledger discovered";
  }

  function modelCallUsage(call) {
    const usage = call.usage || {};
    const parts = [];
    if (Number.isFinite(Number(usage.input_tokens))) parts.push(`${number(usage.input_tokens)} in`);
    if (Number.isFinite(Number(usage.output_tokens))) parts.push(`${number(usage.output_tokens)} out`);
    return parts.length ? parts.join(" · ") : "usage pending";
  }

  function shortTaskId(value) {
    const task = text(value, "not recorded");
    return task.length > 27 ? `${task.slice(0, 12)}…${task.slice(-12)}` : task;
  }

  function renderProviderWork(snapshot) {
    const work = snapshot.provider_work || {};
    const items = Array.isArray(work.live_items)
      ? work.live_items
      : (Array.isArray(work.items) ? work.items : []);
    const running = Number(work.running) || 0;
    const queued = Number(work.queued) || 0;
    const reviewPending = Number(work.review_pending) || 0;
    const suffix = work.truncated ? " · live list capped" : "";
    $("#provider-work-caption").textContent = `${number(work.active ?? items.length)} active · ${number(running)} running · ${number(queued)} queued${reviewPending ? ` · ${number(reviewPending)} review` : ""}${suffix}`;

    const renderCard = (item) => {
      const stateValue = item.state || "unknown";
      const timing = ["running", "queued", "started", "proposed"].includes(stateValue)
        ? `${duration(item.elapsed_seconds)} elapsed`
        : text(item.response_status, "state recorded");
      const target = item.target ? `<span><small>TARGET</small><code>${escapeHtml(item.target)}</code></span>` : "";
      return `<article class="execution-work-card execution-work-card--${statusClass(stateValue)}">
        <div class="execution-work-top">
          <div class="execution-work-heading">
            <div class="execution-work-provider">${escapeHtml(text(item.provider, item.operation))} · ${escapeHtml(text(item.operation, "operation"))}</div>
            <div class="execution-work-title">${escapeHtml(text(item.label, "Provider operation"))}</div>
          </div>
          ${statusPill(stateValue)}
        </div>
        <div class="execution-work-role">${escapeHtml(text(item.role, "role not recorded"))}</div>
        <div class="execution-work-focus">${escapeHtml(text(item.focus, "Provider work admitted"))}</div>
        <div class="execution-work-meta">
          <span><small>STAGE</small><b>${escapeHtml(item.stage_id ? stageName(snapshot, item.stage_id) : "not recorded")}</b></span>
          <span><small>TASK</small><code title="${escapeHtml(text(item.task_id))}">${escapeHtml(shortTaskId(item.task_id))}</code></span>
          ${target}
        </div>
        <div class="execution-work-footer"><span>${escapeHtml(timing)}</span><span>${escapeHtml(relativeDate(item.updated_at || item.started_at))}</span></div>
      </article>`;
    };

    $("#execution-work-grid").innerHTML = items.length
      ? items.map(renderCard).join("")
      : '<div class="execution-work-empty"><strong>No retrieval or service operation is active.</strong><span>Model activity is tracked separately below.</span></div>';
  }

  function renderModelCalls(snapshot) {
    const modelCalls = snapshot.model_calls || {};
    const liveCalls = Array.isArray(modelCalls.live_items)
      ? modelCalls.live_items
      : (Array.isArray(modelCalls.items) ? modelCalls.items : []);
    const reviewCalls = Array.isArray(modelCalls.review_items) ? modelCalls.review_items : [];
    const recentCalls = Array.isArray(modelCalls.recent_items) ? modelCalls.recent_items : [];
    const provider = snapshot.execution?.provider || {};
    const capacity = provider.worker_capacity ?? provider.durable_window_capacity ?? provider.configured_capacity;
    const active = modelCalls.active ?? liveCalls.length;
    const reviewPending = modelCalls.review_pending ?? reviewCalls.length;
    const slotText = capacity === null || capacity === undefined ? "call capacity not recorded" : `${number(capacity)} call slots`;
    const reviewText = reviewPending ? ` · ${number(reviewPending)} awaiting review` : "";
    const liveSuffix = modelCalls.truncated ? " · live list capped" : "";
    $("#model-calls-caption").textContent = `${number(active)} live · ${slotText}${reviewText}${liveSuffix}`;

    const renderCard = (call, history = false) => {
      const stateValue = call.state || "unknown";
      const responseRef = call.response_ref;
      const timing = ["running", "queued", "started"].includes(call.state)
        ? `${duration(call.elapsed_seconds)} elapsed`
        : call.finished_at ? relativeDate(call.finished_at) : "finish time not recorded";
      const endpoint = call.endpoint ? ` · ${escapeHtml(call.endpoint)}` : "";
      const pool = call.provider_pool ? ` · ${escapeHtml(call.provider_pool)}` : "";
      const cache = call.cache || {};
      const readRatio = Number(cache.read_ratio);
      const readPercent = Number.isFinite(readRatio) ? ` · ${(readRatio * 100).toFixed(1)}%` : "";
      const cacheLabel = ["hit", "partial"].includes(cache.status) && Number.isFinite(Number(cache.read_tokens))
        ? `${text(cache.status, "cache")} · ${number(cache.read_tokens)} read${readPercent}`
        : cache.status === "primed" && Number.isFinite(Number(cache.write_tokens))
          ? `primed · ${number(cache.write_tokens)} written`
        : text(cache.status, "not recorded");
      return `<article class="model-call-card${history ? " model-call-card--history" : ""} model-call-card--${statusClass(stateValue)}">
        <div class="model-call-top">
          <div class="model-call-heading">
            <div class="model-call-provider">${escapeHtml(text(call.provider, "ROUTE"))}${endpoint}${pool}</div>
            <div class="model-call-model">${escapeHtml(text(call.model, "model not recorded"))}</div>
          </div>
          ${statusPill(stateValue)}
        </div>
        <div class="model-call-role">${escapeHtml(text(call.role, "role not recorded"))}</div>
        <div class="model-call-meta">
          <span><small>STAGE</small><b>${escapeHtml(call.stage_id ? stageName(snapshot, call.stage_id) : "not recorded")}</b></span>
          <span><small>TASK</small><code title="${escapeHtml(text(call.task_id))}">${escapeHtml(shortTaskId(call.task_id))}</code></span>
          <span><small>RESPONSE</small><b class="response-state response-state--${statusClass(call.response_status)}">${escapeHtml(text(call.response_status, "not recorded"))}</b></span>
          <span><small>USAGE / CACHE</small><b>${escapeHtml(modelCallUsage(call))} · ${escapeHtml(cacheLabel)}</b></span>
        </div>
        <div class="model-call-footer"><span>${escapeHtml(timing)}</span><span>${escapeHtml(relativeDate(call.updated_at || call.started_at))}</span></div>
        <div class="model-call-action">${responseRef
          ? `<button class="model-call-inspect" type="button" data-model-response-ref="${escapeHtml(responseRef)}">Inspect response</button>`
          : `<span class="model-call-no-action">response artifact pending</span>`}</div>
      </article>`;
    };

    $("#model-call-grid").innerHTML = liveCalls.length
      ? liveCalls.map((call) => renderCard(call)).join("")
      : '<div class="model-call-empty"><strong>No model call is running.</strong><span>Retrieval and service work is shown in Provider operations above.</span></div>';

    const historyCalls = [...reviewCalls, ...recentCalls].slice(0, 16);
    const history = $("#model-call-history");
    history.hidden = !historyCalls.length;
    if (historyCalls.length) {
      const recentCount = recentCalls.length;
      const historySuffix = modelCalls.history_truncated ? " · bounded" : "";
      $("#model-call-history-summary").textContent = `${number(reviewPending)} review pending · ${number(recentCount)} recent outputs${historySuffix}`;
      $("#model-call-history-grid").innerHTML = historyCalls.map((call) => renderCard(call, true)).join("");
    } else {
      $("#model-call-history-grid").innerHTML = "";
    }
    $$('[data-model-response-ref]').forEach((button) => button.addEventListener("click", () => openInspector(button.dataset.modelResponseRef)));
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
      const visibleItems = items.filter((item) => item.engaged || ["failed", "blocked", "rejected"].includes(item.status));
      const idleItems = items.filter((item) => !visibleItems.includes(item));
      const agentRow = (item) => `<div class="agent-row agent-row--clickable" data-agent-role="${escapeHtml(item.role)}" tabindex="0" role="button" title="${escapeHtml(item.role)}">
        <span class="status-dot status-dot--${statusClass(item.status)}"></span>
        <span class="agent-row-name">${escapeHtml(text(item.label, compactRole(item.role)))}</span>
        <span class="agent-row-state">${escapeHtml(text(item.status, "idle"))}</span>
      </div>`;
      const rows = visibleItems.length ? visibleItems.map(agentRow).join("") : '<div class="agent-list-empty">No active or flagged roles.</div>';
      const idle = idleItems.length ? `<details class="agent-idle-details"><summary>${number(idleItems.length)} idle capabilities</summary><div class="agent-idle-list">${idleItems.map(agentRow).join("")}</div></details>` : "";
      return `<article class="department-card"><div class="department-header"><div class="department-heading"><div class="department-name">${escapeHtml(text(department.label, department.id))}</div><div class="department-id mono">${escapeHtml(department.id)}</div></div><div class="department-stats"><div class="department-stat"><strong>${number(engaged)}</strong><span>active</span></div><div class="department-stat"><strong>${number(failures)}</strong><span>flagged</span></div><div class="department-stat"><strong>${number(items.length)}</strong><span>eligible</span></div></div></div><div class="department-leads"><span>CHIEF <b>${escapeHtml(chief)}</b></span><span>ADVERSARY <b>${escapeHtml(adversary)}</b></span></div><div class="agent-list">${rows}${idle}</div></article>`;
    }).join("");
  }

  function renderSpecialists(snapshot) {
    const items = snapshot.specialists || [];
    const work = snapshot.recent_work || [];
    if (!work.length) {
      const active = items.filter((item) => item.engaged).length;
      $("#specialist-body").innerHTML = '<tr><td colspan="5" class="empty-cell">No logical assignment artifact recorded yet. Actual provider work is shown in Model calls above.</td></tr>';
      $("#specialist-caption").textContent = `${number(items.length)} roles · ${number(active)} active · live model work shown above`;
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
    const allItems = snapshot.logs || snapshot.activity || [];
    const meaningful = [];
    let composerCheckpointShown = false;
    allItems.forEach((item) => {
      const title = String(item.title || "").toLowerCase();
      const detail = String(item.detail || "");
      if (title === "progress / checkpointed" || (title === "artifact / published" && detail.includes("artifact:command/progress/"))) return;
      if (title === "task / proposed") return;
      if ((title === "budget / reserved" || title === "budget / settled") && (!detail || detail === "No detail recorded.")) return;
      if (title === "artifact / published" && detail.includes("artifact:command/composer/checkpoints/")) {
        if (composerCheckpointShown) return;
        composerCheckpointShown = true;
      }
      meaningful.push(item);
    });
    const items = meaningful.slice(0, 24);
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
    const databases = snapshot.integrity?.databases || [];
    const execution = snapshot.execution || {};
    const roleAssignments = execution.role_assignments || {};
    const provider = execution.provider || {};
    const gates = databases.length ? databases.map((item) => `<div class="gate-row"><span class="status-dot status-dot--${item.status === "ok" ? "pass" : "warn"}"></span><span>${escapeHtml(item.root_key)} ledger · ${escapeHtml(item.status)}</span></div>`).join("") : '<div class="gate-row"><span class="status-dot status-dot--warn"></span><span>No ledger verification available</span></div>';
    const totalCapacity = provider.durable_window_capacity ?? provider.configured_capacity;
    const workerCapacity = provider.worker_capacity ?? provider.configured_worker_concurrency;
    const dispatchRows = [
      ["stage role limit", roleAssignments.limit === null || roleAssignments.limit === undefined ? "not recorded" : number(roleAssignments.limit)],
      ["provider calls", `${number(provider.running_tasks)} running · ${number(provider.awaiting_review_tasks)} awaiting review`],
      ["worker capacity", workerCapacity === null || workerCapacity === undefined ? "not recorded" : number(workerCapacity)],
      ["total call cap", totalCapacity === null || totalCapacity === undefined ? "not recorded" : number(totalCapacity)],
      ["capacity guard", provider.within_capacity === false ? "OVER CAPACITY" : "within capacity"],
    ];
    const poolRows = Object.entries(provider.pools || {}).map(([name, item]) => [
      `${name} pool`, `${number(item.running)} / ${number(item.max_concurrent)} running${item.within_capacity === false ? " · OVER" : ""}`,
    ]);
    $("#resource-stack").innerHTML = `<div class="resource-card"><div class="resource-card-heading"><span>MISSION USAGE</span><strong>${usageEntries(usage).length ? "live" : "not recorded"}</strong></div><div class="resource-rows">${resourceRows(usageEntries(usage))}</div></div><div class="resource-card"><div class="resource-card-heading"><span>DISPATCH SEMANTICS</span><strong>${escapeHtml(text(provider.capacity_source, "not recorded"))}</strong></div><div class="resource-rows">${resourceRows(dispatchRows)}${poolRows.length ? resourceRows(poolRows) : ""}${provider.source_root ? `<div class="resource-row"><span class="resource-key">allocation root</span><span class="resource-value">${escapeHtml(provider.source_root)}</span></div>` : ""}</div></div><div class="resource-card"><div class="resource-card-heading"><span>INTEGRITY GATES</span><strong>read-only</strong></div><div class="resource-rows">${gates}</div></div>`;
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
      const rootDirectories = directories.filter((item) => item.root_key === root.root_key);
      const entries = rootDirectories.filter((item) => Number(item.depth) <= 1).slice(0, 36);
      const hidden = Math.max(0, rootDirectories.length - entries.length);
      const tree = entries.length ? entries.map((item) => `<div class="tree-row" style="--depth:${Math.min(Math.max(Number(item.depth) || 0, 0), 8)}"><span class="tree-marker">/</span><span>${escapeHtml(item.path)}</span></div>`).join("") : `<div class="tree-empty">${root.covered_by ? `covered by ${escapeHtml(root.covered_by)} root scan` : "no directories discovered"}</div>`;
      const more = hidden ? `<div class="tree-empty">${number(hidden)} deeper directories indexed · use Inventory to inspect files</div>` : "";
      return `<article class="structure-card"><div class="structure-card-header"><div><div class="structure-root">${escapeHtml(root.root_key)}</div><div class="structure-path mono">${escapeHtml(root.path)}</div></div><div class="structure-count">${number(root.files)} files<br>${number(root.directories)} dirs</div></div><div class="tree-list">${tree}${more}</div></article>`;
    }).join("");
  }

  function inventoryItems(snapshot) {
    const checkpoints = (snapshot.checkpoints || []).map((item) => ({ ...item, kind: "checkpoint", displayKind: "checkpoint" }));
    const artifacts = (snapshot.artifacts || []).map((item) => ({ ...item, kind: "artifact", displayKind: item.artifact_type || "artifact", path: item.artifact_ref || item.logical_id, name: item.logical_id || item.artifact_ref, owner: item.owner || item.author }));
    const files = (snapshot.files || []).map((item) => ({ ...item, kind: "file", displayKind: item.kind || "file" }));
    const seen = new Set();
    return [...checkpoints, ...artifacts, ...files].filter((item) => {
      const identity = item.artifact_ref ? `artifact:${item.artifact_ref}` : `${item.kind}:${item.ref || item.path || item.name}`;
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    }).sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")));
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
    renderStageResults(snapshot);
    renderMetrics(snapshot);
    renderProviderWork(snapshot);
    renderModelCalls(snapshot);
    renderDepartments(snapshot);
    renderSpecialists(snapshot);
    renderActivity(snapshot);
    renderResources(snapshot);
    renderStructure(snapshot);
    renderInventory(snapshot);
    renderProjectPage();
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
    renderCurrentProject();
    const selectedRef = state.projectRef || ".";
    const selected = state.projects.find((item) => item.ref === selectedRef);
    $("#project-manager-status").textContent = `${number(state.projects.length)} project${state.projects.length === 1 ? "" : "s"} · ${text(selected?.name, "none")} selected`;
    $("#project-list").innerHTML = state.projects.length ? state.projects.map((project) => `<button class="project-item${project.ref === selectedRef ? " is-selected" : ""}" type="button" data-project-ref="${escapeHtml(project.ref)}">
      <span><strong>${escapeHtml(text(project.name, project.ref))}</strong><small>${escapeHtml(text(project.ref))}</small></span>
      <span class="project-item-status">${statusPill(project.status)}${project.pid ? `<small>PID ${escapeHtml(project.pid)}</small>` : ""}</span>
    </button>`).join("") : '<div class="loading-block">No managed Composer projects found.</div>';
    $$(".project-item").forEach((button) => button.addEventListener("click", () => {
      closeProjectDialog();
      navigateProject(button.dataset.projectRef);
    }));
  }

  function renderRunControls() {
    const run = state.run;
    const names = {ready: "Ready to start", running: "Running", waiting_for_provider: "Waiting for provider", stopping: "Stopping…", stopped: "Stopped", completed: "Complete", deadline_reached: "Deadline reached"};
    $("#run-status").textContent = state.runBusy ? "Applying command…" : (names[run?.status] || "Checking…");
    const processes = run?.processes || [];
    $("#run-detail").textContent = run?.control_error || (processes.length ? `PID ${processes.map((p) => p.pid).join(" · ")}${run.provider_retry_at_epoch ? ` · Retry ${new Date(run.provider_retry_at_epoch * 1000).toLocaleString()}` : ""}` : (run?.initialized ? "Existing checkpoint preserved" : "No run has been started"));
    for (const action of ["start", "stop", "resume"]) $("#run-" + action).disabled = state.runBusy || run?.["can_" + action] !== true;
    const scope = $("#run-scope");
    const options = '<option value="">Workflow complete</option>' + (run?.stages || []).map((stage) => `<option value="${escapeHtml(stage.id)}">${escapeHtml(stage.label)}</option>`).join("");
    if (scope.dataset.project !== state.projectRef || scope.dataset.options !== options) {
      scope.innerHTML = options; scope.value = run?.settings?.stop_after_stage || "";
      scope.dataset.project = state.projectRef; scope.dataset.options = options;
    }
    scope.disabled = !run || state.runBusy || processes.length > 0;
    $("#run-note").textContent = run?.status === "stopping" ? "Waiting for the supervisor to stop its workers and preserve the checkpoint." : "Stopping preserves the checkpoint. Runs continue when this window closes.";
  }

  async function fetchRun() {
    if (state.view !== "project") return;
    const request = ++state.runRequest, project = state.projectRef;
    try {
      const response = await fetch(`/api/run?project=${encodeURIComponent(project || ".")}`, {cache: "no-store"});
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `Run status unavailable (${response.status})`);
      if (request !== state.runRequest || project !== state.projectRef) return;
      state.run = value;
      renderRunControls();
    } catch (error) {
      if (request !== state.runRequest || project !== state.projectRef) return;
      state.run = null; renderRunControls(); $("#run-status").textContent = "Unavailable";
      $("#run-detail").textContent = error.message;
    }
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

  async function runProject(action) {
    if (state.runBusy) return;
    const project = state.projectRef;
    if (project === null) return;
    state.runBusy = true; renderRunControls();
    try {
      const payload = {action: action === "stop" ? "stop_composer" : "start_composer", project};
      if (action !== "stop") { payload.resume = action === "resume"; payload.settings = {development: state.run?.settings?.development ?? true, stop_after_stage: $("#run-scope").value || null}; }
      const result = await postAction(payload);
      showToast(result.status === "already_running" ? "This mission is already running." : result.status === "stopping" ? "Stop requested. Preserving the checkpoint…" : result.status === "already_stopped" ? "This mission is already stopped." : `Mission ${action === "resume" ? "resumed" : "started"} · PID ${result.pid}`);
      await Promise.all([fetchProjects(), fetchRun(), fetchSnapshot()]);
    } catch (error) {
      showToast(error.message);
    } finally {
      state.runBusy = false; renderRunControls();
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
      form.reset();
      closeProjectDialog();
      showToast(startNow && result.start?.pid ? `Project created and started · PID ${result.start.pid}` : `Project created · ${result.project}`);
      await fetchProjects();
      await fetchWorkspace();
      navigateProject(result.project);
    } catch (error) {
      showToast(error.message);
    }
  }

  let snapshotController = null;
  let snapshotRequestProject = null;
  async function fetchSnapshot() {
    if (state.view !== "project") return fetchWorkspace();
    const project = state.projectRef;
    if (snapshotController && snapshotRequestProject === project) return;
    if (snapshotController) snapshotController.abort();
    const controller = new AbortController();
    snapshotController = controller;
    snapshotRequestProject = project;
    try {
      const response = await fetch(`/api/snapshot?ts=${Date.now()}${projectQuery()}`, { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`snapshot request failed (${response.status})`);
      const data = await response.json();
      if (state.view === "project" && state.projectRef === project && snapshotController === controller) render(data);
    } catch (error) {
      if (controller.signal.aborted || state.view !== "project" || state.projectRef !== project) return;
      $("#connection-status").textContent = "Unavailable";
      $("#connection-dot").classList.add("is-error");
      $("#last-updated").textContent = "Waiting for the local server";
      if (state.snapshot) showToast(error.message);
      else $("#objective").textContent = `Unable to read the project snapshot: ${error.message}`;
    } finally {
      if (snapshotController === controller) snapshotController = null;
    }
  }

  async function fetchWorkspace() {
    try {
      const response = await fetch(`/api/workspace?ts=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`workspace request failed (${response.status})`);
      const data = await response.json();
      renderWorkspace(data);
      $("#connection-status").textContent = "Connected";
      $("#connection-dot").classList.remove("is-error");
      if (state.view === "workspace") setView("workspace");
    } catch (error) {
      $("#connection-status").textContent = "Unavailable";
      $("#connection-dot").classList.add("is-error");
      $("#workspace-updated").textContent = "Waiting for the local server";
      if (state.workspace) showToast(error.message);
      else $("#workspace-project-list").innerHTML = `<div class="loading-block">${escapeHtml(error.message)}</div>`;
    }
  }

  async function refreshCurrent() {
    await Promise.all([fetchWorkspace(), state.view === "project" ? fetchSnapshot() : Promise.resolve(), fetchRun()]);
  }

  function setInspectorRaw(href) {
    const link = $("#inspector-raw");
    link.classList.toggle("is-disabled", !href);
    link.setAttribute("aria-disabled", String(!href));
    if (href) { link.setAttribute("href", href); link.removeAttribute("tabindex"); }
    else { link.removeAttribute("href"); link.setAttribute("tabindex", "-1"); }
  }

  function beginInspector() {
    setInspectorRaw(null);
    return ++state.inspectorRequest;
  }

  function closeInspector() {
    ++state.inspectorRequest;
    $("#inspector").close();
  }

  function setInspectorView(raw) {
    $("#inspector-content").hidden = !raw;
    $("#inspector-readable").hidden = raw;
    $("#inspector-readable-button").setAttribute("aria-pressed", String(!raw));
    $("#inspector-json-button").setAttribute("aria-pressed", String(raw));
  }

  function setInspectorContent(content) {
    const original = typeof content === "string" ? content : JSON.stringify(content, null, 2);
    let value = original;
    try { value = JSON.parse(original); } catch (_) { /* Preserve plain text previews. */ }
    $("#inspector-content").textContent = original;
    $("#inspector-readable").innerHTML = window.ScisaurusOutputView.document(value);
    setInspectorView(false);
  }

  async function openInspector(ref, title = null) {
    const request = beginInspector();
    const dialog = $("#inspector");
    $("#inspector-title").textContent = "Loading file";
    $("#inspector-meta").textContent = ref;
    setInspectorContent("Loading…");
    setInspectorRaw(null);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      const response = await fetch(`/api/file?ref=${encodeURIComponent(ref)}${projectQuery()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`file request failed (${response.status})`);
      const file = await response.json();
      if (request !== state.inspectorRequest) return;
      $("#inspector-title").textContent = title || window.ScisaurusOutputView.label(text(file.name, "File").replace(/\.json$/, ""));
      $("#inspector-meta").textContent = `${text(file.root_key)} · ${text(file.path)} · ${bytes(file.size)} · ${relativeDate(file.updated_at)}`;
      setInspectorContent(text(file.text, "No preview available."));
      $("#inspector-note").textContent = file.truncated ? "Preview truncated at 800 KB." : "Preview is bounded and read-only.";
      setInspectorRaw(`/api/raw?ref=${encodeURIComponent(ref)}${projectQuery()}`);
    } catch (error) {
      if (request !== state.inspectorRequest) return;
      setInspectorContent(error.message);
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
    const request = beginInspector();
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
    setInspectorContent("Loading agent record…");
    $("#inspector-note").textContent = "Task, attempt, and response artifact are read-only.";
    setInspectorRaw(null);
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

      if (request !== state.inspectorRequest) return;
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
          setInspectorRaw(`/api/raw?ref=${encodeURIComponent(selected.responseRef)}${projectQuery()}`);
          $("#inspector-note").textContent = `Response file · ${text(selected.responseRef)}${displayFile.truncated ? " · preview truncated" : ""}`;
        } else {
          responseText = displayFile.text;
          setInspectorRaw(`/api/raw?ref=${encodeURIComponent(selected.artifact.file_ref)}${projectQuery()}`);
          $("#inspector-note").textContent = `${assignmentRecord(selected.record) ? "Latest result record" : "Latest response artifact"} · ${text(selected.artifact.artifact_ref)}${displayFile.truncated ? " · preview truncated" : ""}`;
        }

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
      setInspectorContent(responseText || "No response content.");
    } catch (error) {
      if (request !== state.inspectorRequest) return;
      setInspectorContent(error.message);
      $("#inspector-note").textContent = "The response record could not be read.";
    }
  }

  function bindControls() {
    $("#refresh-button").addEventListener("click", refreshCurrent);
    $("#workspace-link").addEventListener("click", (event) => {
      event.preventDefault();
      navigateWorkspace();
    });
    const openProjectManager = (focusCreate = false) => {
      const dialog = $("#project-dialog");
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      fetchProjects();
      if (focusCreate) window.requestAnimationFrame(() => $("#new-project-slug").focus());
    };
    $("#new-project-button").addEventListener("click", () => openProjectManager(true));
    $("#projects-button").addEventListener("click", () => {
      openProjectManager();
    });
    $("#project-dialog-close").addEventListener("click", closeProjectDialog);
    $("#project-dialog").addEventListener("click", (event) => {
      if (event.target === $("#project-dialog")) closeProjectDialog();
    });
    for (const action of ["start", "stop", "resume"]) $("#run-" + action).addEventListener("click", () => runProject(action));
    $("#project-form").addEventListener("submit", (event) => {
      event.preventDefault();
      createProject(event.submitter?.dataset.start === "true");
    });
    $("#discovery-source-link").addEventListener("click",()=>{const topic=(state.snapshot?.stage_results || []).find((item)=>item.kind === "topic_discovery");if(topic){selectStage(topic.id);$("#topic-papers").scrollIntoView({block:"start"});}});
    $("#topic-paper-search").addEventListener("input",(event)=>{state.topicPaperQuery=event.target.value;renderTopicPapers(state.snapshot?.topic_papers || {});});
    let literatureSearchTimer;
    $("#literature-search").addEventListener("input", (event) => {
      state.literatureQuery = event.target.value; state.literatureOffset = 0;
      window.clearTimeout(literatureSearchTimer); literatureSearchTimer = window.setTimeout(fetchLiterature, 250);
    });
    $("#literature-evidence").addEventListener("change",(event)=>{state.literatureEvidence=event.target.value;state.literatureOffset=0;fetchLiterature();});
    $("#literature-prev").addEventListener("click",()=>{state.literatureOffset=Math.max(0,state.literatureOffset-25);fetchLiterature();});
    $("#literature-next").addEventListener("click",()=>{state.literatureOffset+=25;fetchLiterature();});
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
    $("#inspector-readable-button").addEventListener("click", () => setInspectorView(false));
    $("#inspector-json-button").addEventListener("click", () => setInspectorView(true));
    $("#inspector-close").addEventListener("click", closeInspector);
    $("#inspector").addEventListener("close", () => { if (!$("#inspector").open) ++state.inspectorRequest; });
    $("#inspector").addEventListener("cancel", () => { ++state.inspectorRequest; });
    $("#inspector").addEventListener("click", (event) => {
      if (event.target === $("#inspector")) closeInspector();
    });
    $$("#project-nav .nav-link").forEach((link) => link.addEventListener("click", (event) => {
      event.preventDefault();
      navigateOperation(link.getAttribute("href").slice(1));
    }));
    window.addEventListener("popstate", () => {
      closeInspector();
      state.stageId = new URLSearchParams(window.location.search).get("stage");
      const project = new URLSearchParams(window.location.search).get("project");
      if (project === null) {
        setView("workspace");
        fetchWorkspace();
      } else {
        setView("project", project || ".");
        fetchSnapshot();
        fetchProjects();
        fetchRun();
      }
    });
    window.addEventListener("hashchange", () => {
      if (state.view === "project") renderProjectPage();
    });
  }

  bindControls();
  setView(state.view, state.projectRef);
  fetchProjects();
  fetchWorkspace();
  if (state.view === "project") fetchSnapshot();
  fetchRun();
  window.setInterval(refreshCurrent, 5000);
  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (state.snapshot && state.view === "project") render(state.snapshot);
      renderProjectPage();
    }, 120);
  });
})();
