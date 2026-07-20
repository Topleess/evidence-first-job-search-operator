const test = require('node:test');
const assert = require('node:assert/strict');
const { classifyForm, classifyPage, classifyPostSubmit, validateEligibility, validateVacancyUrl, validateApplyUrl } = require('./hh_form_state');

test('known required identity and cover fields produce a ready plan', () => {
  const snapshot = {
    url: 'https://hh.kz/applicant/vacancy_response?vacancyId=101',
    questions: [
      { key: 'phone', label: 'Телефон', type: 'text', required: true, options: [] },
      { key: 'cover', label: 'Сопроводительное письмо', type: 'textarea', required: false, options: [] },
    ],
  };
  const truth = {
    phone: { value: '+79990000000', provenance: 'candidate_profile.phone' },
    cover: { value: 'Здравствуйте!', provenance: 'approved_cover:101' },
  };

  const result = classifyForm(snapshot, truth);

  assert.equal(result.state, 'form_ready');
  assert.deepEqual(result.actions.map(x => [x.key, x.value]), [
    ['phone', '+79990000000'],
    ['cover', 'Здравствуйте!'],
  ]);
  assert.match(result.fingerprint, /^[a-f0-9]{64}$/);
});

test('unknown required question blocks only the current vacancy', () => {
  const snapshot = {
    url: 'https://hh.kz/applicant/vacancy_response?vacancyId=102',
    questions: [
      { key: 'custom:salary', label: 'Ваши зарплатные ожидания?', type: 'text', required: true, options: [] },
      { key: 'cover', label: 'Сопроводительное письмо', type: 'textarea', required: false, options: [] },
    ],
  };

  const result = classifyForm(snapshot, {
    cover: { value: 'Здравствуйте!', provenance: 'approved_cover:102' },
  });

  assert.equal(result.state, 'blocked_required_answer');
  assert.deepEqual(result.blockers, [{
    key: 'custom:salary',
    label: 'Ваши зарплатные ожидания?',
    reason: 'required_truthful_answer_missing',
  }]);
});

test('choice answer must exactly match a visible option', () => {
  const snapshot = {
    questions: [{
      key: 'custom:relocation',
      label: 'Готовы к релокации?',
      type: 'choice',
      required: true,
      options: ['Да', 'Нет'],
    }],
  };

  const result = classifyForm(snapshot, {
    'custom:relocation': { value: 'Возможно', provenance: 'candidate_profile.relocation' },
  });

  assert.equal(result.state, 'blocked_invalid_answer');
  assert.equal(result.blockers[0].reason, 'invalid_choice_answer');
});

test('normalized label truth-map entry resolves randomized HH field keys', () => {
  const result = classifyForm({ questions: [{
    key: 'random-uuid', label: 'Номер телефона *', type: 'text', required: true, options: [],
  }] }, {
    'label:номер телефона *': { value: '+79990000000', provenance: 'runtime_env.HH_PHONE' },
  });
  assert.equal(result.state, 'form_ready');
  assert.equal(result.actions[0].key, 'random-uuid');
});

test('required field already filled by HH is satisfied without a truth-map action', () => {
  const result = classifyForm({ questions: [{
    key: 'resume', label: 'Резюме', type: 'select', required: true, options: ['Основное резюме'], currentValue: 'Основное резюме',
  }] }, {});
  assert.equal(result.state, 'form_ready');
  assert.deepEqual(result.actions, []);
});

