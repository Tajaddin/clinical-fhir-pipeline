"""Faithfulness eval for summaries: every claim must cite a fact of that patient, repeat an anchor of it
(the name, a value or a date), and contradict none of the facts it cites: each number and date in the claim
must appear in them (a number in a cited fact named next to it), and a negation or a status the facts do not
state (resolved, still active) fails it.
Deterministic, so it runs in CI; an optional LLM judge is reported next to it, never instead of it."""

from __future__ import annotations

import json
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
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
    problem: str | None = None  # what in an anchored claim contradicts the cited facts


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


def supports(claim_text: str, *facts: Fact) -> str | None:
    """The anchor that ties the claim to one of the cited facts, or None when there is none or the claim
    contradicts them."""
    anchor = next((a for f in facts if (a := anchor_of(claim_text, f))), None)
    return anchor if anchor and contradiction(claim_text, facts) is None else None


MONTHS = {
    name: i
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
         ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")],
        start=1,
    )
    for name in names
}  # fmt: skip
# Month names in any case; abbreviations and "May" only capitalised, so "may" the verb is not a month.
_MONTH = (
    r"\b(?P<mon>(?i:january|february|march|april|june|july|august|september|october|november|december)"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\b)\.?"
)
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
# Date forms a model writes, most specific first; each date found is blanked before the next pattern runs.
DATE_PATTERNS = [
    re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)"),
    re.compile(_MONTH + r"\s+" + _DAY + r",?\s+(?P<year>\d{4})(?!\d)"),
    re.compile(r"\b" + _DAY + r"\s+(?:of\s+)?" + _MONTH + r",?\s+(?P<year>\d{4})(?!\d)"),
    re.compile(r"(?<![\d/])(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})(?!\d)"),
    re.compile(_MONTH + r",?\s+(?:of\s+)?(?P<year>\d{4})(?!\d)"),
    re.compile(_MONTH + r"\s+" + _DAY + r"(?![\d:])"),
]
NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?")
PSEUDONYM = re.compile(r"\bP-[0-9a-f]{10}\b")
# Negated phrases that say an item is over; matched first so "not currently active" is one cue, not three.
ENDED_PHRASE = re.compile(
    r"\b(?:no longer|not (?:currently|presently|still|active|ongoing|current))"
    r"(?:\s+(?:active|ongoing|current|taking|taken|prescribed))?\b"
)
ENDED = ("resolved", "resolution", "abated", "cured", "recovered", "healed", "stopped", "discontinued", "ended",
         "ceased", "completed", "inactive", "remission")  # fmt: skip
ONGOING = ("active", "ongoing", "current", "currently", "still", "continues", "continuing", "persistent", "persists")
NEGATION = ("no", "not", "cannot", "never", "denies", "denied", "negative", "without", "ruled out", "rule out",
            "excluded", "absent", "absence", "free of", "none")  # fmt: skip
CONTRACTED_NOT = re.compile(r"n['\u2019]t\b")  # doesn't, isn't, wasn't: read as "not"
DateParts = tuple[int | None, int, int | None]


def _claim_dates(text: str) -> tuple[list[DateParts], str]:
    """(year, month, day) of every date written in the text, None for a part not written, and the text with
    those dates blanked out."""
    found: list[DateParts] = []

    def take(m: re.Match[str]) -> str:
        parts = m.groupdict()
        month = MONTHS.get(parts["mon"].lower()) if parts.get("mon") else int(parts["month"])
        if month is None or not 1 <= month <= 12:
            return m.group(0)
        found.append(
            (int(parts["year"]) if parts.get("year") else None, month, int(parts["day"]) if parts.get("day") else None)
        )
        return " "

    for pattern in DATE_PATTERNS:
        text = pattern.sub(take, text)
    return found, text


def _cue(text: str, word: str) -> bool:
    return re.search(rf"\b{word}\b", text) is not None


def _status(fact_text: str) -> str | None:
    t = fact_text.lower()
    if "(active)" in t:
        return "active"
    if re.search(r"\((?:stopped|completed|cancelled|inactive|resolved|revoked)\)|, from \d{4}-\d{2}-\d{2} to ", t):
        return "ended"
    return None


def contradiction(claim_text: str, facts: tuple[Fact, ...] | list[Fact]) -> str | None:
    """The first thing the claim states that the cited facts do not: a number, a date, a negation, or a status
    (over vs ongoing). A number must be in a cited fact named next to it, or in any cited fact when the claim
    names none; cue words the facts use themselves don't count."""
    return _value_problem(claim_text, facts) or _wording_problem(claim_text, facts)


