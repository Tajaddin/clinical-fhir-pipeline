"""Cited patient summaries: retrieve the patient's own facts per section, ask the model for JSON claims that
cite fact ids, and keep only citations that exist in what the model was shown."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from rank_bm25 import BM25Okapi

from cfp.lake import Lake
from cfp.llm import LLMClient, OllamaEmbedder

PROMPT_VERSION = "summary-v1"

# Section -> (title shown to the model, clinical priority query used to rank that section's facts)
SECTION_KEY = {
    "problems": "P",
    "medications": "M",
    "encounters": "E",
    "results": "R",
    "allergies": "A",
    "procedures": "X",
    "immunizations": "I",
    "care plans": "C",
}
SECTIONS: dict[str, tuple[str, str]] = {
    "problems": (
        "Problems",
        "active chronic disorder diabetes hypertension heart failure kidney cancer stroke coronary copd",
    ),
    "medications": ("Medications", "active medication tablet insulin anticoagulant statin inhaler injection"),
    "encounters": ("Recent encounters", "inpatient admission emergency visit hospital readmission"),
    "results": ("Latest results", "blood pressure body mass index hemoglobin a1c glucose creatinine cholesterol"),
    "allergies": ("Allergies", "allergy intolerance medication high criticality reaction"),
    "procedures": ("Recent procedures", "surgery procedure operation imaging"),
    "immunizations": ("Immunizations", "vaccine influenza covid pneumococcal"),
    "care plans": ("Care plans", "care plan self management active"),
}

SECTION_SYSTEM_PROMPT = """You write one section of a clinical summary for a care team from facts taken from one patient's record.
Rules:
- Use only the facts given. Do not add knowledge, advice or anything not stated in a fact.
- Write one short claim per fact; merge facts that repeat the same item. Keep dates and values exactly as given.
- Do not write claims about the patient's sex, age or death; only about the facts listed.
- Every claim cites the keys of the facts it uses: the short codes in square brackets in this message, which all start
  with {prefix} ({prefix}1, {prefix}2, ...). Copy them exactly.
- Return JSON only, in this shape: {{"claims": [{{"text": "...", "citations": ["{prefix}1"]}}]}}"""

SYSTEM_PROMPT = """You write short clinical summaries for a care team from a list of facts taken from one patient's record.
Rules:
- Use only the facts given. Do not add knowledge, advice or anything not stated in a fact.
- Every claim must cite one or more fact keys (the short code in square brackets, like P1 or M3) in its "citations" list.
- One fact per claim where possible. Keep the fact's dates and values exactly.
- Return JSON only, in this shape:
{"sections": [{"title": "Problems", "claims": [{"text": "...", "citations": ["P1"]}]}]}
- Use the section titles given. Leave a section out if it has no facts."""


@dataclass
class Fact:
    fact_id: str
    resource_type: str
    section: str
    ts: datetime | None
    text: str


@dataclass
class Claim:
    text: str
    citations: list[str]
    section: str


@dataclass
class Summary:
    patient_id: str
    pseudonym: str
    model: str
    prompt_version: str
    created_at: str
    sections: list[dict[str, Any]]
    facts_shown: list[str]
    dropped_citations: int
    latency_s: float
    prompt_tokens: int | None
    completion_tokens: int | None
    retrieval: str = "bm25+recency"
    facts_available: int = 0
    repaired_json: bool = False
    facts_cited: int = 0
    sections_shown: int = 0
    model_calls: int = 0

    @property
    def coverage(self) -> float | None:
        return round(self.facts_cited / len(self.facts_shown), 4) if self.facts_shown else None

    def claims(self) -> list[Claim]:
        return [
            Claim(c["text"], list(c.get("citations", [])), s["title"])
            for s in self.sections
            for c in s.get("claims", [])
        ]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "coverage": self.coverage}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1]


def load_facts(lake: Lake, patient_id: str) -> list[Fact]:
    con = lake.connect([("gold", "patient_facts")])
    rows = con.execute(
        "select fact_id, resource_type, section, ts, text from gold_patient_facts where patient_id = ? order by ts desc",
        [patient_id],
    ).fetchall()
    return [Fact(*r) for r in rows]


def load_patient(lake: Lake, patient_id: str) -> dict[str, Any] | None:
    con = lake.connect([("silver", "patients")])
    row = con.execute("select * from silver_patients where patient_id = ?", [patient_id]).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in (con.description or [])]
    return dict(zip(cols, row, strict=True))


@dataclass
class Retriever:
    per_section: int = 8
    embedder: OllamaEmbedder | None = None
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = "embeddings+recency" if self.embedder else "bm25+recency"

    def retrieve(self, facts: list[Fact]) -> dict[str, list[Fact]]:
        """Per section: relevance to the section's clinical query blended with recency; active problems first."""
        out: dict[str, list[Fact]] = {}
        for section, (_, query) in SECTIONS.items():
            candidates = [f for f in facts if f.section == section]
            if not candidates:
                continue
            relevance = self._relevance(query, candidates)
            latest = max((f.ts for f in candidates if f.ts), default=None)
            scored = []
            for f, rel in zip(candidates, relevance, strict=True):
                age_days = (latest - f.ts).days if (latest and f.ts) else 3650
                recency = math.exp(-age_days / 730.0)
                active = 1.0 if "(active)" in f.text or "(active)" in f.text.lower() else 0.0
                scored.append((0.5 * rel + 0.4 * recency + 0.3 * active, f))
            scored.sort(key=lambda t: -t[0])
            out[section] = [f for _, f in scored[: self.per_section]]
        return out

    def _relevance(self, query: str, candidates: list[Fact]) -> list[float]:
        if self.embedder is not None:
            vectors = self.embedder.embed([query, *[c.text for c in candidates]])
            q = vectors[0]
            sims = [_cosine(q, v) for v in vectors[1:]]
            lo, hi = min(sims), max(sims)
            return [(s - lo) / (hi - lo) if hi > lo else 0.5 for s in sims]
        corpus = [_tokens(c.text) for c in candidates]
        if not any(corpus):
            return [0.0] * len(candidates)
        scores = BM25Okapi(corpus).get_scores(_tokens(query))
        hi = float(max(scores)) if len(scores) else 0.0
        return [float(s) / hi if hi > 0 else 0.0 for s in scores]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def citation_keys(retrieved: dict[str, list[Fact]]) -> dict[str, str]:
    """Short keys per section (P1, M2, ...) -> FHIR reference. UUID references cost the model 25 tokens each
    in and out; a key costs two and is easier to copy correctly."""
    keys: dict[str, str] = {}
    for section, facts in retrieved.items():
        prefix = SECTION_KEY.get(section, "F")
        for i, f in enumerate(facts, start=1):
            keys[f"{prefix}{i}"] = f.fact_id
    return keys


