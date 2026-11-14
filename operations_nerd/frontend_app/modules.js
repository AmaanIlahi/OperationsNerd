'use strict';

// Modules register their own views. The host loads only manifest-declared local
// assets and owns the shared session, business context and approval controls.
const BusinessModules = (() => {
  const registry = new Map(), loading = new Map(), notices = new Map(), selectedTabs = new Map();
  let generation = 0;
  const key = () => 'web-' + (globalThis.crypto?.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2));
  const day = () => { const d = new Date(); return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-'); };
  const label = text => String(text ?? '').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
  const money = cents => (Number(cents || 0) / 100).toFixed(2);
  const norm = text => String(text).toLowerCase().replace(/[^a-z0-9]/g, '').replace(/s$/, '');
  function minor(value) {
    const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(value).trim());
    if (!match) throw new Error('Enter an amount with at most two decimal places.');
    const cents = Number(match[1]) * 100 + Number((match[2] || '').padEnd(2, '0'));
    if (!Number.isSafeInteger(cents)) throw new Error('This amount is too large.');
    return cents;
  }
  function register(id, implementation) { registry.set(id, implementation); }
  async function load(nerd) {
    if (registry.has(nerd.id)) return registry.get(nerd.id);
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(nerd.id)) throw new Error('This module has an invalid identifier.');
    if (!loading.has(nerd.id)) loading.set(nerd.id, new Promise((resolve, reject) => {
      const url = new URL(nerd.frontend_script || '/app/' + nerd.id + '.js', location.origin);
      if (url.origin !== location.origin || !url.pathname.startsWith('/app/')) return reject(new Error('The module interface must be installed locally.'));
      const script = document.createElement('script'); script.src = url.href;
      script.onload = () => registry.has(nerd.id) ? resolve(registry.get(nerd.id)) : reject(new Error('The module interface did not register.'));
      script.onerror = () => reject(new Error('The module interface could not load.'));
      document.head.append(script);
    }).catch(error => { loading.delete(nerd.id); throw error; }));
    return loading.get(nerd.id);
  }
  async function render(context) {
    const baseIsCurrent = context.baseIsCurrent || context.isCurrent, token = ++generation;
    const ctx = {...context, baseIsCurrent, isCurrent: () => baseIsCurrent() && token === generation};
    const {el, fill, api, container, business, isCurrent, id} = ctx;
    fill(container, el('p', {class: 'muted', text: 'Loading workspace…'}));
    try {
      const manifests = await api('GET', `/businesses/${business.id}/nerds`);
      if (!isCurrent()) return;
      const nerd = manifests.nerds.find(module => module.id === id);
      if (!nerd) throw new Error('This capability is not installed.');
      const name = (nerd.display_name || nerd.name || label(id)).replace(/Nerd$/, '');
      const body = el('div');
      fill(container, el('div', {class: 'row logistics-head'}, el('div', null,
        el('span', {class: 'logistics-kicker', text: 'Business operations'}), el('h2', {text: name}), el('p', {class: 'muted', text: business.name})),
      el('span', {class: 'spacer'}), el('span', {class: 'badge' + (nerd.status === 'enabled' ? ' ok' : ''), text: nerd.status === 'enabled' ? 'Enabled' : 'Not enabled'})), body);
      if (nerd.available === false) { fill(body, el('div', {class: 'warn', text: name + ' has not been configured for this installation.'})); return; }
      const plugin = await load(nerd);
      if (!isCurrent()) return;
      ctx.nerd = nerd; ctx.name = name; ctx.plugin = plugin;
      if (nerd.status !== 'enabled') return mapping(ctx, body);
      const workspace = await api('GET', `/businesses/${business.id}/${id}/workspace`);
      if (!isCurrent()) return;
      const h = helpers(ctx);
      const notice = notices.get(business.id + ':' + id);
      if (notice) body.append(el('div', {class: 'notice', role: 'status', text: notice}));
      const view = await plugin.renderWorkspace(ctx, workspace, h);
      if (!isCurrent()) return;
      body.append(view, settings(ctx));
    } catch (error) { if (isCurrent()) fill(container, el('h2', {text: label(id)}), el('div', {class: 'error', role: 'alert', text: error.message}), el('button', {text: 'Try again', onclick: () => render(ctx)})); }
  }
  function mapping(ctx, target) {
    const {el, fill, api, business, nerd, plugin, name, id, isCurrent} = ctx;
    const entities = business.spec.entities.filter(entity => !entity.archived), bindings = {}, areas = {};
    const definitions = plugin.roles;
    const errors = el('div', {role: 'alert'}), review = el('div');
    for (const [role, definition] of Object.entries(definitions)) {
      const matches = entities.filter(entity => [role, ...(definition.names || [])].some(alias => norm(alias) === norm(entity.label)));
      bindings[role] = {entity: nerd.bindings?.[role]?.entity || (matches.length === 1 ? matches[0].key : ''), fields: {...nerd.bindings?.[role]?.fields}, links: {}};
    }
    function draw(role) {
      const binding = bindings[role], entity = entities.find(entity => entity.key === binding.entity);
      const fields = (entity?.fields || []).filter(field => !field.archived);
      fill(areas[role], Object.entries(definitions[role].fields).map(([meaning, def]) => {
        const options = fields.filter(field => def.type === 'text' ? ['text', 'long_text', 'select'].includes(field.type) : field.type === def.type);
        if (!options.some(field => field.key === binding.fields[meaning])) {
          const matches = options.filter(field => [meaning, def.label].some(alias => norm(alias) === norm(field.label)));
          binding.fields[meaning] = matches.length === 1 ? matches[0].key : '';
        }
        const select = el('select', {required: !def.optional, 'aria-label': label(role) + ': ' + def.label, onchange: event => { binding.fields[meaning] = event.target.value; fill(review); }},
          el('option', {value: '', text: 'Choose…'}), options.map(field => el('option', {value: field.key, text: field.label})));
        select.value = binding.fields[meaning];
        return el('label', {class: 'logistics-field'}, def.label + (def.optional ? ' (optional)' : ' *'), select);
      }));
    }
    const cards = Object.entries(definitions).map(([role, definition]) => {
      areas[role] = el('div');
      const select = el('select', {required: true, 'aria-label': label(role) + ' record type', onchange: event => { bindings[role] = {entity: event.target.value, fields: {}, links: {}}; draw(role); fill(review); }},
        el('option', {value: '', text: 'Choose…'}), entities.map(entity => el('option', {value: entity.key, text: entity.label})));
      select.value = bindings[role].entity;
      return el('fieldset', null, el('legend', {text: definition.label || label(role)}), el('label', {class: 'logistics-field'}, 'Record type *', select), areas[role]);
    });
    for (const role of Object.keys(definitions)) draw(role);
    const button = el('button', {type: 'submit', class: 'primary', text: 'Review ' + name + ' setup'});
    const form = el('form', {onsubmit: async event => {
      event.preventDefault(); button.disabled = true; fill(errors); fill(review);
      try {
        const payload = Object.fromEntries(Object.entries(bindings).map(([role, binding]) => [role, {entity: binding.entity, fields: Object.fromEntries(Object.entries(binding.fields).filter(([, value]) => value)), links: {}}]));
        const proposal = await api('POST', `/businesses/${business.id}/nerds/${id}/proposals`, {base_version: business.current_version, bindings: payload});
        if (!isCurrent()) return;
        const errorBox = el('div', {role: 'alert'});
        const approve = el('button', {class: 'primary', text: 'Approve and enable ' + name, onclick: async () => {
          approve.disabled = true; fill(errorBox);
          try { await api('POST', `/businesses/${business.id}/nerds/${id}/proposals/${proposal.id}/approve`, {}); if (isCurrent()) await ctx.refreshBusiness(); }
          catch (error) { if (isCurrent()) fill(errorBox, el('div', {class: 'error', text: error.message})); approve.disabled = false; }
        }});
        fill(review, el('div', {class: 'proposal'}, el('h3', {text: 'Review before enabling ' + name}),
          el('p', {text: plugin.setupDescription || name + ' uses your existing business records. Its required fields will be protected while it is enabled.'}),
          el('ul', null, Object.entries(payload).map(([role, binding]) => {
            const entity = entities.find(entity => entity.key === binding.entity);
            return el('li', null, el('strong', {text: label(role) + ' → ' + entity.label}), el('div', {class: 'muted', text: Object.entries(binding.fields).map(([meaning, key]) => label(meaning) + ': ' + entity.fields.find(field => field.key === key).label).join(' · ')}));
          })), errorBox, el('div', {class: 'row'}, approve, el('button', {text: 'Keep editing', onclick: () => fill(review)}))));
      } catch (error) { if (isCurrent()) fill(errors, el('div', {class: 'error', text: error.message})); }
      finally { button.disabled = false; }
    }}, el('div', {class: 'logistics-mapping'}, cards), errors, el('div', {class: 'logistics-actions'}, button));
    fill(target, nerd.bindings ? el('div', {class: 'notice', text: 'Saved module history is retained. Review the original mappings to re-enable this workspace.'}) : null,
      el('h3', {text: 'Use your existing business records'}), el('p', {class: 'muted', text: 'Review the suggested record and field matches. Add any missing fields in CRM before enabling this capability.'}), form, review);
  }
  function helpers(ctx) {
    const {el, fill, business, api, id, isCurrent} = ctx, path = `/businesses/${business.id}/${id}`;
    function table(name, rows, columns) {
      return el('section', {class: 'logistics-card'}, el('h3', {text: name}), rows?.length ? el('div', {class: 'table-wrap'}, el('table', null,
        el('thead', null, el('tr', null, columns.map(([name]) => el('th', {text: name})))), el('tbody', null, rows.map(row => el('tr', null, columns.map(([, get]) => el('td', {text: String(get(row) ?? '')})))))))
        : el('p', {class: 'muted', text: 'No ' + name.toLowerCase() + ' yet.'}));
    }
    function input(def) {
      const control = def.type === 'select' ? el('select', {required: !def.optional}, el('option', {value: '', text: 'Choose…'}), (def.options || []).map(([value, text]) => el('option', {value: String(value), text})))
        : el(def.type === 'textarea' ? 'textarea' : 'input', {type: def.type === 'money' ? 'text' : def.type === 'integer' ? 'number' : def.type || 'text', value: def.value ?? '', required: !def.optional,
          min: def.type === 'integer' ? (def.min ?? 1) : def.min, max: def.type === 'integer' ? Number.MAX_SAFE_INTEGER : def.max, step: def.type === 'integer' ? 1 : def.step,
          inputMode: def.type === 'money' ? 'decimal' : def.inputMode, pattern: def.type === 'money' ? '\\d+(\\.\\d{1,2})?' : def.pattern, rows: def.type === 'textarea' ? 3 : null});
      control.setAttribute('aria-label', def.label);
      if (def.type === 'select' && def.value !== undefined) control.value = String(def.value);
      return control;
    }
    function form(name, fields, operation, transform, explanation) {
      const controls = {}, errorBox = el('div', {role: 'alert'}); let previous;
      const nodes = Object.entries(fields).map(([key, def]) => {
        const control = input(def); controls[key] = {control, def};
        return el('label', {class: 'logistics-field'}, def.label + (def.optional ? ' (optional)' : ''), control, def.help ? el('small', {text: def.help}) : null);
      });
      const button = el('button', {type: 'submit', class: 'primary', text: name});
      return el('form', {class: 'logistics-card', onsubmit: async event => {
        event.preventDefault(); button.disabled = true; fill(errorBox);
        try {
          const payload = {};
          for (const [key, {control, def}] of Object.entries(controls)) {
            if (!control.value && def.optional) continue;
            payload[key] = def.type === 'money' ? minor(control.value) : def.type === 'integer' ? Number(control.value) : def.type === 'select' && /^\d+$/.test(control.value) ? Number(control.value) : control.value.trim();
          }
          const body = transform ? transform(payload) : payload, fingerprint = JSON.stringify(body);
          if (!previous || previous.fingerprint !== fingerprint) previous = {fingerprint, key: key()};
          body.idempotency_key = previous.key;
          await api('POST', path + '/commands/' + operation, body);
          if (isCurrent()) { notices.set(business.id + ':' + id, name + ' completed.'); await render(ctx); }
        } catch (error) { if (isCurrent()) fill(errorBox, el('div', {class: 'error', text: error.message})); }
        finally { button.disabled = false; }
      }}, el('h3', {text: name}), explanation ? el('p', {class: 'muted', text: explanation}) : null, el('div', {class: 'logistics-form-grid'}, nodes), errorBox, button);
    }
    function action(name, operation, buildPayload, contents, explanation) {
      const errorBox = el('div', {role: 'alert'}), button = el('button', {type: 'submit', class: 'primary', text: name}); let previous;
      return el('form', {class: 'logistics-card', onsubmit: async event => {
        event.preventDefault(); button.disabled = true; fill(errorBox);
        try {
          const payload = buildPayload(), fingerprint = JSON.stringify(payload);
          if (!previous || previous.fingerprint !== fingerprint) previous = {fingerprint, key: key()};
          payload.idempotency_key = previous.key;
          await api('POST', path + '/commands/' + operation, payload);
          if (isCurrent()) { notices.set(business.id + ':' + id, name + ' completed.'); await render(ctx); }
        } catch (error) { if (isCurrent()) fill(errorBox, el('div', {class: 'error', text: error.message})); }
        finally { button.disabled = false; }
      }}, el('h3', {text: name}), explanation ? el('p', {class: 'muted', text: explanation}) : null, contents, errorBox, button);
    }
    function tabs(names, renderTab) {
      const nav = el('div', {class: 'tabs logistics-tabs', role: 'tablist'}), body = el('div');
      const viewKey = business.id + ':' + id;
      const select = name => { selectedTabs.set(viewKey, name); fill(nav, names.map(item => el('button', {role: 'tab', class: 'tab' + (item === name ? ' active' : ''), 'aria-selected': String(item === name), text: item, onclick: () => select(item)}))); fill(body, renderTab(name)); };
      select(names.includes(selectedTabs.get(viewKey)) ? selectedTabs.get(viewKey) : names[0]); return el('div', null, nav, body);
    }
    const field = (label, type = 'text', extra = {}) => ({label, type, ...extra});
    const select = (label, options, optional = false) => field(label, 'select', {options, optional});
    return {el, fill, table, form, action, field, select, tabs, money, minor, day, label, input, key, path,
      refresh: () => render(ctx), api, notify: text => notices.set(business.id + ':' + id, text)};
  }
  function settings(ctx) {
    const {el, fill, api, business, id, name, isCurrent} = ctx, confirmation = el('div');
    return el('details', {class: 'logistics-settings'}, el('summary', {text: 'Module settings'}),
      el('p', {class: 'muted', text: 'Disable ' + name + ' while retaining its history and the shared CRM records.'}),
      el('button', {text: 'Review disabling ' + name, onclick: () => {
        const errors = el('div', {role: 'alert'});
        const confirm = el('button', {class: 'danger', text: 'Confirm disable', onclick: async () => {
          confirm.disabled = true;
          try { await api('POST', `/businesses/${business.id}/nerds/${id}/disable`, {confirm: true}); notices.delete(business.id + ':' + id); if (isCurrent()) await ctx.refreshBusiness(); }
          catch (error) { if (isCurrent()) fill(errors, el('div', {class: 'error', text: error.message})); confirm.disabled = false; }
        }});
        fill(confirmation, el('div', {class: 'confirm'}, el('p', {text: 'Disable ' + name + '? Its saved history will be retained.'}), errors, el('div', {class: 'row'}, confirm, el('button', {text: 'Cancel', onclick: () => fill(confirmation)}))));
      }}), confirmation);
  }
  function reset() { ++generation; notices.clear(); selectedTabs.clear(); for (const plugin of registry.values()) plugin.reset?.(); }
  return {register, render, reset};
})();
