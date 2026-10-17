import { expect, test } from '@playwright/test';

const ANALYST = process.env['DASHBOARD_API_KEY'] ?? 'e2e-analyst';
const ADMIN = process.env['DASHBOARD_ADMIN_KEY'] ?? 'e2e-admin';

async function useKey(page: import('@playwright/test').Page, key: string) {
  await page.getByTestId('api-key').fill(key);
  await page.getByTestId('save-key').click();
  await page.waitForLoadState('networkidle');
}

test('cohort, worklist, patient with cited summary, model card and audit log', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Clinical FHIR pipeline' })).toBeVisible();
  await expect(page.getByTestId('cohort-error')).toContainText('API key');

  await useKey(page, ANALYST);
  await expect(page.getByTestId('cohort-stats')).toBeVisible();
  await expect(page.getByTestId('cohort-stats')).toContainText('inpatient admissions');
  await expect(page.getByTestId('quality-table').locator('tbody tr')).toHaveCount(25);

  await page.getByTestId('nav-patients').click();
  const rows = page.getByTestId('patient-row');
  await expect(rows.first()).toBeVisible();
  await expect(rows.first()).toHaveAttribute('data-risk', /high|medium|low/);
  await rows.first().click();

  await expect(page.getByTestId('patient-title')).toContainText('P-');
  await expect(page.getByTestId('risk-card')).toContainText('%');
  await expect(page.getByTestId('facts-table').locator('tbody tr').first()).toBeVisible();
  const bodyText = await page.locator('main').innerText();
  expect(bodyText).not.toMatch(/Fakeson|999-12-3456|Imaginary Street/);

  await page.getByTestId('summarize').click();
  await expect(page.getByTestId('claim').first()).toBeVisible({ timeout: 90_000 }); // a local model takes a while
  await expect(page.getByTestId('faithfulness')).toContainText(/\d+\/\d+ claims supported/);
  await page.getByTestId('claim').first().locator('.cite').first().click();
  await expect(page.locator('tr.fact-highlight')).toHaveCount(1);

  await page.getByTestId('nav-model').click();
  await expect(page.getByTestId('model-table')).toContainText('champion');

  await page.getByTestId('nav-audit').click();
  await expect(page.getByTestId('audit-error')).toContainText('not an admin key');
  await useKey(page, ADMIN);
  await page.getByTestId('nav-audit').click();
  await expect(page.getByTestId('audit-row').first()).toBeVisible();
  const actions = await page.getByTestId('audit-row').allInnerTexts();
  expect(actions.join('\n')).toMatch(/summary/);
  expect(actions.join('\n')).toMatch(/403/);
});
