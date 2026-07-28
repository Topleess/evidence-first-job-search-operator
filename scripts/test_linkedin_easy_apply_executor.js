'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const executor = require('./linkedin_easy_apply_executor');

test('state bridge sends execution token only through child environment', () => {
  const token = 'secret-execution-token';
  let observed;
  const fakeSpawn = (command, args, options) => {
    observed = { command, args, options };
    return { status: 0, stdout: '{"state":"execution_fence_valid"}\n', stderr: '' };
  };

  const result = executor.stateCall('/tmp/funnel.sqlite3', ['check', '--intent-id', '7'], token, fakeSpawn);

  assert.deepEqual(result, { state: 'execution_fence_valid' });
  assert.equal(observed.command, 'python3');
  assert.equal(observed.args.includes(token), false);
  assert.equal(observed.args.includes('--token'), false);
  assert.equal(observed.options.env.LINKEDIN_EXECUTION_TOKEN, token);
});

test('semantic fingerprint ignores transient control ids', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <div role="dialog">
      <label for="dynamic-123">Current location *</label>
      <input id="dynamic-123" name="location" required value="Moscow, Russia">
      <fieldset><legend>Work authorization *</legend>
        <label><input type="radio" name="authorized" value="yes" required checked>Yes</label>
        <label><input type="radio" name="authorized" value="no" required>No</label>
      </fieldset>
    </div>`);
  const first = await executor.semanticSnapshot(page.locator('[role=dialog]'));
  await page.locator('#dynamic-123').evaluate(element => { element.id = 'dynamic-999'; });
  await page.locator('label[for="dynamic-123"]').evaluate(element => { element.htmlFor = 'dynamic-999'; });
  const second = await executor.semanticSnapshot(page.locator('[role=dialog]'));
  await browser.close();

  assert.equal(executor.semanticFingerprint(first), executor.semanticFingerprint(second));
});

test('known controls upload the real resume and verify populated form values', async t => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'linkedin-executor-'));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const resume = path.join(temp, 'resume.pdf');
  fs.writeFileSync(resume, Buffer.from('%PDF-1.4\nlocal-test-resume'));
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.setContent(`
    <div role="dialog">
      <label for="resume">Resume *</label><input id="resume" type="file" required>
      <label for="location">Current location *</label><input id="location" required>
      <label for="country">Phone country *</label><select id="country" required>
        <option value="">Choose</option><option value="ru">Russia (+7)</option>
      </select>
      <fieldset><legend>Work authorization *</legend>
        <label><input type="radio" name="work" value="yes" required checked>Yes</label>
        <label><input type="radio" name="work" value="no" required>No</label>
      </fieldset>
      <label><input type="checkbox" required checked>I agree</label>
      <label for="note">Optional note</label><textarea id="note"></textarea>
    </div>`);

  const blockers = await executor.fillKnown(page.locator('[role=dialog]'), {
    resume, location: 'Moscow, Russia', firstName: '', lastName: '', phoneNational: '',
  });

  assert.deepEqual(blockers, []);
  assert.equal(await page.locator('#location').inputValue(), 'Moscow, Russia');
  assert.equal(await page.locator('#country').inputValue(), 'ru');
  assert.deepEqual(await page.locator('#resume').evaluate(input => [...input.files].map(file => ({ name: file.name, size: file.size }))), [
    { name: 'resume.pdf', size: fs.statSync(resume).size },
  ]);
  assert.equal(await page.locator('input[type=radio][value=yes]').isChecked(), true);
  assert.equal(await page.locator('input[type=checkbox]').isChecked(), true);
});

test('pre-submit fence rejects ambiguous submit controls', async t => {
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.setContent(`
    <div role="dialog">
      <label for="location">Current location *</label><input id="location" required value="Moscow">
      <button type="button">Submit application</button>
      <button type="button">Submit application</button>
    </div>`);

  const fence = await executor.captureSubmitFence(page.locator('[role=dialog]'));

  assert.deepEqual(fence, { ok: false, reason: 'submit_control_ambiguous' });
});

test('read-back identity comes from the observed job DOM', async t => {
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.setContent(`
    <main data-job-id="4453058523">
      <a href="https://www.linkedin.com/jobs/view/4453058523/"><h1>Observed Product Manager</h1></a>
      <a href="https://www.linkedin.com/company/example/">Observed Company</a>
      <p>Application submitted</p>
    </main>`);

  const identity = await executor.observeJobIdentity(page);

  assert.deepEqual(identity, {
    observed_job_url: 'https://www.linkedin.com/jobs/view/4453058523/',
    observed_title: 'Observed Product Manager',
    observed_company: 'Observed Company',
  });
});
