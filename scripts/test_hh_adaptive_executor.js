const test = require('node:test');
const assert = require('node:assert/strict');
const { truthFor } = require('./hh_adaptive_executor');

test('cover-letter template is rendered per vacancy from approved facts', () => {
  const truth = truthFor(
    { title: 'Product Owner', company_name: 'Acme' },
    { '$cover_letter_template': { value: 'Здравствуйте, {company}! Откликаюсь на {title}.', provenance: 'approved candidate profile' } },
    [],
  );
  assert.deepEqual(truth['label:сопроводительное письмо'], {
    value: 'Здравствуйте, Acme! Откликаюсь на Product Owner.',
    provenance: 'approved candidate profile',
  });
  assert.equal('$cover_letter_template' in truth, false);
});
