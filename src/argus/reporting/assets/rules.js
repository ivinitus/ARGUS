(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('rules-config').textContent);
  const M = window.ArgusRules, $ = id => document.getElementById(id);
  const base = '/teams/' + encodeURIComponent(config.team) + '/rules';
  let state = null, draft = null, preview = null, busy = false, formState = null, viewedVersion = null;
  let ruleSettings = null;
  let activeTab = 'rules', pollTimer = null, searchTimer = null, pickerSerial = 0, reportsLoaded = false;
  const expanded = new Set(), pendingRequests = new Map();
  let picker = {folder:'', revision:'', items:[], selected:new Set(), offset:0, next:null, total:0, loading:false};
  const formFields = {name:'custom-name',enabled:'custom-enabled',applies_when:'custom-applies',requirement:'custom-requirement',expected_evidence:'custom-evidence',examples:'custom-examples',mode:'custom-mode',severity:'custom-severity'};
  const el = (tag, text, cls) => {const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node;};
  function button(text, fn, cls='rules-link') {const node=el('button',text,cls);node.type='button';node.addEventListener('click',fn);return node;}
  function option(value,text) {const node=el('option',text);node.value=value;return node;}
  function formatDate(value) {if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString();}
  function actor(value) {return value==='packaged-defaults'?'Initial configuration':config.demo?'Local user':value;}
  function dirty() {return !!state && !M.equal(draft,(state.draft||state.active).document);}
  function authoringPending(){return !!formState&&!!config.builder&&$('authoring-text').value.trim()!==(formState.acceptedSource||'');}
  function formDirty() {return ruleSettings?!M.equal(readSettings(),ruleSettings.original):!!formState&&(authoringPending()||!M.equal(readForm(),formState.original));}
  function workingDocument() {
    if(ruleSettings)return settingsDocument();
    if(!formState||M.equal(readForm(),formState.original))return draft;
    const working=M.clone(draft),rule=readForm();
    if(formState.index<0)working.custom_rules.push(rule);else working.custom_rules[formState.index]=rule;
    return working;
  }
  function editable() {return state && state.can_edit && !busy;}
  function matchesPreview(candidate=preview) {return candidate && state && state.draft && candidate.draft_key===state.draft.id && candidate.draft_sha256===state.draft.sha256 && candidate.active_revision===state.active.revision;}
  function notice(text,error=false) {$('notice').textContent=text;$('notice').className='rules-alert '+(error?'error':'success');$('notice').hidden=false;}
  function storageGet() {try{return sessionStorage.getItem('rules-preview-'+config.team);}catch(_){return null;}}
  function storageSet(id) {try{if(id)sessionStorage.setItem('rules-preview-'+config.team,id);else sessionStorage.removeItem('rules-preview-'+config.team);}catch(_){/* Session storage is optional. */}}
  async function request(path,body) {
    const options={credentials:'include',headers:{}};let signature;
    if(body!==undefined){signature=path+JSON.stringify(body);if(!pendingRequests.has(signature))pendingRequests.set(signature,crypto.randomUUID().replaceAll('-',''));
      options.method='POST';options.headers={'Content-Type':'application/json','X-Argus-Request':'1','Idempotency-Key':pendingRequests.get(signature)};options.body=JSON.stringify(body);}
    const response=await fetch(config.api+path,options);
    let data;try{data=await response.json();}catch(_){throw new Error('Could not connect to the rules service. Sign in or reconnect.');}
    if(!response.ok){if(response.status<500&&signature)pendingRequests.delete(signature);if(response.status===401)$('signin').hidden=false;throw new Error(data.error||'The request failed. Refresh and try again.');}
    if(signature)pendingRequests.delete(signature);return data;
  }
  async function action(fn) {if(busy)return;busy=true;update();try{await fn();}catch(error){notice(error.message,true);}finally{busy=false;update();}}
  function update() {
    const unsaved=dirty(), formUnsaved=formDirty(), changes=state?M.changes(state.active.document,workingDocument(),state.catalog,config.builder):[];
    $('editor').inert=busy;$('refresh').disabled=busy;
    $('save-draft').disabled=!editable()||!unsaved||!!formState||!!ruleSettings||!!viewedVersion;
    $('save-draft').hidden=!!formState||!!ruleSettings||!!viewedVersion;
    $('test-saved-draft').hidden=!config.settingsOnly||!state?.draft||!!formState||!!ruleSettings||!!viewedVersion||activeTab==='test'||!state.can_edit;
    $('test-saved-draft').disabled=busy||unsaved;
    $('save-rule-settings').disabled=!editable()||!ruleSettings||!formUnsaved&&!unsaved;
    $('add-rule').disabled=!editable()||draft.custom_rules.length>=50;
    $('draft-status').textContent=unsaved||formUnsaved?'Unsaved changes':state&&state.draft?'Draft saved':'No draft';
    $('draft-status').className='rules-status'+(unsaved||formUnsaved?' dirty':state&&state.draft?' saved':'');
    $('change-count').textContent=changes.length;$('view-changes').disabled=!state||busy||!changes.length;
    $('selection-count').textContent=picker.selected.size+' of 10 selected';
    $('run-preview').disabled=!editable()||unsaved||!state.draft||picker.loading||!picker.revision||!picker.selected.size||!!(preview&&['pending','running'].includes(preview.phase));
    $('publish').disabled=!editable()||unsaved||!matchesPreview()||preview.phase!=='complete'||!$('publish-note').value.trim();
    $('publish-requirement').textContent=matchesPreview()&&preview.phase==='complete'&&!unsaved?'Tests complete for this draft.':'A completed test of this draft is required.';
    $('test-requirement').textContent=unsaved?'Save your changes before testing.':state&&!state.draft?'Save a draft before testing.':!picker.selected.size?'Select 1–10 saved tests.':'Current rules and draft will use the same saved evidence.';
    $('save-custom').disabled=!editable()||authoringPending();$('remove-custom').disabled=!editable();$('use-example').disabled=!editable();
    $('builder-try').disabled=$('builder-example').disabled=!editable();
    $('builder-try').disabled=!editable()||authoringPending();
    $('inspect-rule').disabled=!editable()||authoringPending();
    $('authoring-review').disabled=!editable()||!$('authoring-text').value.trim();
    $('authoring-pending').hidden=!authoringPending();
    $('builder-add-condition').disabled=!editable()||$('builder-conditions').querySelectorAll('.rules-condition-row').length>=5;
    $('reload-tests').disabled=!editable()||!picker.folder||picker.loading;
    const summary=$('publish-summary');summary.replaceChildren();
    summary.append(el('strong',changes.length+' rule-setting change'+(changes.length===1?'':'s')));
    if(changes.length)summary.append(document.createTextNode(' · '),button('Review changes',showChanges));
  }
  function changed() {preview=null;clearTimeout(pollTimer);renderPreview();update();}
  function renderDiff(root,rows,beforeLabel='Published',afterLabel='Draft') {
    root.replaceChildren();if(!rows.length){root.append(el('p','No differences.','rules-muted'));return;}
    for(const row of rows){const line=el('div',undefined,'rules-diff-row');line.append(el('div',row.label));
      for(const [label,value] of [[beforeLabel,row.before],[afterLabel,row.after]]){const cell=el('div');cell.append(el('small',label));
        if(value.length>500){const details=el('details');details.append(el('summary','View text'),el('p',value));cell.append(details);}else cell.append(el('span',value));line.append(cell);}root.append(line);}
  }
  function showChanges(){renderDiff($('changes-content'),M.changes(state.active.document,workingDocument(),state.catalog,config.builder));$('changes-dialog').showModal();}
  function chooseTab(name,focus=false) {
    if(busy||!state)return;
    if(formDirty()&&!confirm('Discard the changes in this rule form?'))return;
    formState=null;ruleSettings=null;viewedVersion=null;activeTab=name;
    if(!state.can_edit&&['test','history'].includes(name))activeTab='rules';
    history.replaceState(null,'','#'+activeTab);renderView(focus);
    if(activeTab==='history')loadHistory().catch(error=>notice(error.message,true));
    if(activeTab==='test'&&!reportsLoaded)loadReports().catch(error=>notice(error.message,true));
  }
  function renderView(focus=false) {
    $('rule-form-panel').hidden=!formState;$('rule-settings-panel').hidden=!ruleSettings;$('version-panel').hidden=!viewedVersion;$('tabs').hidden=!!formState||!!ruleSettings||!!viewedVersion;
    document.querySelectorAll('[role=tabpanel]').forEach(node=>{node.hidden=!!formState||!!ruleSettings||!!viewedVersion||node.id!=='tab-'+activeTab;});
    document.querySelectorAll('[data-tab]').forEach(node=>{const selected=node.dataset.tab===activeTab;node.setAttribute('aria-selected',String(selected));node.tabIndex=selected?0:-1;});
    const title=ruleSettings?'Rule settings':formState?(formState.index<0?'Create rule':(state.can_edit?'Edit rule':'Rule details')):viewedVersion?'Version '+viewedVersion.revision:'Team rules';
    $('page-title').textContent=title;$('page-description').hidden=!!formState||!!ruleSettings||!!viewedVersion;
    $('detail-crumb').hidden=!formState&&!ruleSettings&&!viewedVersion;$('detail-crumb').textContent='› '+(ruleSettings?ruleSettings.rule.name:title);
    if(focus)$('page-title').focus();update();
  }
  function renderRules() {
    if(!state)return;
    const root=$('rules-body');root.replaceChildren();
    const search=$('rule-search').value.trim().toLowerCase(),type=$('rule-type').value,status=$('rule-state').value,onlyChanged=$('changed-only').checked;
    let shown=0;
    const matches=(rule,kind,isOn,modified)=>!((type&&type!==kind&&!(type==='custom'&&kind!=='builtin'))||(status&&(status==='enabled')!==isOn)||(onlyChanged&&!modified))&&(!search||(rule.id+' '+rule.name+' '+(rule.description||M.ruleText(rule,config.builder)||'')).toLowerCase().includes(search));
    function builtinRow(rule,children=[]) {
      const modified=M.ruleChanged(state.active.document.policy,draft.policy,rule),on=M.enabled(draft.policy,rule);
      const row=el('tr',undefined,rule.parent?'rules-child':'');row.dataset.ruleId=rule.id;
      const name=el('td'),wrap=el('div',undefined,'rules-rule-name'),text=el('div');
      if(children.length){const expand=button(expanded.has(rule.id)||search?'▾':'▸',()=>{expanded.has(rule.id)?expanded.delete(rule.id):expanded.add(rule.id);renderRules();},'rules-expand');expand.setAttribute('aria-label','Related checks for '+rule.name);expand.setAttribute('aria-expanded',String(expanded.has(rule.id)||!!search));wrap.append(expand);}else wrap.append(el('span',undefined,'rules-expand-spacer'));
      const heading=el('div');heading.append(el('span',rule.id,'rules-code'),config.settingsOnly?button(rule.name,()=>openSettings(rule)):el('strong',rule.name));text.append(heading,el('small',rule.description));wrap.append(text);name.append(wrap);
      const kind=el('td');kind.append(el('span','Built-in','rules-badge'));
      const enabledCell=el('td'),enabledControl=el('div',undefined,'rules-switch-control'),toggle=el('input',undefined,'rules-switch');toggle.type='checkbox';toggle.checked=on;toggle.disabled=!state.can_edit||rule.locked;toggle.setAttribute('aria-label','Enable '+rule.id);toggle.setAttribute('role',children.length?'checkbox':'switch');
      if(children.length){const flags=[on,...children.map(child=>M.enabled(draft.policy,child))];toggle.checked=flags.every(Boolean);toggle.indeterminate=flags.some(Boolean)&&!flags.every(Boolean);}
      toggle.addEventListener('change',()=>{M.toggle(draft.policy,state.catalog,rule,toggle.checked);changed();renderRules();renderPolicies();});
      const statusText=el('span',toggle.indeterminate?'Some':toggle.checked?'On':'Off','rules-switch-state');statusText.setAttribute('aria-hidden','true');enabledControl.append(toggle,statusText);enabledCell.append(enabledControl);
      const sev=el('td',undefined,'rules-severity');
      if(rule.locked){sev.append(el('span','High · Protected','rules-lock'));}
      else{const select=el('select');select.append(option('','Use rule default'),option('high','High'),option('medium','Medium'),option('low','Low'));select.value=draft.policy.rule_severities[rule.id]||'';select.disabled=!state.can_edit;select.setAttribute('aria-label','Severity for '+rule.id);
        select.addEventListener('change',()=>{if(select.value)draft.policy.rule_severities[rule.id]=select.value;else delete draft.policy.rule_severities[rule.id];changed();renderRules();});
        const effective=M.severity(draft.policy,rule),detail=el('details');detail.append(el('summary',effective.label+' · '+effective.source),el('p',effective.explanation));sev.append(select,detail);}
      const change=el('td');if(modified)change.append(el('span','Changed','rules-badge changed'));else change.textContent='—';
      row.append(name,kind,enabledCell,sev,change);root.append(row);shown++;
    }
    if(type!=='custom')for(const rule of state.catalog.filter(item=>!item.parent)){
      const children=state.catalog.filter(child=>child.parent===rule.id);
      const visibleChildren=children.filter(child=>matches(child,'builtin',M.enabled(draft.policy,child),M.ruleChanged(state.active.document.policy,draft.policy,child)));
      if(matches(rule,'builtin',M.enabled(draft.policy,rule),M.ruleChanged(state.active.document.policy,draft.policy,rule))||visibleChildren.length){builtinRow(rule,children);if(expanded.has(rule.id)||search||onlyChanged||status)visibleChildren.forEach(child=>builtinRow(child));}
    }
    draft.custom_rules.forEach((rule,index)=>{
      const original=state.active.document.custom_rules.find(item=>item.id===rule.id),modified=!M.equal(original||null,rule);
      if(!matches(rule,rule.kind==='structured'?'structured':'evidence',rule.enabled,modified))return;
      const row=el('tr'),name=el('td'),wrap=el('div',undefined,'rules-rule-name'),text=el('div');text.append(button(rule.name,()=>openRule(index)),el('small',rule.kind==='structured'?M.ruleText(rule,config.builder):rule.applies_when));wrap.append(el('span',undefined,'rules-expand-spacer'),text);name.append(wrap);
      const kind=el('td');kind.append(el('span',rule.kind==='structured'?'Python check':'LLM check','rules-badge'));const on=el('td'),control=el('div',undefined,'rules-switch-control'),toggle=el('input',undefined,'rules-switch');toggle.type='checkbox';toggle.setAttribute('role','switch');toggle.checked=rule.enabled;toggle.disabled=!state.can_edit;toggle.setAttribute('aria-label','Enable '+rule.name);toggle.addEventListener('change',()=>{rule.enabled=toggle.checked;changed();renderRules();});const statusText=el('span',rule.enabled?'On':'Off','rules-switch-state');statusText.setAttribute('aria-hidden','true');control.append(toggle,statusText);on.append(control);
      const sev=el('td',undefined,'rules-severity');sev.append(el('strong',M.cap(rule.severity)),el('small',rule.mode==='advisory'||rule.severity==='low'?'Advisory only':'Affects verdict'));const change=el('td');change.append(el('span',modified?(original?'Changed':'Added'):'—',modified?'rules-badge changed':''));row.append(name,kind,on,sev,change);root.append(row);shown++;
    });
    $('rules-empty').hidden=shown>0;$('rule-count').textContent='('+ (state.catalog.length+draft.custom_rules.length) +')';$('visible-count').textContent=shown+' rows';
  }
  function fieldError(input,message) {
    const id=input.id+'-error';let error=$(id);if(!error){error=el('p',undefined,'rules-field-error');error.id=id;input.after(error);}
    const descriptions=(input.getAttribute('aria-describedby')||'').split(' ').filter(value=>value&&value!==id);if(message)descriptions.push(id);
    input.setCustomValidity(message);input.setAttribute('aria-invalid',String(!!message));input.setAttribute('aria-describedby',descriptions.join(' '));error.textContent=message;error.hidden=!message;
  }
  function readSettings(){
    const result={severity:$('settings-severity').value};
    if(ruleSettings.rule.id==='R22')result.account=$('settings-account').value;
    else result.enabled=$('settings-state').value;
    if(ruleSettings.index>=0)result.mode=$('settings-impact').value;
    return result;
  }
  function settingsDocument(){
    const working=M.clone(draft),values=readSettings(),{rule,index,original}=ruleSettings;
    if(index>=0){
      Object.assign(working.custom_rules[index],{enabled:values.enabled==='true',severity:values.severity,mode:values.mode});
    }else{
      if(rule.id==='R22'){
        if(values.account!==original.account){M.toggle(working.policy,state.catalog,rule,values.account!=='disabled');working.policy.r22_mode=values.account;}
      }else if(values.enabled!==original.enabled&&values.enabled!=='mixed')M.toggle(working.policy,state.catalog,rule,values.enabled==='true');
      if(!rule.locked){if(values.severity)working.policy.rule_severities[rule.id]=values.severity;else delete working.policy.rule_severities[rule.id];}
    }
    return working;
  }
  function updateSettings(){
    const {rule,index}=ruleSettings,document=settingsDocument();
    if(index<0){const result=M.severity(document.policy,rule);$('settings-severity-help').textContent=result.label+' · '+result.source+'. '+result.explanation;}
    else $('settings-severity-help').textContent=$('settings-impact').value==='advisory'||$('settings-severity').value==='low'?'Findings leave the verdict unchanged.':$('settings-severity').value==='high'?'A violation produces a deviation.':'A violation produces a concern.';
    $('settings-account-help').textContent=({disabled:'Skip the test-account check.',advisory:'Report missing account details without changing the verdict.',enforced:'Missing account details can affect the verdict.'})[$('settings-account').value];
    if(rule.id==='R22'&&$('settings-account').value==='enforced'&&M.severity(document.policy,rule).label==='Low')$('settings-account-help').textContent='Missing account details are reported. Low severity leaves the verdict unchanged.';
    update();
  }
  function policyLocation(key){
    // Validate before rebuilding policy controls so incomplete map entries are retained.
    if(!validatePolicies())return;
    draft=workingDocument();ruleSettings=null;activeTab='policies';history.replaceState(null,'','#policies');
    renderRules();renderPolicies();renderView();
    const target=document.querySelector('[data-policy-key="'+key+'"]');
    if(!target)return;
    for(let parent=target.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    document.querySelectorAll('.rules-setting-selected').forEach(node=>node.classList.remove('rules-setting-selected'));
    target.classList.add('rules-setting-selected');target.scrollIntoView({block:'center'});
    const control=target.querySelector('input:not(:disabled),select:not(:disabled),button:not(:disabled)');
    (control||target).focus({preventScroll:true});
  }
  function openSettings(rule,index=-1){
    if(busy)return;
    formState=null;viewedVersion=null;ruleSettings={rule,index,original:null};
    const builtin=index<0,locked=!!rule.locked,account=builtin&&rule.id==='R22';
    $('settings-rule-name').textContent=(builtin?rule.id+' · ':'')+rule.name;
    $('settings-rule-kind').textContent=locked?'Protected check':builtin?'Built-in':'Saved custom rule';
    $('settings-rule-description').textContent=rule.description||(rule.kind==='structured'?M.ruleText(rule,config.builder):rule.applies_when);
    $('settings-protected').hidden=!locked;
    $('settings-state-field').hidden=account;$('settings-account-field').hidden=!account;$('settings-impact-field').hidden=builtin;
    const flags=builtin?[rule,...state.catalog.filter(item=>item.parent===rule.id)].map(item=>M.enabled(draft.policy,item)):[rule.enabled];
    const mixed=flags.some(Boolean)&&!flags.every(Boolean);
    $('settings-state').replaceChildren(option('true','Enabled'),option('false','Disabled'));
    if(mixed)$('settings-state').append(option('mixed','Partially enabled'));
    $('settings-state').value=mixed?'mixed':String(flags.every(Boolean));
    $('settings-state-help').textContent=flags.length>1?'Changing this setting also updates its related checks.':'';
    $('settings-account').value=account&&M.enabled(draft.policy,rule)?draft.policy.r22_mode:'disabled';
    $('settings-severity').replaceChildren();
    if(builtin)$('settings-severity').append(option('','Use rule default'));
    for(const level of ['high','medium','low'])$('settings-severity').append(option(level,M.cap(level)));
    $('settings-severity').value=builtin?draft.policy.rule_severities[rule.id]||'':rule.severity;
    $('settings-impact').value=rule.mode||'advisory';
    $('rule-settings-form').querySelectorAll('select').forEach(input=>{input.disabled=!state.can_edit||locked;});
    $('save-rule-settings').hidden=!state.can_edit||locked;
    $('cancel-rule-settings').textContent=state.can_edit&&!locked?'Cancel':'Back to rules';
    ruleSettings.original=readSettings();
    const root=$('settings-related'),keys=builtin?M.relatedSettings(rule):[];root.replaceChildren();
    $('settings-related-section').hidden=!builtin;
    if(!keys.length)root.append(el('p','This check has no additional policy settings.','rules-help'));
    const list=el('div',undefined,'rules-setting-links');root.append(list);
    let more;
    keys.forEach((key,index)=>{
      if(index===4&&keys.length>6){const details=el('details',undefined,'rules-advanced');details.append(el('summary','More settings ('+(keys.length-4)+')'));more=el('div',undefined,'rules-setting-links');details.append(more);root.append(details);}
      const row=el('div',undefined,'rules-setting-link'),value=draft.policy[key];
      const label=button(M.labels[key],()=>policyLocation(key));
      const formatted=key==='r22_mode'&&!M.enabled(draft.policy,rule)?'Disabled':value&&typeof value==='object'&&!Array.isArray(value)?Object.keys(value).length+' entries':M.displayPolicy(key,value);
      row.append(label,el('span',formatted,'rules-muted'));(more||list).append(row);
    });
    $('settings-definition-section').hidden=builtin;$('settings-definition').replaceChildren();
    if(!builtin)for(const [key,value] of Object.entries(rule))if(!['id','name','kind','enabled','mode','severity'].includes(key)){
      const row=el('div',undefined,'rules-definition-row');row.append(el('strong',M.labels[key]||key),el('p',M.ruleValue(key,value,config.builder)));$('settings-definition').append(row);
    }
    updateSettings();renderView(true);
  }
  function renderPolicies() {
    const root=$('policy-fields');
    const openState=new Map([...root.querySelectorAll('[data-policy-section]')].map(node=>[node.dataset.policySection,node.open]));
    root.replaceChildren();
    const sections=[
      {id:'evidence',title:'Evidence and coverage',description:'Checks and completion targets',groups:[
        ['Environment evidence',['environment_check_enabled','environment_reference_evidence','env_markers_in_page_body']],
        ['Execution checks',['empty_step_status_optional','r22_mode']],
        ['Completion',['completion_threshold','min_assigned_for_completion']]
      ]},
      {id:'environment',title:'Environment and marketplace',description:'Hosts, locations, and exceptions',groups:[
        ['Storefronts and marketplaces',['storefront_domain','storefront_domains','marketplace_tlds']],
        ['Environment markers',['expected_environment_markers','comment_required_environment_markers','ignored_environment_host_patterns']],
        ['Production exceptions',['production_navigation_notice_labels','approved_production_paths','approved_production_host_patterns']],
        ['Run and case exceptions',['environment_expected_hosts_by_run','environment_allowed_host_exceptions','environment_required_host_overrides']]
      ]},
      {id:'defects',title:'Defects and status conventions',description:'Issue references and status names',groups:[
        ['Defect references',['defect_key_prefixes','external_defect_key_prefixes','defect_url_hosts']],
        ['Execution statuses',['statuses','status_aliases']]
      ]}
    ];
    const help={
      environment_check_enabled:'Verify the environment and URL-based marketplace evidence.',
      environment_reference_evidence:'Accept attached setup or debug references as evidence.',
      env_markers_in_page_body:'Read environment hostnames within screenshots and debug content.',
      empty_step_status_optional:'Ignore unused, blank step rows. Written steps still require a status.',
      completion_threshold:'Completed assignments per tester. Failed tests count as executed.',
      min_assigned_for_completion:'Apply the completion warning only at or above this assignment count.'
    };
    const modeHelp={disabled:'Skip the test-account check.',advisory:'Report missing account details without changing the verdict.',enforced:'Missing account details can affect the verdict.'};
    function field(key,grid){
      const value=draft.policy[key],wrap=el('div',undefined,'rules-setting');wrap.dataset.policyKey=key;wrap.tabIndex=-1;grid.append(wrap);
      if(value&&typeof value==='object'&&!Array.isArray(value)){
        wrap.classList.add('rules-setting-map');
        const heading=el('div',undefined,'rules-map-heading'),title=el('h4',M.labels[key]),rows=el('div',undefined,'rules-map-rows');
        const listValues=key.startsWith('environment_');
        function updateMap(){
          const next={},seen=new Set();
          rows.querySelectorAll('.rules-map-row').forEach(row=>{
            const [a,b]=row.querySelectorAll('input'),name=a.value.trim();let error='';
            if(!name&&b.value.trim())error='Enter a name or key.';
            else if(name&&seen.has(name))error='This name or key is already used.';
            fieldError(a,error);
            if(name){seen.add(name);next[name]=listValues?b.value.split(',').map(v=>v.trim()).filter(Boolean):b.value.trim();}
          });
          draft.policy[key]=next;changed();
        }
        function addRow(name='',val=''){
          const row=el('div',undefined,'rules-map-row'),a=el('input'),b=el('input');
          a.id='map-'+crypto.randomUUID();b.id='map-'+crypto.randomUUID();a.value=name;b.value=Array.isArray(val)?val.join(', '):val;
          a.placeholder='Name or key';b.placeholder=listValues?'Hostnames, separated by commas':'Value';
          a.setAttribute('aria-label',M.labels[key]+' key');b.setAttribute('aria-label',M.labels[key]+' value');a.disabled=b.disabled=!state.can_edit;
          a.addEventListener('input',updateMap);b.addEventListener('input',updateMap);
          const remove=button('×',()=>{row.remove();updateMap();},'rules-map-remove');
          remove.setAttribute('aria-label','Remove '+M.labels[key]+' entry');remove.title='Remove entry';remove.disabled=!state.can_edit;
          const keyCell=el('div',undefined,'rules-map-cell'),valueCell=el('div',undefined,'rules-map-cell');keyCell.append(a);valueCell.append(b);
          row.append(keyCell,valueCell,remove);rows.append(row);
        }
        Object.entries(value).forEach(([name,val])=>addRow(name,val));
        const add=button('Add entry',()=>{addRow();rows.lastElementChild.querySelector('input').focus();});add.disabled=!state.can_edit;
        heading.append(title,add);wrap.append(heading,rows);return;
      }
      const id='policy-'+key,label=el('label',M.labels[key]),text=el('div',undefined,'rules-setting-text'),control=el('div',undefined,'rules-setting-control');
      label.htmlFor=id;text.append(label);wrap.append(text,control);
      const description=el('p',help[key]||(Array.isArray(value)?'Separate entries with commas.':''),'rules-help');
      description.id=id+'-help';
      let input;
      if(typeof value==='boolean'){
        wrap.classList.add('rules-setting-toggle');control.classList.add('rules-switch-control');
        input=el('input',undefined,'rules-switch');input.type='checkbox';input.setAttribute('role','switch');input.checked=value;
        const status=el('span',value?'On':'Off','rules-switch-state');status.setAttribute('aria-hidden','true');control.append(input,status);
        input.addEventListener('change',()=>{draft.policy[key]=input.checked;status.textContent=input.checked?'On':'Off';changed();renderRules();});
      }else{
        input=el(key==='r22_mode'?'select':'input');
        if(key==='r22_mode')input.append(option('disabled','Disabled'),option('advisory','Advisory'),option('enforced','Enforced'));
        input.value=Array.isArray(value)?value.join(', '):key==='completion_threshold'?Number((value*100).toFixed(10)):value;
        if(key==='r22_mode'){
          if(!M.enabled(draft.policy,state.catalog.find(rule=>rule.id==='R22')))input.value='disabled';
          description.textContent=modeHelp[input.value];
        }
        if(typeof value==='number'){
          control.classList.add('rules-number-control');input.type='number';input.required=true;
          input.min=key==='completion_threshold'?'0':'1';input.max=key==='completion_threshold'?'100':'100000';input.step=key==='completion_threshold'?'any':'1';
        }
        input.addEventListener('input',()=>{
          fieldError(input,'');
          if(key==='r22_mode'){M.toggle(draft.policy,state.catalog,state.catalog.find(rule=>rule.id==='R22'),input.value!=='disabled');description.textContent=modeHelp[input.value];}
          draft.policy[key]=Array.isArray(value)?input.value.split(',').map(v=>v.trim()).filter(Boolean):typeof value==='number'?Number(input.value)/(key==='completion_threshold'?100:1):input.value;
          changed();renderRules();
        });
        control.append(input);
        if(key==='completion_threshold')control.append(el('span','%','rules-input-unit'));
      }
      input.id=id;input.disabled=!state.can_edit;
      if(description.textContent){text.append(description);input.setAttribute('aria-describedby',description.id);}
    }
    for(const {id,title,description,groups} of sections){
      const section=el('details',undefined,'rules-policy');section.dataset.policySection=id;section.open=openState.get(id)??id==='evidence';
      const summary=el('summary');summary.append(el('span',title),el('span',description,'rules-policy-summary'));section.append(summary);
      const body=el('div',undefined,'rules-policy-body'+(id==='evidence'?' rules-policy-basics':''));
      for(const [title,keys] of groups){const group=el('section',undefined,'rules-policy-group'),grid=el('div',undefined,'rules-policy-fields');group.append(el('h3',title),grid);keys.forEach(key=>field(key,grid));body.append(group);}
      section.append(body);root.append(section);
    }
    $('evidence-policy').value=draft.evidence_policy;$('evidence-policy').disabled=!state.can_edit;$('evidence-policy').readOnly=!!config.settingsOnly;
    if(config.settingsOnly)$('evidence-help').textContent='Maintained by developers. The current evidence policy is shown for reference.';
  }
  function validatePolicies(){
    let first=null;
    document.querySelectorAll('#tab-policies input,#tab-policies select,#tab-policies textarea').forEach(input=>{if(!input.checkValidity()){fieldError(input,input.validationMessage);if(!first)first=input;}});
    if(first){draft=workingDocument();activeTab='policies';formState=null;ruleSettings=null;renderRules();renderView();let parent=first.parentElement;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}first.focus();first.reportValidity();return false;}return true;
  }
  function readForm(){
    if(!formState)return null;
    const structured=formState.method==='structured',rule=structured?M.emptyStructured():M.emptyRule();rule.id=formState.original.id;
    Object.entries(formFields).forEach(([key,id])=>{if(!structured||['name','enabled','mode','severity'].includes(key))rule[key]=key==='enabled'?$(id).value==='true':$(id).value;});
    if(structured){
      rule.match=$('builder-match').value;
      rule.conditions=[...$('builder-conditions').querySelectorAll('.rules-condition-row')].map(row=>({field:row.querySelector('[data-condition=field]').value,operator:row.querySelector('[data-condition=operator]').value,value:row.querySelector('[data-condition=value]').value}));
      if($('builder-check').value==='field_value'){
        const field=$('builder-field').value,operator=$('builder-operator').value,value=$('builder-value').value;
        rule.check={type:'field_value',field,operator,value:['present','absent'].includes(operator)?null:config.builder.valueFields[field].type==='number'?Number(value):operator==='in'?value.split(',').map(item=>item.trim()).filter(Boolean):value};
      }else rule.check=$('builder-check').value==='attachment_count'?{type:'attachment_count',minimum:Number($('builder-minimum').value)}:{type:$('builder-check').value,scope:$('builder-scope').value};
    }else{rule.case_keys=$('custom-cases').value.toUpperCase().split(',').map(v=>v.trim()).filter(Boolean);rule.run_keys=$('custom-runs').value.toUpperCase().split(',').map(v=>v.trim()).filter(Boolean);}
    if(config.builder&&formState.acceptedSource)rule.source_text=formState.acceptedSource;
    return rule;
  }
  function clearBuilderResults(){$('builder-results').replaceChildren();$('builder-test-status').textContent='Test this rule against four sample executions.';}
  function builderSummary(){if(formState&&config.builder)$('builder-summary').textContent=(readForm().enabled?'':'Disabled. ')+M.ruleText(readForm(),config.builder)+' '+$('custom-impact').textContent;}
  function builderChanged(){clearBuilderResults();$('rule-matches').hidden=true;builderSummary();update();}
  function renderConditions(conditions){
    const root=$('builder-conditions');root.replaceChildren();
    if(!conditions.length)root.append(el('p','Applies to all executions. Add a condition to narrow the scope.','rules-help'));
    conditions.forEach((condition,index)=>{
      const row=el('div',undefined,'rules-condition-row'),inputs={};
      for(const [key,title] of [['field','Field'],['operator','Operator'],['value','Value']]){
        const cell=el('div'),label=el('label',title),select=el('select');select.id='condition-'+crypto.randomUUID();select.dataset.condition=key;label.htmlFor=select.id;select.setAttribute('aria-label',title+' for condition '+(index+1));select.disabled=!state.can_edit;inputs[key]=select;cell.append(label,select);row.append(cell);
      }
      Object.entries(config.builder.fields).forEach(([value,field])=>inputs.field.append(option(value,field.label)));
      inputs.field.value=condition.field;inputs.operator.append(option('is','is'),option('is_not','is not'));inputs.operator.value=condition.operator;
      function values(){inputs.value.replaceChildren();Object.entries(config.builder.fields[inputs.field.value].values).forEach(([value,label])=>inputs.value.append(option(value,label)));}
      values();inputs.value.value=condition.value;
      inputs.field.addEventListener('change',()=>{values();builderChanged();});
      const remove=button('×',()=>{const next=readForm().conditions;next.splice(index,1);renderConditions(next);builderChanged();(root.querySelector('select')||$('builder-add-condition')).focus();},'rules-map-remove');
      remove.setAttribute('aria-label','Remove condition '+(index+1));remove.disabled=!state.can_edit;row.append(remove);root.append(row);
    });
  }
  function fieldOperators(selected){
    const field=config.builder.valueFields[$('builder-field').value],select=$('builder-operator');select.replaceChildren();
    Object.entries(config.builder.operators[field.type]).forEach(([value,label])=>select.append(option(value,label)));
    if(selected&&config.builder.operators[field.type][selected])select.value=selected;
  }
  function builderControls(){
    const structured=formState?.method==='structured';
    ['builder-conditions-section','builder-check-section'].forEach(id=>{$(id).hidden=!structured;$(id).querySelectorAll('input,select,textarea').forEach(input=>{input.disabled=!structured||!state.can_edit;});});
    $('builder-preview-section').hidden=!config.builder;
    $('authoring-section').hidden=!config.builder;$('authoring-text').disabled=!state.can_edit;
    document.querySelectorAll('.rules-evidence-fields').forEach(section=>{section.hidden=structured;section.querySelectorAll('input,select,textarea').forEach(input=>{input.disabled=structured||!state.can_edit;});});
    $('rule-method-section').hidden=!config.builder;$('rule-method').value=formState?.method||'evidence';$('rule-method').disabled=!state.can_edit||formState.index>=0;
    $('rule-method-help').textContent=structured?'Conditions and checks run in Python. No model calls.':'A model interprets the requirement and its evidence. Local demo results are simulated.';
    $('builder-engine').textContent=structured?'Deterministic':'AI · Simulated';
    $('builder-impact').textContent=structured?'These checks make no model calls. Sample runtime is not a full-audit benchmark.':'AI assessment can add audit time. Sample outcomes are simulated; production speed and accuracy have not been measured.';
    const count=$('builder-check').value==='attachment_count',fieldCheck=$('builder-check').value==='field_value';
    $('builder-scope-field').hidden=count||fieldCheck;$('builder-minimum-field').hidden=!count;
    $('builder-scope').disabled=!structured||count||fieldCheck||!state.can_edit;$('builder-minimum').disabled=!structured||!count||!state.can_edit;
    for(const id of ['builder-value-field','builder-operator-field']){$(id).hidden=!fieldCheck;$(id).querySelector('select').disabled=!structured||!fieldCheck||!state.can_edit;}
    const field=config.builder?.valueFields[$('builder-field').value],operator=$('builder-operator').value,presence=['present','absent'].includes(operator);
    $('builder-value-input-field').hidden=!fieldCheck||presence;
    const value=$('builder-value');value.disabled=!structured||!fieldCheck||presence||!state.can_edit;value.type=field?.type==='number'?'number':'text';
    value.min='0';value.max='100000';value.step='1';value.maxLength=operator==='in'?6000:200;
    $('builder-value-label').textContent=operator==='in'?'Allowed values':'Comparison value';
    $('builder-value-help').textContent=operator==='in'?'Separate up to 30 values with commas.':'Text comparisons ignore case.';
    $('builder-check-help').textContent=config.builder?.checks[$('builder-check').value]?.help||'';
    builderSummary();
  }
  function fillForm(rule){
    formState.method=rule.kind==='structured'?'structured':'evidence';
    formState.acceptedSource=rule.source_text||'';$('authoring-text').value=formState.acceptedSource;
    $('authoring-recommendation').hidden=true;formState.proposal=null;$('rule-matches').hidden=true;
    Object.entries(formFields).forEach(([key,id])=>{$(id).value=String(rule[key]??'');});$('custom-cases').value=(rule.case_keys||[]).join(', ');$('custom-runs').value=(rule.run_keys||[]).join(', ');
    if(formState.method==='structured'){$('builder-match').value=rule.match;renderConditions(rule.conditions);$('builder-check').value=rule.check.type;$('builder-scope').value=rule.check.scope||'all';$('builder-minimum').value=rule.check.minimum||1;
      $('builder-field').value=rule.check.field||'comment';fieldOperators(rule.check.operator);$('builder-value').type=config.builder.valueFields[$('builder-field').value].type==='number'?'number':'text';$('builder-value').value=Array.isArray(rule.check.value)?rule.check.value.join(', '):rule.check.value??'';}
    clearBuilderResults();customImpact();builderControls();update();
  }
  function customImpact(){const mode=$('custom-mode').value,severity=$('custom-severity').value;$('custom-impact').textContent=mode==='advisory'||severity==='low'?'Findings are advisory and do not change the verdict.':severity==='high'?'A confirmed violation produces a deviation.':'A confirmed violation produces a concern.';}
  function openRule(index){if(busy)return;if(index<0&&(!state.can_edit||draft.custom_rules.length>=50||config.settingsOnly&&!config.builder))return;const rule=index<0?(config.builder?M.emptyStructured():M.emptyRule()):M.clone(draft.custom_rules[index]);formState={index,original:rule,cache:{}};viewedVersion=null;ruleSettings=null;
    $('rule-form').querySelectorAll('input,select,textarea').forEach(input=>{input.disabled=!state.can_edit;fieldError(input,'');});fillForm(rule);$('save-custom').hidden=!state.can_edit;$('remove-custom').hidden=index<0||!state.can_edit;$('use-example').hidden=!state.can_edit;$('cancel-custom').textContent=state.can_edit?'Cancel':'Close';renderView(true);$('custom-name').focus();}
  function showRelated(matches,root=$('rule-matches')){
    root.replaceChildren();root.hidden=false;
    root.append(el('strong',matches.some(item=>item.kind==='duplicate')?'Existing rule found':matches.length?'Related rules to review':'No exact duplicate found'));
    if(!matches.length)root.append(el('p','Different text may still describe the same requirement. Semantic equivalence is not assessed by this demo.','rules-help'));
    for(const item of matches){
      const row=el('div',undefined,'rules-related-rule');
      row.append(button(item.name,()=>{
        if(formDirty()&&!confirm('Discard this form and open the existing rule?'))return;
        const index=draft.custom_rules.findIndex(rule=>rule.id===item.id);
        if(index>=0)openRule(index);else{const existing=state.catalog.find(rule=>rule.id===item.id);if(existing)openSettings(existing);}
      }),el('span',(item.enabled?'Enabled':'Disabled')+' · '+(item.kind==='duplicate'?'Exact duplicate':item.kind==='overlap'?'Possible overlap':'Review wording'),'rules-badge'),el('p',item.description,'rules-help'));
      root.append(row);
    }
  }
  function useInterpretation(rule){
    const previous=readForm(),next={...M.clone(rule),id:formState.original.id};
    if(previous.name)next.name=previous.name;
    if(formState.index>=0)Object.assign(next,{enabled:previous.enabled,mode:previous.mode,severity:previous.severity});
    fillForm(next);$('rule-method-section').scrollIntoView({block:'start'});$('custom-name').focus({preventScroll:true});
  }
  function showInterpretation(result,matches=[]){
    const root=$('authoring-recommendation');root.replaceChildren();root.hidden=false;
    root.append(el('strong',({structured:'Recommended: deterministic checks',evidence:'Recommended: AI evidence assessment',clarify:'Clarification needed'})[result.route]));
    root.append(el('p',result.message,'rules-help'));
    if(result.rule){
      root.append(el('p',M.ruleText(result.rule,config.builder),'rules-builder-summary'));
      const related=el('div',undefined,'rules-related-rules');showRelated(matches,related);root.append(related);
      if(!matches.some(item=>item.kind==='duplicate'))root.append(button('Use this interpretation',()=>useInterpretation(result.rule),'cs-btn'));
    }else{
      const actions=el('div',undefined,'rules-action-row');
      actions.append(button('Build with controls',()=>{$('authoring-text').value=formState.acceptedSource||'';root.hidden=true;update();$('rule-method').focus();},'cs-btn'),
        button('Use AI evidence assessment',()=>{
          useInterpretation({...M.emptyRule(),name:readForm().name||'New evidence rule',applies_when:'',requirement:result.source_text,source_text:result.source_text});
          $('custom-applies').focus();
        },'cs-btn'));
      root.append(actions);
    }
  }
  async function saveDocument(document){const result=await request(base+'/draft',{version:state.version,document});state.version=result.version;state.draft=result.draft;draft=M.clone(result.draft.document);preview=null;clearTimeout(pollTimer);storageSet(null);formState=null;ruleSettings=null;renderAll();notice('Draft saved.');}
  function renderAll(){
    $('editor').hidden=false;$('read-only').hidden=state.can_edit;$('active-version').textContent='Published version '+state.active.revision;$('published-detail').textContent=actor(state.active.author)+' · '+formatDate(state.active.published_at);
    $('tab-button-test').hidden=$('tab-button-history').hidden=!state.can_edit;renderRules();renderPolicies();renderPreview();renderView();
  }
  async function load(){
    const session=await request('/session');$('identity').textContent=config.demo?'Local user':session.alias;
    state=await request(base);draft=M.clone((state.draft||state.active).document);formState=null;ruleSettings=null;viewedVersion=null;preview=null;clearTimeout(pollTimer);$('signin').hidden=true;
    activeTab=['rules','policies','test','history'].includes(location.hash.slice(1))?location.hash.slice(1):'rules';if(!state.can_edit&&['test','history'].includes(activeTab))activeTab='rules';renderAll();
    const stored=storageGet();if(stored&&state.draft&&state.can_edit){try{const candidate=await request(base+'/previews/'+stored);if(matchesPreview(candidate)){preview=candidate;renderPreview();if(['pending','running'].includes(candidate.phase))pollPreview(candidate.id);}}catch(_){/* Recovery does not block editing. */}}
    if(activeTab==='history'&&state.can_edit)await loadHistory();if(activeTab==='test'&&state.can_edit)await loadReports();
    if(location.hash==='#builder'&&config.builder&&state.can_edit)openRule(-1);
  }
  async function loadReports(){
    const data=await request(base+'/reports');reportsLoaded=true;const select=$('report-select'),previous=select.value;select.replaceChildren(option('','Choose a report'));
    data.reports.forEach(report=>select.append(option(report.folder,report.title+' · '+report.count+' tests')));select.value=previous;
    if(!data.reports.length)$('picker-notice').textContent='No saved reports are available.';
  }
  async function loadTests(offset=0,reset=false){
    const folder=$('report-select').value,serial=++pickerSerial;
    if(reset||folder!==picker.folder){picker={folder,revision:'',items:[],selected:new Set(),offset:0,next:null,total:0,loading:false};preview=null;renderPreview();}
    if(!folder){$('test-picker').hidden=true;$('picker-notice').textContent='Choose a report to see its saved tests.';update();return;}
    picker.loading=true;update();$('picker-notice').textContent='Loading saved tests…';
    try{const data=await request(base+'/reports/'+encodeURIComponent(folder)+'/executions?q='+encodeURIComponent($('test-search').value)+'&cursor='+offset);
      if(serial!==pickerSerial)return;
      if(picker.revision&&picker.revision!==data.revision){picker.selected.clear();preview=null;renderPreview();notice('The saved report changed. Select tests from the refreshed list.');}
      Object.assign(picker,{folder,revision:data.revision,items:data.items,offset,next:data.next_cursor,total:data.total});$('picker-notice').textContent='Saved '+formatDate(data.timestamp);renderTests();
    }catch(error){if(serial===pickerSerial){picker.revision='';$('test-picker').hidden=true;$('picker-notice').textContent=error.message;}}
    finally{if(serial===pickerSerial){picker.loading=false;update();}}
  }
  function verdict(audit){const status=audit.analysis_status==='incomplete'?'incomplete':audit.overall_verdict;return el('span',({pass:'✓ Compliant',concerns:'△ Concerns',fail:'⊗ Deviation',incomplete:'Incomplete'})[status]||'Unavailable','rules-verdict '+status);}
  function renderTests(){
    $('test-picker').hidden=false;const root=$('test-rows');root.replaceChildren();
    picker.items.forEach(item=>{const row=el('tr'),cell=el('td'),input=el('input');input.type='checkbox';input.checked=picker.selected.has(item.key);input.setAttribute('aria-label','Select '+item.title);input.disabled=!state.can_edit||(!input.checked&&picker.selected.size>=10);
      input.addEventListener('change',()=>{input.checked?picker.selected.add(item.key):picker.selected.delete(item.key);preview=null;renderPreview();renderTests();update();});cell.append(input);
      const name=el('td');name.append(el('strong',item.title),el('small',item.key));const run=el('td',item.run_name||item.run||'—'),result=el('td');result.append(verdict({overall_verdict:item.verdict,analysis_status:item.analysis_status}));row.append(cell,name,run,result,el('td',item.execution_date||'—'));root.append(row);});
    if(!picker.items.length){const row=el('tr'),cell=el('td','No saved tests match this search.','rules-empty');cell.colSpan=5;row.append(cell);root.append(row);}
    $('tests-previous').disabled=picker.offset===0;$('tests-next').disabled=picker.next===null;$('test-page-count').textContent=picker.total?`${picker.offset+1}–${picker.offset+picker.items.length} of ${picker.total}`:'0 tests';
  }
  function findingChanges(root,before,after){
    const rows=M.findings(before,after);if(!rows.length){root.append(el('p','No finding changes.','rules-muted'));return;}
    rows.forEach(row=>{const block=el('div',undefined,'rules-finding-change'),finding=row.after||row.before;block.append(el('strong',row.kind+' · '+(finding.title||finding.rule||'Finding')));
      for(const [label,value] of [['Current',row.before],['Draft',row.after]])if(value){block.append(el('p',label+': '+value.description),el('small',M.cap(value.severity||'')+(value.advisory?' · Advisory only':'')+(value.assessment_status==='needs_review'?' · Needs review':'')));}root.append(block);});
  }
  function renderPreview(){
    const root=$('preview-results');root.replaceChildren();$('preview-status').textContent=preview?({pending:'Waiting for worker',running:'Testing changes…',complete:'Tests complete',failed:'Tests failed'})[preview.phase]||preview.phase:'';
    if(!preview){if(state)update();return;}
    if(preview.diagnostic)root.append(el('div',preview.diagnostic,'rules-alert'+(preview.phase==='failed'?' error':'')));
    if(preview.results&&preview.results.length){const card=el('section',undefined,'rules-panel-section'),header=el('div',undefined,'cs-container-header');header.append(el('h2','Test results'));card.append(header);const counts={Changed:0,Added:0,Removed:0};let verdictChanges=0;
      preview.results.forEach(result=>{if(result.before.overall_verdict!==result.after.overall_verdict)verdictChanges++;M.findings(result.before.findings,result.after.findings).forEach(item=>counts[item.kind]++);});
      const summary=el('div',undefined,'rules-result-summary');for(const [count,label] of [[verdictChanges,'verdict changes'],[counts.Added,'findings added'],[counts.Removed,'findings removed'],[counts.Changed,'findings changed']]){const item=el('span');item.append(el('strong',count),document.createTextNode(label));summary.append(item);}card.append(summary);
      preview.results.forEach(result=>{const item=el('details',undefined,'rules-result'),head=el('summary'),name=el('div',undefined,'rules-result-name');name.append(el('strong',result.title||result.sample?.title||result.execution),el('small',result.execution));head.append(name);
        const comparison=el('div',undefined,'rules-verdict-comparison');for(const [label,audit] of [['Current',result.before],['Draft',result.after]]){const column=el('div',undefined,'rules-verdict-column');column.append(el('small',label),verdict(audit));comparison.append(column);}head.append(comparison);
        item.append(head);const body=el('div',undefined,'rules-section-body');findingChanges(body,result.before.findings,result.after.findings);if(result.before.analysis_status==='incomplete'||result.after.analysis_status==='incomplete')body.append(el('p','This assessment did not complete. It cannot be used to publish.','rules-alert error'));item.append(body);card.append(item);});root.append(card);
    }
    if(preview.coverage&&preview.coverage.length){const card=el('section',undefined,'rules-panel-section'),head=el('div',undefined,'cs-container-header');head.append(el('h2','Folder coverage changes'));card.append(head);preview.coverage.forEach(result=>{const body=el('div',undefined,'rules-section-body');body.append(el('h3',result.folder));findingChanges(body,result.before,result.after);card.append(body);});root.append(card);}update();
  }
  function pollPreview(id){clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{try{const candidate=await request(base+'/previews/'+id);if(!preview||preview.id!==id||!matchesPreview(candidate)||dirty())return;preview=candidate;renderPreview();if(['pending','running'].includes(preview.phase))pollPreview(id);}catch(error){notice(error.message,true);}},config.demo?350:3000);}
  async function loadHistory(){const data=await request(base+'/history'),root=$('history');root.replaceChildren();$('history-count').textContent='('+data.versions.length+')';data.versions.forEach(version=>{const row=el('tr'),name=el('td');name.append(button('Version '+version.revision,()=>action(()=>openVersion(version.revision))));if(version.revision===state.active.revision)name.append(el('small','Published','rules-badge'));
    const published=el('td');published.append(el('span',formatDate(version.published_at)),el('small',actor(version.author),'rules-muted'));const actions=el('td'),wrap=el('div',undefined,'rules-history-actions');wrap.append(button('View changes',()=>action(()=>openVersion(version.revision))),button('Restore as draft',()=>action(()=>restore(version.revision))));actions.append(wrap);row.append(name,published,el('td',version.note),actions);root.append(row);});}
  async function openVersion(revision){viewedVersion=await request(base+'/versions/'+revision);formState=null;$('version-title').textContent='Version '+revision;$('version-meta').textContent=actor(viewedVersion.author)+' · '+formatDate(viewedVersion.published_at)+' · '+viewedVersion.note;
    renderDiff($('version-diff'),M.changes(state.active.document,viewedVersion.document,state.catalog,config.builder),'Published version '+state.active.revision,'Version '+revision);
    const root=$('version-contents');root.replaceChildren();const rows=state.catalog.map(rule=>({label:rule.id+' · '+rule.name,before:M.enabled(viewedVersion.document.policy,rule)?'Enabled':'Disabled',after:M.severity(viewedVersion.document.policy,rule).label}));renderDiff(root,rows,'State','Severity');
    for(const rule of viewedVersion.document.custom_rules){const block=el('div',undefined,'rules-finding-change');block.append(el('h3',rule.name));Object.entries(rule).filter(([key])=>key!=='id').forEach(([key,value])=>block.append(el('p',(M.labels[key]||key)+': '+M.ruleValue(key,value,config.builder))));root.append(block);}
    const policy=el('details',undefined,'rules-advanced');policy.append(el('summary','Policy settings'));const values=el('div');for(const [key,value] of Object.entries(viewedVersion.document.policy))if(!['enabled_rules','disabled_rules','rule_severities'].includes(key))values.append(el('p',(M.labels[key]||key)+': '+M.displayPolicy(key,value)));values.append(el('p',viewedVersion.document.evidence_policy));policy.append(values);root.append(policy);renderView(true);
  }
  async function restore(revision){if(dirty()&&!confirm('Replace the unsaved draft changes with this version?'))return;const result=await request(base+'/restore',{version:state.version,revision});state.version=result.version;state.draft=result.draft;draft=M.clone(result.draft.document);preview=null;storageSet(null);formState=null;viewedVersion=null;activeTab='test';renderAll();await loadReports();notice('Version '+revision+' restored as a draft.');}
  document.querySelectorAll('[data-tab]').forEach(node=>node.addEventListener('click',()=>chooseTab(node.dataset.tab)));
  $('tabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const tabs=[...$('tabs').querySelectorAll('button:not([hidden])')],index=tabs.indexOf(document.activeElement);if(index<0)return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;chooseTab(tabs[next].dataset.tab);tabs[next].focus();});
  $('rule-search').addEventListener('input',renderRules);['rule-type','rule-state','changed-only'].forEach(id=>$(id).addEventListener('change',renderRules));
  $('clear-filters').addEventListener('click',()=>{$('rule-search').value=$('rule-type').value=$('rule-state').value='';$('changed-only').checked=false;renderRules();});
  $('view-changes').addEventListener('click',showChanges);$('close-changes').addEventListener('click',()=>$('changes-dialog').close());
  $('evidence-policy').addEventListener('input',()=>{if(config.settingsOnly)return;fieldError($('evidence-policy'),'');draft.evidence_policy=$('evidence-policy').value;changed();});
  $('rule-settings-form').addEventListener('input',updateSettings);
  $('rule-settings-form').addEventListener('submit',event=>{event.preventDefault();const document=workingDocument();if(!validatePolicies())return;action(async()=>{await saveDocument(document);activeTab='rules';history.replaceState(null,'','#rules');renderView(true);});});
  $('cancel-rule-settings').addEventListener('click',()=>chooseTab('rules',true));
  $('test-saved-draft').addEventListener('click',()=>chooseTab('test',true));
  $('add-rule').addEventListener('click',()=>openRule(-1));
  $('rule-method').addEventListener('change',()=>{
    if(authoringPending()){$('rule-method').value=formState.method;notice('Review or discard the changed description before switching methods.',true);return;}
    const previous=readForm(),method=$('rule-method').value;formState.cache[formState.method]=previous;
    const next=formState.cache[method]||{...(method==='structured'?M.emptyStructured():M.emptyRule()),name:previous.name,enabled:previous.enabled};
    fillForm(next);
  });
  $('authoring-text').addEventListener('input',()=>{$('authoring-recommendation').hidden=true;formState.proposal=null;update();});
  $('authoring-discard').addEventListener('click',()=>{$('authoring-text').value=formState.acceptedSource||'';$('authoring-recommendation').hidden=true;update();});
  for(const [id,kind] of [['authoring-example-check','structured'],['authoring-example-evidence','evidence']])$(id).addEventListener('click',()=>{
    if(authoringPending()&&!confirm('Replace the description with this example?'))return;
    $('authoring-text').value=config.builder.textExamples[kind];$('authoring-recommendation').hidden=true;update();$('authoring-text').focus();
  });
  $('authoring-review').addEventListener('click',()=>action(async()=>{
    const result=await request(base+'/builder/interpret',{text:$('authoring-text').value.trim()});
    let matches=[];
    if(result.rule){result.rule.id=formState.original.id;matches=(await request(base+'/builder/inspect',{rule:result.rule})).matches;}
    showInterpretation(result,matches);
  }));
  $('builder-example').addEventListener('click',()=>{if(formDirty()&&!confirm('Replace this form with the defect-reference example?'))return;fillForm({...M.structuredExample(),id:formState.original.id});});
  $('builder-add-condition').addEventListener('click',()=>{const conditions=readForm().conditions;if(conditions.length>=5)return;conditions.push({field:'test_type',operator:'is',value:'purchase'});renderConditions(conditions);builderChanged();$('builder-conditions').lastElementChild.querySelector('select').focus();});
  $('builder-check').addEventListener('change',()=>{builderControls();builderChanged();});
  $('builder-field').addEventListener('change',()=>{fieldOperators();$('builder-value').value='';builderControls();builderChanged();});
  $('builder-operator').addEventListener('change',()=>{builderControls();builderChanged();});
  $('inspect-rule').addEventListener('click',()=>{
    if(authoringPending()||!$('rule-form').reportValidity())return;
    action(async()=>showRelated((await request(base+'/builder/inspect',{rule:readForm()})).matches));
  });
  $('builder-try').addEventListener('click',()=>{
    if(!$('rule-form').reportValidity())return;
    action(async()=>{
      $('builder-test-status').textContent=formState.method==='structured'?'Running deterministic checks…':'Loading simulated outcomes…';
      let result;
      try{result=await request(base+'/builder/test',{rule:readForm()});}
      catch(error){$('builder-test-status').textContent='Sample test failed. Try again.';throw error;}
      const root=$('builder-results');root.replaceChildren();
      showRelated(result.matches||[]);
      for(const item of result.results){
        const details=el('details',undefined,'rules-builder-result'),summary=el('summary'),name=el('div');
        name.append(el('strong',item.sample.title),el('small',item.description));
        summary.append(name,el('span',({met:'Met',violated:'Not met',not_applicable:'Not applicable',uncertain:'Needs review'})[item.status],'rules-verdict '+({met:'pass',violated:'fail',not_applicable:'neutral',uncertain:'concerns'})[item.status]));details.append(summary);
        const body=el('div',undefined,'rules-builder-facts');
        item.conditions.forEach((condition,index)=>body.append(el('p',M.conditionText(readForm().conditions[index],config.builder)+' · '+(condition.actual===null?'Unavailable':condition.matched?'Matched':'Did not match'))));
        body.append(el('p','Execution comment: '+(item.sample.comment||'None')),el('p','Step comments: '+(item.sample.steps.join(' · ')||'None')),el('p','Attachments: '+item.sample.attachments.length));
        details.append(body);root.append(details);
      }
      $('builder-test-status').textContent=result.simulated?result.results.length+' simulated outcomes · No model calls · Not an accuracy benchmark':result.results.length+' tests complete · Python · '+result.model_calls+' model calls';
      if(!result.simulated)$('builder-impact').textContent='Local check evaluation: '+result.elapsed_ms+' ms for '+result.results.length+' samples. No model calls. Full-audit latency and accuracy have not been benchmarked.';
    });
  });
  $('use-example').addEventListener('click',()=>{const current=readForm();if((current.requirement||current.expected_evidence)&&!confirm('Replace this form with the purchase-email example?'))return;fillForm({...M.example(),id:current.id});});
  $('cancel-custom').addEventListener('click',()=>chooseTab('rules',true));
  $('rule-form').addEventListener('input',event=>{fieldError(event.target,'');customImpact();clearBuilderResults();$('rule-matches').hidden=true;builderSummary();update();});
  $('rule-form').addEventListener('submit',event=>{event.preventDefault();let valid=true;for(const [id,pattern] of [['custom-cases',/^[A-Z][A-Z0-9_]*-T[0-9]+$/],['custom-runs',/^[A-Z][A-Z0-9_]*-C[0-9]+$/]]){const input=$(id);if(input.disabled)continue;const values=input.value.toUpperCase().split(',').map(v=>v.trim()).filter(Boolean);const message=values.length>100||values.some(value=>!pattern.test(value))?'Enter valid keys separated by commas (up to 100).':'';fieldError(input,message);if(message){input.closest('details').open=true;valid=false;}}
    if(!valid){$('rule-form').reportValidity();return;}if(authoringPending())return;const rule=readForm();const document=M.clone(draft);if(formState.index<0)document.custom_rules.push(rule);else document.custom_rules[formState.index]=rule;
    if(!validatePolicies())return;action(async()=>{if(config.builder){const inspected=await request(base+'/builder/inspect',{rule});showRelated(inspected.matches);if(inspected.matches.some(item=>item.kind==='duplicate')){$('rule-matches').scrollIntoView({block:'center'});notice('This rule already exists. Open the existing rule to edit or re-enable it.',true);return;}}await saveDocument(document);activeTab='rules';history.replaceState(null,'','#rules');renderView(true);});
  });
  $('remove-custom').addEventListener('click',()=>{if(!confirm('Remove this rule from the draft?'))return;const document=M.clone(draft);document.custom_rules.splice(formState.index,1);action(async()=>{await saveDocument(document);activeTab='rules';renderView(true);});});
  $('save-draft').addEventListener('click',()=>{if(validatePolicies())action(()=>saveDocument(draft));});
  $('report-select').addEventListener('change',()=>{$('test-search').value='';loadTests(0,true);});$('reload-tests').addEventListener('click',()=>loadTests(0,true));
  $('test-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadTests(0),250);});
  $('tests-previous').addEventListener('click',()=>loadTests(Math.max(0,picker.offset-100)));$('tests-next').addEventListener('click',()=>loadTests(picker.next));
  $('clear-selection').addEventListener('click',()=>{picker.selected.clear();preview=null;renderPreview();renderTests();update();});
  $('run-preview').addEventListener('click',()=>action(async()=>{const result=await request(base+'/preview',{version:state.version,executions:[...picker.selected],report:{folder:picker.folder,revision:picker.revision}});state.version=result.version;preview=result.preview;storageSet(preview.id);renderPreview();pollPreview(preview.id);}));
  $('publish-note').addEventListener('input',update);
  $('publish').addEventListener('click',()=>action(async()=>{const result=await request(base+'/publish',{version:state.version,preview_id:preview.id,note:$('publish-note').value.trim()});state.version=result.version;state.active=result.active;state.draft=null;draft=M.clone(result.active.document);preview=null;storageSet(null);$('publish-note').value='';renderAll();notice('Version '+result.active.revision+' published.');}));
  $('version-back').addEventListener('click',()=>chooseTab('history',true));$('version-restore').addEventListener('click',()=>action(()=>restore(viewedVersion.revision)));
  const refresh=()=>{if((dirty()||formDirty())&&!confirm('Discard unsaved changes and load the latest rules?'))return;action(load);};$('refresh').addEventListener('click',refresh);$('reconnect').addEventListener('click',refresh);
  if(config.builder){Object.entries(config.builder.checks).forEach(([value,check])=>$('builder-check').append(option(value,check.label)));Object.entries(config.builder.scopes).forEach(([value,label])=>$('builder-scope').append(option(value,label)));Object.entries(config.builder.valueFields).forEach(([value,field])=>$('builder-field').append(option(value,field.label)));fieldOperators();}
  else $('rule-type').querySelector('[value=structured]').remove();
  $('add-rule').hidden=!!config.settingsOnly&&!config.builder;$('rules-settings-hint').hidden=!config.settingsOnly;
  if(config.settingsOnly){$('page-description').textContent='Create and configure audit checks and evidence requirements.';$('demo-banner').querySelector('span').textContent='Sample data · Deterministic checks run locally. Text interpretation is limited; AI outcomes are simulated.';}
  $('demo-banner').hidden=!config.demo;$('signin-link').href=config.api+'/login?team='+encodeURIComponent(config.team);$('signin-link').target='_blank';$('signin-link').rel='noopener';
  document.addEventListener('click',event=>{const link=event.target.closest('a');if(!link||link.target==='_blank')return;if(link.getAttribute('href')==='#rules'){event.preventDefault();chooseTab('rules',true);return;}if((busy||(dirty()||formDirty())&&!confirm('Leave this page and discard unsaved changes?')))event.preventDefault();});
  addEventListener('hashchange',()=>{if(location.hash==='#builder'&&config.builder&&state?.can_edit&&!busy&&!formState&&!ruleSettings)openRule(-1);});
  addEventListener('beforeunload',event=>{if(dirty()||formDirty()){event.preventDefault();event.returnValue='';}});
  load().catch(error=>{$('signin').hidden=config.demo;notice(error.message,true);});
})();
