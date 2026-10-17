# 0003. Readmission model: discharge-time features, a split by patient, calibration before discrimination

Date: 2026-10-17

## Context

The brief asks for a readmission-risk model with SHAP and a model card that covers fairness by age and sex,
and for the number to be AUROC with calibration on a held-out cohort. Readmission models are a classic
place to leak the future into the features and to report a flattering AUROC on a random row split.

## Decision

- **Cohort and label** in SQL (gold): one row per inpatient encounter with a discharge; the label is another
  inpatient admission 0 to 30 days after discharge. In-hospital deaths and admissions less than 30 days
  before the end of the data are excluded from modelling and kept in the table with a flag.
- **Prediction time is discharge.** Every feature is computable at discharge: history strictly before the
  admission (prior admissions, ED and clinic visits, active chronic conditions, medications in the past
  year, days since the last discharge), the stay itself (length of stay, diagnoses, procedures and
  medications during the stay, the admission reason) and the latest vitals and labs before discharge.
- **Split by patient**, not by row, so a patient's other admissions never help predict one of theirs.
  Validation (early stopping, model selection, calibration) and test are separate folds; the test fold is
  touched once.
- **Two models**: a regularised logistic regression on imputed, scaled features and a gradient-boosted tree
  model (XGBoost). The champion is picked on validation AUROC, never on the test fold.
- **Isotonic calibration** on the validation fold for both, because a risk score that is read as a
  probability must be one; the card reports Brier, expected calibration error, calibration slope and
  intercept and the reliability curve, raw and calibrated.
- **Fairness** is reported on the test fold by sex, age band, race and ethnicity: AUROC, calibration-in-
  the-large, selection rate and true/false positive rates at the operating threshold. Age and sex are
  features (they are clinically meaningful); race and ethnicity are not.
- **Explanations**: SHAP for the tree model, standardised coefficients for the linear one; the three
  largest SHAP contributions per admission are stored next to the score so the dashboard can show them.
- Scores for every admission, with the split each one belonged to, are written back to gold, and the run
  is tracked in MLflow.

## Consequences

- The held-out AUROC is what it is; on this synthetic cohort the boosted model reaches about 0.83 and the
  linear model about 0.80, with ECE around 0.03 to 0.04 after calibration. Synthea's readmissions are
  driven by its modules (chemotherapy cycles, post-surgical care), so the number says the pipeline works,
  not that the model would transfer to a hospital.
- Chronic-condition flags come from display-text patterns on SNOMED names; a production version needs value
  sets.
- The bootstrap confidence interval on AUROC is a row bootstrap of the test fold; a cluster bootstrap by
  patient would be slightly wider.
