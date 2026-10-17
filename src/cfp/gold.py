"""Gold: the readmission cohort with its label and discharge-time features, and the per-patient facts the
summaries cite. SQL in DuckDB over the silver Delta tables, written back as Delta."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from cfp.lake import Lake

# Chronic-condition flags from the condition display text (SNOMED display strings in Synthea are stable).
CONDITION_FLAGS: dict[str, list[str]] = {
    "diabetes": ["%diabetes mellitus%"],
    "prediabetes": ["%prediabetes%"],
    "hypertension": ["%hypertens%"],
    "heart_failure": ["%heart failure%"],
    "copd": ["%chronic obstructive%", "%emphysema%", "%pulmonary emphysema%"],
    "ckd": ["%chronic kidney%", "%renal failure%", "%end stage renal%"],
    "cancer": ["%carcinoma%", "%neoplasm%", "%cancer%", "%lymphoma%", "%leukemia%", "%sarcoma%"],
    "dementia": ["%dementia%", "%alzheimer%"],
    "stroke": ["%stroke%", "%cerebrovascular%"],
    "coronary": ["%coronary%", "%myocardial infarction%", "%ischemic heart%"],
    "atrial_fibrillation": ["%atrial fibrillation%"],
    "asthma": ["%asthma%"],
    "depression": ["%depress%"],
    "obesity": ["%obes%", "%body mass index 30%", "%body mass index 40%"],
    "substance_use": ["%alcoholism%", "%dependence%", "%opioid abuse%", "%drug overdose%"],
}

# Latest value before admission; LOINC codes as Synthea emits them.
LAB_FEATURES: dict[str, str] = {
    "bmi": "39156-5",
    "systolic_bp": "8480-6",
    "diastolic_bp": "8462-4",
    "heart_rate": "8867-4",
    "hba1c": "4548-4",
    "glucose": "2339-0",
    "creatinine": "2160-0",
    "hemoglobin": "718-7",
    "ldl": "18262-6",
    "egfr": "33914-3",
}

FLAG_COLUMNS = [f"has_{k}" for k in CONDITION_FLAGS]
LAB_COLUMNS = list(LAB_FEATURES)
HISTORY_COLUMNS = [
    "prior_admissions_365d",
    "prior_admissions_all",
    "prior_ed_visits_365d",
    "prior_ambulatory_365d",
    "days_since_last_discharge",
    "n_active_conditions",
    "n_medications_365d",
    "n_conditions_in_stay",
    "n_procedures_in_stay",
    "n_medications_in_stay",
]


@dataclass
class GoldStats:
    admissions: int = 0
    modeling_rows: int = 0
    readmitted: int = 0
    facts: int = 0
    patients_with_facts: int = 0
    extra: dict = field(default_factory=dict)


def _flag_sql(patterns: list[str]) -> str:
    return " or ".join(f"lower(c.display) like '{p}'" for p in patterns)


def admissions_sql(readmission_days: int, reference_date: date) -> str:
    flags = ",\n".join(
        f"      coalesce(max(case when {_flag_sql(p)} then 1 else 0 end), 0) as has_{name}"
        for name, p in CONDITION_FLAGS.items()
    )
    labs = ",\n".join(
        f"      max(case when o.code = '{code}' then o.value_num end) as {name}" for name, code in LAB_FEATURES.items()
    )
    return f"""
