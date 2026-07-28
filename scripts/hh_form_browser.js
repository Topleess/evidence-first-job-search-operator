async function selectFormRoots(page) {
  const finalControls = page.locator('[data-qa="vacancy-response-submit-popup"]:visible');
  const roots = [];
  for (let i = 0; i < await finalControls.count(); i++) {
    const root = finalControls.nth(i).locator('xpath=ancestor::*[self::form or @role="dialog" or self::main][1]');
    if (await root.count()) roots.push(root);
  }
  if (!roots.length) {
    const scopes = page.locator('form:visible, [role="dialog"]:visible, main:visible');
    for (let i = 0; i < await scopes.count(); i++) {
      const scope = scopes.nth(i);
      const hasNestedApplicationScope = await scope.evaluate(el => [...el.querySelectorAll('form, [role="dialog"], main')].some(child =>
        child !== el && child.querySelector('[data-question-id], textarea[name^="task_"], select[name^="task_"], input[name^="task_"]:not([type="hidden"])')
      ));
      if (!hasNestedApplicationScope && await scope.locator('[data-question-id]:visible, textarea[name^="task_"]:visible, select[name^="task_"]:visible, input[name^="task_"]:visible').count()) roots.push(scope);
    }
  }
  if (roots.length > 1) return { state: 'ambiguous', reason: 'form_root_ambiguous' };
  if (!roots.length) return { state: 'missing', reason: 'form_root_missing' };
  return { state: 'unique', root: roots[0] };
}

async function snapshotQuestions(page) {
  return page.evaluate(() => {
    const visible = el => {
      const style = getComputedStyle(el);
      const box = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
    };
    const finalControls = [...document.querySelectorAll('[data-qa="vacancy-response-submit-popup"]')].filter(visible);
    let rootCandidates = [...new Set(finalControls.map(el => el.closest('form, [role="dialog"], main')).filter(Boolean))];
    if (!rootCandidates.length) {
      for (const scope of document.querySelectorAll('form, [role="dialog"], main')) {
        if (visible(scope) && scope.querySelector('[data-question-id], textarea[name^="task_"], select[name^="task_"], input[name^="task_"]:not([type="hidden"])')) rootCandidates.push(scope);
      }
    }
    rootCandidates = rootCandidates.filter(root => !rootCandidates.some(other => other !== root && root.contains(other)));
    if (rootCandidates.length !== 1) return { url: location.href, questions: [], ambiguousRoot: true };
    const root = rootCandidates[0];
    const controls = [...root.querySelectorAll('input, textarea, select')]
      .filter(el => visible(el) && !['hidden', 'submit', 'button'].includes((el.type || '').toLowerCase()));
    const containers = [];
    const seen = new Set();
    for (const control of controls) {
      const container = control.closest('[data-question-id], [data-qa="task-body"], [data-qa*="question"], fieldset, .bloko-form-item') || control.parentElement;
      if (!container || seen.has(container)) continue;
      seen.add(container);
      containers.push(container);
    }
    for (const container of containers) {
      const nested = [...container.querySelectorAll('input, textarea, select')]
        .filter(el => visible(el) && !['hidden', 'submit', 'button'].includes((el.type || '').toLowerCase()));
      const kinds = nested.map(el => (el.type || el.tagName).toLowerCase());
      if (nested.length > 1 && !kinds.every(kind => kind === 'radio') && !kinds.every(kind => kind === 'checkbox')) {
        return { url: location.href, questions: [], ambiguousRoot: true };
      }
    }
    const questions = containers.map((container, index) => {
      const nested = [...container.querySelectorAll('input, textarea, select')]
        .filter(el => visible(el) && !['hidden', 'submit', 'button'].includes((el.type || '').toLowerCase()));
      const first = nested[0];
      const types = nested.map(el => (el.type || el.tagName).toLowerCase());
      const isChoice = types.some(x => x === 'radio');
      const isMulti = !isChoice && types.some(x => x === 'checkbox');
      let type = isChoice ? 'choice' : isMulti ? 'multichoice' : first?.tagName === 'TEXTAREA' ? 'textarea' : first?.tagName === 'SELECT' ? 'select' : (first?.type || 'text').toLowerCase();
      const legend = container.querySelector('legend');
      const linked = first?.id ? container.querySelector(`label[for="${CSS.escape(first.id)}"]`) : null;
      const title = legend || container.querySelector('[data-qa="task-question"], [data-qa*="title"], .bloko-form-legend, .bloko-form-label') || linked;
      const labelledBy = (first?.getAttribute('aria-labelledby') || '')
        .split(/\s+/)
        .map(id => document.getElementById(id)?.textContent || '')
        .join(' ');
      const label = (title?.textContent || labelledBy || first?.getAttribute('aria-label') || first?.name || '').trim().replace(/\s+/g, ' ');
      const options = (isChoice || isMulti) ? nested.map(el => {
        const optionLabel = el.closest('label') || (el.id ? container.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null);
        return (optionLabel?.textContent || el.value || '').trim().replace(/\s+/g, ' ');
      }).filter(Boolean) : first?.tagName === 'SELECT' ? [...first.options].map(x => x.textContent.trim()).filter(Boolean) : [];
      const explicitlyRequired = nested.some(el => el.required || el.getAttribute('aria-required') === 'true') || /(^|\s)\*(\s|$)|обязательное поле/i.test(label) || container.getAttribute('aria-required') === 'true';
      const explicitlyOptional = /необязательно|optional/i.test(label) || nested.every(el => el.getAttribute('aria-required') === 'false');
      const required = explicitlyRequired ? true : explicitlyOptional ? false : 'unknown';
      const key = container.getAttribute('data-question-id') || first?.name || first?.id || container.getAttribute('data-qa') || `dom:${index}`;
      const question = { key, label, type, required, options };
      const currentSelected = (isChoice || isMulti) ? nested.filter(el => el.checked).map(el => {
        const optionLabel = el.closest('label') || (el.id ? container.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null);
        return (optionLabel?.textContent || el.value || '').trim().replace(/\s+/g, ' ');
      }).filter(Boolean) : [];
      const currentValue = (!isChoice && !isMulti && first) ? String(first.value || '').trim() : '';
      if (currentSelected.length) question.currentSelected = currentSelected;
      if (currentValue) question.currentValue = currentValue;
      return question;
    });
    if (new Set(questions.map(question => question.key)).size !== questions.length || questions.some(question => question.key.startsWith('dom:'))) {
      return { url: location.href, questions: [], ambiguousRoot: true };
    }
    return { url: location.href, questions, ambiguousRoot: false };
  });
}

