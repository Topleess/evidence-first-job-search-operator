#!/usr/bin/env node
'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { chromium } = require('playwright');
const { selectFormRoots, snapshotQuestions, fillAndVerify, findSafeNext } = require('./hh_form_browser');
const { classifyForm, classifyPage, classifyPostSubmit, validateEligibility, validateVacancyUrl, validateApplyUrl } = require('./hh_form_state');

function arg(name, fallback = '') { const i = process.argv.indexOf(`--${name}`); return i >= 0 ? process.argv[i + 1] : fallback; }
function flag(name) { return process.argv.includes(`--${name}`); }
function stableJson(value) {
  if (value === undefined || typeof value === 'function' || typeof value === 'symbol' || (typeof value === 'number' && !Number.isFinite(value))) throw new TypeError('non_canonical_json_value');
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
function sha(value) { return crypto.createHash('sha256').update(typeof value === 'string' ? value : stableJson(value)).digest('hex'); }
function stateCall(db, args) {
  const safeArgs = [...args];
  const tokenIndex = safeArgs.indexOf('--token');
  let token;
  if (tokenIndex >= 0) token = safeArgs.splice(tokenIndex, 2)[1];
  const r = spawnSync('python3', ['scripts/hh_submit_state.py', '--db', db, ...safeArgs], { encoding: 'utf8', env: { ...process.env, HH_EXECUTION_TOKEN: token || '' } });
  if (r.status !== 0) throw new Error(`state_bridge_failed: ${(r.stderr || r.stdout).trim()}`);
  return JSON.parse(r.stdout);
}
function writeJson(file, value) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(value, null, 2)); }
function safeId(value) { return String(value).replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80); }
const CLOSED = /вакансия в архиве|Вакансия закрыта|работодатель уже наш[её]л|вакансия больше не доступна/i;

async function hasAppliedMarker(page) {
  return page.evaluate(() => [...document.querySelectorAll('body *')].some(el => {
    if (el.children.length || !/^Вы[\s\u00a0]*откликнулись$/i.test((el.textContent || '').trim())) return false;
    if (el.closest('[data-qa="vacancy-description"]')) return false;
    let card = el.parentElement;
    for (let i = 0; card && i < 6; i++, card = card.parentElement) {
      if (/^Вы[\s\u00a0]*откликнулись(?:[\s\u00a0]*Чат)?$/i.test((card.textContent || '').trim()) && /Чат/i.test(card.textContent || '')) return true;
    }
    return false;
  }));
}

async function validationErrors(page) {
  const nodes = page.locator('[data-qa*="error" i]:visible, [role="alert"]:visible, .bloko-form-error:visible');
  const errors = [];
  for (let i = 0; i < await nodes.count(); i++) {
    const text = (await nodes.nth(i).innerText().catch(() => '')).trim().replace(/\s+/g, ' ');
    if (text && !errors.includes(text)) errors.push(text);
  }
  return errors.slice(0, 20);
}

async function pageSignals(page, origin) {
  const hasOtpControl = await page.locator('input[autocomplete="one-time-code"], [data-qa*="otp" i]').count() > 0;
  const hasCaptchaControl = await page.locator('iframe[src*="captcha" i], iframe[title*="captcha" i], [data-qa*="captcha" i], img[src*="captcha" i], input[name*="captcha" i]').count() > 0;
  return { origin, hasOtpControl, hasCaptchaControl };
}

