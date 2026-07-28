#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { chromium } = require('playwright');

function arg(name, fallback = '') {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : fallback;
}
function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
function sha(value) { return crypto.createHash('sha256').update(typeof value === 'string' ? value : stableJson(value)).digest('hex'); }
function semanticFingerprint(snapshotValue) { return sha(snapshotValue); }

async function semanticSnapshot(scope) {
  const raw = await scope.locator('input:not([type=hidden]), textarea, select').evaluateAll(controls => {
    const normalized = value => String(value || '').trim().replace(/\s+/g, ' ');
    const visible = element => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      if (style.display === 'none' || style.visibility === 'hidden' || element.closest('[hidden], [aria-hidden="true"]')) return false;
      if ((element.type || '').toLowerCase() === 'file') return Boolean(element.parentElement);
      return box.width > 0 && box.height > 0;
    };
    const labelFor = element => {
      const labelledBy = normalized((element.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' '));
      const explicit = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
      const wrapping = element.closest('label');
      const group = element.closest('fieldset, [role="radiogroup"], [role="group"], .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, [data-test-form-element]');
      const title = group?.querySelector('legend, [data-test-form-element-label], .fb-dash-form-element__label, .artdeco-text-input--label');
      return normalized(labelledBy || element.getAttribute('aria-label') || explicit?.textContent || wrapping?.textContent || title?.textContent || '');
    };
    const optionLabel = element => {
      const explicit = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
      return normalized(explicit?.textContent || element.closest('label')?.textContent || element.value || '');
    };
    const seen = new Set();
    const result = [];
    for (const control of controls) {
      if (!visible(control) || control.disabled) continue;
      const type = (control.type || control.tagName).toLowerCase();
      if (type === 'radio' || type === 'checkbox') {
        const group = control.closest('fieldset, [role="radiogroup"], [role="group"], .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, [data-test-form-element]');
        const peers = [...(group || control.form || document).querySelectorAll(`input[type="${type}"]`)]
          .filter(peer => visible(peer) && !peer.disabled && (group || !control.name || peer.name === control.name));
        const key = group || (control.name ? `${type}:${control.name}` : control);
        if (seen.has(key)) continue;
        seen.add(key);
        const legend = group?.querySelector('legend, [data-test-form-element-label], .fb-dash-form-element__label');
        result.push({
          kind: type === 'radio' ? 'choice' : 'multichoice',
          label: normalized(legend?.textContent || labelFor(control)),
          required: peers.some(peer => peer.required || peer.getAttribute('aria-required') === 'true') || group?.getAttribute('aria-required') === 'true',
          options: peers.map(optionLabel),
          value: peers.filter(peer => peer.checked).map(optionLabel).sort(),
        });
        continue;
      }
      const entry = {
        kind: type === 'select-one' ? 'select' : type,
        label: labelFor(control),
        required: Boolean(control.required || control.getAttribute('aria-required') === 'true'),
        options: control.tagName === 'SELECT' ? [...control.options].map(option => normalized(option.textContent)).filter(Boolean) : [],
        value: type === 'file' ? [...control.files].map(file => ({ name: file.name, size: file.size, type: file.type })) : String(control.value || '').trim(),
      };
      result.push(entry);
    }
    return result;
  });
  return raw.map(control => ({
    kind: control.kind,
    label: control.label,
    required: control.required,
    options: control.options,
    value_sha256: sha(control.value),
  }));
}
function stateCall(db, args, executionToken = '', spawn = spawnSync) {
  const r = spawn('python3', ['scripts/linkedin_submit_state.py', '--db', db, ...args], {
    encoding: 'utf8', env: { ...process.env, LINKEDIN_EXECUTION_TOKEN: executionToken },
  });
  if (r.status !== 0) throw new Error(`state bridge failed: ${(r.stderr || r.stdout).trim()}`);
  return JSON.parse(r.stdout);
}
function requiredArgs() {
  const o = {
    jobId: arg('job-id'),
    runId: arg('run-id'),
    searchUrl: arg('search-url'),
    db: arg('db', 'state/job_funnel.sqlite3'),
    profile: arg('profile', 'data/browser_profiles/linkedin'),
    resume: arg('resume', 'resume/resume_product_manager_alexander_shamshurin_2026-07-09.pdf'),
    location: arg('location', 'Moscow, Russia'),
    firstName: arg('first-name', process.env.LINKEDIN_FIRST_NAME || ''),
    lastName: arg('last-name', process.env.LINKEDIN_LAST_NAME || ''),
    phoneNational: arg('phone-national', process.env.LINKEDIN_PHONE_NATIONAL || ''),
    evidenceDir: arg('evidence-dir', 'state/linkedin-evidence'),
    dryRun: process.argv.includes('--dry-run'),
  };
  if (!/^\d+$/.test(o.jobId) || !o.searchUrl || !o.runId) throw new Error('--job-id, --search-url and --run-id are required');
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
  const fileInputs = scope.locator('input[type=file]');
  for (let i = 0; i < await fileInputs.count(); i++) {
    const input = fileInputs.nth(i);
    if (await input.isDisabled()) continue;
    const label = await fieldLabel(input);
    const required = await input.evaluate(element => Boolean(element.required || element.getAttribute('aria-required') === 'true'));
    if (!/resume|résumé|cv|curriculum|резюме/i.test(label) && !required) continue;
    try {
      await input.setInputFiles(opts.resume);
      const observed = await input.evaluate(element => [...element.files].map(file => ({ name: file.name, size: file.size })));
      const expected = { name: path.basename(opts.resume), size: fs.statSync(opts.resume).size };
      if (observed.length !== 1 || observed[0].name !== expected.name || observed[0].size !== expected.size) {
        blockers.push({ label: label.slice(0, 500), required, kind: 'file', reason: 'resume_upload_readback_mismatch' });
      }
    } catch (_) {
      blockers.push({ label: label.slice(0, 500), required, kind: 'file', reason: 'resume_upload_failed' });
    }
  }

  const fields = scope.locator('input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]):not([type=submit]):not([type=button]), textarea, select');
  for (let i = 0; i < await fields.count(); i++) {
    const field = fields.nth(i);
    if (!await field.isVisible() || await field.isDisabled()) continue;
    const label = await fieldLabel(field);
    const tag = await field.evaluate(element => element.tagName);
    const required = await field.evaluate(element => Boolean(element.required || element.getAttribute('aria-required') === 'true'));
    let value = await field.inputValue().catch(() => '');
    if (!value) {
      let wanted = '';
      if (/current location|текущее местоположение|местонахожд/i.test(label)) wanted = opts.location;
      else if (/first name|^имя$/i.test(label)) wanted = opts.firstName;
      else if (/last name|фамилия/i.test(label)) wanted = opts.lastName;
      else if (/mobile phone|phone number|номер телефона/i.test(label)) wanted = opts.phoneNational;
      if (wanted) {
        await field.fill(wanted);
        value = await field.inputValue();
        if (value.trim() !== String(wanted).trim()) blockers.push({ label: label.slice(0, 500), required, kind: tag.toLowerCase(), reason: 'value_readback_mismatch' });
      } else if (tag === 'SELECT' && /phone country|код страны|страна.*телефон/i.test(label)) {
        const options = await field.locator('option').allTextContents();
        const option = options.find(text => /Russia\s*\(\+7\)|Россия\s*\(\+7\)/i.test(text));
        if (option) {
          await field.selectOption({ label: option });
          value = await field.inputValue();
        }
      }
    }
    if (required && !String(value || '').trim()) blockers.push({ label: label.slice(0, 500), required, kind: tag.toLowerCase(), reason: 'required_value_missing' });
  }

  const choices = scope.locator('input[type=radio], input[type=checkbox]');
  const seen = new Set();
  for (let i = 0; i < await choices.count(); i++) {
    const choice = choices.nth(i);
    if (!await choice.isVisible() || await choice.isDisabled()) continue;
    const description = await choice.evaluate(element => {
      const container = element.closest('fieldset, [role="radiogroup"], [role="group"], .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, [data-test-form-element]');
      const peers = [...(container || element.form || document).querySelectorAll(`input[type="${element.type}"]`)]
        .filter(peer => !element.name || peer.name === element.name || Boolean(container));
      const label = container?.querySelector('legend, [data-test-form-element-label], .fb-dash-form-element__label')?.textContent
        || element.closest('label')?.textContent || element.getAttribute('aria-label') || '';
      return {
        key: container?.getAttribute('data-test-form-element') || `${element.type}:${element.name || element.id || label}`,
        label: String(label).trim().replace(/\s+/g, ' '),
        required: peers.some(peer => peer.required || peer.getAttribute('aria-required') === 'true') || container?.getAttribute('aria-required') === 'true',
        checked: peers.some(peer => peer.checked),
        kind: element.type === 'radio' ? 'choice' : 'multichoice',
      };
    });
    if (seen.has(description.key)) continue;
    seen.add(description.key);
    if (description.required && !description.checked) blockers.push({ label: description.label.slice(0, 500), required: true, kind: description.kind, reason: 'required_choice_missing' });
  }
  return blockers;
}
async function nextButton(scope) {
  const names = /Перейти к следующему шагу|Review your application|Continue to next step|Проверить заявку|^(Next|Continue|Review|Далее|Продолжить|Проверить)$/i;
  const buttons = scope.getByRole('button', { name: names });
  for (let i = 0; i < await buttons.count(); i++) if (await buttons.nth(i).isVisible() && !await buttons.nth(i).isDisabled()) return buttons.nth(i);
  return null;
}
async function captureSubmitFence(scope) {
  const controls = scope.getByRole('button', { name: /^(Submit application|Отправить заявку|Подать заявку|Bewerbung absenden)$/i });
  const candidates = [];
  for (let i = 0; i < await controls.count(); i++) {
    const control = controls.nth(i);
    if (await control.isVisible() && !await control.isDisabled()) candidates.push(control);
  }
  if (candidates.length === 0) return { ok: false, reason: 'submit_control_missing' };
  if (candidates.length !== 1) return { ok: false, reason: 'submit_control_ambiguous' };
  return {
    ok: true,
    submit: candidates[0],
    fingerprint: semanticFingerprint(await semanticSnapshot(scope)),
  };
}
async function observeJobIdentity(page) {
  return page.evaluate(() => {
    const normalized = value => String(value || '').trim().replace(/\s+/g, ' ');
    const parseJobUrl = value => {
      try {
        const parsed = new URL(value, document.baseURI);
        const match = parsed.pathname.match(/^\/jobs\/view\/(\d+)\/?$/);
        if (!/^(www\.)?linkedin\.com$/i.test(parsed.hostname) || !match) return null;
        return { id: match[1], url: `https://www.linkedin.com/jobs/view/${match[1]}/` };
      } catch (_) { return null; }
    };
    const titleElement = document.querySelector(
      '.job-details-jobs-unified-top-card__job-title h1, .job-details-jobs-unified-top-card__job-title, main h1, h1'
    );
    const root = titleElement?.closest('[data-job-id], [data-occludable-job-id], main, .jobs-search__job-details') || document;
    const stableId = normalized(
      root.getAttribute?.('data-job-id') || root.getAttribute?.('data-occludable-job-id') || ''
    ).match(/^\d+$/)?.[0] || '';
    const titleLink = titleElement?.closest('a[href*="/jobs/view/"]');
    const jobLinks = [titleLink, ...root.querySelectorAll?.('a[href*="/jobs/view/"]') || []];
    let observed = null;
    for (const link of jobLinks) {
      const parsed = parseJobUrl(link?.href || '');
      if (parsed && (!stableId || parsed.id === stableId)) { observed = parsed; break; }
    }
    if (!observed && stableId) observed = { id: stableId, url: `https://www.linkedin.com/jobs/view/${stableId}/` };
    if (!observed) observed = parseJobUrl(location.href);
    const companyElement = root.querySelector?.(
      '.job-details-jobs-unified-top-card__company-name a, .job-details-jobs-unified-top-card__company-name, a[href*="/company/"]'
    ) || document.querySelector('a[href*="/company/"]');
    const observedTitle = normalized(titleElement?.textContent || '');
    const observedCompany = normalized(companyElement?.textContent || '');
    if (!observed || !observedTitle || !observedCompany) {
      return { observed_job_url: '', observed_title: '', observed_company: '' };
    }
    return {
      observed_job_url: observed.url,
      observed_title: observedTitle.slice(0, 300),
      observed_company: observedCompany.slice(0, 300),
    };
  });
}
async function snapshot(scope) {
  return scope.locator('input,textarea,select').evaluateAll(xs => xs.filter(e => e.offsetWidth || e.offsetHeight || e.getClientRects().length).map(e => ({
    id: e.id || '', type: e.type || e.tagName.toLowerCase(), required: !!e.required || e.getAttribute('aria-required') === 'true',
    valueDigest: crypto.subtle ? '' : '',
    label: ((e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)?.innerText) || e.getAttribute('aria-label') || e.closest('.jobs-easy-apply-form-section__grouping, .fb-dash-form-element')?.innerText || '').trim().slice(0, 500),
    value: e.type === 'file' ? '' : e.value || ''
  })));
}

