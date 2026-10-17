"""Build the LLM client and the retriever from settings (shared by the CLI and the API)."""

from __future__ import annotations

from cfp.config import Settings
from cfp.llm import FakeLLM, LLMClient, OllamaEmbedder, OpenAICompatible
from cfp.summaries import Retriever


def make_llm(s: Settings) -> LLMClient:
    if s.llm_base_url:
        return OpenAICompatible(
            base_url=s.llm_base_url, model=s.llm_model, api_key=s.llm_api_key, timeout_s=s.llm_timeout_s
        )
    return FakeLLM()


def make_retriever(s: Settings) -> Retriever:
    embedder = OllamaEmbedder(s.llm_base_url, s.embed_model) if (s.embed_model and s.llm_base_url) else None
    return Retriever(per_section=s.facts_per_section, embedder=embedder)