with index_adm as (
  select e.encounter_id as admission_id, e.patient_id, e.start_ts as admit_ts, e.end_ts as discharge_ts,
         e.length_hours / 24.0 as los_days, e.reason_display, e.type_display,
         lead(e.start_ts) over (partition by e.patient_id order by e.start_ts) as next_admit_ts,
         lead(e.encounter_id) over (partition by e.patient_id order by e.start_ts) as next_admission_id,
         lag(e.end_ts) over (partition by e.patient_id order by e.start_ts) as previous_discharge_ts
  from silver_encounters e
  where e.class_code = 'IMP' and e.start_ts is not null and e.end_ts is not null and e.end_ts >= e.start_ts
),
data_end as (select max(end_ts) as ts from silver_encounters),
labelled as (
  select a.*,
         date_diff('day', a.discharge_ts, a.next_admit_ts) as days_to_next_admission,
         coalesce(date_diff('day', a.discharge_ts, a.next_admit_ts) between 0 and {readmission_days}, false) as readmitted,
         (p.deceased_ts is not null and p.deceased_ts <= a.discharge_ts + interval 1 day) as died_in_hospital,
         (a.discharge_ts + interval {readmission_days} day > (select ts from data_end)) as censored,
         p.sex, p.age_band as age_band_now, p.race, p.ethnicity,
         greatest(0, p.age_years - cast(floor(date_diff('day', a.admit_ts, cast('{reference_date.isoformat()}' as timestamp)) / 365.25) as integer)) as age_at_admission
  from index_adm a join silver_patients p using (patient_id)
),
prior_enc as (
  select a.admission_id,
         count(*) filter (where e.class_code = 'IMP' and e.start_ts >= a.admit_ts - interval 365 day) as prior_admissions_365d,
         count(*) filter (where e.class_code = 'IMP') as prior_admissions_all,
         count(*) filter (where e.class_code = 'EMER' and e.start_ts >= a.admit_ts - interval 365 day) as prior_ed_visits_365d,
         count(*) filter (where e.class_code = 'AMB' and e.start_ts >= a.admit_ts - interval 365 day) as prior_ambulatory_365d
  from labelled a join silver_encounters e on e.patient_id = a.patient_id and e.start_ts < a.admit_ts
  group by a.admission_id
),
active_cond as (
  select a.admission_id, count(*) as n_active_conditions,
{flags}
  from labelled a join silver_conditions c on c.patient_id = a.patient_id and c.onset_ts < a.admit_ts
       and (c.abatement_ts is null or c.abatement_ts > a.admit_ts)
  group by a.admission_id
),
meds as (
  select a.admission_id,
         count(*) filter (where m.authored_ts < a.admit_ts and m.authored_ts >= a.admit_ts - interval 365 day) as n_medications_365d,
         count(*) filter (where m.encounter_id = a.admission_id) as n_medications_in_stay
  from labelled a join silver_medication_requests m on m.patient_id = a.patient_id
  group by a.admission_id
),
in_stay as (
  select a.admission_id,
         (select count(*) from silver_conditions c where c.encounter_id = a.admission_id) as n_conditions_in_stay,
         (select count(*) from silver_procedures pr where pr.encounter_id = a.admission_id) as n_procedures_in_stay
  from labelled a
),
latest_obs as (
  select admission_id,
{labs}
  from (
    select a.admission_id, o.code, arg_max(o.value_num, o.effective_ts) as value_num
    from labelled a join silver_observations o on o.patient_id = a.patient_id and o.effective_ts <= a.discharge_ts
         and o.effective_ts >= a.admit_ts - interval 730 day and o.value_num is not null
         and o.code in ({", ".join("'" + c + "'" for c in LAB_FEATURES.values())})
    group by a.admission_id, o.code
  ) o
  group by admission_id
)
select l.admission_id, l.patient_id, l.admit_ts, l.discharge_ts, round(l.los_days, 3) as los_days, l.reason_display, l.type_display,
       l.next_admission_id, l.days_to_next_admission, l.readmitted, l.died_in_hospital, l.censored,
       (not l.died_in_hospital and not l.censored) as in_modeling_cohort,
       l.sex, l.age_at_admission,
       case when l.age_at_admission < 18 then '0-17' when l.age_at_admission < 40 then '18-39' when l.age_at_admission < 65 then '40-64'
            when l.age_at_admission < 90 then '65-89' else '90+' end as age_band,
       l.race, l.ethnicity,
       coalesce(pe.prior_admissions_365d, 0) as prior_admissions_365d,
       coalesce(pe.prior_admissions_all, 0) as prior_admissions_all,
       coalesce(pe.prior_ed_visits_365d, 0) as prior_ed_visits_365d,
       coalesce(pe.prior_ambulatory_365d, 0) as prior_ambulatory_365d,
       date_diff('day', l.previous_discharge_ts, l.admit_ts) as days_since_last_discharge,
       coalesce(ac.n_active_conditions, 0) as n_active_conditions,
       {", ".join(f"coalesce(ac.has_{k}, 0) as has_{k}" for k in CONDITION_FLAGS)},
       coalesce(md.n_medications_365d, 0) as n_medications_365d,
       coalesce(md.n_medications_in_stay, 0) as n_medications_in_stay,
       coalesce(st.n_conditions_in_stay, 0) as n_conditions_in_stay,
       coalesce(st.n_procedures_in_stay, 0) as n_procedures_in_stay,
       {", ".join(f"lo.{k}" for k in LAB_FEATURES)}