async function protectedAuthProbe(context, origin) {
  const probe = await context.newPage();
  try {
    await probe.goto(`${origin}/applicant/resumes`, { waitUntil: 'domcontentloaded', timeout: 90000 });
    await probe.waitForTimeout(1800);
    const text = (await probe.locator('body').innerText()).slice(0, 100000);
    const state = classifyPage({ url: probe.url(), text, ...(await pageSignals(probe, origin)) });
    if (state.state.startsWith('blocked_')) return state;
    if (!/Мои резюме|Отклики и приглашения/i.test(text)) return { state: 'blocked_auth_unknown' };
    return { state: 'authenticated' };
  } finally { await probe.close(); }
}
async function finalSubmit(page) {
  const relocation = page.locator('[data-qa="relocation-warning-confirm"]:visible');
  const relocationCount = await relocation.count();
  if (relocationCount === 1) return { locator: relocation.first(), root: null, kind: 'relocation_warning' };
  if (relocationCount > 1) throw new Error('blocked_ambiguous_action_multiple_relocation_confirm');
  const roots = await selectFormRoots(page);
  if (roots.state !== 'unique') return null;
  const exact = roots.root.locator('[data-qa="vacancy-response-submit-popup"]:visible');
  const exactCount = await exact.count();
  if (exactCount === 1) return { locator: exact.first(), root: roots.root };
  if (exactCount > 1) throw new Error('blocked_ambiguous_action_multiple_submit');
  if (!/\/applicant\/vacancy_response/.test(new URL(page.url()).pathname)) return null;
  const fallback = roots.root.locator('button[type="submit"]:visible').filter({ hasText: /^(Откликнуться|Отправить отклик|Подать заявку)$/i });
  const count = await fallback.count();
  if (count === 1) return { locator: fallback.first(), root: roots.root };
  if (count > 1) throw new Error('blocked_ambiguous_action_multiple_submit');
  return null;
}

function truthFor(_job, base, _questions) {
  return { ...base };
}

