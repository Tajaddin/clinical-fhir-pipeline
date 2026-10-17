import { DecimalPipe, KeyValuePipe, PercentPipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ApiService, Cohort, QualityRow } from '../api.service';

@Component({
  selector: 'app-cohort',
  imports: [DecimalPipe, PercentPipe, KeyValuePipe, RouterLink],
  template: `
    <h2>Cohort</h2>
    @if (error()) {
      <p class="error" data-testid="cohort-error">{{ error() }}</p>
    }
    @if (cohort(); as c) {
      <div class="grid" data-testid="cohort-stats">
        <div class="card stat">
          <span class="value">{{ c.patients | number }}</span
          ><span class="label">patients (synthetic)</span>
        </div>
        <div class="card stat">
          <span class="value">{{ c.encounters | number }}</span
          ><span class="label">encounters</span>
        </div>
        @if (c.admissions; as a) {
          <div class="card stat">
            <span class="value">{{ a.total | number }}</span
            ><span class="label">inpatient admissions</span>
          </div>
          <div class="card stat">
            <span class="value">{{ a.readmission_rate | percent: '1.1-1' }}</span>
            <span class="label"
              >readmitted within 30 days ({{ a.readmitted }} of {{ a.modeled }} modeled)</span
            >
          </div>
        }
        @if (c.model; as m) {
          <div class="card stat">
            <span class="value">{{ m.auroc | number: '1.3-3' }}</span>
            <span class="label"
              >held-out AUROC, {{ m.champion }} (CI {{ m.auroc_ci95[0] | number: '1.2-2' }}–{{
                m.auroc_ci95[1] | number: '1.2-2'
              }}); ECE {{ m.ece | number: '1.3-3' }}</span
            >
          </div>
        }
        @if (c.summaries; as s) {
          <div class="card stat">
            <span class="value">{{ s.faithfulness_rate | percent: '1.1-1' }}</span>
            <span class="label"
              >summary claims supported ({{ s.claims }} claims, {{ s.patients }} patients,
              {{ s.model }})</span
            >
          </div>
        }
      </div>

      <div class="grid">
        <div class="card">
          <h3>Patients by age band</h3>
          <table>
            <tbody>
              @for (row of c.by_age_band | keyvalue; track row.key) {
                <tr>
                  <td>{{ row.key }}</td>
                  <td style="width: 55%">
                    <div class="bar">
                      <span [style.width.%]="(100 * row.value) / c.patients"></span>
                    </div>
                  </td>
                  <td>{{ row.value }}</td>
                </tr>
              }
            </tbody>
          </table>
        </div>
        <div class="card">
          <h3>Readmission rate by age band at admission</h3>
          <table>
            <tbody>
              @for (row of c.readmission_by_age_band ?? []; track row.age_band) {
                <tr>
                  <td>{{ row.age_band }}</td>
                  <td style="width: 55%">
                    <div class="bar"><span [style.width.%]="100 * row.rate"></span></div>
                  </td>
                  <td>
                    {{ row.rate | percent: '1.1-1' }}
                    <span class="muted">of {{ row.admissions }}</span>
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
        <div class="card">
          <h3>Patients by sex</h3>
          <table>
            <tbody>
              @for (row of c.by_sex | keyvalue; track row.key) {
                <tr>
                  <td>{{ row.key }}</td>
                  <td>{{ row.value }}</td>
                </tr>
              }
              <tr>
                <td class="muted">deceased</td>
                <td>{{ c.deceased }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h3>Data quality checks on silver</h3>
        @if (c.quality; as q) {
          <p class="muted">
            @for (sev of q | keyvalue; track sev.key) {
              <span
                class="badge"
                [class.ok]="sev.value.passed === sev.value.checks"
                [class.warning]="sev.value.passed !== sev.value.checks"
                >{{ sev.key }}: {{ sev.value.passed }}/{{ sev.value.checks }} passed</span
              >&nbsp;
            }
          </p>
        }
        <table data-testid="quality-table">
          <thead>
            <tr>
              <th>check</th>
              <th>table</th>
              <th>severity</th>
              <th>failing</th>
              <th>result</th>
            </tr>
          </thead>
          <tbody>
            @for (r of quality(); track r.check_name) {
              <tr>
                <td>
                  {{ r.check_name }}
                  <div class="muted">{{ r.description }}</div>
                </td>
                <td>{{ r.table_name }}</td>
                <td>
                  <span class="badge" [class]="'badge ' + r.severity">{{ r.severity }}</span>
                </td>
                <td>
                  {{ r.failing | number }} <span class="muted">of {{ r.total | number }}</span>
                </td>
                <td>
                  <span class="badge" [class.passed]="r.passed" [class.failed]="!r.passed">{{
                    r.passed ? 'pass' : 'fail'
                  }}</span>
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
      <p class="muted">
        Open the <a routerLink="/patients">risk worklist</a> or the
        <a routerLink="/model">model card</a>.
      </p>
    }
  `,
})
export class CohortComponent {
  private readonly api = inject(ApiService);
  protected readonly cohort = signal<Cohort | null>(null);
  protected readonly quality = signal<QualityRow[]>([]);
  protected readonly error = signal('');

  constructor() {
    this.api.cohort().subscribe({
      next: (c) => this.cohort.set(c),
      error: (e) =>
        this.error.set(
          e.status === 401
            ? 'Enter an API key (top right) to see the cohort.'
            : `Could not load the cohort (${e.status}).`,
        ),
    });
    this.api
      .quality()
      .subscribe({ next: (q) => this.quality.set(q), error: () => this.quality.set([]) });
  }
}
