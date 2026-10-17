"""De-identification in the Safe Harbor style, applied on the way from bronze to silver.

- Names, street addresses, phone numbers, email, SSN, licence, passport and medical record numbers are
  never extracted. Addresses keep the state and the first three digits of the zip code, with the sparsely
  populated prefixes collapsed to 000 as the rule requires.
- Birth dates become a birth year and an age band; anyone over 89 is reported as 90+ with no birth year.
- Every other date is shifted by a per-patient offset of 1 to 30 days in either direction, derived from an
  HMAC of the patient id with a secret salt. Intervals inside one patient's record survive; the offset
  itself is never written anywhere, so silver cannot be mapped back without the salt.

Synthea data has no real people in it. The step is here because the pipeline is meant to run the same
way on data that does.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

# Three-digit zip prefixes with 20,000 or fewer people (45 CFR 164.514(b)(2)(i)(B), 2010 census list).
SPARSE_ZIP3 = frozenset(
    {
        "036",
        "059",
        "063",
        "102",
        "203",
        "556",
        "692",
        "790",
        "821",
        "823",
        "830",
        "831",
        "878",
        "879",
        "884",
        "890",
        "893",
    }
)
AGE_BANDS: tuple[tuple[int, str], ...] = ((18, "0-17"), (40, "18-39"), (65, "40-64"), (90, "65-89"), (10**6, "90+"))


@dataclass(frozen=True)
class DeidContext:
    salt: str
    reference_date: date
    max_shift_days: int = 30

    def offset_days(self, patient_id: str) -> int:
        return patient_offset_days(patient_id, self.salt, self.max_shift_days)


def _digest(patient_id: str, salt: str) -> str:
    return hmac.new(salt.encode("utf-8"), patient_id.encode("utf-8"), hashlib.sha256).hexdigest()


def patient_offset_days(patient_id: str, salt: str, max_days: int = 30) -> int:
    """Deterministic shift in [-max_days, -1] or [1, max_days]."""
    n = int(_digest(patient_id, salt)[:8], 16) % (2 * max_days)
    offset = n - max_days
    return offset + 1 if offset >= 0 else offset


def pseudonym(patient_id: str, salt: str) -> str:
    return "P-" + _digest(patient_id, salt)[8:18]


def shift_ts(value: str | None, offset_days: int) -> datetime | None:
    """ISO date or datetime (with or without zone) -> naive UTC datetime shifted by the offset."""
    if not value:
        return None
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day) + timedelta(days=offset_days)
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt + timedelta(days=offset_days)


def age_at(birth_date: str, reference: date) -> int:
    b = date.fromisoformat(birth_date[:10])
    return reference.year - b.year - ((reference.month, reference.day) < (b.month, b.day))


def age_band(age: int | None) -> str | None:
    if age is None:
        return None
    for upper, label in AGE_BANDS:
        if age < upper:
            return label
    return None


def zip3(postal_code: str | None) -> str | None:
    if not postal_code:
        return None
    digits = "".join(ch for ch in postal_code if ch.isdigit())[:3]
    if len(digits) < 3:
        return None
    return "000" if digits in SPARSE_ZIP3 else digits
