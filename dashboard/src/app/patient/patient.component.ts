import { DatePipe, DecimalPipe, PercentPipe } from '@angular/common';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ApiService, Fact, PatientDetail, Risk, Summary, Verdict } from '../api.service';

@Component({
  selector: 'app-patient',
  imports: [PercentPipe, DecimalPipe, DatePipe, RouterLink],
  template: `
    <p><a routerLink="/patients">← worklist</a></p>
    @if (error()) {
      <p class="error" data-testid="patient-error">{{ error() }}</p>
    }
    @if (patient(); as p) {
      <h2 data-testid="patient-title">
        {{ p.pseudonym }}
        <span class="muted">· {{ p.sex }} · {{ p.age_band }} · {{ p.state }} {{ p.zip3 }}</span>
      </h2>
      <div class="grid">
        <div class="card" data-testid="risk-card">
          <h3>30-day readmission risk</h3>
          @if (risk(); as r) {
            <p>
              <span class="badge" [class]="'badge ' + r.risk_band" style="font-size: 1.1rem">{{
                r.risk | percent: '1.0-0'
              }}</span>
              <span class="muted">
                at discharge {{ r.discharge_ts | date: 'yyyy-MM-dd' }} ·
                {{ r.reason_display ?? 'no reason recorded' }} ·
                {{ r.los_days | number: '1.1-1' }} days</span
              >
            </p>
            <table>
              <thead>
                <tr>
                  <th>factor</th>
                  <th>value</th>
                  <th>effect</th>
                </tr>
              </thead>
              <tbody>
                @for (f of r.top_factors; track f.feature) {
                  <tr>
                    <td>{{ f.feature }}</td>
                    <td>{{ f.value ?? '—' }}</td>
                    <td [class.error]="f.shap > 0" [class.muted]="f.shap <= 0">
                      {{ f.shap > 0 ? '+' : '' }}{{ f.shap | number: '1.2-2' }}
                    </td>
                  </tr>
                }
              </tbody>
            </table>
            <p class="muted">
              {{ r.model_version }} · split: {{ r.split }} · threshold {{ r.threshold | percent }}
            </p>
          } @else {
            <p class="muted">{{ riskNote() }}</p>
          }
        </div>
        <div class="card">
          <h3>Admissions</h3>
          <table>
            <thead>
              <tr>
                <th>admitted</th>
                <th>days</th>
                <th>reason</th>
                <th>readmitted</th>
                <th>risk</th>
              </tr>
            </thead>
            <tbody>
              @for (a of p.admissions ?? []; track a.admission_id) {
                <tr>
                  <td>{{ a.admit_ts | date: 'yyyy-MM-dd' }}</td>
                  <td>{{ a.los_days | number: '1.1-1' }}</td>
                  <td>{{ a.reason_display ?? '—' }}</td>
                  <td>
                    {{ a.readmitted ? 'yes, after ' + a.days_to_next_admission + ' d' : 'no' }}
                  </td>
                  <td>{{ a.risk !== null ? (a.risk | percent: '1.0-0') : '—' }}</td>
                </tr>
              } @empty {
                <tr>
                  <td colspan="5" class="muted">no inpatient admissions</td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      </div>

      <div class="card" data-testid="summary-card">
        <div class="toolbar">
          <h3 style="margin: 0">Cited summary</h3>
          <button
            class="primary"
            (click)="loadSummary(false)"
            [disabled]="loadingSummary()"
            data-testid="summarize"
          >
            {{ summary() ? 'Reload' : 'Generate' }}
          </button>
          <button
            (click)="loadSummary(true)"
            [disabled]="loadingSummary()"
            data-testid="regenerate"
          >
            Regenerate
          </button>
          @if (loadingSummary()) {
            <span class="muted">asking the model…</span>
          }
          @if (summary(); as s) {
            <span class="muted">
              {{ s.model }} · {{ s.retrieval }} ·
              {{
                s.cached
                  ? 'stored ' + (s.created_at | date: 'yyyy-MM-dd HH:mm')
                  : s.latency_s + ' s'
              }}
              ·
              <span
                class="badge"
                [class.ok]="(s.faithfulness?.faithfulness_rate ?? 0) >= 0.9"
                [class.warning]="(s.faithfulness?.faithfulness_rate ?? 0) < 0.9"
                data-testid="faithfulness"
              >
                {{ s.faithfulness?.supported }}/{{ s.faithfulness?.claims }} claims supported
              </span>
            </span>
          }
        </div>
        @if (summaryError()) {
          <p class="error">{{ summaryError() }}</p>
        }
        @if (summary(); as s) {
          @for (section of s.sections; track section.title) {
            <h4 style="margin: 0.75rem 0 0.25rem">{{ section.title }}</h4>
            <ul style="margin: 0; padding-left: 1.2rem">
              @for (c of section.claims; track $index) {
                <li data-testid="claim">
                  {{ c.text }}
                  @for (cite of c.citations; track cite) {
                    <span
                      class="cite"
                      (click)="highlight(cite)"
                      (keydown.enter)="highlight(cite)"
                      tabindex="0"
                      role="button"
                      title="show the cited resource"
                      >{{ short(cite) }}</span
                    >
                  }
                  @if (verdict(section.title, c.text); as v) {
                    <span class="badge" [class]="'badge ' + v.status" [title]="v.anchor ?? ''">{{
                      v.status.replace('_', ' ')
                    }}</span>
                  }
                </li>
              }
            </ul>
          }
          <p class="muted" style="margin-top: 0.75rem">
            Every claim must cite a resource of this patient and repeat its name, value or date; the
            badge is the deterministic check, not the model's opinion.
            {{ s.facts_shown.length }} of {{ s.facts_available }} facts were shown to the model.
          </p>
        }
      </div>

      <div class="card">
        <div class="toolbar">
          <h3 style="margin: 0">Record facts</h3>
          <select
            [value]="section()"
            (change)="section.set($any($event.target).value)"
            data-testid="section"
          >
            <option value="">all sections</option>
            @for (s of sections; track s) {
              <option [value]="s">{{ s }}</option>
            }
          </select>
          <span class="muted">{{ visibleFacts().length }} facts</span>
        </div>
        <table data-testid="facts-table">
          <thead>
            <tr>
              <th>resource</th>
              <th>date</th>
              <th>fact</th>
            </tr>
          </thead>
          <tbody>
            @for (f of visibleFacts(); track f.fact_id) {
              <tr [id]="'fact-' + f.fact_id" [class.fact-highlight]="highlighted() === f.fact_id">
                <td>
                  <code>{{ short(f.fact_id) }}</code>
                </td>
                <td>{{ f.ts ? (f.ts | date: 'yyyy-MM-dd') : '' }}</td>
                <td>{{ f.text }}</td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    }
  `,
})
export class PatientComponent {
  readonly id = input.required<string>();
  private readonly api = inject(ApiService);
  protected readonly patient = signal<PatientDetail | null>(null);
  protected readonly risk = signal<Risk | null>(null);
  protected readonly riskNote = signal('');
  protected readonly facts = signal<Fact[]>([]);
  protected readonly summary = signal<Summary | null>(null);
  protected readonly summaryError = signal('');
  protected readonly loadingSummary = signal(false);
  protected readonly error = signal('');
  protected readonly section = signal('');
  protected readonly highlighted = signal('');
  protected readonly sections = [
    'problems',
    'medications',
    'encounters',
    'results',
    'allergies',
    'procedures',
    'immunizations',
    'care plans',
  ];
  protected readonly visibleFacts = computed(() => {
    const s = this.section();
    const all = this.facts();
    const picked = s ? all.filter((f) => f.section === s) : all;
    return picked.slice(0, 400);
  });