def _value_problem(claim_text: str, facts: tuple[Fact, ...] | list[Fact]) -> str | None:
    fact_dates = [date.fromisoformat(d) for f in facts for d in _dates(f.text)]
    written, rest = _claim_dates(PSEUDONYM.sub(" ", claim_text))
    for year, month, day in written:
        if not any(d.month == month and year in (None, d.year) and day in (None, d.day) for d in fact_dates):
            shown = "-".join("?" if p is None else f"{p:02d}" for p in (year, month, day))
            return f"date {shown} not in the cited facts"
    lowered = rest.lower()
    for m in NUMBER.finditer(rest):
        n = m.group(0)
        if not any(_states(f, n) for f in facts):
            return f"value {n} not in the cited facts"
        named = _named_next_to(lowered, m.start(), m.end(), facts)
        if named and not any(_states(f, n) for f in named):
            return f"value {n} not in the cited fact named next to it"
    return None


def _states(fact: Fact, number: str) -> bool:
    """The fact has this number as a value, or as the year of one of its dates."""
    values = {float(v) for v in NUMBER.findall(re.sub(r"\d{4}-\d{2}-\d{2}", " ", fact.text))}
    return float(number) in values or (
        re.fullmatch(r"\d{4}", number) is not None and number in {d[:4] for d in _dates(fact.text)}
    )


def _named_next_to(text: str, start: int, end: int, facts: tuple[Fact, ...] | list[Fact]) -> list[Fact]:
    """The cited facts whose name is written closest before the number, and those closest after it."""
    before: dict[int, list[Fact]] = {}
    after: dict[int, list[Fact]] = {}
    for f in facts:
        for token in set(_name_tokens(f.text)):
            if (i := text.rfind(token, 0, start)) >= 0:
                before.setdefault(i + len(token), []).append(f)
            if (j := text.find(token, end)) >= 0:
                after.setdefault(j, []).append(f)
    return (before[max(before)] if before else []) + (after[min(after)] if after else [])


def _wording_problem(claim_text: str, facts: tuple[Fact, ...] | list[Fact]) -> str | None:
    claim = CONTRACTED_NOT.sub(" not", claim_text.lower())
    facts_text = CONTRACTED_NOT.sub(" not", " ".join(f.text.lower() for f in facts))
    statuses = {s for f in facts if (s := _status(f.text))}
    phrase = ENDED_PHRASE.search(claim)
    if phrase and statuses != {"ended"}:
        return f"says '{phrase.group(0)}' but the cited facts do not say it ended"
    claim = ENDED_PHRASE.sub(" ", claim)
    ended = next((w for w in ENDED if _cue(claim, w) and not _cue(facts_text, w)), None)
    if ended and statuses == {"active"}:
        return f"says '{ended}' but the cited fact is active"
    ongoing = next((w for w in ONGOING if _cue(claim, w) and not _cue(facts_text, w)), None)
    if ongoing and statuses == {"ended"}:
        return f"says '{ongoing}' but the cited fact has ended"
    negation = next((w for w in NEGATION if _cue(claim, w) and not _cue(facts_text, w)), None)
    return f"negation '{negation}' not in the cited facts" if negation else None


def anchor_of(claim_text: str, fact: Fact) -> str | None:
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
    # One resource can carry several facts (a blood pressure Observation: systolic and diastolic).
    by_id: dict[str, list[Fact]] = {}
    for f in facts:
        by_id.setdefault(f.fact_id, []).append(f)
    verdicts = []
    for claim in summary.claims():
        if not claim.citations:
            verdicts.append(ClaimVerdict(claim.section, claim.text, [], "missing_citation"))
            continue
        if any(c not in by_id for c in claim.citations):
            verdicts.append(ClaimVerdict(claim.section, claim.text, claim.citations, "unknown_citation"))
            continue
        cited = [f for c in claim.citations for f in by_id[c]]
        anchor = next((a for f in cited if (a := anchor_of(claim.text, f))), None)
        problem = contradiction(claim.text, cited) if anchor else None
        status = "supported" if anchor and not problem else "unsupported"
        verdicts.append(ClaimVerdict(claim.section, claim.text, claim.citations, status, anchor, problem))
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
    sections_shown_total = sections_written_total = dropped_total = contradicted = 0
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
            contradicted += v.problem is not None
        if with_llm_judge:
            for v in verdicts:
                cited = [f for f in facts if f.fact_id in v.citations]
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
        "contradicted": contradicted,
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
