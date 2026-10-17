"""LLM access: any OpenAI-compatible chat endpoint (Ollama locally), an embedding endpoint, and a
deterministic fake for tests and CI."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from cfp.logging_setup import get_logger

log = get_logger("llm")


@dataclass
class Completion:
    content: dict[str, Any]
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: str = ""
    repaired: bool = False


class LLMClient(Protocol):
    name: str

    def complete_json(self, system: str, user: str, max_tokens: int = 1800) -> Completion: ...


def _extract_json(text: str) -> tuple[dict[str, Any], bool]:
    """Parse the model's JSON; strip fences or prose around it; if the output was cut off, keep the complete
    objects and close the open brackets. Returns (content, repaired)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        pass
    end = text.rfind("}")
    if end > 0:
        try:
            return json.loads(text[: end + 1]), False
        except json.JSONDecodeError:
            pass
    return _repair_truncated(text), True


def _repair_truncated(text: str) -> dict[str, Any]:
    cuts = [m.start() for m in re.finditer(r"}", text)]
    for cut in reversed(cuts[-60:]):
        candidate = text[: cut + 1]
        closers = _closers(candidate)
        if closers is None:
            continue
        try:
            return json.loads(candidate + closers)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("unrepairable model output", text, 0)


def _closers(candidate: str) -> str | None:
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in candidate:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if in_string:
        return None
    return "".join("}" if ch == "{" else "]" for ch in reversed(stack))


@dataclass
class OpenAICompatible:
    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 120.0
    retries: int = 2
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.model
        self._client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout_s,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )

    def complete_json(self, system: str, user: str, max_tokens: int = 1800) -> Completion:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                r = self._client.post("/chat/completions", json=payload)
                r.raise_for_status()
                body = r.json()
                text = body["choices"][0]["message"]["content"] or ""
                usage = body.get("usage") or {}
                content, repaired = _extract_json(text)
                return Completion(
                    content=content,
                    latency_s=time.perf_counter() - started,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    raw=text,
                    repaired=repaired,
                )
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as exc:
                last = exc
                log.warning("llm call failed", attempt=attempt, error=str(exc)[:200])
        raise RuntimeError(f"LLM call failed after {self.retries + 1} attempts: {last}")


@dataclass
class OllamaEmbedder:
    base_url: str
    model: str
    timeout_s: float = 60.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = httpx.Client(base_url=self.base_url.rstrip("/"), timeout=self.timeout_s)
        r = client.post("/embeddings", json={"model": self.model, "input": texts})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


@dataclass
class FakeLLM:
    """Builds claims straight from the facts in the prompt: one claim per fact, cited correctly. With
    `unsupported_every`, every n-th claim is replaced by an invented one to exercise the eval."""

    unsupported_every: int = 0
    name: str = "fake"
    calls: int = 0
    claims_made: int = 0  # runs across calls so `unsupported_every` also works with one call per section

    def complete_json(self, system: str, user: str, max_tokens: int = 1800) -> Completion:
        self.calls += 1
        sections: dict[str, list[dict]] = {}
        current = "summary"
        for line in user.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                continue
            m = re.match(r"^\[(?P<id>[A-Z]\d+)\]\s+(?P<text>.+)$", line.strip())
            if not m:
                continue
            self.claims_made += 1
            if self.unsupported_every and self.claims_made % self.unsupported_every == 0:
                claim = {"text": "Patient reports excellent sleep and runs marathons.", "citations": [m.group("id")]}
            else:
                claim = {"text": m.group("text"), "citations": [m.group("id")]}
            sections.setdefault(current, []).append(claim)
        content: dict[str, Any]
        if len(sections) == 1:
            content = {"claims": next(iter(sections.values()))}
        else:
            content = {"sections": [{"title": title, "claims": claims} for title, claims in sections.items()]}
        return Completion(
            content=content, latency_s=0.0, prompt_tokens=len(user) // 4, completion_tokens=200, raw=json.dumps(content)
        )
