# 0002. Safe Harbor de-identification on the way into silver

Date: 2026-10-17

## Context

Synthea data has no real people in it, so de-identification is not legally required here. The pipeline is
meant to be the shape of one that runs on real records, where the analytics layer must not carry direct
identifiers and the people who query it must not be able to reconstruct a timeline that identifies someone.

## Decision

Bronze keeps the raw resources (identifiers included) and is the layer with the tightest access in a real
deployment. Silver and gold are built through one de-identification step modelled on the HIPAA Safe Harbor
list:

- Names, street addresses, phone numbers, e-mail, SSN, licence, passport and medical record numbers are
  never extracted; the extractors do not have columns for them.
- Addresses keep the state and the first three digits of the zip code, with the 17 sparsely populated
  prefixes collapsed to 000.
- Birth dates become a birth year and an age band; anyone over 89 is reported as 90+ with no birth year.
- Every other date is shifted by a per-patient offset of 1 to 30 days in either direction, derived from an
  HMAC of the patient id with a secret salt. Intervals inside a patient's record survive (length of stay,
  days to readmission), so the analytics are unchanged; dates across patients are no longer comparable to
  the day, which is the intended loss.
- Patient ids are kept (they are synthetic UUIDs, not identifiers in the Safe Harbor sense) and a
  pseudonym is derived with the same HMAC for display. No re-identification table is written; the salt is
  the only way back and lives in an environment variable.
- Free-text documents (DocumentReference, DiagnosticReport presentedForm) are not extracted at all, because
  Synthea's notes contain the patient's name and de-identifying free text is a different project.

## Consequences

- Date shifting makes ages at admission approximate to within a month; the cohort and the model card say so.
- Shifted dates cross the reference date by up to 30 days, so "recent" in the summaries is relative.
- Changing the salt changes every pseudonym and shifts every date, which is the correct behaviour for a
  key rotation and must be done as a full rebuild of silver and gold.
