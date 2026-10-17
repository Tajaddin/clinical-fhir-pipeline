import { DecimalPipe, KeyValuePipe, PercentPipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';

import { ApiService, ModelCard } from '../api.service';

@Component({
  selector: 'app-model-card',
  imports: [DecimalPipe, PercentPipe, KeyValuePipe],
  template: `
    <h2>Model card</h2>
    @if (error()) {
      <p class="error">{{ error() }}</p>
    }
    @if (card(); as m) {
      <p class="muted">
        {{ m.model_version }} · trained {{ m.trained_at.slice(0, 10) }} · champion
        {{ m.champion }} · {{ m.features.length }} features · threshold {{ m.threshold | percent }}
      </p>
      <div class="card">
        <h3>Held-out performance (split by patient)</h3>
        <table data-testid="model-table">
          <thead>
            <tr>
              <th>model</th>
              <th>val AUROC</th>
              <th>test AUROC</th>
              <th>95% CI</th>
              <th>AUPRC</th>
              <th>Brier</th>
              <th>ECE raw</th>
              <th>ECE calibrated</th>
            </tr>
          </thead>
          <tbody>
            @for (row of m.models | keyvalue; track row.key) {
              <tr>
                <td>
                  {{ row.key }}
                  @if (row.key === m.champion) {
                    <span class="badge ok">champion</span>
                  }
                </td>
                <td>{{ row.value.validation.auroc | number: '1.3-3' }}</td>
                <td>{{ n(row.value.test['auroc']) | number: '1.3-3' }}</td>
                <td>{{ ci(row.value.test['auroc_ci95']) }}</td>
                <td>{{ n(row.value.test['auprc']) | number: '1.3-3' }}</td>
                <td>{{ n(row.value.test['brier']) | number: '1.3-3' }}</td>
                <td>{{ n(row.value.test_uncalibrated['ece']) | number: '1.3-3' }}</td>
                <td>{{ n(row.value.test['ece']) | number: '1.3-3' }}</td>
              </tr>
            }
          </tbody>
        </table>
        <p class="muted">
          Cohort: {{ m.cohort['admissions_modeled'] }} admissions from
          {{ m.cohort['patients_modeled'] }} patients; prevalence
          {{ n(m.cohort['prevalence']) | percent: '1.1-1' }}; test
          {{ m.cohort['test'] }} admissions.
        </p>
      </div>
      <div class="grid">
        @for (attr of fairnessAttrs; track attr) {
          <div class="card">
            <h3>By {{ attr.replace('_', ' ') }}</h3>
            <table>
              <thead>
                <tr>
                  <th>group</th>
                  <th>n</th>
                  <th>prevalence</th>
                  <th>AUROC</th>
                  <th>selection rate</th>
                  <th>calibration</th>
                </tr>
              </thead>
              <tbody>
                @for (r of m.fairness[attr]; track r.group) {
                  <tr>
                    <td>{{ r.group }}</td>
                    <td>{{ r.n }}</td>
                    <td>{{ r.prevalence !== null ? (r.prevalence | percent: '1.0-0') : '—' }}</td>
                    <td>{{ r.auroc !== undefined ? (r.auroc | number: '1.2-2') : '—' }}</td>
                    <td>
                      {{ r.selection_rate !== null ? (r.selection_rate | percent: '1.0-0') : '—' }}
                    </td>
                    <td>
                      {{
                        r.calibration_in_the_large !== undefined
                          ? (r.calibration_in_the_large > 0 ? '+' : '') +
                            (r.calibration_in_the_large | number: '1.3-3')
                          : '—'
                      }}
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
      </div>
      <div class="card">
        <h3>What moves the boosted model (mean |SHAP| on the held-out cohort)</h3>
        <table>
          <tbody>
            @for (f of m.explanations.xgboost_mean_abs_shap.slice(0, 15); track f.feature) {
              <tr>
                <td style="width: 40%">{{ f.feature }}</td>
                <td>
                  <div class="bar">
                    <span
                      [style.width.%]="
                        (100 * f.mean_abs_shap) /
                        m.explanations.xgboost_mean_abs_shap[0].mean_abs_shap
                      "
                    ></span>
                  </div>
                </td>
                <td>{{ f.mean_abs_shap | number: '1.3-3' }}</td>
              </tr>
            }
          </tbody>
        </table>
      </div>
      <p class="muted">
        Synthetic patients only. The full card with limitations is docs/MODEL-CARD.md in the
        repository.
      </p>
    }
  `,
})
export class ModelCardComponent {
  private readonly api = inject(ApiService);
  protected readonly card = signal<ModelCard | null>(null);
  protected readonly error = signal('');
  protected readonly fairnessAttrs = ['sex', 'age_band', 'race', 'ethnicity'];

  constructor() {
    this.api.modelCard().subscribe({
      next: (c) => this.card.set(c),
      error: (e) =>
        this.error.set(
          e.status === 404
            ? 'No trained model yet.'
            : e.status === 401
              ? 'Enter an API key.'
              : `Could not load the model card (${e.status}).`,
        ),
    });
  }

  protected n(v: unknown): number | null {
    return typeof v === 'number' ? v : null;
  }

  protected ci(v: unknown): string {
    return Array.isArray(v) && v.length === 2
      ? `${(v[0] as number).toFixed(2)}–${(v[1] as number).toFixed(2)}`
      : '—';
  }
}
