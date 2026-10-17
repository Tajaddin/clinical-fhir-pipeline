import { DatePipe, PercentPipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { Router } from '@angular/router';

import { ApiService, PatientRow } from '../api.service';

@Component({
  selector: 'app-patients',
  imports: [PercentPipe, DatePipe],
  template: `
    <h2>Readmission risk worklist</h2>
    <div class="toolbar">
      <input
        placeholder="pseudonym contains…"
        [value]="search()"
        (input)="search.set($any($event.target).value)"
        (keyup.enter)="load()"
        data-testid="search"
      />
      <label
        >min risk
        <input
          type="number"
          min="0"
          max="1"
          step="0.05"
          style="width: 5rem"
          [value]="minRisk()"
          (input)="minRisk.set($any($event.target).valueAsNumber)"
          data-testid="min-risk"
      /></label>
      <button class="primary" (click)="load()" data-testid="apply">Apply</button>
      <span class="muted">{{ total() }} patients; sorted by the risk of the latest admission</span>
    </div>
    @if (error()) {
      <p class="error">{{ error() }}</p>
    }
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>pseudonym</th>
            <th>sex</th>
            <th>age band</th>
            <th>admissions</th>
            <th>last discharge</th>
            <th>risk of 30-day readmission</th>
          </tr>
        </thead>
        <tbody>
          @for (p of rows(); track p.patient_id) {
            <tr
              class="clickable"
              (click)="open(p)"
              data-testid="patient-row"
              [attr.data-risk]="band(p.risk)"
            >
              <td>
                {{ p.pseudonym }}
                @if (p.deceased) {
                  <span class="badge">deceased</span>
                }
              </td>
              <td>{{ p.sex }}</td>
              <td>{{ p.age_band }}</td>
              <td>{{ p.admissions ?? '—' }}</td>
              <td>{{ p.last_discharge ? (p.last_discharge | date: 'yyyy-MM-dd') : '—' }}</td>
              <td>
                @if (p.risk !== null) {
                  <span class="badge" [class]="'badge ' + band(p.risk)">{{
                    p.risk | percent: '1.0-0'
                  }}</span>
                } @else {
                  <span class="muted">no admission</span>
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
      <div class="toolbar" style="margin-top: 0.75rem">
        <button (click)="page(-1)" [disabled]="offset() === 0">Previous</button>
        <span class="muted">{{ offset() + 1 }}–{{ offset() + rows().length }}</span>
        <button (click)="page(1)" [disabled]="offset() + rows().length >= total()">Next</button>
      </div>
    </div>
  `,
})
export class PatientsComponent {
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);
  protected readonly rows = signal<PatientRow[]>([]);
  protected readonly total = signal(0);
  protected readonly search = signal('');
  protected readonly minRisk = signal<number>(0);
  protected readonly offset = signal(0);
  protected readonly error = signal('');
  readonly limit = 25;

  constructor() {
    this.load();
  }

  protected load(): void {
    const minRisk = Number.isFinite(this.minRisk()) && this.minRisk() > 0 ? this.minRisk() : null;
    this.api
      .patients({ limit: this.limit, offset: this.offset(), minRisk, search: this.search() })
      .subscribe({
        next: (page) => {
          this.rows.set(page.items);
          this.total.set(page.total);
          this.error.set('');
        },
        error: (e) =>
          this.error.set(
            e.status === 401
              ? 'Enter an API key to list patients.'
              : e.status === 429
                ? 'Rate limited; wait a minute.'
                : `Could not load patients (${e.status}).`,
          ),
      });
  }

  protected page(direction: number): void {
    this.offset.set(Math.max(0, this.offset() + direction * this.limit));
    this.load();
  }

  protected open(p: PatientRow): void {
    void this.router.navigate(['/patients', p.patient_id]);
  }

  protected band(risk: number | null): string {
    if (risk == null) return '';
    return risk >= 0.3 ? 'high' : risk >= 0.15 ? 'medium' : 'low';
  }
}