async function executeJob(context, options, job) {
  job = job && typeof job === 'object' && !Array.isArray(job) ? job : {};
  const id = String(job.source_job_id || job.external_vacancy_id || '').trim();
  const url = job.source_url || job.job_url || `https://hh.kz/vacancy/${id}`;
  const trustedUrl = validateVacancyUrl(url, id);
  const origin = trustedUrl.ok ? trustedUrl.origin : '';
  const row = { source: 'hh', id, url, company: job.company_name || job.company || '', title: job.title || job.job_title || '', status: 'started', submitted: false, read_back_verified: false, steps: [] };
  let page = null;
  let intentId = null, token = null, submitClicked = false;
  try {
    page = await context.newPage();
    if (!trustedUrl.ok) return { ...row, status: `blocked_${trustedUrl.reason}` };
    const eligibility = validateEligibility(job, id);
    if (!eligibility.ok) return { ...row, status: `blocked_${eligibility.reason}` };
    row.steps.push('live_eligibility_evidence_present');
    const auth = await protectedAuthProbe(context, origin);
    if (auth.state !== 'authenticated') return { ...row, status: auth.state };
    row.steps.push('authenticated');
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 }); await page.waitForTimeout(2500);
    const landedVacancy = validateVacancyUrl(page.url(), id);
    if (!landedVacancy.ok || landedVacancy.origin !== origin) return { ...row, status: 'blocked_vacancy_redirect_mismatch' };
    let text = await page.locator('body').innerText();
    if (CLOSED.test(text)) return { ...row, status: 'closed' };
    if (await hasAppliedMarker(page)) {
      const evidence = path.join(options.evidenceDir, `${safeId(id)}-duplicate-readback.json`);
      writeJson(evidence, { id, url, final_url: page.url(), marker: 'already_applied_on_reopen', observed_at: new Date().toISOString(), body_sha256: sha(text) });
      return { ...row, status: 'duplicate', submitted: true, read_back_verified: true, evidence_path: evidence };
    }
    const apply = page.locator('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"]').first();
    if (!(await apply.count())) return { ...row, status: 'blocked_apply_entry_missing' };
    const href = await apply.getAttribute('href');
    if (!href) return { ...row, status: 'blocked_unsafe_apply_entry_without_href' };
    const formUrl = new URL(href, page.url()).toString();
    const trustedForm = validateApplyUrl(formUrl, id, origin);
    if (!trustedForm.ok) return { ...row, status: `blocked_${trustedForm.reason}` };
    await apply.click(); await page.waitForTimeout(2200);
    row.steps.push('opened_apply_entry_readonly');
    let previousFingerprint = '';
    for (let step = 0; step < 8; step++) {
      text = await page.locator('body').innerText();
      const currentUrl = page.url();
      const boundForm = validateApplyUrl(currentUrl, id, origin);
      const boundVacancy = validateVacancyUrl(currentUrl, id);
      if (!boundForm.ok && !boundVacancy.ok) return { ...row, status: `blocked_${boundForm.reason}`, step, observed_form_url: currentUrl };
      const pageState = classifyPage({ url: page.url(), text, ...(await pageSignals(page, origin)) });
      if (pageState.state.startsWith('blocked_')) return { ...row, status: pageState.state, step };
      if (await hasAppliedMarker(page)) {
        const duplicateBound = validateVacancyUrl(page.url(), id);
        if (!duplicateBound.ok || duplicateBound.origin !== origin) return { ...row, status: 'blocked_unbound_applied_marker', step };
        const evidence = path.join(options.evidenceDir, `${safeId(id)}-duplicate-readback.json`);
        writeJson(evidence, { id, url, final_url: page.url(), marker: 'already_applied_on_reopen', observed_at: new Date().toISOString(), body_sha256: sha(text) });
        return { ...row, status: 'duplicate', submitted: true, read_back_verified: true, evidence_path: evidence };
      }
      const relocationWarning = page.locator('[data-qa="relocation-warning-confirm"]:visible');
      const relocationCount = await relocationWarning.count();
      if (relocationCount > 1) return { ...row, status: 'blocked_ambiguous_action_multiple_relocation_confirm', step };
      let truth;
      let plan;
      let verifiedPlan;
      if (relocationCount === 1) {
        const fingerprint = sha({ route: 'relocation_warning_confirm', vacancy_id: id, url: page.url() });
        truth = {};
        plan = { state: 'form_ready', actions: [], fingerprint };
        verifiedPlan = plan;
        row.steps.push(`classified_relocation_warning_${step}`);
      } else {
        const snapshot = await snapshotQuestions(page);
        if (snapshot.ambiguousRoot) return { ...row, status: 'blocked_ambiguous_form_root', step };
        if (!snapshot.questions.length) return { ...row, status: 'blocked_form_render_missing', step };
        truth = truthFor(job, options.truth, snapshot.questions);
        plan = classifyForm(snapshot, truth);
        row.steps.push(`classified_step_${step}`);
        if (plan.state !== 'form_ready') return { ...row, status: plan.state, blockers: plan.blockers, form_fingerprint: plan.fingerprint, step };
        const filled = await fillAndVerify(page, plan.actions);
        if (!filled.ok) return { ...row, status: 'blocked_fill_verification', blocker: filled, step };
        const verifiedSnapshot = await snapshotQuestions(page);
        verifiedPlan = classifyForm(verifiedSnapshot, truth);
        if (verifiedPlan.state !== 'form_ready') return { ...row, status: 'blocked_form_changed_after_fill', blockers: verifiedPlan.blockers, step };
      }
      const submit = await finalSubmit(page);
      if (submit) {
        let freshPlan;
        if (submit.kind === 'relocation_warning') {
          freshPlan = { state: 'form_ready', actions: [], fingerprint: verifiedPlan.fingerprint };
        } else {
          const freshSnapshot = await snapshotQuestions(page);
          if (freshSnapshot.ambiguousRoot) return { ...row, status: 'blocked_ambiguous_form_root_before_submit', step };
          freshPlan = classifyForm(freshSnapshot, truth);
        }
        if (freshPlan.state !== 'form_ready' || freshPlan.fingerprint !== verifiedPlan.fingerprint) return { ...row, status: 'blocked_form_changed_before_submit', form_fingerprint: verifiedPlan.fingerprint, observed_fingerprint: freshPlan.fingerprint, step };
        const payload = {
          run_id: options.runId,
          vacancy: { id, url, company: row.company, title: row.title },
          form_fingerprint: verifiedPlan.fingerprint,
          truth_map_sha256: sha(truth),
          plan_sha256: sha(plan.actions),
          daily_cap: options.dailyCap,
        };
        const intentFile = path.join(options.evidenceDir, `${safeId(id)}-intent.json`); writeJson(intentFile, payload);
        if (options.dryRun) return { ...row, status: 'dry_run_ready', dry_run: true, form_fingerprint: verifiedPlan.fingerprint, intent_file: intentFile, step };
        const reserved = stateCall(options.db, ['reserve', '--input', intentFile]); intentId = reserved.intent_id;
        token = stateCall(options.db, ['begin', '--intent-id', String(intentId), '--worker-id', 'hh-adaptive-browser']).execution_token;
        const authAgain = await protectedAuthProbe(context, origin);
        if (authAgain.state !== 'authenticated') {
          stateCall(options.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', 'auth_lost_before_submit']);
          return { ...row, status: 'blocked_auth_before_submit', intent_id: intentId };
        }
        const finalBoundForm = validateApplyUrl(page.url(), id, origin);
        const finalBoundVacancy = validateVacancyUrl(page.url(), id);
        const finalPageText = await page.locator('body').innerText();
        const finalPageState = classifyPage({ url: page.url(), text: finalPageText, ...(await pageSignals(page, origin)) });
        const finalSubmitNow = await finalSubmit(page);
        const isRelocationFinal = finalSubmitNow && finalSubmitNow.kind === 'relocation_warning';
        let finalPlan;
        if (isRelocationFinal) {
          finalPlan = { state: 'form_ready', fingerprint: verifiedPlan.fingerprint };
        } else {
          const finalSnapshot = await snapshotQuestions(page);
          finalPlan = classifyForm(finalSnapshot, truth);
        }
        const expectedPageState = 'form_step';
        if ((!finalBoundForm.ok && !finalBoundVacancy.ok) || finalPageState.state !== expectedPageState || finalPlan.state !== 'form_ready' || finalPlan.fingerprint !== verifiedPlan.fingerprint) {
          stateCall(options.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', 'final_submit_fence_changed']);
          return { ...row, status: 'blocked_final_submit_fence', intent_id: intentId, step };
        }
        const submitHandle = finalSubmitNow && await finalSubmitNow.locator.elementHandle();
        const selectedRootHandle = finalSubmitNow && finalSubmitNow.root ? await finalSubmitNow.root.elementHandle() : null;
        const submitScoped = isRelocationFinal
          ? Boolean(submitHandle && finalBoundVacancy.ok && await submitHandle.evaluate(el => el.getAttribute('data-qa') === 'relocation-warning-confirm'))
          : submitHandle && selectedRootHandle && await submitHandle.evaluate((el, selectedRoot) => {
              const root = el.closest('form, [role="dialog"], main');
              return Boolean(selectedRoot.contains(el) && root === selectedRoot && root.querySelector('[data-question-id], textarea[name^="task_"], select[name^="task_"], input[name^="task_"]:not([type="hidden"])'));
            }, selectedRootHandle);
        if (!submitHandle || !submitScoped) {
          stateCall(options.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', 'final_submit_control_changed']);
          return { ...row, status: 'blocked_final_submit_control_changed', intent_id: intentId, step };
        }
        stateCall(options.db, ['check', '--intent-id', String(intentId), '--token', token]);
        submitClicked = true;
        row.steps.push('submit_click_dispatched');
        await submitHandle.click();
        await submitHandle.dispose();
        if (selectedRootHandle) await selectedRootHandle.dispose();
        row.steps.push('submit_click_resolved');
        await page.waitForTimeout(4000);
        const afterText = await page.locator('body').innerText();
        const afterState = classifyPage({ url: page.url(), text: afterText, ...(await pageSignals(page, origin)) });
        const postSubmit = classifyPostSubmit({ pageState: afterState.state, validationErrors: await validationErrors(page) });
        row.steps.push(`post_submit_${postSubmit.state}`);
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 }); await page.waitForTimeout(2500);
        const readbackUrl = validateVacancyUrl(page.url(), id);
        if (!readbackUrl.ok || readbackUrl.origin !== origin) {
          stateCall(options.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', 'readback_redirect_mismatch']);
          return { ...row, status: 'ambiguous_readback_redirect', intent_id: intentId };
        }
        const readbackText = await page.locator('body').innerText();
        if (!(await hasAppliedMarker(page))) {
          stateCall(options.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', `post_submit_${postSubmit.state}_without_verified_readback`]);
          return { ...row, status: `post_submit_${postSubmit.state}`, intent_id: intentId, final_url: page.url(), validation_errors: postSubmit.errors || [] };
        }
        const evidence = path.join(options.evidenceDir, `${safeId(id)}-readback.json`);
        const observedAt = new Date().toISOString();
        const normalizedReadback = readbackText.replace(/\s+/g, ' ').trim();
        const result = { id, url, company: row.company, title: row.title, evidence_path: evidence, submitted_at: observedAt, observed_at: observedAt, marker: 'already_applied_on_reopen', intent_id: intentId, execution_token_sha256: sha(token), form_fingerprint: payload.form_fingerprint, truth_map_sha256: payload.truth_map_sha256, plan_sha256: payload.plan_sha256 };
        writeJson(evidence, { ...result, readback_text: normalizedReadback, readback_text_sha256: sha(normalizedReadback), final_url: page.url() });
        const receipt = stateCall(options.db, ['receipt', '--intent-id', String(intentId), '--token', token, '--result', evidence]);
        return { ...row, status: 'verified', submitted: true, read_back_verified: true, intent_id: intentId, receipt_id: receipt.receipt_id, evidence_path: evidence };
      }
      const next = await findSafeNext(page);
      if (!next) return { ...row, status: 'blocked_no_next_or_submit', form_fingerprint: plan.fingerprint, step };
      if (previousFingerprint && previousFingerprint === plan.fingerprint) return { ...row, status: 'blocked_unchanged_state', form_fingerprint: plan.fingerprint, step };
      previousFingerprint = plan.fingerprint;
      await next.click(); row.steps.push(`advanced_step_${step}`); await page.waitForTimeout(1800);
    }
    return { ...row, status: 'blocked_step_limit' };
  } catch (error) {
    if (intentId && token) try { stateCall(options.db, ['ambiguous', '--intent-id', String(intentId), '--token', token, '--reason', String(error.message).slice(0, 150)]); } catch (_) {}
    return { ...row, status: submitClicked ? 'ambiguous_submit' : 'error_before_submit', error: String(error.message), intent_id: intentId };
  } finally {
    if (page) {
      const shot = path.join(options.evidenceDir, `${safeId(id || Date.now())}-${safeId(row.status)}.png`);
      await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
      await page.close().catch(() => {});
    }
  }
}

(async () => {
  const input = arg('input'); const runId = arg('run-id');
  if (!input || !runId) throw new Error('--input and --run-id are required');
  const options = {
    runId, db: arg('db', 'state/job_funnel.sqlite3'), profile: arg('profile', 'data/browser_profiles/hh_ru'),
    evidenceDir: arg('evidence-dir', 'state/hh-evidence'), dryRun: flag('dry-run'), dailyCap: Number(arg('daily-cap', '20')),
    truth: arg('truth-map') ? JSON.parse(fs.readFileSync(arg('truth-map'), 'utf8')) : {},
  };
  fs.mkdirSync(options.evidenceDir, { recursive: true });
  const jobs = JSON.parse(fs.readFileSync(input, 'utf8')).slice(0, Number(arg('limit', '5')));
  const output = arg('output', 'state/hh_adaptive_batch_latest.json');
  const context = await chromium.launchPersistentContext(options.profile, { headless: true, viewport: { width: 1440, height: 1100 }, locale: 'ru-RU' });
  const results = [];
  try {
    for (const job of jobs) {
      results.push(await executeJob(context, options, job));
      writeJson(output, results);
    }
  } finally { await context.close().catch(() => {}); }
  writeJson(output, results);
  console.log(JSON.stringify({ output, results }, null, 2));
})().catch(error => { console.error(error.stack || error); process.exit(2); });
