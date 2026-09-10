"""Bounded calls to explicitly configured Ollama or compatible GPU servers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from scisaurus.core.errors import ValidationError


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
                 reasoning_effort: str | None = None, output_format: str | None = None):
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
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or reasoning_effort not in {"none", "low", "medium", "high"}
        ):
            raise ValidationError("reasoning_effort must be none, low, medium, or high when configured")
        if output_format is not None and output_format != "json_object":
            raise ValidationError("output_format must be json_object when configured")
        if protocol != "openai_compatible" and (reasoning_effort is not None or output_format is not None):
            raise ValidationError("reasoning_effort and output_format require the openai_compatible protocol")
        if auth_env is not None and (not isinstance(auth_env, str) or not auth_env or not os.environ.get(auth_env)):
            raise ValidationError("configured model authentication environment variable is absent")
        self.base_url, self.model, self.protocol = base_url.rstrip("/"), model, protocol
        self.timeout_seconds, self.max_output_tokens = timeout_seconds, max_output_tokens
        self.max_response_bytes, self.auth_env = max_response_bytes, auth_env
        self.reasoning_effort, self.output_format = reasoning_effort, output_format

    def complete(self, *, system: str, prompt: str) -> ModelResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        body = {"model": self.model, "messages": messages, "stream": False}
        if self.protocol == "ollama":
            path = "/api/chat"
            body["options"] = {"num_predict": self.max_output_tokens}
        else:
            path = "/chat/completions"
            body["max_tokens"] = self.max_output_tokens
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
        request = urllib.request.Request(self.base_url + path, json.dumps(body).encode(), headers)
        started = time.monotonic()
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise ModelCallError(f"model HTTP request failed with status {code}", outcome_known=400 <= code < 500) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelCallError(f"model transport failed: {type(exc).__name__}") from None
        elapsed = time.monotonic() - started
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
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ModelCallError("model returned an invalid or incomplete response") from None
        return ModelResult(text, data.get("model", self.model),
                           {"model_calls": 1, **usage}, elapsed, reason)