async function fillAndVerify(page, actions) {
  const roots = await selectFormRoots(page);
  if (roots.state !== 'unique') return { ok: false, reason: roots.reason || 'form_root_ambiguous' };
  const root = roots.root;
  const verified = [];
  const labelText = async input => input.evaluate(el => {
    const label = el.closest('label') || (el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null);
    return (label?.textContent || el.value || '').trim().replace(/\s+/g, ' ');
  });
  for (const action of actions) {
    const escaped = String(action.key).replace(/["\\]/g, '\\$&');
    const idEscaped = String(action.key).replace(/[^a-zA-Z0-9_-]/g, char => `\\${char.codePointAt(0).toString(16)} `);
    let candidates = root.locator(`[data-question-id="${escaped}"]:visible, [data-qa="${escaped}"]:visible`);
    if (await candidates.count() > 1) return { ok: false, key: action.key, reason: 'question_container_ambiguous' };
    let container = candidates.first();
    if (!(await container.count())) {
      const namedCandidates = root.locator(`[name="${escaped}"]:visible, #${idEscaped}:visible`);
      const namedCount = await namedCandidates.count();
      if (namedCount > 1) {
        const types = [];
        for (let i = 0; i < namedCount; i++) types.push(String(await namedCandidates.nth(i).getAttribute('type') || '').toLowerCase());
        if (!types.every(type => type === 'radio') && !types.every(type => type === 'checkbox')) return { ok: false, key: action.key, reason: 'question_container_ambiguous' };
        const semantic = namedCandidates.first().locator('xpath=ancestor::*[@data-qa="task-body" or self::fieldset or @role="radiogroup" or @role="group" or contains(concat(" ", normalize-space(@class), " "), " bloko-form-item ")][1]');
        if (!(await semantic.count())) return { ok: false, key: action.key, reason: 'question_container_ambiguous' };
        container = semantic;
      }
      const named = namedCandidates.first();
      if (namedCount === 1) {
        const namedType = String(await named.getAttribute('type') || '').toLowerCase();
        if (namedType === 'radio' || namedType === 'checkbox') {
          const semantic = named.locator('xpath=ancestor::*[self::fieldset or @role="radiogroup" or @role="group" or contains(concat(" ", normalize-space(@class), " "), " bloko-form-item ")][1]');
          container = (await semantic.count()) ? semantic : named.locator('xpath=..');
        } else {
          container = named;
        }
      }
    }
    if (!(await container.count())) return { ok: false, key: action.key, reason: 'question_container_missing' };
    const radios = container.locator('input[type="radio"]:visible');
    const checkboxes = container.locator('input[type="checkbox"]:visible');
    if (await radios.count()) {
      let matched = false;
      for (let i = 0; i < await radios.count(); i++) {
        const input = radios.nth(i);
        const text = await labelText(input);
        if (text === String(action.value).trim()) {
          await input.check();
          if (!(await input.isChecked())) return { ok: false, key: action.key, reason: 'choice_readback_mismatch' };
          matched = true;
          break;
        }
      }
      if (!matched) return { ok: false, key: action.key, reason: 'choice_option_missing' };
    } else if (await checkboxes.count()) {
      const wanted = Array.isArray(action.value) ? action.value : [action.value];
      for (let i = 0; i < await checkboxes.count(); i++) {
        const input = checkboxes.nth(i);
        const text = await labelText(input);
        await input.setChecked(wanted.includes(text));
      }
      const selected = [];
      for (let i = 0; i < await checkboxes.count(); i++) {
        const input = checkboxes.nth(i);
        if (await input.isChecked()) selected.push(await labelText(input));
      }
      if (JSON.stringify(selected.sort()) !== JSON.stringify([...wanted].sort())) return { ok: false, key: action.key, reason: 'multichoice_readback_mismatch' };
    } else {
      const containerIsControl = await container.evaluate(el => el.matches('textarea, select, input:not([type="hidden"]):not([type="submit"]):not([type="button"])'));
      const input = containerIsControl
        ? container
        : container.locator('textarea, select, input:not([type="hidden"]):not([type="submit"]):not([type="button"])').first();
      if (!(await input.count())) return { ok: false, key: action.key, reason: 'control_missing' };
      const tag = await input.evaluate(el => el.tagName);
      if (tag === 'SELECT') {
        await input.selectOption({ label: String(action.value) });
        const selectedLabel = await input.evaluate(el => el.selectedOptions[0]?.textContent?.trim() || '');
        if (selectedLabel !== String(action.value).trim()) return { ok: false, key: action.key, reason: 'value_readback_mismatch' };
      } else {
        await input.fill(String(action.value));
        if ((await input.inputValue()).trim() !== String(action.value).trim()) return { ok: false, key: action.key, reason: 'value_readback_mismatch' };
      }
    }
    verified.push(action.key);
  }
  return { ok: true, verified };
}

async function findSafeNext(page) {
  const roots = await selectFormRoots(page);
  if (roots.state !== 'unique') return null;
  const buttons = roots.root.getByRole('button', { name: /^(Далее|Продолжить|Следующий шаг|Перейти к следующему шагу)$/i });
  const eligible = [];
  for (let i = 0; i < await buttons.count(); i++) {
    const button = buttons.nth(i);
    const type = String(await button.getAttribute('type') || 'submit').toLowerCase();
    if (await button.isVisible() && !(await button.isDisabled()) && type !== 'submit' && !(await button.getAttribute('data-qa'))) eligible.push(button);
  }
  return eligible.length === 1 ? eligible[0] : null;
}

module.exports = { selectFormRoots, snapshotQuestions, fillAndVerify, findSafeNext };
