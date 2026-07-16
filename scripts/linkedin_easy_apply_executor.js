#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { chromium } = require('/tmp/pw/node_modules/playwright');

function arg(name, fallback = '') {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : fallback;
}
function sha(value) { return crypto.createHash('sha256').update(String(value)).digest('hex'); }
function stateCall(db, args) {
  const r = spawnSync('python3', ['scripts/linkedin_submit_state.py', '--db', db, ...args], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`state bridge failed: ${(r.stderr || r.stdout).trim()}`);
  return JSON.parse(r.stdout);
}
function requiredArgs() {
  const o = {
    jobId: arg('job-id'),
    searchUrl: arg('search-url'),
    db: arg('db', 'state/agent-linkedin.sqlite3'),
    profile: arg('profile', 'data/browser_profiles/linkedin'),
    resume: arg('resume', 'resume/resume_product_manager_alexander_shamshurin_2026-07-09.pdf'),
    location: arg('location', 'Moscow, Russia'),
    firstName: arg('first-name', process.env.LINKEDIN_FIRST_NAME || ''),
    lastName: arg('last-name', process.env.LINKEDIN_LAST_NAME || ''),
    phoneNational: arg('phone-national', process.env.LINKEDIN_PHONE_NATIONAL || ''),
    evidenceDir: arg('evidence-dir', 'state/linkedin-evidence'),
  };
  if (!/^\d+$/.test(o.jobId) || !o.searchUrl) throw new Error('--job-id and --search-url are required');
  return o;
}
async function visibleScope(page) {
  const dialog = page.locator('[role=dialog]').last();
  return await dialog.count() ? dialog : page.locator('body');
}
async function clickApply(page, jobId) {
  const card = page.locator(`a[href*="/jobs/view/${jobId}"]`).first();
  if (await card.count()) { await card.click(); await page.waitForTimeout(2000); }
  const names = /Простая подача заявки на вакансию|Easy Apply to|^(Простая подача заявки|Easy Apply|Продолжить подачу заявки|Continue application)$/i;
  const buttons = page.getByRole('button', { name: names });
  for (let i = 0; i < await buttons.count(); i++) {
    if (await buttons.nth(i).isVisible() && !await buttons.nth(i).isDisabled()) {
      await buttons.nth(i).click(); await page.waitForTimeout(1400); return;
    }
  }
  throw new Error('easy_apply_entry_not_found');
}
async function fieldLabel(control) {
  return control.evaluate(e => {
    const explicit = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
    if (explicit) return (explicit.innerText || '').trim();
    const group = e.closest('.jobs-easy-apply-form-section__grouping, .fb-dash-form-element, [data-test-form-element]');
    return ((group && group.innerText) || e.getAttribute('aria-label') || '').trim().slice(0, 800);
  });
}
async function fillKnown(scope, opts) {
  const blockers = [];
  const fields = scope.locator('input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea, select');
  for (let i = 0; i < await fields.count(); i++) {
    const f = fields.nth(i);
    if (!await f.isVisible() || await f.isDisabled()) continue;
    const label = await fieldLabel(f);
    const tag = await f.evaluate(e => e.tagName);
    const value = await f.inputValue().catch(() => '');
    const required = await f.evaluate(e => !!e.required || e.getAttribute('aria-required') === 'true');
    if (value) continue;
    if (/current location|текущее местоположение|местонахожд/i.test(label)) { await f.fill(opts.location); continue; }
    if (/first name|^имя$/i.test(label) && opts.firstName) { await f.fill(opts.firstName); continue; }
    if (/last name|фамилия/i.test(label) && opts.lastName) { await f.fill(opts.lastName); continue; }
    if (/mobile phone|phone number|номер телефона/i.test(label) && opts.phoneNational) { await f.fill(opts.phoneNational); continue; }
    if (tag === 'SELECT') {
      const options = await f.locator('option').allTextContents();
      if (/phone country/i.test(label) && options.some(x => /Russia \(\+7\)/.test(x))) { await f.selectOption({ label: options.find(x => /Russia \(\+7\)/.test(x)) }); continue; }
    }
    if (required || label) blockers.push({ label: label.slice(0, 500), required, kind: tag.toLowerCase() });
  }
  return blockers;
}
async function nextButton(scope) {
  const names = /Перейти к следующему шагу|Review your application|Continue to next step|Проверить заявку|^(Next|Continue|Review|Далее|Продолжить|Проверить)$/i;
  const buttons = scope.getByRole('button', { name: names });
  for (let i = 0; i < await buttons.count(); i++) if (await buttons.nth(i).isVisible() && !await buttons.nth(i).isDisabled()) return buttons.nth(i);
  return null;
}
async function snapshot(scope) {
  return scope.locator('input,textarea,select').evaluateAll(xs => xs.filter(e => e.offsetWidth || e.offsetHeight || e.getClientRects().length).map(e => ({
    id: e.id || '', type: e.type || e.tagName.toLowerCase(), required: !!e.required || e.getAttribute('aria-required') === 'true',
    valueDigest: crypto.subtle ? '' : '',
    label: ((e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)?.innerText) || e.getAttribute('aria-label') || e.closest('.jobs-easy-apply-form-section__grouping, .fb-dash-form-element')?.innerText || '').trim().slice(0, 500),
    value: e.type === 'file' ? '' : e.value || ''
  })));
}

