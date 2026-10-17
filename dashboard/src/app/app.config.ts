import {
  HttpInterceptorFn,
  provideHttpClient,
  withFetch,
  withInterceptors,
} from '@angular/common/http';
import {
  ApplicationConfig,
  inject,
  provideBrowserGlobalErrorListeners,
  provideZoneChangeDetection,
} from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { ApiService } from './api.service';
import { routes } from './app.routes';

// The API key and the purpose of use travel on every request; the API records both in its audit log.
const credentials: HttpInterceptorFn = (req, next) => {
  const api = inject(ApiService);
  const headers: Record<string, string> = {};
  if (api.apiKey()) headers['X-API-Key'] = api.apiKey();
  if (api.purpose()) headers['X-Purpose'] = api.purpose();
  return next(req.clone({ setHeaders: headers }));
};

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes, withComponentInputBinding()),
    provideHttpClient(withFetch(), withInterceptors([credentials])),
  ],
};
