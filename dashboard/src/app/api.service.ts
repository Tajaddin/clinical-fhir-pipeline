import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable } from 'rxjs';

declare global {
  interface Window {
    __CFP_API_BASE__?: string;
  }
}

export interface PatientRow {
  patient_id: string;
  pseudonym: string;
  sex: string | null;
  age_band: string | null;
  age_years: number | null;
  deceased: boolean;
  risk: number | null;
  admissions: number | null;
  last_discharge: string | null;
}

export interface PatientPage {
  total: number;
  items: PatientRow[];
}

export interface Admission {
  admission_id: string;
  admit_ts: string;
  discharge_ts: string;
  los_days: number;
  reason_display: string | null;
  readmitted: boolean;
  days_to_next_admission: number | null;
  risk: number | null;
  split: string | null;
}

export interface PatientDetail extends Omit<PatientRow, 'admissions'> {
  race: string | null;
  ethnicity: string | null;
  state: string | null;
  zip3: string | null;
  active_problems: { condition_id: string; display: string; onset_ts: string }[];
  medications: {
    medication_request_id: string;
    display: string;
    status: string;
    authored_ts: string;
    reason_display: string | null;
  }[];
  encounters: {
    encounter_id: string;
    class_code: string;
    type_display: string;
    reason_display: string | null;
    start_ts: string;
    length_hours: number | null;
  }[];
  allergies: {
    allergy_id: string;
    display: string;
    category: string | null;
    criticality: string | null;
    reactions: string | null;
  }[];
  admissions?: Admission[];
}

export interface RiskFactor {
  feature: string;
  value: number | string | null;
  shap: number;
}

export interface Risk {
  patient_id: string;
  admission_id: string;
  discharge_ts: string;
  risk: number;
  risk_band: 'low' | 'medium' | 'high';
  threshold: number;
  split: string;
  top_factors: RiskFactor[];
  model_version: string;
  reason_display: string | null;
  los_days: number;
  note: string;
}

export interface Fact {
  fact_id: string;
  resource_type: string;
  section: string;
  ts: string | null;
  text: string;
}

export interface Claim {
  text: string;
  citations: string[];
}

export interface Verdict {
  section: string;
  text: string;
  citations: string[];
  status: 'supported' | 'unsupported' | 'missing_citation' | 'unknown_citation';
  anchor: string | null;
}

export interface Summary {
  cached: boolean;
  created_at: string;
  model: string;
  prompt_version: string;
  retrieval: string;
  latency_s: number;
  sections: { title: string; claims: Claim[] }[];
  facts_shown: string[];
  facts_available: number;
  dropped_citations: number;
  faithfulness: {
    claims: number;
    supported: number;
    faithfulness_rate: number | null;
    verdicts: Verdict[];
  } | null;
}

export interface Cohort {
  patients: number;
  by_sex: Record<string, number>;
  by_age_band: Record<string, number>;
  deceased: number;
  encounters: number;
  admissions?: {
    total: number;
    modeled: number;
    readmitted: number;
    readmission_rate: number | null;
    mean_los_days: number;
  };
  readmission_by_age_band?: { age_band: string; admissions: number; rate: number }[];
  quality?: Record<string, { checks: number; passed: number }>;
  model?: {
    version: string;
    champion: string;
    auroc: number;
    auroc_ci95: [number, number];
    auprc: number;
    brier: number;
    ece: number;
    test_admissions: number;
  };
  summaries?: {
    model: string;
    patients: number;
    claims: number;
    faithfulness_rate: number;
    breakdown: Record<string, number>;
  };
}

export interface QualityRow {
  check_name: string;
  table_name: string;
  severity: string;
  description: string;
  failing: number;
  total: number;
  failing_share: number;
  passed: boolean;
  ran_at: string;
}

export interface AuditRow {
  id: number;
  ts: string;
  actor: string;
  role: string;
  action: string;
  patient_id: string | null;
  resource: string;
  purpose: string | null;
  request_id: string;
  client_ip: string;
  status_code: number;
}

