const crypto = require('node:crypto');

function normalizedQuestion(q) {
  return {
    key: String(q.key || ''),
    label: String(q.label || '').trim().toLowerCase().replace(/\s+/g, ' '),
    type: String(q.type || 'text'),
    required: q.required === 'unknown' ? 'unknown' : Boolean(q.required),
    options: [...(q.options || [])].map(x => String(x).trim()).sort(),
    currentValue: String(q.currentValue || '').trim(),
    currentSelected: [...(q.currentSelected || [])].map(x => String(x).trim()).sort(),
  };
}

function classifyForm(snapshot, truth) {
  const semantic = (snapshot.questions || []).map(normalizedQuestion);
  const fingerprint = crypto.createHash('sha256').update(JSON.stringify(semantic)).digest('hex');
  const actions = [];
  const blockers = [];
  for (const question of snapshot.questions || []) {
    if (String(question.currentValue ?? '').trim() || (question.currentSelected || []).length) continue;
    const labelKey = `label:${normalizedQuestion(question).label}`;
    const answer = truth[question.key] || truth[labelKey];
    if (answer && String(answer.value ?? '').trim() && String(answer.provenance ?? '').trim()) {
      const options = (question.options || []).map(value => String(value).trim());
      if (question.type === 'choice' && !options.includes(String(answer.value).trim())) {
        blockers.push({
          key: question.key,
          label: question.label,
          reason: 'invalid_choice_answer',
        });
      } else {
        actions.push({ key: question.key, value: answer.value, provenance: answer.provenance });
      }
    } else if (answer && String(answer.value ?? '').trim()) {
      blockers.push({
        key: question.key,
        label: question.label,
        reason: 'answer_provenance_missing',
      });
    } else if (question.required === 'unknown') {
      blockers.push({ key: question.key, label: question.label, reason: 'requiredness_unknown' });
    } else if (question.required) {
      blockers.push({
        key: question.key,
        label: question.label,
        reason: 'required_truthful_answer_missing',
      });
    }
  }
  const invalid = blockers.some(x => x.reason === 'invalid_choice_answer');
  const ambiguous = blockers.some(x => x.reason === 'requiredness_unknown');
  return {
    state: ambiguous ? 'blocked_ambiguous_question' : invalid ? 'blocked_invalid_answer' : blockers.length ? 'blocked_required_answer' : 'form_ready',
    fingerprint,
    actions,
    blockers,
  };
}

function classifyPage(page) {
  const url = String(page.url || '');
  const text = String(page.text || '');
  if (page.origin) {
    try { if (new URL(url).origin !== new URL(page.origin).origin) return { state: 'blocked_external_redirect' }; } catch (_) { return { state: 'blocked_external_redirect' }; }
  }
  if (/\/account\/(?:login|signup)/i.test(url) || /Вход для соискателей|Введите телефон/i.test(text)) return { state: 'blocked_auth' };
  if (page.hasOtpControl || /код (?:из|в) (?:смс|sms|письм)|одноразов(?:ый|ого) код/i.test(text)) return { state: 'blocked_otp' };
  if (page.hasCaptchaControl || /captcha/i.test(url) || /код с картинки|проверочный код с изображения|подтвердите, что вы не робот/i.test(text)) return { state: 'blocked_captcha' };
  if (/assessment|test/i.test(url) || /Пройти тестирование|выполнить тестовое/i.test(text)) return { state: 'blocked_assessment' };
  if (page.advanced && page.fingerprint && page.fingerprint === page.previousFingerprint) return { state: 'blocked_unchanged_state' };
  if (/Вы\s*откликнулись|Отклик отправлен|Резюме доставлено/i.test(text)) return { state: 'verified_marker' };
  return { state: 'form_step' };
}

function classifyPostSubmit(result) {
  if (result.pageState === 'verified_marker') return { state: 'success_marker' };
  if (result.pageState === 'blocked_auth') return { state: 'auth_redirect' };
  if ((result.validationErrors || []).length) return { state: 'validation_failed', errors: result.validationErrors };
  return { state: 'ambiguous' };
}