def build_prompt(patient: dict[str, Any], retrieved: dict[str, list[Fact]]) -> str:
    keys = {v: k for k, v in citation_keys(retrieved).items()}
    header = (
        f"Patient {patient.get('pseudonym')}: {patient.get('sex') or 'sex unknown'}, age band {patient.get('age_band')}"
        + (", deceased" if patient.get("deceased") else "")
        + ". Dates are shifted for de-identification; keep them as given.\n"
    )
    parts = [header]
    for section, facts in retrieved.items():
        parts.append(f"\n## {SECTIONS[section][0]}")
        parts.extend(f"[{keys[f.fact_id]}] {f.text}" for f in facts)
    return "\n".join(parts)


def patient_header(patient: dict[str, Any]) -> str:
    return (
        f"Patient {patient.get('pseudonym')}: {patient.get('sex') or 'sex unknown'}, age band {patient.get('age_band')}"
        + (", deceased" if patient.get("deceased") else "")
        + ". Dates are shifted for de-identification; keep them as given."
    )


def build_section_prompt(patient: dict[str, Any], section: str, facts: list[Fact], keys: dict[str, str]) -> str:
    by_ref = {v: k for k, v in keys.items()}
    lines = [
        f"Facts for patient {patient.get('pseudonym')} (dates are shifted for de-identification; keep them as given).",
        "",
        f"## {SECTIONS[section][0]}",
    ]
    lines.extend(f"[{by_ref[f.fact_id]}] {f.text}" for f in facts)
    return "\n".join(lines)


def summarize(lake: Lake, patient_id: str, llm: LLMClient, retriever: Retriever) -> Summary | None:
    """One model call per section: small prompts suit small models, and every section the retriever filled
    gets its own claims instead of being folded into the first one."""
    patient = load_patient(lake, patient_id)
    if patient is None:
        return None
    facts = load_facts(lake, patient_id)
    retrieved = retriever.retrieve(facts)
    keys = citation_keys(retrieved)
    shown = set(keys.values())
    sections: list[dict[str, Any]] = []
    dropped = 0
    latency = 0.0
    prompt_tokens = completion_tokens = 0
    repaired = False
    cited: set[str] = set()
    for section, section_facts in retrieved.items():
        prefix = SECTION_KEY.get(section, "F")
        section_keys = {k: v for k, v in keys.items() if k.startswith(prefix)}
        completion = llm.complete_json(
            SECTION_SYSTEM_PROMPT.format(prefix=prefix),
            build_section_prompt(patient, section, section_facts, keys),
            max_tokens=700,
        )
        claims, dropped_here = _normalise_claims(completion.content.get("claims", []), section_keys)
        dropped += dropped_here
        latency += completion.latency_s
        prompt_tokens += completion.prompt_tokens or 0
        completion_tokens += completion.completion_tokens or 0
        repaired = repaired or completion.repaired
        if claims:
            sections.append({"title": SECTIONS[section][0], "claims": claims})
            cited.update(c for claim in claims for c in claim["citations"])
    return Summary(
        patient_id=patient_id,
        pseudonym=patient["pseudonym"],
        model=llm.name,
        prompt_version=PROMPT_VERSION,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sections=sections,
        facts_shown=sorted(shown),
        dropped_citations=dropped,
        latency_s=round(latency, 3),
        prompt_tokens=prompt_tokens or None,
        completion_tokens=completion_tokens or None,
        retrieval=retriever.name,
        facts_available=len(facts),
        repaired_json=repaired,
        facts_cited=len(cited),
        sections_shown=len(retrieved),
        model_calls=len(retrieved),
    )


def _normalise_claims(raw: Any, keys: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """Map citation keys back to FHIR references, drop citations that were not in the prompt (counted), coerce
    types. A full reference the model echoed back is accepted too."""
    by_ref = set(keys.values())
    dropped = 0
    claims = []
    for c in raw or []:
        if not isinstance(c, dict) or not str(c.get("text", "")).strip():
            continue
        cites = c.get("citations") or []
        if isinstance(cites, str):
            cites = [cites]
        kept = []
        for x in cites:
            token = str(x).strip("[] ")
            if token in keys:
                kept.append(keys[token])
            elif token in by_ref:
                kept.append(token)
        dropped += len(cites) - len(kept)
        claims.append({"text": str(c["text"]).strip(), "citations": kept})
    return claims, dropped
