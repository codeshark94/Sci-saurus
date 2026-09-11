#!/usr/bin/env python3
"""Run the three-role manuscript review gate against a frozen JSON document."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scisaurus.runtime.manuscript_review import ManuscriptReviewRunner
from scisaurus.core.schema import canonical_bytes


def image_descriptor(path):
    path = path.resolve(strict=True)
    suffix = path.suffix.casefold()
    media_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix)
    if media_type is None:
        raise ValueError("--image must point to a PNG or JPEG file")
    return {"path": str(path), "media_type": media_type,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manuscript", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image", action="append", default=[], type=Path,
                        help="Attach a PNG/JPEG image to every independent review")
    args = parser.parse_args()
    manuscript = json.loads(args.manuscript.read_text())
    config = json.loads(args.config.read_text())
    runner = ManuscriptReviewRunner(config["model"], reviewers=config.get("reviewers"),
                                    max_workers=config.get("max_workers", 3))
    images = [image_descriptor(path) for path in args.image]
    result = runner.run(manuscript, images=images)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_bytes(result))
    print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
                      "model_calls": result["model_calls"]}, indent=2))


if __name__ == "__main__":
    main()