test('executor requires official HH vacancy URL and fresh vacancy-bound eligibility evidence', () => {
  const now = new Date('2026-07-17T12:00:00Z');
  assert.deepEqual(validateVacancyUrl('https://evil.example/vacancy/123', '123'), { ok: false, reason: 'untrusted_vacancy_url' });
  assert.deepEqual(validateVacancyUrl('https://hh.kz/vacancy/123', '123'), { ok: true, origin: 'https://hh.kz' });
  assert.deepEqual(validateVacancyUrl('https://hh.kz:444/vacancy/123', '123'), { ok: false, reason: 'untrusted_vacancy_url' });
  assert.deepEqual(validateVacancyUrl('https://hh.kz/vacancy/abc', 'abc'), { ok: false, reason: 'untrusted_vacancy_url' });
  assert.deepEqual(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=123', '123', 'https://hh.kz'), { ok: true });
  assert.deepEqual(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=123&employerId=456&hhtmFrom=vacancy', '123', 'https://hh.kz'), { ok: true });
  assert.deepEqual(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=123&startedWithQuestion=false&hhtmFrom=vacancy', '123', 'https://hh.kz'), { ok: true });
  assert.equal(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=123&startedWithQuestion=maybe', '123', 'https://hh.kz').ok, false);
  assert.equal(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=123&redirect=https://evil.test', '123', 'https://hh.kz').ok, false);
  assert.deepEqual(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=999', '123', 'https://hh.kz'), { ok: false, reason: 'apply_vacancy_mismatch' });
  assert.deepEqual(validateApplyUrl('https://hh.ru/applicant/vacancy_response?vacancyId=123', '123', 'https://hh.kz'), { ok: false, reason: 'untrusted_apply_url' });
  assert.equal(validateApplyUrl('https://hh.kz:443/applicant/vacancy_response?vacancyId=123', '123', 'https://hh.kz').ok, false);
  assert.equal(validateApplyUrl('https://hh.kz/applicant/vacancy_response?vacancyId=123&vacancyId=999', '123', 'https://hh.kz').ok, false);
  assert.deepEqual(validateEligibility({}, '123', now), { ok: false, reason: 'live_eligibility_missing' });
  assert.deepEqual(validateEligibility({ eligibility: { eligible: true, checked_at: 'bad', evidence: 'live', evidence_vacancy_id: '123' } }, '123', now), { ok: false, reason: 'live_eligibility_timestamp_invalid' });
  assert.deepEqual(validateEligibility({ eligibility: { eligible: true, checked_at: '2026-07-15T12:00:00Z', evidence: 'live', evidence_vacancy_id: '123' } }, '123', now), { ok: false, reason: 'live_eligibility_stale' });
  assert.deepEqual(validateEligibility({ eligibility: { eligible: true, checked_at: '2026-07-17T11:00:00Z', evidence: 'live', evidence_vacancy_id: '999' } }, '123', now), { ok: false, reason: 'live_eligibility_identity_mismatch' });
  assert.deepEqual(validateEligibility({ eligibility: { eligible: true, checked_at: '2026-07-17T11:00:00Z', evidence: 'live', evidence_vacancy_id: '123' } }, '123', now), { ok: true });
});

test('unknown requiredness blocks instead of assuming optional', () => {
  const result = classifyForm({ questions: [{ key: 'task_1', label: 'Комментарий', type: 'text', required: 'unknown', options: [] }] }, {});
  assert.equal(result.state, 'blocked_ambiguous_question');
  assert.equal(result.blockers[0].reason, 'requiredness_unknown');
});

test('answer provenance is mandatory and requiredness participates in fingerprint', () => {
  const question = { key: 'task_1', label: 'Комментарий', type: 'text', required: true, options: [] };
  const missing = classifyForm({ questions: [question] }, { task_1: { value: 'Ответ' } });
  assert.equal(missing.state, 'blocked_required_answer');
  assert.equal(missing.blockers[0].reason, 'answer_provenance_missing');
  const known = classifyForm({ questions: [question] }, {});
  const unknown = classifyForm({ questions: [{ ...question, required: 'unknown' }] }, {});
  assert.notEqual(known.fingerprint, unknown.fingerprint);
});

test('post-submit classifier distinguishes marker, validation, auth and ambiguous', () => {
  assert.equal(classifyPostSubmit({ pageState: 'verified_marker' }).state, 'success_marker');
  assert.equal(classifyPostSubmit({ pageState: 'blocked_auth' }).state, 'auth_redirect');
  assert.equal(classifyPostSubmit({ validationErrors: ['Обязательное поле'] }).state, 'validation_failed');
  assert.equal(classifyPostSubmit({ pageState: 'form_step' }).state, 'ambiguous');
});

test('page classifier fail-closes auth, captcha, otp, external redirects, assessment and unchanged steps', () => {
  assert.equal(classifyPage({ url: 'https://hh.kz/account/login', text: 'Вход для соискателей', origin: 'https://hh.kz' }).state, 'blocked_auth');
  assert.equal(classifyPage({ url: 'https://hh.kz/applicant/vacancy_response', text: 'Введите код с картинки', origin: 'https://hh.kz' }).state, 'blocked_captcha');
  assert.equal(classifyPage({ url: 'https://hh.kz/applicant/vacancy_response', text: 'Введите код из СМС', hasOtpControl: true, origin: 'https://hh.kz' }).state, 'blocked_otp');
  assert.equal(classifyPage({ url: 'https://assessment.example/test', text: 'Begin', origin: 'https://hh.kz' }).state, 'blocked_external_redirect');
  assert.equal(classifyPage({ url: 'https://hh.kz/assessment/start', text: 'Пройти тестирование', origin: 'https://hh.kz' }).state, 'blocked_assessment');
  assert.equal(classifyPage({ url: 'https://hh.kz/applicant/vacancy_response', text: 'Шаг 1', fingerprint: 'a', previousFingerprint: 'a', advanced: true, origin: 'https://hh.kz' }).state, 'blocked_unchanged_state');
});
