import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./cohort/cohort.component').then((m) => m.CohortComponent),
    title: 'Cohort',
  },
  {
    path: 'patients',
    loadComponent: () => import('./patients/patients.component').then((m) => m.PatientsComponent),
    title: 'Patients',
  },
  {
    path: 'patients/:id',
    loadComponent: () => import('./patient/patient.component').then((m) => m.PatientComponent),
    title: 'Patient',
  },
  {
    path: 'model',
    loadComponent: () =>
      import('./model-card/model-card.component').then((m) => m.ModelCardComponent),
    title: 'Model card',
  },
  {
    path: 'audit',
    loadComponent: () => import('./audit/audit.component').then((m) => m.AuditComponent),
    title: 'Audit log',
  },
  { path: '**', redirectTo: '' },
];
