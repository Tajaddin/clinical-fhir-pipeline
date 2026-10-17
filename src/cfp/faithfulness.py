"""Faithfulness eval for summaries: every claim must cite a fact of that patient and repeat an anchor of
it (the name, a value or a date). Deterministic, so it runs in CI; an optional LLM judge is reported next
to it, never instead of it."""

from __future__ import annotations

import json
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cfp.lake import Lake
from cfp.llm import LLMClient
from cfp.summaries import Fact, Retriever, Summary, load_facts, summarize

STOP = {
    "disorder", "finding", "procedure", "regime", "therapy", "record", "artifact", "situation", "environment", "qualifier",
    "value", "with", "and", "the", "for", "from", "since", "active", "ordered", "recorded", "on", "to", "of", "in", "by",
    "oral", "tablet", "mg", "ml", "hr", "unknown", "status", "completed", "stopped", "visit", "encounter", "patient",
}  # fmt: skip


@dataclass
class ClaimVerdict:
    section: str
    text: str
    citations: list[str]
    status: str  # supported | unsupported | missing_citation | unknown_citation
    anchor: str | None = None


def _name_tokens(fact_text: str) -> list[str]:
    """Distinctive words of the fact's name: the text before the first ':' or ',' or date, aliases in
    parentheses included ("Body mass index (BMI)")."""
    head = re.split(r"[:,]| on \d{4}| since \d{4}| from \d{4}", fact_text, maxsplit=1)[0]
    tokens = [t for t in re.findall(r"[a-z][a-z0-9\-]+", head.lower()) if len(t) >= 3 and t not in STOP]
    if tokens:
        return tokens
    body = re.sub(r"\d{4}-\d{2}-\d{2}", " ", fact_text.lower())
    return [t for t in re.findall(r"[a-z][a-z0-9\-]+", body) if len(t) >= 2 and t not in STOP][:6]


def _all_tokens(fact_text: str) -> list[str]:
    body = re.sub(r"\d{4}-\d{2}-\d{2}", " ", fact_text.lower())
    return [t for t in re.findall(r"[a-z][a-z0-9\-]+", body) if len(t) >= 3 and t not in STOP]


def _numbers(text: str) -> set[str]:
    return {m.rstrip("0").rstrip(".") if "." in m else m for m in re.findall(r"\d+(?:\.\d+)?", text)}


def _dates(text: str) -> set[str]:
    return set(re.findall(r"\d{4}-\d{2}-\d{2}", text))


def supports(claim_text: str, fact: Fact) -> str | None:
    """The anchor that ties the claim to the fact, or None. Anchors, in order: most of the fact's name; two
    words of its name; its date plus any word of it; a value of it plus any word of it; a value plus the date."""
    claim = claim_text.lower()
    name = _name_tokens(fact.text)
    name_hits = [t for t in name if t in claim]
    if name and len(name_hits) >= max(1, round(0.6 * len(name))):
        return "name:" + " ".join(name_hits[:3])
    if len(name_hits) >= 2:
        return "name:" + " ".join(name_hits[:3])
    any_hits = [t for t in _all_tokens(fact.text) if t in claim]
    shared_dates = _dates(fact.text) & _dates(claim_text)
    if shared_dates and any_hits:
        return "date:" + sorted(shared_dates)[0]
    values = {n for n in _numbers(fact.text) if len(n) >= 2 and not re.fullmatch(r"\d{4}", n)}
    shared_values = values & _numbers(claim_text)
    if shared_values and any_hits:
        return "value:" + sorted(shared_values)[0]
    if shared_values and shared_dates:
        return "value+date:" + sorted(shared_values)[0]
    return None


def judge_summary(summary: Summary, facts: list[Fact]) -> list[ClaimVerdict]:
    by_id = {f.fact_id: f for f in facts}
    verdicts = []
    for claim in summary.claims():
        if not claim.citations:
            verdicts.append(ClaimVerdict(claim.section, claim.text, [], "missing_citation"))
            continue
        cited = [by_id.get(c) for c in claim.citations]
        if any(f is None for f in cited):
            verdicts.append(ClaimVerdict(claim.section, claim.text, claim.citations, "unknown_citation"))
            continue
        anchor = next((a for f in cited if f is not None and (a := supports(claim.text, f))), None)
        verdicts.append(
            ClaimVerdict(claim.section, claim.text, claim.citations, "supported" if anchor else "unsupported", anchor)
        )
    return verdicts


