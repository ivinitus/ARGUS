/* Pure policy presentation logic, shared by the editor and its regression checks. */
window.ArgusRules = (() => {
  const clone = value => JSON.parse(JSON.stringify(value));
  const canonical = value => JSON.stringify(value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, JSON.parse(canonical(value[key]))])) : value);
  const equal = (a, b) => canonical(a) === canonical(b);
  const cap = value => value ? value[0].toUpperCase() + value.slice(1) : '';
  const labels = {
    completion_threshold: 'Required completion rate', min_assigned_for_completion: 'Minimum assigned cases',
    environment_check_enabled: 'Check the test environment', empty_step_status_optional: 'Allow empty placeholder steps without a status',
    r22_mode: 'Test-account check mode', defect_key_prefixes: 'Defect key prefixes', external_defect_key_prefixes: 'External defect key prefixes',
    defect_url_hosts: 'Accepted defect tracker hosts', storefront_domain: 'Default storefront', storefront_domains: 'Storefront domains',
    expected_environment_markers: 'Accepted environment markers', comment_required_environment_markers: 'Markers that require an explanation',
    ignored_environment_host_patterns: 'Ignored environment hosts', production_navigation_notice_labels: 'Informational production page labels',
    approved_production_paths: 'Approved production setup paths', approved_production_host_patterns: 'Approved production storefront hosts',
    environment_reference_evidence: 'Allow setup references as environment evidence', env_markers_in_page_body: 'Read environment markers in page content',
    environment_expected_hosts_by_run: 'Expected hosts by run-name pattern', environment_allowed_host_exceptions: 'Allowed hosts by test case',
    environment_required_host_overrides: 'Required hosts by test case', marketplace_tlds: 'Marketplace domain suffixes',
    statuses: 'Execution status names', status_aliases: 'Additional status aliases', name: 'Name', enabled: 'State',
    applies_when: 'Conditions', requirement: 'Requirement', expected_evidence: 'Expected evidence', examples: 'Examples',
    severity: 'Severity', mode: 'Impact on results', case_keys: 'Test cases', run_keys: 'Test runs', evidence_policy: 'Evidence policy',
    kind: 'Evaluation method', match: 'Match conditions', conditions: 'Conditions', check: 'Check', source_text: 'Original description'
  };
  function enabled(policy, rule) {
    if (rule.locked) return true;
    const family = rule.parent || rule.id;
    if (family === 'R22' && policy.r22_mode === 'disabled') return false;
    if (!policy.environment_check_enabled && ['R9', 'R13'].includes(family)) return false;
    return !policy.disabled_rules.includes(rule.id) && !policy.disabled_rules.includes(family) &&
      (!policy.enabled_rules.length || policy.enabled_rules.includes(rule.id) || policy.enabled_rules.includes(family));
  }
  function toggle(policy, catalog, rule, value) {
    const flags = Object.fromEntries(catalog.map(item => [item.id, enabled(policy, item)]));
    for (const item of catalog) if (item.id === rule.id || item.parent === rule.id) flags[item.id] = value;
    if (value && ['R9', 'R13'].includes(rule.parent || rule.id)) policy.environment_check_enabled = true;
    if (rule.id === 'R22') {
      if (!value) policy.r22_mode = 'disabled';
      else if (policy.r22_mode === 'disabled') policy.r22_mode = 'advisory';
    }
    // Explicit enabled checks plus disabled children express mixed families
    // without accidentally expanding a sparse allowlist into all checks.
    policy.enabled_rules = catalog.filter(item => item.locked || flags[item.id]).map(item => item.id);
    policy.disabled_rules = catalog.filter(item => !item.locked && !flags[item.id] && item.parent && flags[item.parent]).map(item => item.id);
  }
  function severity(policy, rule, ignoreOwn = false) {
    if (rule.locked) return {label: 'High', source: 'Protected check', explanation: 'This check cannot be weakened.'};
    if (rule.id === 'R22' && policy.r22_mode === 'disabled') return {label: 'Not evaluated', source: 'Disabled', explanation: 'The test-account check is disabled.'};
    const own = !ignoreOwn && policy.rule_severities[rule.id];
    const inherited = rule.parent && policy.rule_severities[rule.parent];
    if (own) return {label: cap(own), source: 'Override', explanation: 'This rule uses an explicit severity.'};
    if (inherited) return {label: cap(inherited), source: 'From ' + rule.parent, explanation: 'Inherited from the parent rule.'};
    if (rule.id === 'R22') return {label: policy.r22_mode === 'enforced' ? 'Medium' : 'Low', source: 'Rule default', explanation: rule.default_explanation};
    const levels = rule.default_severities || [];
    return {label: levels.length === 1 ? cap(levels[0]) : 'Conditional', source: 'Rule default', explanation: rule.default_explanation || 'Determined by the finding.'};
  }
  function display(value) {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'boolean') return value ? 'Enabled' : 'Disabled';
    if (Array.isArray(value)) return value.length ? value.join(', ') : 'None';
    if (typeof value === 'object') return Object.entries(value).map(([k,v]) => k + ': ' + display(v)).join('\n') || 'None';
    return ({disabled: 'Disabled', advisory: 'Advisory only', enforce: 'Affects verdict', enforced: 'Enforced', high: 'High', medium: 'Medium', low: 'Low'})[value] || String(value);
  }
  function displayPolicy(key, value) {
    return key === 'completion_threshold' && typeof value === 'number'
      ? Number((value * 100).toFixed(10)) + '%' : display(value);
  }
  function ruleChanged(before, after, rule) {
    return enabled(before, rule) !== enabled(after, rule) ||
      before.rule_severities[rule.id] !== after.rule_severities[rule.id] ||
      !equal(severity(before, rule), severity(after, rule));
  }
  function relatedSettings(rule) {
    const statuses = ['statuses', 'status_aliases'];
    const steps = ['empty_step_status_optional', ...statuses];
    const defects = ['defect_key_prefixes', 'external_defect_key_prefixes', 'defect_url_hosts'];
    const map = {
      R0: statuses, R1: steps, R2: steps, R2B: steps, R4: steps, R5: steps,
      R6: [...steps, ...defects], R7: steps, R8: steps,
      R9: ['environment_check_enabled','environment_reference_evidence','env_markers_in_page_body',
        'environment_expected_hosts_by_run','environment_allowed_host_exceptions','environment_required_host_overrides',
        'storefront_domain','storefront_domains','expected_environment_markers','comment_required_environment_markers',
        'ignored_environment_host_patterns','production_navigation_notice_labels','approved_production_paths','approved_production_host_patterns'],
      R10: defects, R11: [...steps, ...defects], R12: [], R13: ['environment_check_enabled','marketplace_tlds','storefront_domain','storefront_domains'],
      R14: [], R15: ['completion_threshold','min_assigned_for_completion'],
      R15A: [], R15B: [], R15C: ['completion_threshold','min_assigned_for_completion'],
      R16: [...statuses, ...defects], R17: [...statuses, ...defects], R18: [...statuses, ...defects],
      R19: [], R20: [...statuses, ...defects], R21: [...statuses, ...defects],
      R22: ['r22_mode', ...statuses], 'R-RUNTIME': []
    };
    return map[rule.id] || map[rule.parent] || [];
  }
  function conditionText(condition, builder) {
    const field = builder?.fields[condition.field];
    return (field?.label || condition.field) + (condition.operator === 'is_not' ? ' is not ' : ' is ') +
      (field?.values[condition.value] || condition.value);
  }
  function checkText(check, builder) {
    if (check.type === 'attachment_count') return 'At least ' + check.minimum + ' attachment' + (check.minimum === 1 ? '' : 's');
    if (check.type === 'field_value') {
      const field=builder?.valueFields[check.field],operator=builder?.operators[field?.type]?.[check.operator]||check.operator;
      return (field?.label||check.field)+' '+operator+(check.value===null?'':' '+(Array.isArray(check.value)?check.value.join(', '):String(check.value)));
    }
    return (builder?.checks[check.type]?.label || check.type) + ' in ' + (builder?.scopes[check.scope] || check.scope).toLowerCase();
  }
  function ruleText(rule, builder) {
    if (rule.kind !== 'structured') return rule.requirement;
    return (rule.conditions.length?'When ' + rule.conditions.map(item => conditionText(item, builder)).join(rule.match === 'any' ? ' OR ' : ' AND '):'For all executions') +
      ', check: ' + checkText(rule.check, builder) + '.';
  }
  function ruleValue(key, value, builder) {
    if (key === 'conditions') return value.map(item => conditionText(item, builder)).join('; ');
    if (key === 'check') return checkText(value, builder);
    if (key === 'kind') return value === 'structured' ? 'Python check' : 'LLM check';
    if (key === 'match') return value === 'all' ? 'All conditions' : 'Any condition';
    return display(value);
  }
  function changes(before, after, catalog, builder) {
    const rows = [];
    const add = (label, a, b) => { if (!equal(a ?? null, b ?? null)) rows.push({label, before: display(a), after: display(b)}); };
    for (const rule of catalog) {
      add(rule.name + ' · State', enabled(before.policy, rule), enabled(after.policy, rule));
      const a = severity(before.policy, rule), b = severity(after.policy, rule);
      add(rule.name + ' · Severity', a.label + ' · ' + a.source, b.label + ' · ' + b.source);
    }
    for (const key of Object.keys(after.policy)) {
      if (['enabled_rules','disabled_rules','rule_severities'].includes(key)) continue;
      const a = before.policy[key], b = after.policy[key];
      if (key === 'completion_threshold') {
        add(labels[key], displayPolicy(key, a), displayPolicy(key, b));
        continue;
      }
      if (a && b && typeof b === 'object' && !Array.isArray(b)) {
        for (const name of new Set([...Object.keys(a), ...Object.keys(b)])) add((labels[key] || key) + ' · ' + name, a[name], b[name]);
      } else add(labels[key] || key, a, b);
    }
    const oldRules = new Map(before.custom_rules.map(rule => [rule.id, rule]));
    const newRules = new Map(after.custom_rules.map(rule => [rule.id, rule]));
    for (const rule of after.custom_rules) {
      const old = oldRules.get(rule.id);
      if (!old) { add(rule.name + (rule.kind === 'structured' ? ' · Python rule' : ' · Custom rule'), null, ruleText(rule, builder)); continue; }
      for (const key of Object.keys(rule)) if (key !== 'id' && !equal(old[key], rule[key])) add(rule.name + ' · ' + (labels[key] || key), old[key] === undefined ? null : ruleValue(key, old[key], builder), ruleValue(key, rule[key], builder));
    }
    for (const rule of before.custom_rules) if (!newRules.has(rule.id)) add(rule.name + (rule.kind === 'structured' ? ' · Python rule removed' : ' · Custom rule removed'), ruleText(rule, builder), null);
    add('Evidence policy', before.evidence_policy, after.evidence_policy);
    return rows;
  }
  function findings(before = [], after = []) {
    const left = [...before], right = [...after];
    for (let i = left.length - 1; i >= 0; i--) {
      const match = right.findIndex(item => equal(item, left[i]));
      if (match >= 0) {left.splice(i, 1); right.splice(match, 1);}
    }
    const rows = [];
    for (const previous of left) {
      const index = right.findIndex(item => (item.rule || item.title) === (previous.rule || previous.title));
      if (index >= 0) rows.push({kind:'Changed', before:previous, after:right.splice(index,1)[0]});
      else rows.push({kind:'Removed', before:previous, after:null});
    }
    right.forEach(item => rows.push({kind:'Added', before:null, after:item}));
    return rows;
  }
  const emptyRule = () => ({id:'',name:'',enabled:true,applies_when:'All audited executions',case_keys:[],run_keys:[],requirement:'',expected_evidence:'',examples:'',severity:'medium',mode:'advisory'});
  const example = () => ({...emptyRule(),name:'Purchase confirmation email',applies_when:'Purchase tests with a successful transaction',requirement:'A successful purchase must have a confirmation email in the supplied evidence.',expected_evidence:'An email showing the purchased item and successful transaction.',examples:'A purchase with an email passes. A purchase without email evidence violates this requirement.'});
  const emptyStructured = () => ({id:'',kind:'structured',name:'',enabled:true,match:'all',conditions:[{field:'status',operator:'is',value:'failed'}],check:{type:'defect_reference',scope:'all'},severity:'medium',mode:'enforce'});
  const structuredExample = () => ({...emptyStructured(),name:'Failed tests need a defect'});
  return {clone, equal, cap, labels, enabled, toggle, severity, display, displayPolicy, ruleChanged, relatedSettings, changes, findings, emptyRule, example, emptyStructured, structuredExample, conditionText, ruleText, ruleValue};
})();
