#!/usr/bin/env node
"use strict";
const { chromium } = require('playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
function flag(name) { return process.argv.includes(name); }
function inside(root, candidate) {
  return candidate === root || candidate.startsWith(root + path.sep);
}

const defaultHome = process.env.JOB_SEARCH_HOME || path.join(
  process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'),
  'job-search-operator'
);
const runtimeHome = path.resolve(arg('--runtime-home', defaultHome));
const profile = path.resolve(arg('--profile', path.join(runtimeHome, 'profiles', 'hh')));
const output = path.resolve(arg('--output', path.join(runtimeHome, 'evidence', 'hh', 'auth.json')));
const timeoutMs = Number(arg('--timeout-ms', '600000'));
const headless = flag('--headless');
if (!inside(runtimeHome, profile) || !inside(runtimeHome, output)) {
  throw new Error('profile and output must stay inside the isolated runtime home');
}

const account = /Мои резюме|Отклики и приглашения/i;
const login = /Введите телефон|Вход для соискателей|Войдите в аккаунт/i;

(async () => {
  fs.mkdirSync(profile, { recursive: true });
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, {
      headless,
      viewport: { width: 1280, height: 900 },
      locale: 'ru-RU'
    });
    const page = context.pages()[0] || await context.newPage();
    await page.goto('https://hh.ru/applicant/resumes', {
      waitUntil: 'domcontentloaded',
      timeout: 90000
    });
    const deadline = Date.now() + timeoutMs;
    let status = 'login_required';
    while (Date.now() < deadline) {
      const body = await page.locator('body').innerText().catch(() => '');
      const url = page.url();
      if (account.test(body) && !/\/account\/login/.test(url)) {
        status = 'authenticated';
        break;
      }
      if (headless || (!login.test(body) && /\/applicant\/resumes/.test(url))) {
        status = 'unknown';
        break;
      }
      await page.waitForTimeout(1500);
    }
    const result = {
      channel: 'hh',
      status,
      authenticated: status === 'authenticated',
      official_origin: new URL(page.url()).origin,
      observed_at: new Date().toISOString(),
      human_login_required: status !== 'authenticated'
    };
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, JSON.stringify(result, null, 2), { mode: 0o600 });
    console.log(JSON.stringify(result));
    process.exitCode = result.authenticated ? 0 : 3;
  } finally {
    if (context) await context.close();
  }
})().catch(error => {
  console.error(JSON.stringify({ error_type: error.name, error: error.message }));
  process.exit(2);
});