LLM_JUDGE_SYSTEM = """You check whether a claim is fully supported by the given facts. Answer JSON only: {"supported": true|false, "reason": "..."}."""


def llm_judge(llm: LLMClient, claim: str, facts: list[Fact]) -> bool:
    user = "Facts:\n" + "\n".join(f"- {f.text}" for f in facts) + f"\n\nClaim: {claim}"
    try:
        return bool(llm.complete_json(LLM_JUDGE_SYSTEM, user, max_tokens=120).content.get("supported"))
    except Exception:
        return False


def evaluate(
    lake: Lake,
    llm: LLMClient,
    retriever: Retriever,
    patient_ids: list[str],
    report_path: Path | None,
    examples_dir: Path | None = None,
    with_llm_judge: bool = False,
) -> dict[str, Any]:
    per_patient = []
    verdict_counter: Counter[str] = Counter()
    section_counter: dict[str, Counter[str]] = {}
    latencies = []
    agree = total_judged = 0
    failed: list[dict[str, str]] = []
    repaired = 0
    coverages: list[float] = []
    sections_shown_total = sections_written_total = dropped_total = 0
    started = time.perf_counter()
    for pid in patient_ids:
        try:
            summary = summarize(lake, pid, llm, retriever)
        except Exception as exc:
            failed.append({"patient_id": pid, "error": str(exc)[:200]})
            continue
        if summary is None:
            continue
        repaired += int(summary.repaired_json)
        if summary.coverage is not None:
            coverages.append(summary.coverage)
        sections_shown_total += summary.sections_shown
        sections_written_total += len(summary.sections)
        dropped_total += summary.dropped_citations
        facts = load_facts(lake, pid)
        verdicts = judge_summary(summary, facts)
        for v in verdicts:
            verdict_counter[v.status] += 1
            section_counter.setdefault(v.section, Counter())[v.status] += 1
        if with_llm_judge:
            by_id = {f.fact_id: f for f in facts}
            for v in verdicts:
                cited = [by_id[c] for c in v.citations if c in by_id]
                if cited:
                    total_judged += 1
                    agree += int(llm_judge(llm, v.text, cited) == (v.status == "supported"))
        latencies.append(summary.latency_s)
        per_patient.append(
            {
                "patient_id": pid,
                "claims": len(verdicts),
                "supported": sum(v.status == "supported" for v in verdicts),
                "facts_available": summary.facts_available,
                "facts_shown": len(summary.facts_shown),
                "facts_cited": summary.facts_cited,
                "coverage": summary.coverage,
                "sections_shown": summary.sections_shown,
                "sections_written": len(summary.sections),
                "dropped_citations": summary.dropped_citations,
                "latency_s": summary.latency_s,
            }
        )
        if examples_dir is not None and len(per_patient) <= 3:
            examples_dir.mkdir(parents=True, exist_ok=True)
            (examples_dir / f"{summary.pseudonym}.json").write_text(
                json.dumps({"summary": summary.to_dict(), "verdicts": [asdict(v) for v in verdicts]}, indent=2),
                encoding="utf-8",
            )
    claims = sum(verdict_counter.values())
    report = {
        "evaluated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": llm.name,
        "retrieval": retriever.name,
        "patients": len(per_patient),
        "failed_generations": len(failed),
        "truncated_outputs_repaired": repaired,
        "claims": claims,
        "faithfulness_rate": round(verdict_counter["supported"] / claims, 4) if claims else None,
        "breakdown": dict(verdict_counter),
        "by_section": {
            s: {"claims": sum(c.values()), "supported_rate": round(c["supported"] / sum(c.values()), 4)}
            for s, c in section_counter.items()
        },
        "claims_per_summary": round(claims / len(per_patient), 2) if per_patient else None,
        "coverage": round(statistics.mean(coverages), 4) if coverages else None,
        "sections_written_rate": round(sections_written_total / sections_shown_total, 4)
        if sections_shown_total
        else None,
        "dropped_citations": dropped_total,
        "latency_s": {
            "median": round(statistics.median(latencies), 2),
            "p90": round(sorted(latencies)[int(0.9 * (len(latencies) - 1))], 2),
        }
        if latencies
        else None,
        "llm_judge_agreement": round(agree / total_judged, 4) if total_judged else None,
        "wall_time_s": round(time.perf_counter() - started, 1),
        "per_patient": per_patient,
        "failures": failed,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
