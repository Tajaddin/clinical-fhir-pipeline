# 0004. Cited summaries with a deterministic faithfulness check

Date: 2026-10-17

## Context

The brief asks for an LLM summary of a patient record built with retrieval over the patient's own
resources, citations to FHIR resource ids, and an eval that checks each claim against its source. A clinical
summary that cannot be traced to the record is a liability, and "the model said it was faithful" is not an
eval.

## Decision

- **Facts, not documents.** Gold holds one short fact per clinical resource (problem, medication, encounter,
  latest result per code, allergy, immunization, procedure, care plan), each carrying its FHIR reference
  and its date. Retrieval ranks each section's facts by a BM25 match to a clinical priority query plus
  recency, with active problems first; Ollama embeddings can replace BM25. The top facts per section go
  into the prompt (eight by default, about 60 facts).
- **Short citation keys, one model call per section.** The prompt labels facts `[P1]`, `[M2]`, `[E3]`;
  the model cites those and the service maps them back to `Condition/<uuid>`. A UUID reference costs a
  small model about 25 tokens each way and is easy to corrupt; the first run with full references
  overflowed a 4K context and produced truncated JSON. The second run asked for the whole summary in one
  call and the 3B model folded everything into the first section (5 claims per patient). The third run,
  one call per section, wrote 33 claims per patient but cited `P1` for results, procedures and
  immunizations alike, because the system prompt's example used `P1`: 74% supported. With the example
  written in each section's own prefix and a section's answer allowed to cite only that section's keys,
  the same model reaches 98% supported at 90% coverage. Citations the model invents are dropped and
  counted; the eval records each of these runs.
- **JSON output**, with a repair step for truncated output that keeps the complete claims.
- **The check is deterministic.** A claim is supported when it cites a resource of that patient and repeats
  an anchor of the cited fact: enough of its name tokens, or a value or a date together with a name token.
  Failure modes are counted separately: unsupported, missing citation, unknown citation. An LLM judge can
  be run next to it for agreement, never instead of it.
- **Local model.** The eval runs with qwen2.5:3b on Ollama (free, on the owner's GPU) and the API accepts
  any OpenAI-compatible endpoint; a deterministic fake model makes the tests and CI independent of a model
  server. The fake copies facts verbatim, so the checker must pass it at 100%, which it does; that is the
  checker's own test.

## Consequences

- The faithfulness rate measures whether the model copied the record correctly, not whether the summary is
  clinically good; that is the honest scope of an automatic check.
- The anchor rule is strict on paraphrase: a claim that renames a condition without reusing any of its
  words counts as unsupported even when a clinician would accept it. The rate is therefore a lower bound.
- Summaries are stored with their verdicts, and every generation and read is in the audit log (ADR 0005).