  constructor() {
    effect(() => {
      const id = this.id();
      this.api.patient(id).subscribe({
        next: (p) => this.patient.set(p),
        error: (e) =>
          this.error.set(
            e.status === 404
              ? 'No such patient.'
              : e.status === 401
                ? 'Enter an API key.'
                : `Could not load the patient (${e.status}).`,
          ),
      });
      this.api.risk(id).subscribe({
        next: (r) => this.risk.set(r),
        error: (e) =>
          this.riskNote.set(
            e.status === 404
              ? 'No inpatient admission, so no risk score.'
              : `Risk unavailable (${e.status}).`,
          ),
      });
      this.api
        .facts(id)
        .subscribe({ next: (f) => this.facts.set(f.items), error: () => this.facts.set([]) });
    });
  }

  protected loadSummary(refresh: boolean): void {
    this.loadingSummary.set(true);
    this.summaryError.set('');
    this.api.summary(this.id(), refresh).subscribe({
      next: (s) => {
        this.summary.set(s);
        this.loadingSummary.set(false);
      },
      error: (e) => {
        this.summaryError.set(
          e.status === 502
            ? 'The summary model is unavailable.'
            : e.status === 429
              ? 'Rate limited; wait a minute.'
              : `Summary failed (${e.status}).`,
        );
        this.loadingSummary.set(false);
      },
    });
  }

  protected verdict(sectionTitle: string, text: string): Verdict | undefined {
    return this.summary()?.faithfulness?.verdicts.find(
      (v) => v.section === sectionTitle && v.text === text,
    );
  }

  protected highlight(factId: string): void {
    const fact = this.facts().find((f) => f.fact_id === factId);
    if (fact && this.section() && fact.section !== this.section()) this.section.set('');
    this.highlighted.set(factId);
    setTimeout(
      () =>
        document
          .getElementById('fact-' + factId)
          ?.scrollIntoView({ behavior: 'smooth', block: 'center' }),
      50,
    );
  }

  protected short(ref: string): string {
    const [type, id] = ref.split('/');
    return `${type}/${(id ?? '').slice(0, 8)}`;
  }
}
