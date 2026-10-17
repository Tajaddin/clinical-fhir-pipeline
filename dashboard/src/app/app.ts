import { Component, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { ApiService } from './api.service';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly api = inject(ApiService);
  protected readonly keyDraft = signal(this.api.apiKey());
  protected readonly health = signal<string>('');

  constructor() {
    this.api.health().subscribe({
      next: (h) =>
        this.health.set(
          `${h.status} · ${h.patients} patients · model ${h.model_version ?? 'none'} · summaries by ${h.llm}`,
        ),
      error: () => this.health.set('API unreachable'),
    });
  }

  protected saveKey(): void {
    this.api.setKey(this.keyDraft());
    location.reload();
  }

  protected onPurpose(event: Event): void {
    this.api.setPurpose((event.target as HTMLSelectElement).value);
  }
}
