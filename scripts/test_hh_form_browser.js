const test = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { selectFormRoots, snapshotQuestions, fillAndVerify, findSafeNext } = require('./hh_form_browser');

test('snapshot emits one visual descriptor for a required radio group', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <form>
      <fieldset data-question-id="relocation">
        <legend>Готовы к релокации? *</legend>
        <label><input type="radio" name="relocation" value="yes" required>Да</label>
        <label><input type="radio" name="relocation" value="no" required>Нет</label>
      </fieldset>
      <div data-question-id="phone">
        <label for="phone">Телефон *</label><input id="phone" name="phone" required>
      </div>
    </form>
  `);

  const snapshot = await snapshotQuestions(page);
  await browser.close();

  assert.equal(snapshot.questions.length, 2);
  assert.deepEqual(snapshot.questions[0], {
    key: 'relocation', label: 'Готовы к релокации? *', type: 'choice', required: true, options: ['Да', 'Нет'],
  });
  assert.equal(snapshot.questions[1].key, 'phone');
  assert.equal(snapshot.questions[1].type, 'text');
});

test('snapshot scopes controls to the unique application form root', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <input id="global-search" name="query" aria-label="Поиск вакансий">
    <form id="application">
      <label for="task_1">Комментарий (необязательно)</label>
      <input id="task_1" name="task_1" aria-required="false">
      <button data-qa="vacancy-response-submit-popup" type="submit">Отправить отклик</button>
    </form>`);

  const snapshot = await snapshotQuestions(page);
  assert.deepEqual(snapshot.questions.map(x => x.key), ['task_1']);
  assert.equal(snapshot.ambiguousRoot, false);
  await browser.close();
});

test('snapshot excludes zero-sized controls and fill supports linked labels and select labels', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <form>
      <input name="task_hidden" style="width:0;height:0;border:0;padding:0">
      <fieldset data-question-id="choice"><legend>Формат *</legend>
        <input id="remote" type="radio" name="format" value="r" required><label for="remote">Удалённо</label>
      </fieldset>
      <div data-question-id="country"><label for="country">Страна *</label>
        <select id="country" required><option value="ru">Россия</option></select>
      </div>
    </form>`);

  const snapshot = await snapshotQuestions(page);
  assert.equal(snapshot.questions.some(x => x.key === 'task_hidden'), false);
  assert.deepEqual(await fillAndVerify(page, [
    { key: 'choice', value: 'Удалённо' },
    { key: 'country', value: 'Россия' },
  ]), { ok: true, verified: ['choice', 'country'] });
  await browser.close();
});

test('fill resolves HH task name when data-question-id is absent', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`<form><label>Комментарий <input name="task_42" aria-required="false"></label></form>`);

  const result = await fillAndVerify(page, [{ key: 'task_42', value: 'Проверено' }]);

  assert.deepEqual(result, { ok: true, verified: ['task_42'] });
  assert.equal(await page.locator('[name="task_42"]').inputValue(), 'Проверено');
  await browser.close();
});

test('safe Next never returns a submit control', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`<form><input data-question-id="q" required><button type="submit">Продолжить</button><button type="button" id="safe">Продолжить</button></form>`);
  const next = await findSafeNext(page);
  assert.equal(await next.getAttribute('id'), 'safe');
  await browser.close();
});

test('ambiguous multiple safe Next controls fail closed', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`<button type="button">Далее</button><button type="button">Продолжить</button>`);
  assert.equal(await findSafeNext(page), null);
  await browser.close();
});

test('fill resolves id-only controls and ignores hidden duplicate choices', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <form>
      <label for="id_only">Комментарий</label><input id="id_only">
      <fieldset data-question-id="format"><legend>Формат</legend>
        <label style="display:none"><input type="radio" name="format" value="hidden">Удалённо</label>
        <label><input type="radio" name="format" value="visible">Удалённо</label>
      </fieldset>
    </form>`);
  assert.deepEqual(await fillAndVerify(page, [
    { key: 'id_only', value: 'Проверено' },
    { key: 'format', value: 'Удалённо' },
  ]), { ok: true, verified: ['id_only', 'format'] });
  assert.equal(await page.locator('input[value="hidden"]').isChecked(), false);
  assert.equal(await page.locator('input[value="visible"]').isChecked(), true);
  await browser.close();
});

test('duplicate visible key outside application form fails closed', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <input name="task_9" value="outside">
    <form><label>Внутри <input name="task_9" required></label></form>`);
  const result = await fillAndVerify(page, [{ key: 'task_9', value: 'secret' }]);
  assert.deepEqual(result, { ok: true, verified: ['task_9'] });
  assert.equal(await page.locator('input').nth(0).inputValue(), 'outside');
  assert.equal(await page.locator('input').nth(1).inputValue(), 'secret');
  await browser.close();
});

test('selected application root excludes unrelated fallback submit in shared main', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <main>
      <form id="application-form">
        <input data-question-id="phone" required>
      </form>
      <button id="wrong-global-submit" type="submit">Отправить отклик</button>
    </main>`);
  const roots = await selectFormRoots(page);
  assert.equal(roots.state, 'unique');
  assert.equal(await roots.root.getAttribute('id'), 'application-form');
  assert.equal(await roots.root.locator('button[type="submit"]:visible').count(), 0);
  assert.equal(await page.locator('#wrong-global-submit').isVisible(), true);
  await browser.close();
});

test('fill verifies selected radio and actual text value', async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setContent(`
    <form>
      <fieldset data-question-id="relocation"><legend>Релокация</legend>
        <label><input type="radio" name="relocation" value="yes">Да</label>
        <label><input type="radio" name="relocation" value="no">Нет</label>
      </fieldset>
      <div data-question-id="phone"><label for="phone">Телефон</label><input id="phone" name="phone"></div>
    </form>`);
  const result = await fillAndVerify(page, [
    { key: 'relocation', value: 'Да' },
    { key: 'phone', value: '+79990000000' },
  ]);
  await browser.close();
  assert.deepEqual(result, { ok: true, verified: ['relocation', 'phone'] });
});