(async () => {
  const o = requiredArgs(); fs.mkdirSync(o.evidenceDir, { recursive: true });
  const context = await chromium.launchPersistentContext(o.profile, { headless: true, viewport: { width: 1440, height: 1100 }, locale: 'en-US' });
  const page = context.pages()[0] || await context.newPage();
  let intentId = null, token = null, submitClicked = false;
  try {
    await page.goto(o.searchUrl, { waitUntil: 'domcontentloaded', timeout: 90000 }); await page.waitForTimeout(4000);
    const body = (await page.locator('body').innerText()).slice(0, 6000);
    if (/\/checkpoint\/|\/challenge\/|security verification|verify your identity|проверка безопасности/i.test(page.url() + body)) throw new Error('captcha_or_2fa_challenge');
    if (/\/login|sign in|войти в linkedin/i.test(page.url())) throw new Error('login_required');
    await clickApply(page, o.jobId);
    const allSnapshots = [];
    for (let step = 0; step < 10; step++) {
      const scope = await visibleScope(page); const text = await scope.innerText();
      const blockers = await fillKnown(scope, o);
      if (blockers.length) {
        const blockerFile = path.join(o.evidenceDir, `${o.jobId}-blocker.json`);
        fs.writeFileSync(blockerFile, JSON.stringify({ job_id: o.jobId, blockers }, null, 2));
        console.log(JSON.stringify({ status: 'blocked_unknown_question', blocker_file: blockerFile, blockers })); return;
      }
      const raw = await snapshot(scope);
      allSnapshots.push(raw.map(x => ({ id: x.id, type: x.type, required: x.required, label: x.label, value_sha256: sha(x.value) })));
      if (/Submit application|Отправить заявку|Подать заявку/.test(text)) break;
      const next = await nextButton(scope); if (!next) throw new Error('no_next_or_submit_control');
      await next.click(); await page.waitForTimeout(1500);
    }
    const scope = await visibleScope(page); const reviewText = await scope.innerText();
    const submit = scope.getByRole('button', { name: /Submit application|Отправить заявку|Подать заявку/i });
    if (!await submit.count()) throw new Error('review_reached_without_submit');
    const fingerprint = sha(JSON.stringify(allSnapshots));
    const reserveFile = path.join(o.evidenceDir, `${o.jobId}-intent.json`);
    fs.writeFileSync(reserveFile, JSON.stringify({
      job_id: o.jobId, job_url: `https://www.linkedin.com/jobs/view/${o.jobId}/`, form_fingerprint: fingerprint,
      payload: { mode: 'linkedin_easy_apply', form_snapshot_sha256: fingerprint, review_text_sha256: sha(reviewText) }
    }, null, 2));
    const reserved = stateCall(o.db, ['reserve', '--input', reserveFile]); intentId = reserved.intent_id;
    token = stateCall(o.db, ['begin', '--intent-id', String(intentId), '--worker-id', 'linkedin-easy-apply-browser']).execution_token;
    await submit.first().click(); submitClicked = true; await page.waitForTimeout(4500);
    const readbackText = (await page.locator('body').innerText()).slice(0, 12000);
    let marker = null;
    if (/Application submitted|Заявка отправлена|Your application was sent|application has been submitted/i.test(readbackText)) marker = 'application_submitted';
    if (!marker) {
      await page.goto(`https://www.linkedin.com/jobs/view/${o.jobId}/`, { waitUntil: 'domcontentloaded', timeout: 90000 }); await page.waitForTimeout(3500);
      const detail = (await page.locator('body').innerText()).slice(0, 12000);
      if (/Статус заявки[\s\S]{0,200}Заявка отправлена|Application status[\s\S]{0,200}Application submitted/i.test(detail)) marker = 'applied_state_on_job_page';
    }
    if (!marker) { stateCall(o.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', 'submit_clicked_without_verified_readback']); console.log(JSON.stringify({ status: 'ambiguous', intent_id: intentId })); return; }
    const finalText = (await page.locator('body').innerText()).slice(0, 12000);
    const evidenceFile = path.join(o.evidenceDir, `${o.jobId}-readback.txt`);
    const readbackFile = path.join(o.evidenceDir, `${o.jobId}-readback.json`);
    fs.writeFileSync(evidenceFile, finalText);
    fs.writeFileSync(readbackFile, JSON.stringify({ marker, job_id: o.jobId }, null, 2));
    const receipt = stateCall(o.db, ['receipt', '--intent-id', String(intentId), '--token', token, '--readback', readbackFile, '--evidence', evidenceFile]);
    await page.screenshot({ path: path.join(o.evidenceDir, `${o.jobId}-verified.png`), fullPage: true });
    console.log(JSON.stringify({ status: 'verified', intent_id: intentId, receipt_id: receipt.receipt_id, marker }));
  } catch (e) {
    if (submitClicked && intentId && token) { try { stateCall(o.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', String(e.message).slice(0, 300)]); } catch (_) {} }
    throw e;
  } finally { await context.close(); }
})().catch(e => { console.error(e.stack || e); process.exit(2); });
