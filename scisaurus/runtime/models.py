"""Bounded calls to explicitly configured Ollama or compatible GPU servers."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from scisaurus.core.errors import ValidationError


SAMPLING_FIELDS = frozenset({
    "temperature", "top_p", "seed", "presence_penalty", "frequency_penalty",
})
OLLAMA_SAMPLING_FIELDS = frozenset({"temperature", "top_p", "seed"})
# OpenAI-compatible providers commonly expose ``seed`` as a signed int64.
# Keep internally derived seeds inside that wire-level contract so a valid
# exploration hash cannot become a provider-side 400.
MAX_PROVIDER_SEED = (1 << 63) - 1

# Sampling changes how a role explores or checks a response; it does not
# replace the role's prompt or its validation contract.  These defaults are
# deliberately modest so a model can vary the search direction while the
# evidence and review roles remain conservative.  A model configuration may
# override any profile below through ``role_profiles``.
DEFAULT_ROLE_PROFILES = {
    "topic_discovery": {"temperature": 1.1, "top_p": 0.95, "presence_penalty": 0.2},
    "research.topic-discovery": {"temperature": 1.1, "top_p": 0.95, "presence_penalty": 0.2},
    "research.search-planner": {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 0.15},
    "methods.blind-search-planner": {"temperature": 1.05, "top_p": 0.95, "presence_penalty": 0.2},
    "research.literature-mapper": {"temperature": 0.25, "top_p": 0.9},
    "research.literature-reviewer": {"temperature": 0.25, "top_p": 0.9},
    "research.topic-maturity-reviewer": {"temperature": 0.2, "top_p": 0.9},
    "strategy.interpretation": {"temperature": 0.75, "top_p": 0.92},
    "strategy.argument": {"temperature": 0.7, "top_p": 0.92},
    "strategy.argument-reviewer": {"temperature": 0.2, "top_p": 0.9},
    "editorial.writer": {"temperature": 0.65, "top_p": 0.92},
    "scientific-author": {"temperature": 0.65, "top_p": 0.92},
    "editorial.surgical-editor": {"temperature": 0.45, "top_p": 0.9},
    "review.science": {"temperature": 0.2, "top_p": 0.9},
    "review.methods": {"temperature": 0.2, "top_p": 0.9},
    "review.ai_smell": {"temperature": 0.8, "top_p": 0.95, "presence_penalty": 0.2},
    "review.human_scientist": {"temperature": 0.35, "top_p": 0.9},
    "review.editorial_compression": {"temperature": 0.25, "top_p": 0.9},
    "review.journal_editor": {"temperature": 0.2, "top_p": 0.9},
    "review.arbiter": {"temperature": 0.15, "top_p": 0.9},
    "review.synthesizer": {"temperature": 0.3, "top_p": 0.9},
}


def _validate_sampling_options(options, *, name="sampling options"):
    """Validate provider sampling controls before a request is dispatched."""
    if not isinstance(options, dict):
        raise ValidationError(f"{name} must be an object")
    unknown = set(options) - SAMPLING_FIELDS
    if unknown:
        raise ValidationError(f"{name} contains unsupported fields: {', '.join(sorted(unknown))}")
    for field in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if field not in options:
            continue
        value = options[field]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValidationError(f"{name}.{field} must be finite")
        if field == "temperature" and not 0 <= value <= 2:
            raise ValidationError(f"{name}.temperature must be between 0 and 2")
        if field == "top_p" and not 0 < value <= 1:
            raise ValidationError(f"{name}.top_p must be greater than 0 and at most 1")
        if field in {"presence_penalty", "frequency_penalty"} and not -2 <= value <= 2:
            raise ValidationError(f"{name}.{field} must be between -2 and 2")
    if "seed" in options and (
            type(options["seed"]) is not int
            or not 0 <= options["seed"] <= MAX_PROVIDER_SEED):
        raise ValidationError(
            f"{name}.seed must be an integer between 0 and {MAX_PROVIDER_SEED}")
    return options


def resolve_model_config(model, *, role=None, overrides=None):
    """Resolve a model config plus a role's sampling profile.

    The resolver keeps ``role_profiles`` out of the provider payload while
    allowing one shared model file to express different exploration and
    verification personalities.  Explicit global sampling fields win over
    built-in defaults; a named role profile and call-site overrides then win
    over those global values.
    """
    if not isinstance(model, dict):
        raise ValidationError("model configuration must be an object")
    base = dict(model)
    profiles = base.pop("role_profiles", {})
    if not isinstance(profiles, dict):
        raise ValidationError("model.role_profiles must be an object")
    global_sampling = {key: base.pop(key) for key in list(base) if key in SAMPLING_FIELDS}
    for profile_name, profile in profiles.items():
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValidationError("model.role_profiles keys must be nonempty strings")
        _validate_sampling_options(profile, name=f"model.role_profiles.{profile_name}")
    _validate_sampling_options(global_sampling, name="model sampling options")
    if overrides is not None:
        _validate_sampling_options(overrides, name="sampling overrides")
    sampling = dict(DEFAULT_ROLE_PROFILES.get(role, {}))
    sampling.update(global_sampling)
    if role is not None:
        sampling.update(profiles.get(role, {}))
    if overrides:
        sampling.update(overrides)
    _validate_sampling_options(sampling)
    base.update(sampling)
    return base


class ModelCallError(RuntimeError):
    """An invocation failed; unknown outcomes must retain their reservation."""
    def __init__(self, message, *, outcome_known=False):
        super().__init__(message)
        self.outcome_known = outcome_known


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelCallError("model endpoint redirected; configure the final endpoint explicitly", outcome_known=True)


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    usage: dict
    elapsed_seconds: float
    finish_reason: str

    def json_object(self):
        try:
            value = json.loads(self.text)
        except (ValueError, TypeError) as exc:
            raise ValidationError("model output is not a complete JSON object") from exc
        if not isinstance(value, dict):
            raise ValidationError("model output must be a JSON object")
        return value


class ModelClient:
    def __init__(self, *, base_url: str, model: str, protocol: str,
                 timeout_seconds: float, max_output_tokens: int,
                 auth_env: str | None = None, max_response_bytes: int = 2_000_000,
                 reasoning_effort: str | None = None, output_format: str | None = None,
                 max_image_bytes: int = 7_000_000, max_request_bytes: int = 10_000_000,
                 max_retries: int = 2, retry_backoff_seconds: float = 1.0,
                 temperature: float | None = None, top_p: float | None = None,
                 seed: int | None = None, presence_penalty: float | None = None,
                 frequency_penalty: float | None = None):
        if not isinstance(base_url, str):
            raise ValidationError("model base_url must be a URL string")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValidationError("model base_url must be an HTTP(S) URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValidationError("model base_url cannot contain query parameters or fragments")
        if protocol not in {"ollama", "openai_compatible"}:
            raise ValidationError("model protocol must be ollama or openai_compatible")
        if not isinstance(model, str) or not model.strip() or model == "runtime_required":
            raise ValidationError("an explicit model name is required")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValidationError("max_output_tokens must be a positive integer")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValidationError("model timeout must be finite and positive")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValidationError("response byte limit must be a positive integer")
        if type(max_image_bytes) is not int or max_image_bytes <= 0:
            raise ValidationError("image byte limit must be a positive integer")
        if type(max_request_bytes) is not int or max_request_bytes <= 0:
            raise ValidationError("request byte limit must be a positive integer")
        if max_image_bytes >= max_request_bytes:
            raise ValidationError("image byte limit must leave room inside the request byte limit")
        if type(max_retries) is not int or max_retries < 0 or max_retries > 8:
            raise ValidationError("max_retries must be an integer between 0 and 8")
        if type(retry_backoff_seconds) not in (int, float) or not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValidationError("retry_backoff_seconds must be finite and non-negative")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}
        ):
            raise ValidationError("reasoning_effort must be none, low, medium, high, or xhigh when configured")
        if output_format is not None and output_format != "json_object":
            raise ValidationError("output_format must be json_object when configured")
        sampling = {
            key: value for key, value in {
                "temperature": temperature, "top_p": top_p, "seed": seed,
                "presence_penalty": presence_penalty, "frequency_penalty": frequency_penalty,
            }.items() if value is not None
        }
        _validate_sampling_options(sampling)
        if protocol != "openai_compatible" and (reasoning_effort is not None or output_format is not None):
            raise ValidationError("reasoning_effort and output_format require the openai_compatible protocol")
        if auth_env is not None and (not isinstance(auth_env, str) or not auth_env or not os.environ.get(auth_env)):
            raise ValidationError("configured model authentication environment variable is absent")
        self.base_url, self.model, self.protocol = base_url.rstrip("/"), model, protocol
        self.timeout_seconds, self.max_output_tokens = timeout_seconds, max_output_tokens
        self.max_response_bytes, self.auth_env = max_response_bytes, auth_env
        self.reasoning_effort, self.output_format = reasoning_effort, output_format
        self.max_image_bytes, self.max_request_bytes = max_image_bytes, max_request_bytes
        self.max_retries, self.retry_backoff_seconds = max_retries, float(retry_backoff_seconds)
        self.temperature = temperature
        self.top_p = top_p
        self.seed = seed
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty

    @staticmethod
    def _read_image(image):
        if not isinstance(image, dict) or set(image) != {"path", "media_type", "sha256"}:
            raise ValidationError("each model image requires exactly path, media_type, and sha256")
        path, media_type, expected = image["path"], image["media_type"], image["sha256"]
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or media_type not in {"image/png", "image/jpeg"}
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValidationError("model image descriptor is invalid")
        try:
            resolved = Path(path).resolve(strict=True)
            if not resolved.is_file():
                raise OSError("not a regular file")
            body = resolved.read_bytes()
        except OSError as exc:
            raise ValidationError("model image is unavailable") from exc
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise ValidationError("model image content does not match its pinned SHA-256")
        if media_type == "image/png" and not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValidationError("model image media type does not match PNG bytes")
        if media_type == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
            raise ValidationError("model image media type does not match JPEG bytes")
        return body, media_type

    def complete(self, *, system: str, prompt: str, images=None) -> ModelResult:
        if not isinstance(system, str) or not isinstance(prompt, str):
            raise ValidationError("model system and prompt content must be strings")
        images = [] if images is None else images
        if not isinstance(images, list) or len(images) > 16:
            raise ValidationError("model images must be a list containing at most 16 items")
        if images and self.protocol != "openai_compatible":
            raise ValidationError("multimodal image input requires the openai_compatible protocol")
        parts, total = [{"type": "text", "text": prompt}], 0
        for descriptor in images:
            raw, media_type = self._read_image(descriptor)
            total += len(raw)
            if total > self.max_image_bytes:
                raise ValidationError("combined model images exceed the configured byte limit")
            encoded = base64.b64encode(raw).decode("ascii")
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{encoded}"}})
        user_content = parts if images else prompt
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
        body = {"model": self.model, "messages": messages, "stream": False}
        sampling = {
            key: value for key, value in {
                "temperature": self.temperature, "top_p": self.top_p, "seed": self.seed,
                "presence_penalty": self.presence_penalty,
                "frequency_penalty": self.frequency_penalty,
            }.items() if value is not None
        }
        if self.protocol == "ollama":
            path = "/api/chat"
            # Ollama's native options expose temperature/top-p/seed but not
            # the OpenAI presence/frequency penalty names.
            body["options"] = {
                "num_predict": self.max_output_tokens,
                **{key: value for key, value in sampling.items() if key in OLLAMA_SAMPLING_FIELDS},
            }
        else:
            path = "/chat/completions"
            body["max_tokens"] = self.max_output_tokens
            body.update(sampling)
            if self.reasoning_effort is not None:
                body["reasoning_effort"] = self.reasoning_effort
            if self.output_format is not None:
                body["response_format"] = {"type": self.output_format}
        headers = {"Content-Type": "application/json"}
        if self.auth_env:
            key = os.environ.get(self.auth_env)
            if not key:
                raise ModelCallError("model authentication environment variable is absent", outcome_known=True)
            headers["Authorization"] = "Bearer " + key
        wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if len(wire) > self.max_request_bytes:
            raise ValidationError("model request exceeds the configured byte limit")
        request = urllib.request.Request(self.base_url + path, wire, headers)
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
        attempt = 0
        parsed = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelCallError("model request deadline exceeded") from None
            try:
                with urllib.request.build_opener(_NoRedirect()).open(
                        request, timeout=max(0.1, remaining)) as response:
                    # ``HTTPResponse.read(n)`` can legally return a short
                    # chunk and then wait for more bytes.  A provider that
                    # trickles output would therefore evade the original
                    # one-shot socket timeout.  Read in bounded chunks and
                    # re-check the absolute request deadline after every
                    # chunk so the whole response, including body transfer,
                    # stays inside one budget.
                    chunks = []
                    total = 0
                    while total <= self.max_response_bytes:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ModelCallError("model request deadline exceeded")
                        try:
                            chunk = response.read(min(65536, self.max_response_bytes + 1 - total))
                        except TimeoutError:
                            raise ModelCallError("model request deadline exceeded") from None
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    raw = b"".join(chunks)
            except urllib.error.HTTPError as exc:
                code = exc.code
                retry_after = exc.headers.get("Retry-After")
                exc.close()
                if code in retryable_statuses and attempt < self.max_retries:
                    delay = self.retry_backoff_seconds * (2 ** attempt)
                    try:
                        if retry_after is not None:
                            delay = max(delay, min(60.0, float(retry_after)))
                    except (TypeError, ValueError):
                        pass
                    if time.monotonic() + delay >= deadline:
                        raise ModelCallError(f"model HTTP request failed with status {code}",
                                              outcome_known=400 <= code < 500) from None
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise ModelCallError(f"model HTTP request failed with status {code}",
                                     outcome_known=400 <= code < 500) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ModelCallError(f"model transport failed: {type(exc).__name__}") from None
            if len(raw) > self.max_response_bytes:
                raise ModelCallError("model response exceeded the configured byte limit")
            try:
                data = json.loads(raw)
                if self.protocol == "ollama":
                    if data.get("done") is not True:
                        raise ValueError("incomplete response")
                    text = data["message"]["content"]
                    reason = data.get("done_reason", "unknown")
                    usage = {k: data[source] for k, source in
                             (("input_tokens", "prompt_eval_count"), ("output_tokens", "eval_count")) if source in data}
                else:
                    choice = data["choices"][0]
                    text, reason = choice["message"]["content"], choice["finish_reason"]
                    usage = {k: data["usage"][source] for k, source in
                             (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"))
                             if source in data.get("usage", {})}
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("empty text")
                if any(type(value) is not int or value < 0 for value in usage.values()):
                    raise ValueError("invalid usage")
                if reason not in {"stop", "length", "load", "unload", "unknown"}:
                    raise ValueError("unsupported completion state")
                parsed = (text, reason, usage, data.get("model", self.model))
            except (ValueError, TypeError, KeyError, IndexError):
                if attempt >= self.max_retries:
                    raise ModelCallError("model returned an invalid or incomplete response") from None
                delay = self.retry_backoff_seconds * (2 ** attempt)
                if time.monotonic() + delay >= deadline:
                    raise ModelCallError("model returned an invalid or incomplete response") from None
                time.sleep(delay)
                attempt += 1
                continue
            break
        elapsed = time.monotonic() - started
        text, reason, usage, served_model = parsed
        return ModelResult(text, served_model,
                           {"model_calls": 1, **usage}, elapsed, reason)