from labelled l
left join prior_enc pe using (admission_id)
left join active_cond ac using (admission_id)
left join meds md using (admission_id)
left join in_stay st using (admission_id)
left join latest_obs lo using (admission_id)
order by l.patient_id, l.admit_ts
"""


FACTS_SQL = """
with cond as (
  select patient_id, 'Condition/' || condition_id as fact_id, 'Condition' as resource_type, 'problems' as section, onset_ts as ts,
         display || case when abatement_ts is not null then ', from ' || strftime(onset_ts, '%Y-%m-%d') || ' to ' || strftime(abatement_ts, '%Y-%m-%d')
                         else ', since ' || strftime(onset_ts, '%Y-%m-%d') || ' (active)' end as text
  from silver_conditions where display is not null
),
med as (
  select patient_id, 'MedicationRequest/' || medication_request_id as fact_id, 'MedicationRequest' as resource_type, 'medications' as section, authored_ts as ts,
         display || ' (' || coalesce(status, 'unknown status') || '), ordered ' || strftime(authored_ts, '%Y-%m-%d')
         || coalesce(' for ' || reason_display, '') as text
  from silver_medication_requests where display is not null
),
enc as (
  select patient_id, 'Encounter/' || encounter_id as fact_id, 'Encounter' as resource_type, 'encounters' as section, start_ts as ts,
         case class_code when 'IMP' then 'Inpatient admission' when 'EMER' then 'Emergency visit' when 'AMB' then 'Outpatient visit'
                         when 'HH' then 'Home health visit' when 'VR' then 'Virtual visit' when 'OBSENC' then 'Observation stay' else 'Encounter' end
         || ' on ' || strftime(start_ts, '%Y-%m-%d') || coalesce(': ' || type_display, '') || coalesce(', reason ' || reason_display, '')
         || case when class_code = 'IMP' and length_hours is not null then ', ' || cast(round(length_hours / 24.0, 1) as varchar) || ' days' else '' end as text
  from silver_encounters where start_ts is not null
),
obs_latest as (
  select patient_id, code, arg_max(observation_id, effective_ts) as observation_id, arg_max(display, effective_ts) as display,
         max(effective_ts) as ts, arg_max(value_num, effective_ts) as value_num, arg_max(unit, effective_ts) as unit,
         arg_max(value_text, effective_ts) as value_text, arg_max(component_of, effective_ts) as component_of
  from silver_observations
  where category in ('vital-signs', 'laboratory') and (value_num is not null or value_text is not null)
  group by patient_id, code
),
obs as (
  select patient_id, 'Observation/' || coalesce(component_of, observation_id) as fact_id, 'Observation' as resource_type, 'results' as section, ts,
         display || ': ' || coalesce(cast(round(value_num, 2) as varchar) || coalesce(' ' || unit, ''), value_text) || ' on ' || strftime(ts, '%Y-%m-%d') as text
  from obs_latest where display is not null
),
alg as (
  select patient_id, 'AllergyIntolerance/' || allergy_id as fact_id, 'AllergyIntolerance' as resource_type, 'allergies' as section, recorded_ts as ts,
         display || coalesce(' (' || category || coalesce(', ' || criticality || ' criticality', '') || ')', '') || coalesce(': ' || reactions, '')
         || ', recorded ' || strftime(recorded_ts, '%Y-%m-%d') as text
  from silver_allergies where display is not null and recorded_ts is not null
),
imm as (
  select patient_id, 'Immunization/' || immunization_id as fact_id, 'Immunization' as resource_type, 'immunizations' as section, occurrence_ts as ts,
         display || ' on ' || strftime(occurrence_ts, '%Y-%m-%d') as text
  from silver_immunizations where display is not null
),
prc as (
  select patient_id, 'Procedure/' || procedure_id as fact_id, 'Procedure' as resource_type, 'procedures' as section, start_ts as ts,
         display || ' on ' || strftime(start_ts, '%Y-%m-%d') || coalesce(' for ' || reason_display, '') as text
  from silver_procedures where display is not null
),
cp as (
  select patient_id, 'CarePlan/' || careplan_id as fact_id, 'CarePlan' as resource_type, 'care plans' as section, start_ts as ts,
         category_display || ' (' || coalesce(status, 'unknown') || ') since ' || strftime(start_ts, '%Y-%m-%d') || coalesce(' for ' || addresses_display, '') as text
  from silver_careplans where category_display is not null
)
select * from cond union all select * from med union all select * from enc union all select * from obs
union all select * from alg union all select * from imm union all select * from prc union all select * from cp
order by patient_id, section, ts desc
"""


def build_gold(lake: Lake, readmission_days: int, reference_date: date) -> GoldStats:
    con = lake.connect()
    stats = GoldStats()

    admissions = con.sql(admissions_sql(readmission_days, reference_date)).fetch_arrow_table()
    lake.write_delta("gold", "admissions", admissions)
    stats.admissions = admissions.num_rows
    if admissions.num_rows:
        con.register("adm", admissions)
        row = con.sql(
            "select count(*) filter (where in_modeling_cohort), count(*) filter (where in_modeling_cohort and readmitted) from adm"
        ).fetchone()
        stats.modeling_rows, stats.readmitted = (int(row[0]), int(row[1])) if row else (0, 0)

    facts = con.sql(FACTS_SQL).fetch_arrow_table()
    lake.write_delta("gold", "patient_facts", facts, partition_by=None)
    stats.facts = facts.num_rows
    if facts.num_rows:
        con.register("facts", facts)
        row = con.sql("select count(distinct patient_id) from facts").fetchone()
        stats.patients_with_facts = int(row[0]) if row else 0
    return stats
