"""Bounded text extraction from publicly retrievable scholarly PDF files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time


PARSER_NAME = "poppler-pdftotext"
DEFAULT_MAX_PDF_BYTES = 25_000_000
MAX_PDF_BYTES = 100_000_000
DEFAULT_RESULT_MAX_BYTES = 10_000_000
PDF_RESULT_METADATA_RESERVE_BYTES = 262_144
MAX_STDERR_BYTES = 8192


def pdf_capture_budget(result_max_bytes, max_text_chars, configured_pdf_max_bytes):
    """Bound one PDF capture so its serialized worker result fits the IPC limit.

    The reserve covers the text twice (plain text and base64 capture), worst-case
    JSON escaping/UTF-8 expansion, the result envelope, and execution metadata.
    The source PDF itself is stored once as base64 in the execution record.
    """
    if (type(result_max_bytes) is not int or result_max_bytes < 1
            or type(max_text_chars) is not int or max_text_chars < 1
            or type(configured_pdf_max_bytes) is not int or configured_pdf_max_bytes < 1):
        raise ValueError("PDF capture budget inputs must be positive integers")
    text_reserve = max_text_chars * 12
    fixed_reserve = PDF_RESULT_METADATA_RESERVE_BYTES
    available = result_max_bytes - text_reserve - fixed_reserve
    if available <= 0:
        return 0
    return min(configured_pdf_max_bytes, (available // 4) * 3)


def parser_path():
    """Return the installed Poppler extractor without assuming a platform path."""
    return shutil.which("pdftotext")


def parser_identity(executable=None):
    executable = executable or parser_path()
    if not executable or not Path(executable).is_file():
        return None
    path = str(Path(executable).resolve())
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        version = subprocess.run(
            [path, "-v"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version_text = (version.stdout + version.stderr).decode("utf-8", errors="replace").strip()
    if version.returncode != 0 or not version_text:
        return None
    return {"name": PARSER_NAME, "path": path, "version": version_text.splitlines()[0],
            "sha256": digest}


def _run_bounded(command, pdf_bytes, *, timeout, max_output_bytes):
    """Stream Poppler output with a hard byte cap and a wall-clock deadline."""
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as exc:
        return {"outcome": "unsupported_capability", "stdout": b"", "stderr": str(exc).encode(),
                "returncode": None, "truncated": False}

    stdout, stderr = bytearray(), bytearray()
    write_errors = []

    def feed_input():
        try:
            process.stdin.write(pdf_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError) as exc:
            write_errors.append(str(exc))

    writer = threading.Thread(target=feed_input, name="scisaurus-pdf-input", daemon=True)
    writer.start()
    timed_out = output_limited = False
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "stdout":
                    remaining_bytes = max_output_bytes - len(stdout)
                    if len(chunk) > remaining_bytes:
                        stdout.extend(chunk[:remaining_bytes])
                        output_limited = True
                        process.kill()
                        break
                    stdout.extend(chunk)
                elif len(stderr) < MAX_STDERR_BYTES:
                    stderr.extend(chunk[:MAX_STDERR_BYTES - len(stderr)])
            if output_limited:
                break
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        writer.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    return {"outcome": "timeout" if timed_out else None, "stdout": bytes(stdout),
            "stderr": bytes(stderr), "returncode": process.returncode,
            "truncated": output_limited, "write_errors": write_errors}


def extract_pdf_text(pdf_bytes, *, max_chars, timeout, executable=None):
    """Extract a bounded text layer while retaining parser and content identity."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise ValueError("PDF input must be nonempty bytes")
    if (type(max_chars) is not int or not 1 <= max_chars <= 999_999
            or type(timeout) not in (int, float) or timeout <= 0):
        raise ValueError("PDF extraction limits are invalid")
    marker = pdf_bytes[:1024].find(b"%PDF-")
    base = {"input_bytes": len(pdf_bytes), "input_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "capture_truncated": False, "capture_incomplete": False}
    if marker < 0:
        return {"outcome": "parse_error", "text": "", "metadata": base,
                "error": "Downloaded content does not contain a PDF header"}
    identity = parser_identity(executable)
    if identity is None:
        return {"outcome": "unsupported_capability", "text": "", "metadata": base,
                "error": "Poppler pdftotext is not installed or could not be identified"}

    output_limit = max_chars * 4 + 4
    completed = _run_bounded(
        [identity["path"], "-enc", "UTF-8", "-nopgbrk", "-eol", "unix", "-", "-"],
        pdf_bytes, timeout=float(timeout), max_output_bytes=output_limit,
    )
    text = completed["stdout"].decode("utf-8-sig", errors="replace")
    metadata = {**base, "parser": identity, "output_bytes": len(completed["stdout"]),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "parser_returncode": completed["returncode"],
                "parser_stderr": completed["stderr"].decode("utf-8", errors="replace")}
    if completed["outcome"] == "timeout":
        return {"outcome": "timeout", "text": text[:max_chars], "metadata": metadata,
                "error": "PDF text extraction exceeded its time limit"}
    if completed["truncated"] or len(text) > max_chars:
        metadata["capture_truncated"] = True
        return {"outcome": "partial", "text": text[:max_chars], "metadata": metadata,
                "error": "Extracted PDF text exceeded the configured character limit"}
    if completed["returncode"] != 0:
        return {"outcome": "parse_error", "text": "", "metadata": metadata,
                "error": "Poppler could not extract text from the PDF"}
    if not text.strip():
        return {"outcome": "unsupported_capability", "text": "", "metadata": metadata,
                "error": "PDF has no extractable text layer; OCR was not attempted"}
    return {"outcome": "ok", "text": text, "metadata": metadata}
