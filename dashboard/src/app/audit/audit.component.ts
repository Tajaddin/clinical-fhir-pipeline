import { DatePipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';

import { ApiService, AuditRow } from '../api.service';

@Component({
  selector: 'app-audit',
  imports: [DatePipe],
  template: `
    <h2>Audit log</h2>
    <p class="muted">
      Every request that touched patient data, including denied ones: who (a key fingerprint, never
      the key), what, which patient, why, when and the outcome. Admin keys only.
    </p>
    <div class="toolbar">
      <input
        placeholder="patient id"
        [value]="patientId()"
        (input)="patientId.set($any($event.target).value)"
        (keyup.enter)="load()"
        data-testid="audit-patient"
      />
      <button class="primary" (click)="load()" data-testid="audit-apply">Filter</button>
    </div>
    @if (error()) {
      <p class="error" data-testid="audit-error">{{ error() }}</p>
    }
    <div class="card">
      <table data-testid="audit-table">
        <thead>
          <tr>
            <th>when</th>
            <th>actor</th>
            <th>role</th>
            <th>action</th>
            <th>patient</th>
            <th>purpose</th>
            <th>status</th>
            <th>request</th>
          </tr>
        </thead>
        <tbody>
          @for (r of rows(); track r.id) {
            <tr data-testid="audit-row">
              <td>{{ r.ts | date: 'yyyy-MM-dd HH:mm:ss' }}</td>
              <td>
                <code>{{ r.actor }}</code>
              </td>
              <td>{{ r.role }}</td>
              <td>{{ r.action }}</td>
              <td>
                <code>{{ r.patient_id ? r.patient_id.slice(0, 8) : '' }}</code>
              </td>
              <td>{{ r.purpose ?? '' }}</td>
              <td>
                <span
                  class="badge"
                  [class.ok]="r.status_code < 400"
                  [class.warning]="r.status_code >= 400 && r.status_code < 500"
                  [class.failed]="r.status_code >= 500"
                  >{{ r.status_code }}</span
                >
              </td>
              <td>
                <code class="muted">{{ r.request_id.slice(0, 8) }}</code>
              </td>
            </tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class AuditComponent {
  private readonly api = inject(ApiService);
  protected readonly rows = signal<AuditRow[]>([]);
  protected readonly patientId = signal('');
  protected readonly error = signal('');

  constructor() {
    this.load();
  }

  protected load(): void {
    this.api.audit(200, this.patientId() || undefined).subscribe({
      next: (r) => {
        this.rows.set(r.items);
        this.error.set('');
      },
      error: (e) =>
        this.error.set(
          e.status === 403
            ? 'This key is not an admin key; the audit log is restricted.'
            : e.status === 401
              ? 'Enter an API key.'
              : `Could not load the audit log (${e.status}).`,
        ),
    });
  }
}
