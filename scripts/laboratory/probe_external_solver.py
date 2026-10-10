#!/usr/bin/env python3
"""Operational PDE benchmarks; preserve input decks, logs and complete fields."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import re
import shutil
import zipfile

import numpy as np


TOOLS = json.loads(os.environ["SCI_SOLVER_COMMANDS"])


def execute(name, arguments, cwd=None):
    result = subprocess.run([TOOLS[name], *arguments], cwd=cwd, capture_output=True, timeout=240)
    root = Path(cwd or ".")
    (root / (name + ".stdout")).write_bytes(result.stdout)
    (root / (name + ".stderr")).write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError(f"{name} failed rc={result.returncode}: {result.stderr.decode(errors='replace')}")
    return result


def moose():
    Path("heat.i").write_text('''[Mesh]
 type = GeneratedMesh
 dim = 1
 nx = 20
 xmin = 0
 xmax = 1
[]
[Variables]
 [T]
 []
[]
[Kernels]
 [conduction]
  type = HeatConduction
  variable = T
 []
[]
[Materials]
 [conductivity]
  type = GenericConstantMaterial
  prop_names = thermal_conductivity
  prop_values = 1
 []
[]
[BCs]
 [cold]
  type = DirichletBC
  variable = T
  boundary = left
  value = 0
 []
 [hot]
  type = DirichletBC
  variable = T
  boundary = right
  value = 1
 []
[]
[Executioner]
 type = Steady
 solve_type = NEWTON
 nl_abs_tol = 1e-10
[]
[Outputs]
 exodus = true
 file_base = heat
[]
''')
    execute("moose", ["-i", "heat.i"])
    import netCDF4
    with netCDF4.Dataset("heat.e") as data:
        x = np.asarray(data["coordx"][:])
        names = netCDF4.chartostring(data["name_nod_var"][:]).tolist()
        t = np.asarray(data[f"vals_nod_var{names.index('T') + 1}"][-1])
    error = float(np.max(np.abs(t - x)))
    if error > 1e-8:
        raise ValueError(f"heat field disagrees with T(x)=x: {error}")
    return {"reference": "steady unit conductivity, unit length, Dirichlet T(0)=0,T(1)=1: T(x)=x",
            "max_absolute_error": error, "points": x.tolist(), "temperature": t.tolist()}


def elmer():
    n = 10
    root = Path("mesh")
    root.mkdir()
    idx = lambda i, j: j * (n + 1) + i + 1
    nodes = [(idx(i, j), i / n, j / n) for j in range(n + 1) for i in range(n + 1)]
    cells = [(j * n + i + 1, idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1))
             for j in range(n) for i in range(n)]
    boundaries = []
    for j in range(n):
        boundaries.extend([(1, j * n + 1, idx(0, j), idx(0, j + 1)),
                           (2, (j + 1) * n, idx(n, j), idx(n, j + 1))])
    for i in range(n):
        boundaries.extend([(3, i + 1, idx(i, 0), idx(i + 1, 0)),
                           (4, (n - 1) * n + i + 1, idx(i, n), idx(i + 1, n))])
    (root / "mesh.header").write_text(f"{len(nodes)} {len(cells)} {len(boundaries)}\n2\n404 {len(cells)}\n202 {len(boundaries)}\n")
    (root / "mesh.nodes").write_text("".join(f"{k} -1 {x} {y} 0\n" for k, x, y in nodes))
    (root / "mesh.elements").write_text("".join(f"{k} 1 404 {a} {b} {c} {d}\n" for k, a, b, c, d in cells))
    (root / "mesh.boundary").write_text("".join(f"{k} {bc} {parent} 0 202 {a} {b}\n" for k, (bc, parent, a, b) in enumerate(boundaries, 1)))
    Path("heat.sif").write_text('''Header
 Mesh DB "." "mesh"
End
Simulation
 Coordinate System = Cartesian 2D
 Simulation Type = Steady State
 Steady State Max Iterations = 1
 Output File = "heat.result"
End
Body 1
 Equation = 1
 Material = 1
End
Material 1
 Heat Conductivity = 1.0
End
Equation 1
 Active Solvers(2) = 1 2
End
Solver 1
 Equation = Heat Equation
 Procedure = "HeatSolve" "HeatSolver"
 Variable = Temperature
 Variable DOFs = 1
 Linear System Solver = Direct
 Linear System Direct Method = Banded
End
Solver 2
 Exec Solver = After All
 Equation = ResultOutput
 Procedure = "ResultOutputSolve" "ResultOutputSolver"
 Output File Name = "heat"
 Vtu Format = Logical True
End
Boundary Condition 1
 Target Boundaries(1) = 1
 Temperature = 0
End
Boundary Condition 2
 Target Boundaries(1) = 2
 Temperature = 1
End
''')
    execute("elmer", ["heat.sif"])
    import meshio
    file = next(Path("mesh").glob("heat*.vtu"))
    mesh = meshio.read(file)
    t = np.asarray(mesh.point_data["temperature"]).reshape(-1)
    error = float(np.max(np.abs(t - mesh.points[:, 0])))
    if error > 1e-8:
        raise ValueError(f"heat field disagrees with T(x)=x: {error}")
    return {"reference": "unit-square steady conduction T(x,y)=x", "max_absolute_error": error,
            "points": mesh.points.tolist(), "temperature": t.tolist()}


def calculix():
    Path("bar.inp").write_text('''*NODE,NSET=ALL
1,0,0,0
2,1,0,0
3,1,1,0
4,0,1,0
5,0,0,1
6,1,0,1
7,1,1,1
8,0,1,1
*ELEMENT,TYPE=C3D8,ELSET=BAR
1,1,2,3,4,5,6,7,8
*NSET,NSET=LEFT
1,4,5,8
*NSET,NSET=RIGHT
2,3,6,7
*MATERIAL,NAME=UNIT
*ELASTIC
1000,0
*SOLID SECTION,ELSET=BAR,MATERIAL=UNIT
*BOUNDARY
LEFT,1,1
ALL,2,3
*STEP
*STATIC
*CLOAD
RIGHT,1,0.25
*NODE PRINT,NSET=ALL
U
*EL PRINT,ELSET=BAR
S
*NODE FILE
U
*EL FILE
S
*END STEP
''')
    execute("calculix", ["bar"])
    execute("ccx_to_vtu", ["bar.frd", "vtu"])
    import meshio
    converted = meshio.read("bar.vtu")
    if len(converted.points) != 8 or not converted.point_data:
        raise ValueError("CalculiX field conversion omitted nodes or fields")
    lines = Path("bar.dat").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "displacements" in line) + 1
    rows = []
    for line in lines[start:]:
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit():
            rows.append([int(fields[0]), *map(float, fields[1:])])
        elif rows:
            break
    expected = {1: 0, 2: .001, 3: .001, 4: 0, 5: 0, 6: .001, 7: .001, 8: 0}
    if len(rows) != 8:
        raise ValueError("incomplete displacement output")
    error = max(abs(row[1] - expected[row[0]]) for row in rows)
    if error > 1e-8:
        raise ValueError(f"bar displacement disagrees with FL/(EA): {error}")
    return {"reference": "unit cube E=1000,nu=0,F=1,A=L=1: ux(right)=.001", "max_absolute_error": error,
            "nodal_displacements": rows, "converted_point_fields": {key: np.asarray(value).tolist() for key, value in converted.point_data.items()}}


def openfoam():
    root = Path("flow")
    shutil.copytree("/opt/openfoam14/tutorials/incompressibleFluid/planarPoiseuille", root)
    control = root / "system/controlDict"
    text = control.read_text()
    text = re.sub(r"endTime\s+[^;]+;", "endTime 60;", text)
    text = re.sub(r"deltaT\s+[^;]+;", "deltaT 0.005;", text)
    text = re.sub(r"writeInterval\s+[^;]+;", "writeInterval 60;", text)
    control.write_text(text)
    mesh = root / "system/blockMeshDict"
    mesh.write_text(mesh.read_text().replace("simpleGrading (1 4 1)", "simpleGrading (1 1 1)"))
    momentum = root / "constant/momentumTransport"
    header = momentum.read_text().split("simulationType")[0]
    momentum.write_text(header + "simulationType laminar;\n")
    execute("block_mesh", [], root)
    execute("check_mesh", [], root)
    result = execute("openfoam", [], root)
    execute("foam_to_vtk", ["-latestTime", "-ascii"], root)
    import meshio
    file = next((root / "VTK").glob("flow_*.vtk"))
    grid = meshio.read(file)
    u = np.concatenate(grid.cell_data["U"])
    y = np.concatenate([grid.points[cell.data, 1].mean(axis=1) for cell in grid.cells])
    reference = 25 * y * (2 - y)
    error = float(np.max(np.abs(u[:, 0] - reference)))
    transverse = float(np.max(np.abs(u[:, 1:])))
    if error > .02 or transverse > 1e-8 or len(y) != 40:
        raise ValueError(f"Poiseuille field failed: error={error}, transverse={transverse}, cells={len(y)}")
    continuity = [float(x) for x in re.findall(rb"cumulative = ([+\-0-9.eE]+)", result.stdout)]
    if not continuity or abs(continuity[-1]) > 1e-6:
        raise ValueError("continuity diagnostic missing or excessive")
    return {"reference": "half-channel y in [0,1], g=5,nu=.1, no slip at 0 and symmetry at 1: ux=25*y*(2-y)",
            "max_absolute_error": error, "transverse_max": transverse, "continuity_cumulative": continuity[-1],
            "y": y.tolist(), "velocity": u.tolist(),
            "cell_fields": {k: [a.tolist() for a in v] for k, v in grid.cell_data.items()}}


def code_aster():
    Path("bar.mail").write_text('''TITRE
Unit elastic cube
FINSF
COOR_3D
N1 0 0 0
N2 1 0 0
N3 1 1 0
N4 0 1 0
N5 0 0 1
N6 1 0 1
N7 1 1 1
N8 0 1 1
FINSF
HEXA8
M1 N1 N2 N3 N4 N5 N6 N7 N8
FINSF
GROUP_NO
LEFT N1 N4 N5 N8
FINSF
GROUP_NO
RIGHT N2 N3 N6 N7
FINSF
GROUP_NO
ALL N1 N2 N3 N4 N5 N6 N7 N8
FINSF
FIN
''')
    Path("bar.comm").write_text('''import json
from code_aster.Commands import *
DEBUT()
mesh=LIRE_MAILLAGE(FORMAT='ASTER',UNITE=20)
model=AFFE_MODELE(MAILLAGE=mesh,AFFE=_F(TOUT='OUI',PHENOMENE='MECANIQUE',MODELISATION='3D'))
mat=DEFI_MATERIAU(ELAS=_F(E=1000.,NU=0.))
field=AFFE_MATERIAU(MAILLAGE=mesh,AFFE=_F(TOUT='OUI',MATER=mat))
load=AFFE_CHAR_MECA(MODELE=model,DDL_IMPO=(_F(GROUP_NO='LEFT',DX=0.),_F(GROUP_NO='ALL',DY=0.,DZ=0.)),FORCE_NODALE=_F(GROUP_NO='RIGHT',FX=.25))
res=MECA_STATIQUE(MODELE=model,CHAM_MATER=field,EXCIT=_F(CHARGE=load))
res=CALC_CHAMP(reuse=res,RESULTAT=res,CONTRAINTE='SIEF_ELGA')
table=POST_RELEVE_T(ACTION=_F(OPERATION='EXTRACTION',INTITULE='displacement',RESULTAT=res,NOM_CHAM='DEPL',GROUP_NO='ALL',TOUT_CMP='OUI'))
IMPR_TABLE(TABLE=table,UNITE=8)
IMPR_RESU(FORMAT='MED',UNITE=80,RESU=_F(RESULTAT=res))
with open('/work/fields.json','w') as f: json.dump(table.EXTR_TABLE().values(),f)
FIN()
''')
    Path("bar.export").write_text('''P actions make_etude
P time_limit 240
P memory_limit 2048
P ncpus 1
P mpi_nbcpu 1
F comm /work/bar.comm D 1
F mail /work/bar.mail D 20
F mess /work/bar.mess R 6
F resu /work/bar.resu R 8
F rmed /work/bar.med R 80
''')
    execute("code_aster", ["--no-mpi", "--workdir", "/work/aster-work", "bar.export"])
    data = json.loads(Path("fields.json").read_text())
    nodes = [x.strip() for x in data["NOEUD"]]
    if len(nodes) != 8 or len(set(nodes)) != 8 or len(data["COOR_X"]) != 8:
        raise ValueError("incomplete Code_Aster nodal field")
    error = max(abs(dx - .001 * x) for x, dx in zip(data["COOR_X"], data["DX"]))
    if error > 1e-8:
        raise ValueError(f"elastic bar mismatch: {error}")
    return {"reference": "unit cube E=1000,nu=0,F=1,A=L=1: ux(right)=.001", "max_absolute_error": error,
            "nodal_fields": data}


def main():
    request = json.load(sys.stdin)
    tool = request["tool"]
    report = {"moose": moose, "elmer": elmer, "calculix": calculix,
              "openfoam": openfoam, "code_aster": code_aster}[tool]()
    report.update(tool=tool, operational_check="passed", scientific_admission="not_assessed")
    report["files"] = [{"name": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                         "size": p.stat().st_size}
                       for p in sorted(Path(".").rglob("*")) if p.is_file() and p.name != "program.py"]
    with zipfile.ZipFile("solver-case.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in report["files"]:
            archive.write(row["name"], row["name"])
    print(json.dumps(report, allow_nan=False, sort_keys=True))


if __name__ == "__main__":
    main()
