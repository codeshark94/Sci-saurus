# Local dashboard

Sci-saurus includes a small localhost web console for understanding and
managing the whole local research workspace and its bounded Composer projects. It reads the durable workflow checkpoints,
content-addressed artifact store, SQLite control ledger, and project files.
Snapshot and file inspection stay read-only; the Projects dialog exposes only
validated project creation and Composer start/resume actions.

## Start it

From the repository root, the short command opens the local research workspace
when `local-private` exists:

```bash
./dashboard
```

The default Composer mission has the same short root-level entry point:

```bash
./run
```

It discovers the newest direct Composer project under `local-private/` and
resumes it. If one is already running, it exits without starting a duplicate.
Set `SCISAURUS_COMPOSER_WORKFLOW` when a specific workflow must be selected.

The equivalent module command also auto-selects that project:

```bash
python3 -m scisaurus.cli dashboard
```

To inspect another project, pass its directory explicitly:

```bash
./dashboard PROJECT_DIR
```

The command prints a localhost URL and opens it in the default browser. The
root page is the Sci-saurus workspace overview; selecting a project opens its
project detail surface. It chooses a free local port by default. Use `--no-open` when the URL should only
be printed, `--port` to select a fixed port explicitly, or `--host` to select
another bind address.

```bash
python3 -m scisaurus.cli dashboard PROJECT_DIR --no-open --port 8765
```

The dashboard workspace is rooted at the directory passed to the command. It
lists direct child directories that contain a validated workflow marker. A
selected project can be a simple initialized
project, a Composer project, a stage directory, or a custom project that only
has files and checkpoints. When a workflow defines stage project directories,
the project detail view discovers those roots and groups their checkpoints,
tasks, artifacts, and files in one view.

## What is visible

- workspace-level project index, active runs, drafts, stale projects, and
  recently updated missions
- current mission status, phase, elapsed time, deadline, blockers, and process
- pipeline stage status, attempts, active roles, and verifier assignment
- department roster and assignment ledger; selecting a role opens its task,
  attempt, assignment record, and linked response file when one is recorded
- recent department activity and event-ledger activity
- model/API usage, allocation windows, and event-chain integrity
- checkpoint and artifact records plus a paginated, bounded file inventory
- read-only previews for text files and safe raw links for small binary files

The browser polls the snapshot every five seconds. The project checkpoint and
event ledger remain the source of truth; a dashboard snapshot is only a
time-bounded read model.

The sidebar follows the same hierarchy: `Workspace` is the global entry point,
the `Projects` list is always visible, and project-only navigation appears
after a project is selected. The global workspace response is intentionally
lightweight; expensive artifact, task, agent, and evidence inspection is
loaded only for the selected project.

## Research-first view

The main page is organized around the scientific work rather than the raw
ledger:

- `Research brief` shows the current topic, research question, phenomenon,
  mechanism, comparison, measurement, data regime, and disconfirmation test.
- `Pipeline` shows the six gates from topic admission through rendered paper,
  with each stage's deliverable, status, attempt, and active role slots.
- `Agents` shows departments and the bounded specialist roster. `Run log`
  shows recent role activity, including the assigned role, stage, task,
  response/artifact state, and independent reviewer.
- `Structure` and `Inventory` expose the project roots, checkpoints, artifacts,
  and files without turning the page into an unbounded artifact dump.

The dashboard distinguishes logical role assignments from provider execution
capacity. Several specialist assignments can belong to one stage while the
runtime still enforces the durable provider allocation window and worker cap.
The resource panel reports both numbers and the capacity guard.

## Projects and run controls

`Projects` lists the current Composer project and direct child projects under
`<workspace>/missions`. A new project is created by cloning the selected
workflow template into a fresh directory and replacing its objective, project
root, stage roots, topic-history path, and hard deadline. The workflow is
validated before it is written. The UI accepts a slug matching
`[a-z][a-z0-9-]{1,47}` and a hard deadline from one hour to seven days
(`3600`–`604800` seconds); the selected template's stage deadlines must also
fit inside that hard deadline.

The resulting project has this shape:

```text
missions/<slug>/
├── workflow.json
├── composer/
├── projects/
│   ├── topic/
│   ├── survey/
│   ├── experiment/
│   ├── interpretation/
│   ├── argument/
│   └── paper/
└── topic-history.json
```

`Create project` only writes the validated workflow. `Create & start` creates
it and launches the allowlisted Composer command. An existing Composer state
database must be resumed with `Resume current`; a new run cannot silently
overwrite existing state. The dashboard intentionally has no arbitrary shell
execution or stop/kill action.
