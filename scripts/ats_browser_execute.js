#!/usr/bin/env node
'use strict';
const fs = require('fs');
const crypto = require('crypto');
const readline = require('readline');
const { chromium } = require('playwright');

const clean = value => (value || '').trim().replace(/\s+/g, ' ').slice(0, 500);
const canonicalFingerprint = fields => crypto.createHash('sha256').update(JSON.stringify(fields.map(f => ({label:f.label,type:f.type,required:f.required,options:f.options||[]})))).digest('hex');

async function fieldsFromPage(page) {
  return page.evaluate(() => {
    const clean = value => (value || '').trim().replace(/\s+/g, ' ').slice(0, 500);
    const entries = [...document.querySelectorAll('.ashby-application-form-field-entry')];
    if (entries.length) {
      const textFields=entries.map((entry,index)=>{const heading=entry.querySelector('.ashby-application-form-question-title,label');const control=entry.querySelector('textarea,select,input');const options=[...entry.querySelectorAll('button,input[type="checkbox"] + label,input[type="radio"] + label')].map(o=>clean(o.innerText||o.textContent||o.getAttribute('name'))).filter(Boolean);let type=control?.getAttribute('type')||control?.tagName.toLowerCase()||(options.length?'choice':'unknown');if(options.length)type='choice';return{id:entry.getAttribute('data-field-path')||control?.id||control?.getAttribute('name')||`ashby:${index}`,label:clean(heading?.innerText||heading?.textContent),type,required:Boolean(heading?.className?.includes('_required_')||control?.required||control?.getAttribute('aria-required')==='true'),options:[...new Set(options)]}}).filter(f=>f.label);
      const groups=[...new Set([...document.querySelectorAll('input[type="radio"],input[type="checkbox"]')].map(input=>input.closest('[data-field-path]')).filter(Boolean))].filter(group=>!textFields.some(field=>field.id===group.getAttribute('data-field-path'))).map(group=>{const controls=[...group.querySelectorAll('input[type="radio"],input[type="checkbox"]')];const options=controls.map(input=>clean(document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText)).filter(Boolean);const full=clean(group.innerText);const label=clean(full.split(options[0]||'\n')[0]);return{id:group.getAttribute('data-field-path'),label,type:controls[0]?.type==='checkbox'?'multichoice':'choice',required:/required/i.test(group.innerHTML),options}}).filter(f=>f.id&&f.label);
      return [...textFields,...groups];
    }
    return [...document.querySelectorAll('input,textarea,select')].map((el,index) => {
      const id=el.id||el.getAttribute('name')||`dom:${index}`; let label='';
      if(el.id) label=document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText||'';
      label ||= el.closest('label')?.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
      return {id,label:clean(label),type:el.getAttribute('type')||el.tagName.toLowerCase(),required:el.required||el.getAttribute('aria-required')==='true',options:[]};
    }).filter(f=>f.label);
  });
}

async function fillAction(page, action) {
  let box = page.locator(`[data-field-path="${action.field_id.replace(/"/g,'\\"')}"]`).first();
  if (!await box.count()) box = page.locator(`[id="${action.field_id.replace(/"/g,'\\"')}"] ,[name="${action.field_id.replace(/"/g,'\\"')}"]`).first();
  if (!await box.count()) throw new Error(`field_not_found:${action.field_id}`);
  const file = box.locator('input[type=file]').first();
  if (action.kind === 'resume' || await file.count()) {
    const target = await file.count() ? file : box.locator('input[type=file]').first();
    if (!await target.count()) throw new Error(`file_control_not_found:${action.field_id}`);
    await target.setInputFiles(action.value); return;
  }
  const control = box.locator('textarea:visible,input:visible,select:visible').first();
  if (!await control.count()) throw new Error(`visible_control_not_found:${action.field_id}`);
  const tag = await control.evaluate(el => el.tagName.toLowerCase());
  if (tag === 'select') await control.selectOption({label:String(action.value)}).catch(async()=>control.selectOption(String(action.value)));
  else {
    await control.fill(String(action.value));
    if (action.kind === 'location') {
      await page.waitForTimeout(700);
      const option = page.getByRole('option').first();
      if (await option.count()) await option.click();
      else await control.press('ArrowDown').then(()=>control.press('Enter')).catch(()=>{});
    }
  }
}

