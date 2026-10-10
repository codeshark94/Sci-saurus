# Solver selection for material development

Select the useful function and a discriminating first calculation before choosing
a solver. DSH selects the smallest supported physical model, retains an editable
design and verifies a reference case before interpreting its performance.
An installed package, an operational probe and a scientifically validated model
are three different evidence levels.

## Minimum stack by physical question

| Physical question | First solver and required tools | Add only when the model requires it | Main boundary |
|---|---|---|---|
| Steady heat routing or effective conductivity | SfePy continuum; Gmsh for a mesh requiring geometry generation | Elmer for a separately verified thermal formulation; OpenFOAM for conjugate heat transfer with flow | Thermal contact, radiation and temperature dependence need explicit models |
| Linear stiffness, deformation or elastic unit-cell response | SfePy continuum and a checked mesh | CalculiX or Code_Aster for the selected structural formulation | Static elasticity does not establish dynamic attenuation, contact or plasticity |
| Temperature-induced deformation | SfePy conduction followed by elasticity with thermal strain | MOOSE or Elmer for a verified coupling needing feedback | The native thermoelastic profile is one-way |
| Acoustic pressure, resonances or transmission | SfePy with a verified pressure Helmholtz formulation and mesh | Elmer after verifying its chosen acoustic model; another declared solver for physics absent from that model | Complex pressure, excitation, outgoing boundaries, loss and flux normalization require explicit verification |
| Electromagnetic finite-device transmission or scattering | Meep waves runtime | MPB for a separate periodic eigenmode calculation | Meep solves Maxwell equations; it is not the acoustic pressure solver |
| Electromagnetic periodic band structure | MPB waves runtime | Meep to test a finite realization | The standard MPB eigenproblem assumes lossless, frequency-independent material properties; it does not establish finite-device transmission |
| Flow-controlled response, mixing or pressure loss | OpenFOAM in the extended profile | Thermal or structural solver for a declared field exchange | Flow regime, compressibility, turbulence and coupling must match the question |
| Explicit coupled fields, phase evolution or porous flow | MOOSE with the particular physics modules required by the design | Elmer as an alternative for a verified supported formulation | A general multiphysics executable does not supply constitutive laws, coupling or verification automatically |
| Multiscale effective response and a finite specimen | Unit-cell model and finite model using the same applicable physics | A different scale model only when its parameter/field handoff is defined | Homogenization requires justified cell boundaries, averaging and scale separation |

These are starting routes, not exclusive solver assignments or admission of a
particular concept. The fixed laboratory profile and its current attestation
determine which routes are actually available. Alternative tools require their
own declared runtime, current identity and applicable operational probe.

SfePy's [official example index](https://sfepy.org/doc/examples.html) includes
diffusion, elasticity, homogenization and multiphysics formulations. Its
[3D acoustic pressure example](https://sfepy.org/doc/examples/acoustics-acoustics3d.html)
uses complex pressures and interface terms. An example supplies a starting
formulation, not validated material parameters or universal boundary conditions.

[Meep](https://meep.readthedocs.io/en/latest/Introduction/) evolves electromagnetic
fields and supports finite-device spectral measurements.
[MPB](https://mpb.readthedocs.io/en/latest/Introduction/) solves the periodic
electromagnetic eigenproblem. Their equations and model restrictions determine
whether either is appropriate.

[MOOSE modules](https://mooseframework.inl.gov/modules/index.html),
[Elmer](https://github.com/ElmerCSC/elmerfem),
[OpenFOAM](https://openfoam.org/), [CalculiX](https://www.calculix.de/) and
[Code_Aster](https://code-aster.org/en) provide additional formulations. Select
one for a needed physical capability; using every installed package is not a
development objective.

## Geometry and execution tools

- **FreeCAD** is optional for an editable solid model and STEP/FCStd delivery.
  A parametric mesh script can be sufficient for a first pilot.
- **Gmsh** generates meshes and retains physical region/boundary tags. It does
  not solve the physical response.
- **NumPy/SciPy, meshio, HDF5 and plotting tools** support extraction, transfer,
  independent arithmetic and figures. They do not substitute for the selected
  physical solver or its verification case.

## Runtime readiness and scientific readiness

The native profile declares continuum and electromagnetic wave families. The
extended profile additionally declares MOOSE, Elmer, OpenFOAM, CalculiX and
Code_Aster routes. Container startup requires the actual Docker daemon and
resources; importing a package on the controller is insufficient.

The native acoustic operational probe exercises a simple pressure problem. It
does not attest complex transmission, Bloch dispersion, thermoviscous loss or
vibro-acoustic coupling. A proposed calculation requiring those features must
verify that particular formulation and its boundary conditions before making
claims. The same distinction applies to each solver's optional modules.

Before executing a design, DSH records:

1. Governing equations, constitutive assumptions, units and validity range.
2. The exact attested runtime and the required module/formulation.
3. Geometry, material inputs, boundary/excitation definitions and parameter provenance.
4. An analytic or published reference case, followed by an equal-constraint baseline.
5. A small first pilot with retained raw fields, solver diagnostics and a discriminating observable.
6. Necessary field exchanges and their conservation/interpolation checks for coupling.

Missing implementation-critical inputs block the affected calculation. Missing
publication-level novelty does not establish failure or authorize a novelty
claim: a provisional pilot can test the declared mechanism while recording that
uncertainty. Broader sweeps, convergence, sensitivity and final claims follow the
pilot rather than being represented as already completed.

See [runtime dependencies](10-runtime-dependencies.md) for installation and
[laboratory preparation](140-metamaterial-laboratory.md) for profiles, probes,
attestation and artifact transfer.

## Atomistic tools

The current attested laboratory labels do not include an atomistic DFT or MD
runtime. An unrelated module or library with a similar name does not attest an
atomistic workflow. LAMMPS, Quantum ESPRESSO and ASE are potential additions;
they are not currently declared as available execution capabilities.

Provisioning must include executable/version receipts, an analytic or published
small reference calculation, resource bounds, and the exact potential or
pseudopotential provenance. Material applicability, finite-size/convergence
limits and any exchange with continuum fields must be explicit. Installing a
binary alone does not admit a multiscale claim.
