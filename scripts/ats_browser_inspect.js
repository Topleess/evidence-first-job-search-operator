#!/usr/bin/env node
'use strict';
const crypto = require('crypto');
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2];
  if (!url || !/^https:\/\//.test(url)) throw new Error('HTTPS job URL required');
  const proxy=process.env.ATS_BROWSER_PROXY ? {server:process.env.ATS_BROWSER_PROXY.replace(/^socks5h:/,'socks5:')} : undefined;
  const browser = await chromium.launch({headless: true, ...(proxy ? {proxy} : {})});
  try {
    const page = await browser.newPage();
    await page.goto(url, {waitUntil: 'networkidle', timeout: 60000});
    const apply = page.getByRole('link', {name: /Apply for this Job|Apply now|Apply/i}).first();
    if (await apply.count()) {
      await apply.click();
    }
    await page.locator('input:visible,textarea:visible,select:visible').first().waitFor({state: 'visible', timeout: 30000});
    await page.waitForTimeout(1000);
    const fields = await page.evaluate(() => {
      const clean = value => (value || '').trim().replace(/\s+/g, ' ').slice(0, 500);
      const entries = [...document.querySelectorAll('.ashby-application-form-field-entry')];
      if (entries.length) {
        const textFields = entries.map((entry, index) => {
          const heading = entry.querySelector('.ashby-application-form-question-title, label');
          const control = entry.querySelector('textarea,select,input');
          const options = [...entry.querySelectorAll('button, input[type="checkbox"] + label, input[type="radio"] + label')]
            .map(option => clean(option.innerText || option.textContent || option.getAttribute('name'))).filter(Boolean);
          let type = control?.getAttribute('type') || control?.tagName.toLowerCase() || (options.length ? 'choice' : 'unknown');
          if (options.length) type = 'choice';
          return {id:entry.getAttribute('data-field-path') || control?.id || control?.getAttribute('name') || `ashby:${index}`,label:clean(heading?.innerText || heading?.textContent),type,required:Boolean(heading?.className?.includes('_required_') || control?.required || control?.getAttribute('aria-required') === 'true'),options:[...new Set(options)]};
        }).filter(field => field.label);
        const groups = [...new Set([...document.querySelectorAll('input[type="radio"],input[type="checkbox"]')].map(input => input.closest('[data-field-path]')).filter(Boolean))]
          .filter(group => !textFields.some(field => field.id === group.getAttribute('data-field-path')))
          .map(group => {
            const controls=[...group.querySelectorAll('input[type="radio"],input[type="checkbox"]')];
            const options=controls.map(input => clean(document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText)).filter(Boolean);
            const full=clean(group.innerText); const label=clean(full.split(options[0] || '\n')[0]);
            return {id:group.getAttribute('data-field-path'),label,type:controls[0]?.type === 'checkbox' ? 'multichoice' : 'choice',required:/required/i.test(group.innerHTML),options};
          }).filter(field => field.id && field.label);
        return [...textFields, ...groups];
      }
      return [...document.querySelectorAll('input,textarea,select')].map((element, index) => {
        const id = element.id || element.getAttribute('name') || `dom:${index}`;
        let label = '';
        if (element.id) label = document.querySelector(`label[for="${CSS.escape(element.id)}"]`)?.innerText || '';
        label ||= element.closest('label')?.innerText || element.getAttribute('aria-label') || element.getAttribute('placeholder') || '';
        return {id, label: clean(label), type: element.getAttribute('type') || element.tagName.toLowerCase(), required: element.required || element.getAttribute('aria-required') === 'true', options: []};
      }).filter(field => field.label);
    });
    if (!fields.length) throw new Error('No labeled application fields observed; refusing empty snapshot');
    const canonical = JSON.stringify(fields.map(f => ({label: f.label, type: f.type, required: f.required, options: f.options || []})));
    console.log(JSON.stringify({schema_version: 'ats_form_snapshot.v1', source_url: page.url(), title: await page.title(), captured_at: new Date().toISOString(), form_fingerprint: crypto.createHash('sha256').update(canonical).digest('hex'), fields}, null, 2));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error.stack || String(error)); process.exit(1); });
