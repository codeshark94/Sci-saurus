"""Accepted manuscript, survey, and results assembly into a reviewable PDF candidate."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from scisaurus.core.documents import Documents
from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.source_spans import validate as validate_source_span
from scisaurus.core.store import ArtifactStore
from scisaurus.core.surveys import SurveyGate
from scisaurus.runtime.bibliographic_identity import normalize_doi, normalize_title
from scisaurus.runtime.results import validate_results_package


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def validate_paper_config(value):
    base_fields = {"schema_version", "paper_id", "title", "revision", "document_type", "manuscript_project_dir",
                   "survey_project_dir", "survey_ref", "assessment_ref", "results_package",
                   "evidence", "claims", "references", "authors", "keywords"}
    schema = value.get("schema_version") if isinstance(value, dict) else None
    if schema == "paper-release-score-1":
        _exact(value, base_fields, "paper configuration")
        storyline_ids = None
    elif schema == "paper-release-score-2":
        _exact(value, base_fields | {"storyline"}, "paper configuration")
        storyline = value["storyline"]
        _exact(storyline, {"id", "revision", "thesis", "beats"}, "paper storyline")
        _identifier(storyline["id"], "storyline id")
        if type(storyline["revision"]) is not int or storyline["revision"] < 1:
            raise ValidationError("storyline revision must be positive")
        _text(storyline["thesis"], "storyline thesis")
        if not isinstance(storyline["beats"], list) or len(storyline["beats"]) < 3:
            raise ValidationError("storyline requires at least three ordered beats")
        storyline_ids = set()
        for beat in storyline["beats"]:
            _exact(beat, {"id", "role", "proposition"}, "storyline beat")
            _identifier(beat["id"], "storyline beat id")
            if beat["id"] in storyline_ids or beat["role"] not in {
                    "motivation", "question", "hypothesis", "method", "result", "interpretation",
                    "limitation", "conclusion"}:
                raise ValidationError("storyline beat identity or role is invalid")
            storyline_ids.add(beat["id"])
            _text(beat["proposition"], "storyline beat proposition")
    else:
        raise ValidationError("unsupported paper release score")
    _identifier(value["paper_id"], "paper id")
    _text(value["title"], "paper title")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("paper revision must be positive")
    if value["document_type"] not in {"research_paper", "gap_report", "validation_report"}:
        raise ValidationError("document type must be research_paper, gap_report, or validation_report")
    for key in ("manuscript_project_dir", "survey_project_dir", "results_package"):
        path = Path(value[key])
        if not path.is_absolute() or not path.exists():
            raise ValidationError(f"{key} must be an existing absolute path")
    _text(value["survey_ref"], "survey ref"); _text(value["assessment_ref"], "assessment ref")
    for name in ("authors", "keywords"):
        if not isinstance(value[name], list) or not value[name]:
            raise ValidationError(f"{name} must be an explicit nonempty list")
        for item in value[name]:
            _text(item, name)
    evidence_ids = set()
    for item in value["evidence"]:
        fields = {"id", "kind", "locator", "quote"}
        if schema == "paper-release-score-2":
            fields.add("relation")
        if (isinstance(item, dict) and item.get("kind") == "literature"
                and {"start", "end", "quote_sha256"}.intersection(item)):
            fields.update({"start", "end", "quote_sha256"})
        _exact(item, fields, "claim evidence")
        _identifier(item["id"], "evidence id")
        allowed_kinds = ({"result", "literature", "procedure", "finding", "limitation"}
                         if schema == "paper-release-score-2" else {"result", "literature"})
        if item["id"] in evidence_ids or item["kind"] not in allowed_kinds:
            raise ValidationError("evidence identity or kind is invalid")
        _text(item["locator"], "evidence locator"); _text(item["quote"], "evidence quote")
        if schema == "paper-release-score-2" and item["relation"] not in {"support", "qualify", "context"}:
            raise ValidationError("evidence relation must be support, qualify, or context")
        if "start" in item and (type(item["start"]) is not int or type(item["end"]) is not int
                                or not 0 <= item["start"] < item["end"]
                                or not re.fullmatch(r"[0-9a-f]{64}", str(item["quote_sha256"]))):
            raise ValidationError("literature evidence span and quote SHA-256 are invalid")
        evidence_ids.add(item["id"])
    claim_ids = set()
    mapped_storyline_ids = set()
    for claim in value["claims"]:
        fields = {"id", "statement", "unit_ids", "evidence_ids"}
        if schema == "paper-release-score-2":
            fields.add("storyline_id")
        _exact(claim, fields, "paper claim")
        _identifier(claim["id"], "claim id"); _text(claim["statement"], "claim statement")
        if claim["id"] in claim_ids:
            raise ValidationError("paper claim identities must be unique")
        for name in ("unit_ids", "evidence_ids"):
            if not isinstance(claim[name], list) or not claim[name] or len(claim[name]) != len(set(claim[name])):
                raise ValidationError(f"claim {name} must be nonempty and unique")
        if set(claim["evidence_ids"]) - evidence_ids:
            raise ValidationError("paper claim references unknown evidence")
        if schema == "paper-release-score-2":
            if claim["storyline_id"] not in storyline_ids:
                raise ValidationError("paper claim references an unknown storyline beat")
            if not any(next(item for item in value["evidence"] if item["id"] == evidence_id)["relation"]
                       in {"support", "qualify"} for evidence_id in claim["evidence_ids"]):
                raise ValidationError("every storyline claim requires supporting or qualifying evidence")
            mapped_storyline_ids.add(claim["storyline_id"])
        claim_ids.add(claim["id"])
    if storyline_ids is not None and mapped_storyline_ids != storyline_ids:
        raise ValidationError("every storyline beat must map to at least one evidence-bound claim")
    keys = set()
    for reference in value["references"]:
        fields = {"key", "title", "authors", "year", "doi", "url", "source_ref"}
        if isinstance(reference, dict) and "identity_ref" in reference:
            fields.add("identity_ref")
        _exact(reference, fields, "reference")
        _identifier(reference["key"], "reference key")
        if reference["key"] in keys:
            raise ValidationError("reference keys must be unique")
        for name in ("title", "authors", "year", "source_ref"):
            _text(reference[name], f"reference {name}")
        for name in ("doi", "url"):
            if reference[name] is not None:
                _text(reference[name], f"reference {name}")
        if "identity_ref" in reference and reference["identity_ref"] is not None:
            _text(reference["identity_ref"], "reference identity_ref")
        keys.add(reference["key"])
    canonical_bytes(value)
    return value


def _latex(value):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
                    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
                    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(char, char) for char in value)


def _latex_with_citations(value):
    parts, position = [], 0
    for match in re.finditer(r"\[\[cite:([a-z][a-z0-9_-]{0,63})\]\]", value):
        parts.extend([_latex(value[position:match.start()]), r"\cite{" + match.group(1) + "}"])
        position = match.end()
    parts.append(_latex(value[position:]))
    return "".join(parts)


class PaperReleaseBuilder:
    """Prepare a pinned candidate; external submission and final approval remain separate."""

    def __init__(self, release_dir, config):
        self.dir = Path(release_dir).resolve()
        if (self.dir / "state" / "control.sqlite").exists():
            raise ValidationError("paper release build requires a new output directory")
        self.config = validate_paper_config(config)
        self.control = ControlStore(self.dir)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note=self.config["paper_id"])

    def close(self):
        if self.control is not None:
            self.control.close()
            self.control = None

    @staticmethod
    def _body(store, record):
        return json.loads(store.read_body(record["body_hash"]))

    def _manuscript(self):
        control = ControlStore(self.config["manuscript_project_dir"])
        try:
            store = ArtifactStore(control)
            final = store.head("command/results/final")
            if final is None:
                raise ValidationError("manuscript project has no final execution report")
            report = self._body(store, final)
            if report.get("status") != "accepted" or not report.get("incumbent_ref"):
                raise ValidationError("manuscript project has no independently accepted candidate")
            accepted = store.accepted(store.get(report["incumbent_ref"])["artifact_id"])
            if accepted is None or accepted["artifact_ref"] != report["incumbent_ref"]:
                raise ValidationError("manuscript report does not name its current accepted manifest")
            documents = Documents(control, store)
            tree = documents.get_tree(report["incumbent_ref"])
            groups, units = [], {}
            for node in tree["units"]:
                heading = documents.read_unit(node["ref"])["text"]
                children = []
                for child in node["children"]:
                    unit = documents.read_unit(child["ref"])
                    unit_id = store.get(child["ref"])["artifact_id"].split("/")[-1]
                    children.append({"id": unit_id, "kind": unit["kind"], "text": unit["text"], "ref": child["ref"]})
                    units[unit_id] = children[-1]
                groups.append({"title": heading, "units": children})
            verification_refs = [candidate.get("verification_ref") for candidate in report.get("candidates", [])
                                 if candidate.get("candidate_ref") == report["incumbent_ref"]]
            if len(verification_refs) != 1 or not verification_refs[0]:
                raise ValidationError("accepted manuscript lacks one integrated verification record")
            return {"title": self.config["title"], "groups": groups, "units": units,
                    "manifest_ref": report["incumbent_ref"], "score_ref": report["score_ref"],
                    "verification_ref": verification_refs[0], "event_chain": control.verify_chain()}
        finally:
            control.close()

    def _survey(self):
        control = ControlStore(self.config["survey_project_dir"])
        try:
            store = ArtifactStore(control)
            gate = SurveyGate(control, store)
            gate.require_current(self.config["survey_ref"])
            assessment = gate.require_current_assessment(self.config["assessment_ref"])
            body = self._body(store, assessment)
            if self.config["document_type"] == "research_paper" and body["state"] != "eligible_for_experiment":
                raise ValidationError("a research paper candidate requires an experiment-eligible accepted gap assessment")
            survey = self._body(store, store.get(self.config["survey_ref"]))
            sources = {ref: self._body(store, store.get(ref)) for ref in survey["source_refs"]}
            identities = {ref: self._body(store, store.get(ref))
                          for ref in survey.get("identity_refs", [])}
            return {"state": body["state"], "sources": sources,
                    "schema_version": survey["schema_version"], "identities": identities,
                    "survey_ref": self.config["survey_ref"], "assessment_ref": self.config["assessment_ref"],
                    "event_chain": control.verify_chain()}
        finally:
            control.close()

    def _bind(self, manuscript, survey, results):
        if results["schema_version"] == "results-package-2":
            provenance = results["provenance"]
            if provenance["literature_survey_ref"] is not None and (
                    provenance["literature_survey_ref"] != survey["survey_ref"]
                    or provenance["literature_assessment_ref"] != survey["assessment_ref"]):
                raise ValidationError("generated results and paper must use the same accepted literature basis")
        evidence = {item["id"]: item for item in self.config["evidence"]}
        metrics = {item["id"]: item for item in results["metrics"]}
        procedures = {item["id"]: item for item in results["procedures"]}
        findings = {item["id"]: item for item in results["findings"]}
        limitations = {f"limitation/{index}": text for index, text in enumerate(results["limitations"])}
        for item in evidence.values():
            if item["kind"] == "result":
                if item["locator"] not in metrics or item["quote"] != metrics[item["locator"]]["presentation"]:
                    raise ValidationError("result evidence must quote the exact metric presentation")
            elif item["kind"] == "procedure":
                if (item["locator"] not in procedures
                        or item["quote"] != procedures[item["locator"]]["description"]):
                    raise ValidationError("procedure evidence must quote the exact procedure description")
            elif item["kind"] == "finding":
                if item["locator"] not in findings or item["quote"] != findings[item["locator"]]["statement"]:
                    raise ValidationError("finding evidence must quote the exact finding statement")
            elif item["kind"] == "limitation":
                if item["locator"] not in limitations or item["quote"] != limitations[item["locator"]]:
                    raise ValidationError("limitation evidence must quote the exact indexed limitation")
            else:
                source = survey["sources"].get(item["locator"])
                if source is None:
                    raise ValidationError("literature evidence must identify an accepted survey source")
                proof = {"work_id": source["work_id"], "source_ref": item["locator"], "quote": item["quote"]}
                if all(key in item for key in ("start", "end", "quote_sha256")):
                    proof.update({key: item[key] for key in ("start", "end", "quote_sha256")})
                validate_source_span(proof, source,
                                     require_span=survey["schema_version"] == "literature-survey-3")
        for claim in self.config["claims"]:
            if set(claim["unit_ids"]) - set(manuscript["units"]):
                raise ValidationError("claim references a unit outside the accepted manuscript")
            text = " ".join(manuscript["units"][unit_id]["text"] for unit_id in claim["unit_ids"])
            if claim["statement"] not in text:
                raise ValidationError("claim statement is absent from its bound manuscript units")
            for evidence_id in claim["evidence_ids"]:
                if evidence[evidence_id]["quote"] not in text:
                    raise ValidationError("claim unit omits the exact result or source phrase it relies on")
        if self.config["schema_version"] == "paper-release-score-2":
            claims_by_beat = {}
            for claim in self.config["claims"]:
                claims_by_beat.setdefault(claim["storyline_id"], []).append(claim)
            unit_positions = {unit_id: index for index, unit_id in enumerate(manuscript["units"])}
            beat_positions = []
            for beat in self.config["storyline"]["beats"]:
                bound_units = {unit_id for claim in claims_by_beat[beat["id"]] for unit_id in claim["unit_ids"]}
                text = " ".join(manuscript["units"][unit_id]["text"] for unit_id in bound_units)
                if beat["proposition"] not in text:
                    raise ValidationError("accepted manuscript omits a fixed storyline proposition")
                beat_positions.append(min(unit_positions[unit_id] for unit_id in bound_units))
            if beat_positions != sorted(beat_positions):
                raise ValidationError("accepted manuscript does not preserve the ordered storyline beats")
        manuscript_text = " ".join(unit["text"] for unit in manuscript["units"].values())
        required_results = [procedure["description"] for procedure in results["procedures"]]
        required_results += [metric["presentation"] for metric in results["metrics"]]
        required_results += [finding["statement"] for finding in results["findings"]]
        required_results += list(results["limitations"])
        if any(item not in manuscript_text for item in required_results):
            raise ValidationError("accepted manuscript omits a supplied procedure, metric, finding, or limitation")
        source_refs = set(survey["sources"])
        reference_keys = {item["key"] for item in self.config["references"]}
        markers = {match.group(1) for unit in manuscript["units"].values()
                   for match in re.finditer(r"\[\[cite:([a-z][a-z0-9_-]{0,63})\]\]", unit["text"])}
        if markers != reference_keys or any(reference["source_ref"] not in source_refs for reference in self.config["references"]):
            raise ValidationError("citations must exactly match references pinned by the accepted survey")
        for reference in self.config["references"]:
            if reference["doi"] is None:
                continue
            identity = survey["identities"].get(reference.get("identity_ref"))
            source = survey["sources"][reference["source_ref"]]
            if (identity is None or identity.get("status") != "verified"
                    or normalize_doi(reference["doi"]) != normalize_doi(identity.get("doi"))
                    or identity.get("work_id") != source.get("work_id")):
                raise ValidationError("DOI references require a verified survey bibliographic identity")
            title = next((check for check in identity.get("checks", []) if check.get("field") == "title"), None)
            year = next((check for check in identity.get("checks", []) if check.get("field") == "year"), None)
            if (title is None or title.get("outcome") != "match"
                    or normalize_title(reference["title"]) != normalize_title(title.get("openalex"))
                    or year is None or year.get("outcome") != "match"
                    or str(year.get("openalex")) != reference["year"]):
                raise ValidationError("reference fields must match the verified bibliographic identity")
        result = {"schema_version": "paper-claim-index-2", "claims": self.config["claims"],
                  "evidence": self.config["evidence"], "references": self.config["references"]}
        if self.config["schema_version"] == "paper-release-score-2":
            result.update(schema_version="paper-claim-index-3", storyline=self.config["storyline"])
        return result

    def _tex(self, manuscript, results=None):
        lines = [r"\documentclass[11pt]{article}", r"\usepackage[margin=1in]{geometry}",
                 r"\usepackage[hidelinks]{hyperref}", r"\usepackage{microtype}", r"\usepackage[T1]{fontenc}",
                 r"\usepackage{graphicx}",
                 r"\title{" + _latex(manuscript["title"]) + "}",
                 r"\author{" + _latex(", ".join(self.config["authors"])) + "}", r"\date{}", r"\begin{document}",
                 r"\maketitle"]
        for group in manuscript["groups"]:
            lines.append(r"\section{" + _latex(group["title"]) + "}")
            for unit in group["units"]:
                if unit["kind"] == "list_item":
                    lines.extend([r"\begin{itemize}", r"\item " + _latex_with_citations(unit["text"]), r"\end{itemize}"])
                elif unit["kind"] in {"code", "json"}:
                    lines.extend([r"\begin{verbatim}", unit["text"], r"\end{verbatim}"])
                else:
                    lines.extend([_latex_with_citations(unit["text"]), ""])
        figures = [asset for asset in (results or {}).get("assets", []) if asset.get("role") == "figure"]
        if figures:
            lines.append(r"\section{Figures}")
            for asset in figures:
                lines.extend([r"\begin{figure}[htbp]", r"\centering",
                              r"\includegraphics[width=0.78\linewidth]{\detokenize{../assets/" + asset["path"] + "}}",
                              r"\caption{" + _latex(asset["caption"]) + "}", r"\end{figure}"])
        lines.append(r"\section*{Keywords}")
        lines.append(_latex(", ".join(self.config["keywords"])))
        lines.extend([r"\begin{thebibliography}{99}", r"\footnotesize", r"\raggedright",
                      r"\setlength{\itemsep}{0.25em}"])
        for reference in self.config["references"]:
            suffix = (" doi:" + reference["doi"]) if reference["doi"] else (" " + reference["url"] if reference["url"] else "")
            lines.append(r"\bibitem{" + reference["key"] + "} " + _latex(
                f"{reference['authors']} ({reference['year']}). {reference['title']}.{suffix}"))
        lines.extend([r"\end{thebibliography}", r"\end{document}", ""])
        return "\n".join(lines)

    def build(self, *, compile_script):
        manuscript, survey = self._manuscript(), self._survey()
        results_path = Path(self.config["results_package"])
        results = validate_results_package(json.loads(results_path.read_text()), base_dir=results_path.parent)
        claim_index = self._bind(manuscript, survey, results)
        source_dir = self.dir / "output" / "source"
        pdf_dir = self.dir / "output" / "pdf"
        source_dir.mkdir(parents=True); pdf_dir.mkdir(parents=True)
        copied_assets = []
        if results["assets"]:
            asset_dir = self.dir / "output" / "assets"
            asset_dir.mkdir(parents=True)
            for asset in results["assets"]:
                destination = asset_dir / asset["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(results_path.parent / asset["path"], destination)
                copied_assets.append({**asset, "release_path": str(destination.relative_to(self.dir))})
        tex = self._tex(manuscript, results)
        (source_dir / "main.tex").write_text(tex)
        entries = []
        for reference in self.config["references"]:
            fields = [f"  title = {{{reference['title']}}}", f"  author = {{{reference['authors']}}}",
                      f"  year = {{{reference['year']}}}"]
            if reference["doi"]:
                fields.append(f"  doi = {{{reference['doi']}}}")
            if reference["url"]:
                fields.append(f"  url = {{{reference['url']}}}")
            entries.append("@article{" + reference["key"] + ",\n" + ",\n".join(fields) + "\n}")
        bibliography = "\n\n".join(entries) + "\n"
        (source_dir / "references.bib").write_text(bibliography)
        (source_dir / "claim-index.json").write_bytes(canonical_bytes(claim_index))
        compile_result = subprocess.run(
            [sys.executable, str(compile_script), str(source_dir / "main.tex"),
             "--output-directory", str(pdf_dir), "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        if compile_result.returncode != 0 or not (pdf_dir / "main.pdf").is_file():
            raise ValidationError("LaTeX compilation failed: " + compile_result.stderr[-2000:])
        pdf = pdf_dir / f"{self.config['paper_id']}.pdf"
        (pdf_dir / "main.pdf").replace(pdf)
        pdfinfo = shutil.which("pdfinfo")
        pdftoppm = shutil.which("pdftoppm")
        if not pdfinfo or not pdftoppm:
            raise ValidationError("PDF visual verification requires pdfinfo and pdftoppm")
        info = subprocess.run([pdfinfo, str(pdf)], capture_output=True, text=True, check=True, timeout=30).stdout
        match = re.search(r"^Pages:\s+([0-9]+)$", info, re.MULTILINE)
        pages = int(match.group(1)) if match else 0
        render_dir = self.dir / "output" / "rendered"
        render_dir.mkdir()
        subprocess.run([pdftoppm, "-png", "-r", "120", str(pdf), str(render_dir / "page")],
                       capture_output=True, check=True, timeout=120)
        images = sorted(render_dir.glob("page-*.png"))
        visual = {"schema_version": "paper-visual-check-1", "pages": pages,
                  "rendered_pages": [str(path.relative_to(self.dir)) for path in images],
                  "all_pages_rendered": pages > 0 and len(images) == pages,
                  "pdfinfo": info, "manual_review_status": "pending_principal_review"}
        if not visual["all_pages_rendered"] or any(path.stat().st_size < 1000 for path in images):
            raise ValidationError("rendered PDF pages are missing or empty")
        (self.dir / "output" / "visual-review.json").write_bytes(canonical_bytes(visual))
        inputs = self.store.publish_artifact(logical_id="inputs/paper-score", artifact_type="note", author="principal",
            body=canonical_bytes(self.config), media_type="application/json")
        results_record = self.store.publish_artifact(logical_id="inputs/results-package", artifact_type="results_package",
            author="principal", body=canonical_bytes(results), media_type="application/json")
        pdf_record = self.store.publish_artifact(logical_id="releases/paper-pdf", artifact_type="release_note",
            author="archivist", body=pdf.read_bytes(), media_type="application/pdf",
            inputs=[{"ref": inputs["artifact_ref"], "purpose": "subject"},
                    {"ref": results_record["artifact_ref"], "purpose": "subject"}])
        manifest = {"schema_version": ("paper-release-candidate-2" if self.config["schema_version"] == "paper-release-score-2"
                                        else "paper-release-candidate-1"), "paper_id": self.config["paper_id"],
                    "revision": self.config["revision"], "status": "needs_principal_approval",
                    "document_type": self.config["document_type"], "paper_score_ref": inputs["artifact_ref"],
                    "results_ref": results_record["artifact_ref"], "pdf_ref": pdf_record["artifact_ref"],
                    "pdf_sha256": sha256_hex(pdf.read_bytes()), "manuscript_manifest_ref": manuscript["manifest_ref"],
                    "manuscript_score_ref": manuscript["score_ref"], "manuscript_verification_ref": manuscript["verification_ref"],
                    "survey_ref": survey["survey_ref"], "assessment_ref": survey["assessment_ref"],
                    "gap_state": survey["state"], "claim_index_sha256": sha256_hex(canonical_bytes(claim_index)),
                    "storyline_sha256": (sha256_hex(canonical_bytes(self.config["storyline"]))
                                          if self.config["schema_version"] == "paper-release-score-2" else None),
                    "source_sha256": sha256_hex(tex.encode()), "assets": copied_assets, "visual_check": visual,
                    "external_submission": "excluded"}
        release = self.store.publish_artifact(logical_id="releases/candidate", artifact_type="release_note",
            author="archivist", body=canonical_bytes(manifest), media_type="application/json",
            inputs=[{"ref": pdf_record["artifact_ref"], "purpose": "subject"},
                    {"ref": results_record["artifact_ref"], "purpose": "subject"}])
        (self.dir / "output" / "release-manifest.json").write_bytes(canonical_bytes(manifest))
        result = {**manifest, "release_ref": release["artifact_ref"], "event_chain": self.control.verify_chain()}
        self.close()
        return result