export interface FairnessRow {
  group: string;
  n: number;
  prevalence: number | null;
  mean_predicted: number | null;
  selection_rate: number | null;
  auroc?: number;
  calibration_in_the_large?: number;
  tpr?: number;
  fpr?: number;
}

export interface ModelCard {
  model_version: string;
  trained_at: string;
  champion: string;
  threshold: number;
  features: string[];
  cohort: Record<string, number | string>;
  models: Record<
    string,
    {
      validation: { auroc: number | null };
      test: Record<string, unknown>;
      test_uncalibrated: Record<string, unknown>;
    }
  >;
  fairness: Record<string, FairnessRow[]>;
  explanations: { xgboost_mean_abs_shap: { feature: string; mean_abs_shap: number }[] };
}

const KEY_STORAGE = 'cfp.apiKey';
const PURPOSE_STORAGE = 'cfp.purpose';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  readonly base = (window.__CFP_API_BASE__ ?? '/api').replace(/\/$/, '');
  readonly apiKey = signal(localStorage.getItem(KEY_STORAGE) ?? '');
  readonly purpose = signal(localStorage.getItem(PURPOSE_STORAGE) ?? 'treatment');

  setKey(key: string): void {
    this.apiKey.set(key.trim());
    localStorage.setItem(KEY_STORAGE, key.trim());
  }

  setPurpose(purpose: string): void {
    this.purpose.set(purpose);
    localStorage.setItem(PURPOSE_STORAGE, purpose);
  }

  health(): Observable<{
    status: string;
    version: string;
    patients: number;
    model_version: string | null;
    llm: string;
  }> {
    return this.http.get<{
      status: string;
      version: string;
      patients: number;
      model_version: string | null;
      llm: string;
    }>(`${this.base}/health`);
  }

  cohort(): Observable<Cohort> {
    return this.http.get<Cohort>(`${this.base}/cohort`);
  }

  quality(): Observable<QualityRow[]> {
    return this.http.get<QualityRow[]>(`${this.base}/quality`);
  }

  modelCard(): Observable<ModelCard> {
    return this.http.get<ModelCard>(`${this.base}/model/card`);
  }

  patients(
    opts: { limit?: number; offset?: number; minRisk?: number | null; search?: string } = {},
  ): Observable<PatientPage> {
    let params = new HttpParams().set('limit', opts.limit ?? 50).set('offset', opts.offset ?? 0);
    if (opts.minRisk != null) params = params.set('min_risk', opts.minRisk);
    if (opts.search) params = params.set('search', opts.search);
    return this.http.get<PatientPage>(`${this.base}/patients`, { params });
  }

  patient(id: string): Observable<PatientDetail> {
    return this.http.get<PatientDetail>(`${this.base}/patients/${encodeURIComponent(id)}`);
  }

  risk(id: string): Observable<Risk> {
    return this.http.get<Risk>(`${this.base}/patients/${encodeURIComponent(id)}/risk`);
  }

  facts(id: string, section?: string): Observable<{ count: number; items: Fact[] }> {
    const params = section ? new HttpParams().set('section', section) : undefined;
    return this.http.get<{ count: number; items: Fact[] }>(
      `${this.base}/patients/${encodeURIComponent(id)}/facts`,
      { params },
    );
  }

  summary(id: string, refresh = false): Observable<Summary> {
    const params = new HttpParams().set('refresh', refresh);
    return this.http.get<Summary>(`${this.base}/patients/${encodeURIComponent(id)}/summary`, {
      params,
    });
  }

  audit(limit = 100, patientId?: string): Observable<{ items: AuditRow[] }> {
    let params = new HttpParams().set('limit', limit);
    if (patientId) params = params.set('patient_id', patientId);
    return this.http.get<{ items: AuditRow[] }>(`${this.base}/audit`, { params });
  }
}
