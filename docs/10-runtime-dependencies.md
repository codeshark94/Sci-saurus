# Runtime dependencies

Sci-whale separates mission control, engineering workers and scientific execution.
Installing the desktop app does not install every solver. A tool becomes agent-usable
only through its declared profile, immutable runtime identity and operational probes.
An installation check does not establish scientific validity or material performance.

For the required solver by physical question and the minimum first-pilot stack,
see [solver selection](145-metamaterial-solvers.md).

## Dependency ownership

| Environment | Specification | Purpose and boundary |
|---|---|---|
| Control (reference Python 3.14) | `requirements-runtime.txt`, `scripts/setup-runtime.sh` | Controller, JSON contracts, retrieval and MCP; `.venv` |
| HTML extraction | ReadabiliPy bundled package manifest, `scripts/readabilipy-package-lock.json` | Node/npm dependency tree for ReadabiliPy; installed with `npm ci` by runtime setup |
| PDF extraction | Host Poppler tools | Optional source acquisition; unavailable extraction is reported explicitly |
| Experiment analysis (reference Python 3.14) | `requirements-experiment.txt`, `scripts/setup-experiment-runtime.sh` | Separate NumPy/Matplotlib runtime at `.runs/experiment-runtime` |
| Meshing, Python 3.14 | `requirements-meshing.txt` | Isolated Gmsh/meshio environment to avoid native OpenMP conflicts |
| Continuum, Python 3.12 | `config/environments/continuum.yml` | SfePy, numerical analysis and full field extraction |
| Waves, Python 3.12 | `config/environments/waves.yml` | Serial Meep and MPB; no MPI or GPU claim |
| CAD | External FreeCAD application bundle | Own interpreter and native libraries; profile binds the installed bundle |
| DSH | External DSH checkout, its dependency lock and compiled libraries | Fixed provider/model composition per isolated session; runtime hashes pinned by preparation |
| Desktop build | `desktop/package-lock.json`, `desktop/src-tauri/Cargo.lock`, `desktop/requirements-build.txt` | Tauri and PyInstaller backend packaging; scientific runtimes remain external |
| Extended solvers | `scripts/laboratory/containers/Dockerfile.*` plus installed native Elmer | Pinned Linux images and native solver inventory; local Docker daemon required for containers |

Direct version pins and environment recipes are not complete transitive locks.
Conda's installed package inventory and the laboratory's full content attestation
bind the actual scientific runtime. Reprovision after any dependency change;
a package with the same version but changed executable or native content is drift.
Node and Rust use their committed lockfiles. Keep control and solver dependency
graphs separate rather than installing all packages into the desktop backend.

## Control and analysis

From the repository root:

```sh
sh scripts/setup-runtime.sh
sh scripts/setup-experiment-runtime.sh
```

The reference host uses Python 3.14. Setup defaults to `python3`; control setup
accepts `SCISAURUS_PYTHON` to select an interpreter, while analysis setup uses
`python3` from PATH. These scripts do not enforce an interpreter version. Node/npm
are required for control setup. Poppler is optional; inspect
setup diagnostics before enabling a source workflow that requires PDF extraction.
These commands neither enable model dispatch nor start a mission.

## Native laboratory environments

The following recipes describe reference environments. Choose an explicit local
prefix; the resolved profile must point to its actual interpreter. Micromamba
must be installed separately. Substitute a location with sufficient disk capacity.

```sh
micromamba create --yes --strict-channel-priority \
  --prefix /absolute/laboratory/continuum --file config/environments/continuum.yml
micromamba create --yes --strict-channel-priority \
  --prefix /absolute/laboratory/waves --file config/environments/waves.yml
python3.14 -m venv /absolute/laboratory/meshing
/absolute/laboratory/meshing/bin/python -m pip install -r requirements-meshing.txt
mkdir -p /absolute/laboratory/provenance
/absolute/laboratory/meshing/bin/python -m pip freeze \
  > /absolute/laboratory/provenance/meshing-pip-freeze.txt
```

The wave recipe selects serial `nompi` builds. Keep Gmsh in the meshing environment
and use immutable mesh artifacts for handoff to continuum solvers. Install
FreeCAD separately; its bundle supplies its own interpreter. Package availability
and native builds vary by host: a recipe is not a promise of cross-platform
compatibility. Failed installation or unsupported architecture blocks readiness.

Resolve `config/laboratory-metamaterial.example.json` with
`scripts/prepare-laboratory-profile.py`, then run `provision-laboratory` and the
operational probes described in the [laboratory guide](140-metamaterial-laboratory.md).
Preparation records resource bounds, coupling semantics and content identity;
agents select runtime labels rather than arbitrary host executables.

## Extended multiphysics

The tracked Docker recipes provide MOOSE, OpenFOAM/CalculiX and Code_Aster build
inputs, base-image digests and package inventories. Elmer is a separately installed
native serial runtime. Use `config/laboratory-multiphysics.example.json` only after
installing these runtimes and binding immutable image IDs and the local Docker
socket. Tags alone are insufficient.

MOOSE and Code_Aster currently require x86 emulation on Apple Silicon. Measure
resource and runtime cost before admitting a production workload. The controller
bounds CPU, memory, processes, output and time; explicit field transfer does not
establish valid two-way coupling. See the extended profile's limitations and probes
before declaring support for a particular physical model.

## Engineering and desktop

Build DSH's JSON-RPC libraries in its own checkout, then use
`scripts/prepare-dsh-batch.py` to pin the runtime and fixed model composition.
[DSH batches](dsh-batch.md) documents checking the binding without provider calls,
blinded validator authoring, receipts, pause handling and failure outcomes.
All DSH sessions and ordinary model clients share three host-wide dispatch slots.

For the desktop, use `npm ci` in `desktop`; `npm run build` passes `--locked`
to Cargo to retain the committed dependency resolution. Follow the
build procedure in [Desktop app](130-desktop-app.md). The backend build
requirements deliberately omit scientific solvers. Building an app is distinct
from provisioning its external scientific execution environments.

## Credentials and upgrades

Authorized provider, OpenAlex and Semantic Scholar credentials belong in named
environment variables or local deployment configuration, never tracked examples,
lockfiles or documentation. A configured endpoint does not itself authorize live
dispatch. Retain provider failures and usage during recovery.

Update the owning specification, rebuild or reinstall that environment, reprovision
its identity, and verify the affected workflow before adoption. Existing scientific
inputs, raw output and historical receipts remain immutable. A managed mission
uses an explicit additive profile upgrade at a supported stopped checkpoint;
replacing an existing runtime or changing the scientific mandate is not an additive
upgrade. See the laboratory guide for the upgrade contract.