function validateVacancyUrl(rawUrl, vacancyId) {
  try {
    const exactRaw = String(rawUrl);
    if (exactRaw !== exactRaw.trim() || /^https:\/\/[^/]+:443(?:\/|$)/i.test(exactRaw)) return { ok: false, reason: 'untrusted_vacancy_url' };
    const url = new URL(exactRaw);
    const id = String(vacancyId);
    if (exactRaw !== `${url.origin}/vacancy/${id}`) return { ok: false, reason: 'untrusted_vacancy_url' };
    if (!/^\d+$/.test(id) || url.username || url.password || url.search || url.hash || url.protocol !== 'https:' || url.port || !/^hh\.(ru|kz)$/.test(url.hostname) || url.pathname !== `/vacancy/${id}`) {
      return { ok: false, reason: 'untrusted_vacancy_url' };
    }
    return { ok: true, origin: url.origin };
  } catch (_) {
    return { ok: false, reason: 'untrusted_vacancy_url' };
  }
}

function validateEligibility(job, vacancyId, now = new Date()) {
  const gate = job && job.eligibility;
  if (!gate || !gate.checked_at || !String(gate.evidence || '').trim() || !gate.evidence_vacancy_id) return { ok: false, reason: 'live_eligibility_missing' };
  if (gate.eligible !== true) return { ok: false, reason: 'live_eligibility_rejected' };
  if (String(gate.evidence_vacancy_id) !== String(vacancyId)) return { ok: false, reason: 'live_eligibility_identity_mismatch' };
  const checked = new Date(gate.checked_at);
  if (!Number.isFinite(checked.getTime())) return { ok: false, reason: 'live_eligibility_timestamp_invalid' };
  const age = now.getTime() - checked.getTime();
  if (age < -300000 || age > 86400000) return { ok: false, reason: 'live_eligibility_stale' };
  return { ok: true };
}

function validateApplyUrl(rawUrl, vacancyId, expectedOrigin) {
  try {
    const exactRaw = String(rawUrl);
    if (exactRaw !== exactRaw.trim() || /^https:\/\/[^/]+:443(?:\/|$)/i.test(exactRaw)) return { ok: false, reason: 'untrusted_apply_url' };
    const url = new URL(exactRaw);
    const id = String(vacancyId);
    if (!/^\d+$/.test(id) || url.username || url.password || url.hash || url.origin !== expectedOrigin || url.port || url.pathname !== '/applicant/vacancy_response') return { ok: false, reason: 'untrusted_apply_url' };
    const keys = [...url.searchParams.keys()];
    const allowed = new Set(['vacancyId', 'employerId', 'hhtmFrom', 'startedWithQuestion']);
    const vacancyIds = url.searchParams.getAll('vacancyId');
    const employerIds = url.searchParams.getAll('employerId');
    const sources = url.searchParams.getAll('hhtmFrom');
    const started = url.searchParams.getAll('startedWithQuestion');
    if (keys.some(key => !allowed.has(key)) || vacancyIds.length !== 1 || vacancyIds[0] !== id) return { ok: false, reason: 'apply_vacancy_mismatch' };
    if (employerIds.length > 1 || (employerIds.length === 1 && !/^\d+$/.test(employerIds[0]))) return { ok: false, reason: 'untrusted_apply_url' };
    if (sources.length > 1 || (sources.length === 1 && sources[0] !== 'vacancy')) return { ok: false, reason: 'untrusted_apply_url' };
    if (started.length > 1 || (started.length === 1 && !/^(?:true|false)$/.test(started[0]))) return { ok: false, reason: 'untrusted_apply_url' };
    return { ok: true };
  } catch (_) { return { ok: false, reason: 'untrusted_apply_url' }; }
}

module.exports = { classifyForm, classifyPage, classifyPostSubmit, validateEligibility, validateVacancyUrl, validateApplyUrl };