async function main() {
  const o = requiredArgs(); fs.mkdirSync(o.evidenceDir, { recursive: true });
  if (!o.dryRun) {
    const recovery = stateCall(o.db, ['recover', '--older-than-seconds', '900', '--source', 'linkedin']);
    if (recovery.count) console.error(JSON.stringify({ status: 'stale_executions_recovered', intent_ids: recovery.intent_ids }));
  }
  const context = await chromium.launchPersistentContext(o.profile, { headless: true, viewport: { width: 1440, height: 1100 }, locale: 'en-US' });
  const page = context.pages()[0] || await context.newPage();
  let intentId = null, token = null, submitDispatched = false;
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
    const submitFence = await captureSubmitFence(scope);
    if (!submitFence.ok) {
      console.log(JSON.stringify({ status: 'blocked_pre_side_effect', reason: submitFence.reason, job_id: o.jobId }));
      return;
    }
    const fingerprint = submitFence.fingerprint;
    const jobTitle = (await page.locator('h1').first().innerText().catch(() => '')).trim().slice(0, 300) || 'Unknown';
    const company = (await page.locator('a[href*="/company/"]').first().innerText().catch(() => '')).trim().slice(0, 300) || 'Unknown';
    if (o.dryRun) {
      console.log(JSON.stringify({ status: 'dry_run_ready', job_id: o.jobId, form_fingerprint: fingerprint }));
      return;
    }
    const reserveFile = path.join(o.evidenceDir, `${o.jobId}-intent.json`);
    fs.writeFileSync(reserveFile, JSON.stringify({
      job_id: o.jobId, job_url: `https://www.linkedin.com/jobs/view/${o.jobId}/`, form_fingerprint: fingerprint,
      payload: { source: 'linkedin', external_id: o.jobId, company, job_title: jobTitle, mode: 'linkedin_easy_apply', form_snapshot_sha256: fingerprint, review_text_sha256: sha(reviewText) }
    }, null, 2));
    const reserved = stateCall(o.db, ['reserve', '--input', reserveFile, '--run-id', o.runId]);
    if (reserved.state === 'duplicate_verified_receipt') { console.log(JSON.stringify({ status: 'duplicate_verified_receipt', job_id: o.jobId })); return; }
    intentId = reserved.intent_id;
    token = stateCall(o.db, ['begin', '--intent-id', String(intentId), '--worker-id', 'linkedin-easy-apply-browser']).execution_token;
    const liveScope = await visibleScope(page);
    const liveFence = await captureSubmitFence(liveScope);
    if (!liveFence.ok || liveFence.fingerprint !== fingerprint) {
      const reason = liveFence.ok ? 'review_fingerprint_changed' : liveFence.reason;
      stateCall(o.db, ['blocked', '--intent-id', String(intentId), '--reason', reason], token);
      token = null;
      console.log(JSON.stringify({ status: 'blocked_pre_side_effect', intent_id: intentId, reason }));
      return;
    }
    stateCall(o.db, ['check', '--intent-id', String(intentId)], token);
    submitDispatched = true;
    await liveFence.submit.click();
    await page.waitForTimeout(4500);
    const readbackText = (await page.locator('body').innerText()).slice(0, 12000);
    let marker = null;
    if (/Application submitted|Заявка отправлена|Your application was sent|application has been submitted/i.test(readbackText)) marker = 'application_submitted';
    if (!marker) {
      await page.goto(`https://www.linkedin.com/jobs/view/${o.jobId}/`, { waitUntil: 'domcontentloaded', timeout: 90000 }); await page.waitForTimeout(3500);
      const detail = (await page.locator('body').innerText()).slice(0, 12000);
      if (/Статус заявки[\s\S]{0,200}Заявка отправлена|Application status[\s\S]{0,200}Application submitted/i.test(detail)) marker = 'applied_state_on_job_page';
    }
    if (!marker) {
      stateCall(o.db, ['ambiguous', '--intent-id', String(intentId), '--reason', 'submit_dispatched_without_verified_readback'], token);
      token = null;
      console.log(JSON.stringify({ status: 'ambiguous', intent_id: intentId }));
      return;
    }
    const finalText = (await page.locator('body').innerText()).slice(0, 12000);
    const observed = await observeJobIdentity(page);
    if (!observed.observed_job_url || !observed.observed_title || !observed.observed_company) {
      stateCall(o.db, ['ambiguous', '--intent-id', String(intentId), '--reason', 'verified_marker_without_observed_job_identity'], token);
      token = null;
      console.log(JSON.stringify({ status: 'ambiguous', intent_id: intentId, reason: 'observed_job_identity_missing' }));
      return;
    }
    const observedJobId = new URL(observed.observed_job_url).pathname.match(/^\/jobs\/view\/(\d+)\/?$/)[1];
    const evidenceFile = path.join(o.evidenceDir, `${o.jobId}-readback.txt`);
    const readbackFile = path.join(o.evidenceDir, `${o.jobId}-readback.json`);
    fs.writeFileSync(evidenceFile, finalText);
    fs.writeFileSync(readbackFile, JSON.stringify({ marker, job_id: observedJobId, ...observed }, null, 2));
    const receipt = stateCall(o.db, ['receipt', '--intent-id', String(intentId), '--readback', readbackFile, '--evidence', evidenceFile], token);
    token = null;
    await page.screenshot({ path: path.join(o.evidenceDir, `${o.jobId}-verified.png`), fullPage: true });
    console.log(JSON.stringify({ status: 'verified', intent_id: intentId, receipt_id: receipt.receipt_id, marker }));
  } catch (e) {
    if (intentId && token) {
      try {
        const command = submitDispatched ? 'ambiguous' : 'blocked';
        stateCall(o.db, [command, '--intent-id', String(intentId), '--reason', String(e.message).slice(0, 300)], token);
      } catch (_) {}
    }
    throw e;
  } finally { await context.close(); }
}

module.exports = { captureSubmitFence, fillKnown, observeJobIdentity, semanticFingerprint, semanticSnapshot, stateCall };

if (require.main === module) {
  main().catch(e => { console.error(e.stack || e); process.exit(2); });
}
