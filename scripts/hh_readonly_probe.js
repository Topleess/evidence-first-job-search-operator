#!/usr/bin/env node
"use strict";
const { chromium } = require('/tmp/pw/node_modules/playwright');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

function arg(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}
const workspace = path.resolve(__dirname, '..');
const profile = path.resolve(arg('--profile', path.join(workspace, 'data/browser_profiles/hh_ru')));
const output = path.resolve(arg('--output', path.join(workspace, 'data/acceptance/hh_readonly_probe.json')));
const vacancyUrl = arg('--vacancy-url');
if (!vacancyUrl || !/^https:\/\/(?:[^/]+\.)?hh\.(?:ru|kz)\/vacancy\/\d+/.test(vacancyUrl)) {
  throw new Error('--vacancy-url must be an https hh.ru/hh.kz vacancy URL');
}
const host = new URL(vacancyUrl).hostname;
if (!profile.startsWith(workspace + path.sep) || !output.startsWith(workspace + path.sep)) {
  throw new Error('profile and output must stay inside the isolated HH workspace');
}

const applied = /Вы\s*откликнулись|Ваш отклик отправлен работодателю|Отклик отправлен|Резюме доставлено/i;
const closed = /вакансия в архиве|Вакансия закрыта|вакансия больше не доступна/i;
const apply = /Откликнуться|Подать заявку/i;
const account = /Мои резюме|Отклики и приглашения|Создать резюме/i;
const login = /Введите телефон|Вход для соискателей|Войдите в аккаунт/i;
const sha256 = text => crypto.createHash('sha256').update(text).digest('hex');

(async () => {
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, {
      headless: true,
      viewport: { width: 1440, height: 1000 },
      locale: 'ru-RU'
    });
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(15000);

    await page.goto(`https://${host}/applicant/resumes`, { waitUntil: 'domcontentloaded', timeout: 90000 });
    await page.waitForTimeout(3000);
    const authText = await page.locator('body').innerText();
    const authUrl = page.url();
    const authStatus = (/\/account\/login/.test(authUrl) || login.test(authText)) ? 'login_required' : (account.test(authText) ? 'authenticated' : 'unknown');

    await page.goto(vacancyUrl, { waitUntil: 'domcontentloaded', timeout: 90000 });
    await page.waitForTimeout(3000);
    const vacancyText = await page.locator('body').innerText();
    const vacancyStatus = applied.test(vacancyText) ? 'already_applied' : (closed.test(vacancyText) ? 'closed' : (apply.test(vacancyText) ? 'available' : 'unknown'));
    const id = (vacancyUrl.match(/\/vacancy\/(\d+)/) || [])[1] || '';
    const result = {
      read_only: true,
      authenticated: authStatus === 'authenticated',
      auth_status: authStatus,
      auth_final_url: authUrl,
      vacancy_id: id,
      url: vacancyUrl,
      final_url: page.url(),
      vacancy_status: vacancyStatus,
      evidence_sha256: sha256(vacancyText),
      title: await page.title(),
      observed_at: new Date().toISOString(),
      actions: ['navigate_auth_page', 'read_auth_dom', 'navigate_vacancy_page', 'read_vacancy_dom'],
      submit_attempted: false
    };
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ output, result }, null, 2));
  } finally {
    if (context) await context.close();
  }
})().catch(error => {
  console.error(JSON.stringify({ error_type: error.name, error: error.message }));
  process.exit(2);
});