async function main(){
  const payloadPath=process.argv[2], evidenceDir=process.argv[3];
  if(!payloadPath||!evidenceDir) throw new Error('usage: ats_browser_execute.js PAYLOAD_JSON EVIDENCE_DIR');
  const payload=JSON.parse(fs.readFileSync(payloadPath,'utf8')); fs.mkdirSync(evidenceDir,{recursive:true});
  const proxy=process.env.ATS_BROWSER_PROXY ? {server:process.env.ATS_BROWSER_PROXY.replace(/^socks5h:/,'socks5:')} : undefined;
  const browser=await chromium.launch({headless:true,...(proxy?{proxy}:{})}); let submitClicked=false;
  try{
    const page=await browser.newPage(); await page.goto(payload.job_url,{waitUntil:'networkidle',timeout:60000});
    await page.locator('input:visible,textarea:visible,select:visible').first().waitFor({state:'visible',timeout:20000});
    const fields=await fieldsFromPage(page); const fp=canonicalFingerprint(fields);
    if(fp!==payload.form_fingerprint) throw new Error(`fingerprint_changed:${fp}`);
    const captcha=page.locator('iframe[src*="captcha" i],iframe[title*="captcha" i],[class*="captcha" i]');
    if(await captcha.count()) throw new Error('captcha_present');
    for(const action of payload.planned_fields) await fillAction(page,action);
    await page.waitForTimeout(500);
    const missing=await page.evaluate(()=>[...document.querySelectorAll('input[required],textarea[required],select[required]')].filter(el=>!el.disabled&&!el.value).map(el=>el.id||el.name||el.type));
    if(missing.length) throw new Error(`required_missing:${missing.join(',')}`);
    await page.screenshot({path:`${evidenceDir}/pre_submit.png`,fullPage:true});
    process.stdout.write(JSON.stringify({phase:'ready',fingerprint:fp,url:page.url(),fields:fields.length})+'\n');
    const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity}); const command=await new Promise(resolve=>rl.once('line',resolve)); rl.close();
    if(!/^GO [a-f0-9]{32}$/.test(command||'')) throw new Error('execution_token_not_received');
    const submit=page.getByRole('button',{name:/submit application|submit|send application|apply/i}).last();
    if(!await submit.count()||!await submit.isEnabled()) throw new Error('submit_not_available');
    submitClicked=true; await submit.click();
    await Promise.race([page.waitForURL(u=>/thank|confirm|submitted|application/i.test(u.toString()),{timeout:30000}),page.getByText(/application (has been )?submitted|thank you for applying|thanks for applying|application received/i).first().waitFor({state:'visible',timeout:30000})]).catch(()=>{});
    await page.waitForTimeout(1500); const body=clean(await page.locator('body').innerText());
    const confirmed=/application (has been )?submitted|thank you for applying|thanks for applying|application received/i.test(body);
    await page.screenshot({path:`${evidenceDir}/readback.png`,fullPage:true});
    const result={phase:'result',submit_clicked:true,confirmed,url:page.url(),title:await page.title(),body_excerpt:body.slice(0,500),at:new Date().toISOString()};
    fs.writeFileSync(`${evidenceDir}/result.json`,JSON.stringify(result,null,2)); process.stdout.write(JSON.stringify(result)+'\n');
    if(!confirmed) process.exitCode=3;
  }catch(error){
    const result={phase:'error',submit_clicked:submitClicked,error:String(error.message||error),at:new Date().toISOString()};
    fs.writeFileSync(`${evidenceDir}/result.json`,JSON.stringify(result,null,2)); process.stdout.write(JSON.stringify(result)+'\n'); process.exitCode=submitClicked?3:2;
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e.stack||String(e));process.exit(2)});
