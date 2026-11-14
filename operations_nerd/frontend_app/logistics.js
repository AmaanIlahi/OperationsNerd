'use strict';

// The host supplies its session API and DOM helpers. All record references stay
// within the selected business; suggestions never enable a module by themselves.
const LogisticsUI = (() => {
  const roleDefinitions = {
    supplier: {label: 'Suppliers', names: ['supplier', 'vendor'], fields: {name: ['Name', 'text'], address: ['Address', 'text', true]}, links: {}},
    customer: {label: 'Customers', names: ['customer', 'client'], fields: {name: ['Name', 'text'], address: ['Shipping address', 'text', true]}, links: {}},
    item: {label: 'Items', names: ['item', 'product', 'toy'], fields: {name: ['Name', 'text'], sku: ['SKU', 'text'], unit: ['Unit', 'text']}, links: {}},
    location: {label: 'Locations', names: ['location', 'warehouse'], fields: {name: ['Name', 'text'], address: ['Address', 'text', true]}, links: {}},
    order: {label: 'Customer orders', names: ['customer order', 'order'], fields: {number: ['Order number', 'text'], currency: ['Currency', 'text'], date: ['Order date', 'date', true]}, links: {customer: 'customer'}},
    order_line: {label: 'Order lines', names: ['order line', 'customer order line'], fields: {quantity: ['Quantity', 'number'], unit_price_minor: ['Unit price (minor units)', 'number'], required_by: ['Required by', 'date'], budget_minor: ['Budget (minor units)', 'number', true]}, links: {order: 'order', item: 'item'}},
  };
  const tabs = ['Orders', 'Purchasing', 'Receiving', 'Shipping', 'Stock', 'Import', 'Reports'];
  const tabByBusiness = new Map();
  const noticesByBusiness = new Map();
  const importsByBusiness = new Map();
  const reportDateByBusiness = new Map();
  let renderGeneration = 0;
  const normalize = text => String(text).toLowerCase().replace(/[^a-z0-9]/g, '').replace(/s$/, '');
  const title = text => String(text).replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
  const today = () => { const d = new Date(); return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-'); };
  const requestKey = () => 'web-' + (globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2));
  const identifier = row => row && (row.id ?? row.reservation_id ?? row.shipment_id ?? row.po_line_id ?? row.quote_version_id ?? row.demand_line_id ?? row.purchase_order_id);
  const asId = value => /^\d+$/.test(String(value)) ? Number(value) : value;
  const refName = row => row && (row.display || row.snapshot?.name || row.snapshot?.sku || row.external_key || '#' + row.id);
  const cleanRows = rows => Array.isArray(rows) ? rows : [];
  const money = value => Number.isInteger(Number(value)) ? (Number(value) / 100).toFixed(2) : String(value ?? '');

  function parseMoney(value) {
    const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(value).trim());
    if (!match) throw new Error('Enter a nonnegative amount with at most two decimal places.');
    const result = Number(match[1]) * 100 + Number((match[2] || '').padEnd(2, '0'));
    if (!Number.isSafeInteger(result)) throw new Error('This amount is too large.');
    return result;
  }

  async function render(ctx) {
    const baseIsCurrent = ctx.baseIsCurrent || ctx.isCurrent;
    const generation = ++renderGeneration;
    ctx = {...ctx, baseIsCurrent, isCurrent: () => baseIsCurrent() && generation === renderGeneration};
    const {el, fill, container, business, api, isCurrent} = ctx;
    const path = '/businesses/' + business.id;
    fill(container, el('p', {class: 'muted', text: 'Loading logistics…'}));
    try {
      const result = await api('GET', path + '/nerds');
      if (!isCurrent()) return;
      const nerd = cleanRows(result.nerds).find(n => n.id === 'logistics');
      if (!nerd) throw new Error('Logistics is not available in this installation.');
      const heading = el('div', {class: 'row logistics-head'},
        el('div', null, el('span', {class: 'logistics-kicker', text: 'Business operations'}), el('h2', {text: 'Logistics'}), el('p', {class: 'muted', text: business.name})),
        el('span', {class: 'spacer'}),
        el('span', {class: 'badge' + (nerd.status === 'enabled' ? ' ok' : ''), text: nerd.status === 'enabled' ? 'Enabled' : 'Not enabled'}));
      const body = el('div');
      fill(container, heading, body);
      if (nerd.available === false) {
        fill(body, el('div', {class: 'warn', text: 'Logistics has not been configured for this installation. Your CRM records are still available.'}));
      } else if (nerd.status === 'enabled') {
        await api('POST', path + '/logistics/commands/sync_records', {idempotency_key: requestKey()});
        if (!isCurrent()) return;
        await workspace(ctx, body, nerd);
      } else {
        mapping(ctx, body, nerd);
      }
    } catch (error) {
      if (isCurrent()) fill(container, el('h2', {text: 'Logistics'}), el('div', {class: 'error', role: 'alert', text: error.message}),
        el('button', {text: 'Try again', onclick: () => render(ctx)}));
    }
  }

  function mapping(ctx, target, nerd) {
    const {el, fill, business, api, isCurrent} = ctx;
    const spec = business.spec;
    const entities = spec.entities.filter(e => !e.archived);
    const bindings = {};
    const chosen = {};
    const slots = {};
    const errorBox = el('div', {class: 'logistics-status', role: 'alert'});
    const proposalBox = el('div');
    const matchingEntity = def => {
      const matches = entities.filter(e => def.names.some(name => normalize(e.label) === normalize(name)));
      return matches.length === 1 ? matches[0].key : '';
    };
    for (const [role, def] of Object.entries(roleDefinitions)) {
      const old = nerd.bindings?.[role];
      bindings[role] = {entity: old?.entity || matchingEntity(def), fields: {...old?.fields}, links: {...old?.links}};
    }
    function select(options, value, change, label) {
      const node = el('select', {'aria-label': label, onchange: change},
        el('option', {value: '', text: 'Choose…'}), options.map(([key, text]) => el('option', {value: key, text})));
      node.value = value || '';
      return node;
    }
    function drawFields(role) {
      const def = roleDefinitions[role], binding = bindings[role];
      const entity = entities.find(e => e.key === binding.entity);
      const fields = entity ? entity.fields.filter(f => !f.archived) : [];
      const nodes = [];
      for (const [key, [label, type, optional]] of Object.entries(def.fields)) {
        const eligible = fields.filter(f => type === 'text' ? ['text', 'long_text', 'select'].includes(f.type) : f.type === type);
        if (!eligible.some(f => f.key === binding.fields[key])) {
          const aliases = [label, key, key === 'number' ? 'number' : '', key === 'unit_price_minor' ? 'unit price cents' : ''].filter(Boolean);
          const matches = eligible.filter(f => aliases.some(name => normalize(f.label) === normalize(name)));
          binding.fields[key] = matches.length === 1 ? matches[0].key : '';
        }
        const control = select(eligible.map(f => [f.key, f.label]), binding.fields[key], ev => { binding.fields[key] = ev.target.value; fill(proposalBox); }, def.label + ': ' + label);
        control.required = !optional;
        nodes.push(el('label', {class: 'logistics-field'}, label + (optional ? ' (optional)' : ' *'), control));
      }
      for (const [key, targetRole] of Object.entries(def.links)) {
        const eligible = spec.links.filter(l => !l.archived && l.from === binding.entity && l.to === bindings[targetRole].entity && ['many_to_one', 'one_to_one'].includes(l.cardinality));
        if (!eligible.some(l => l.key === binding.links[key])) binding.links[key] = eligible.length === 1 ? eligible[0].key : '';
        const control = select(eligible.map(l => [l.key, l.label]), binding.links[key], ev => { binding.links[key] = ev.target.value; fill(proposalBox); }, def.label + ': ' + title(key) + ' link');
        control.required = true;
        nodes.push(el('label', {class: 'logistics-field'}, title(key) + ' link *', control));
      }
      fill(slots[role], nodes);
    }
    const cards = Object.entries(roleDefinitions).map(([role, def]) => {
      slots[role] = el('div');
      chosen[role] = select(entities.map(e => [e.key, e.label]), bindings[role].entity, ev => {
        bindings[role] = {entity: ev.target.value, fields: {}, links: {}};
        for (const r of Object.keys(roleDefinitions)) drawFields(r);
        fill(proposalBox);
      }, def.label + ' record type');
      chosen[role].required = true;
      return el('fieldset', null, el('legend', {text: def.label}), el('label', {class: 'logistics-field'}, 'Record type *', chosen[role]), slots[role]);
    });
    for (const role of Object.keys(roleDefinitions)) drawFields(role);
    const submit = el('button', {class: 'primary', type: 'submit', text: 'Review logistics setup'});
    const form = el('form', {onsubmit: async event => {
      event.preventDefault();
      fill(errorBox); fill(proposalBox); submit.disabled = true;
      try {
        const payload = Object.fromEntries(Object.entries(bindings).map(([role, b]) => [role, {
          entity: b.entity, fields: Object.fromEntries(Object.entries(b.fields).filter(([, value]) => value)),
          links: Object.fromEntries(Object.entries(b.links).filter(([, value]) => value)),
        }]));
        const proposal = await api('POST', `/businesses/${business.id}/nerds/logistics/proposals`, {base_version: business.current_version, bindings: payload});
        if (!isCurrent()) return;
        reviewProposal(proposal, payload);
      } catch (error) { if (isCurrent()) fill(errorBox, el('div', {class: 'error', text: error.message})); }
      finally { submit.disabled = false; }
    }}, el('div', {class: 'logistics-mapping'}, cards), errorBox, el('div', {class: 'logistics-actions'}, submit));
    function reviewProposal(proposal, payload) {
      const lines = Object.entries(payload).map(([role, binding]) => {
        const entity = entities.find(e => e.key === binding.entity);
        const details = Object.entries(binding.fields).map(([name, key]) => title(name) + ': ' + (entity.fields.find(f => f.key === key)?.label || key));
        const links = Object.entries(binding.links).map(([name, key]) => title(name) + ': ' + (spec.links.find(l => l.key === key)?.label || key));
        return el('li', null, el('strong', {text: roleDefinitions[role].label + ' → ' + entity.label}), el('div', {class: 'muted', text: details.concat(links).join(' · ')}));
      });
      const status = el('div', {role: 'status'});
      const approve = el('button', {class: 'primary', text: 'Approve and enable logistics', onclick: async () => {
        approve.disabled = true; fill(status);
        try {
          await api('POST', `/businesses/${business.id}/nerds/logistics/proposals/${proposal.id ?? proposal.proposal_id}/approve`, {});
          if (isCurrent()) await ctx.refreshBusiness();
        } catch (error) {
          if (isCurrent()) fill(status, el('div', {class: 'error', text: error.message}));
          approve.disabled = false;
        }
      }});
      fill(proposalBox, el('div', {class: 'proposal'},
        el('h3', {text: 'Review before enabling'}),
        el('p', {text: 'Logistics will use these existing records. Fields it depends on will be protected from archiving or changing type while logistics is enabled.'}),
        el('ul', null, lines), status, el('div', {class: 'row'}, approve,
          el('button', {text: 'Keep editing', onclick: () => fill(proposalBox)}))));
      proposalBox.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    fill(target,
      nerd.bindings ? el('div', {class: 'notice', text: 'Logistics history is retained. Review the original mappings to enable this workspace again.'}) : null,
      el('h3', {text: 'Use your existing business records'}),
      el('p', {class: 'muted', text: 'Choose which records and fields logistics should use. Suggested matches are shown for you to review. Add any missing record types or fields in the CRM first.'}),
      form, proposalBox);
  }

  async function workspace(ctx, target, nerd) {
    const {el, fill, business, api, isCurrent} = ctx;
    const path = `/businesses/${business.id}/logistics`;
    const data = await api('GET', path + '/workspace');
    if (!isCurrent()) return;
    const refs = cleanRows(data.references);
    const fulfillment = data.fulfillment || {};
    const lookup = id => refName(refs.find(ref => String(ref.id) === String(id))) || '#' + id;
    const references = kind => refs.filter(r => r.kind === kind && !r.valid_to);
    const demands = cleanRows(data.demands), quotes = cleanRows(data.quotes), purchases = cleanRows(data.purchase_orders), lines = cleanRows(data.purchase_lines), allocations = cleanRows(data.allocations);
    const reservations = cleanRows(fulfillment.reservations), shipments = cleanRows(fulfillment.shipments), orders = cleanRows(fulfillment.orders);
    const orderLabel = reference => orders.find(order => order.order_ref === reference)?.order_number
      || cleanRows(data.orders).find(order => order.order_ref === reference)?.order_number || reference;
    const notice = el('div', {role: 'status'}), body = el('div'), nav = el('div', {class: 'tabs logistics-tabs', role: 'tablist', 'aria-label': 'Logistics steps'});
    if (noticesByBusiness.has(business.id)) fill(notice, el('div', {class: 'notice', text: noticesByBusiness.get(business.id)}));
    const status = el('div', {class: 'row'},
      el('div', {class: 'logistics-stat'}, el('strong', {text: String(demands.length)}), 'Demand lines'),
      el('div', {class: 'logistics-stat'}, el('strong', {text: String(purchases.length)}), 'Purchase orders'),
      el('div', {class: 'logistics-stat'}, el('strong', {text: String(shipments.length)}), 'Shipments'),
      el('span', {class: 'spacer'}), el('button', {text: 'Refresh', onclick: () => render(ctx)}));
    const settings = el('details', {class: 'logistics-settings'});
    const confirmation = el('div');
    const reviewDisable = el('button', {type: 'button', text: 'Review disabling logistics', onclick: () => {
      const errorBox = el('div', {role: 'alert'});
      const confirm = el('button', {type: 'button', class: 'danger', text: 'Confirm disable', onclick: async () => {
        confirm.disabled = true; fill(errorBox);
        try {
          await api('POST', `/businesses/${business.id}/nerds/logistics/disable`, {confirm: true});
          noticesByBusiness.delete(business.id);
          if (isCurrent()) await ctx.refreshBusiness();
        } catch (error) { if (isCurrent()) fill(errorBox, el('div', {class: 'error', text: error.message})); confirm.disabled = false; }
      }});
      fill(confirmation, el('div', {class: 'confirm'}, el('p', {text: 'Disable logistics for ' + business.name + '? Saved purchasing, stock and shipment history will be retained. Submitted orders remain protected.'}),
        errorBox, el('div', {class: 'row'}, confirm, el('button', {type: 'button', text: 'Cancel', onclick: () => fill(confirmation)}))));
    }});
    fill(settings, el('summary', {text: 'Module settings'}),
      el('p', {class: 'muted', text: 'Disable this workspace and its commands while keeping its history. Re-enabling uses the original record mappings.'}), reviewDisable, confirmation);
    fill(target, status, notice, nav, body, settings);
    function table(titleText, rows, columns) {
      return el('section', {class: 'logistics-card'}, el('h3', {text: titleText}), rows.length
        ? el('div', {class: 'table-wrap'}, el('table', null,
          el('thead', null, el('tr', null, columns.map(([label]) => el('th', {text: label})))),
          el('tbody', null, rows.map(row => el('tr', null, columns.map(([, get]) => el('td', {text: String(get(row) ?? '')})))))))
        : el('p', {class: 'logistics-empty', text: 'No ' + titleText.toLowerCase() + ' yet.'}));
    }
    const optionRows = (rows, label) => rows.map(row => [identifier(row), label(row)]);
    const refOptions = kind => references(kind).map(row => [row.id, refName(row)]);
    function field(label, type, options, value, optional) { return {label, type, options, value, optional}; }
    const qty = label => field(label || 'Quantity', 'integer', null, '', false);
    const selectField = (label, options, optional) => field(label, 'select', options, '', optional);
    const date = label => field(label, 'date', null, today());
    const text = (label, value, optional) => field(label, 'text', null, value || '', optional);
    function commandForm(name, fields, operation, transform) {
      const controls = {}, errorBox = el('div', {role: 'alert'});
      let previous = null;
      const nodes = Object.entries(fields).map(([key, def]) => {
        let control;
        if (def.type === 'select') {
          control = el('select', {required: !def.optional}, el('option', {value: '', text: 'Choose…'}),
            (def.options || []).map(([value, label]) => el('option', {value: String(value), text: label})));
        } else {
          control = el('input', {type: def.type === 'money' ? 'text' : def.type === 'integer' ? 'number' : def.type,
            inputMode: def.type === 'money' ? 'decimal' : null, pattern: def.type === 'money' ? '\\d+(\\.\\d{1,2})?' : null,
            min: def.type === 'integer' ? '1' : null, step: def.type === 'integer' ? '1' : null, value: def.value ?? '', required: !def.optional});
        }
        control.setAttribute('aria-label', def.label);
        controls[key] = {control, def};
        return el('label', {class: 'logistics-field'}, def.label + (def.optional ? ' (optional)' : ''), control,
          def.type === 'money' ? el('small', {text: 'Amount in currency units, for example 10.00.'}) : null);
      });
      const button = el('button', {class: 'primary', type: 'submit', text: name});
      const form = el('form', {class: 'logistics-card', onsubmit: async event => {
        event.preventDefault(); button.disabled = true; fill(errorBox);
        try {
          const payload = {};
          for (const [key, {control, def}] of Object.entries(controls)) {
            if (!control.value && def.optional) continue;
            payload[key] = def.type === 'money' ? parseMoney(control.value) : def.type === 'integer' ? Number(control.value) : def.type === 'select' ? asId(control.value) : def.type === 'datetime-local' ? new Date(control.value).toISOString() : control.value.trim();
          }
          const body = transform ? transform(payload) : payload;
          const fingerprint = JSON.stringify(body);
          if (!previous || previous.fingerprint !== fingerprint) previous = {fingerprint, key: requestKey()};
          body.idempotency_key = previous.key;
          const url = typeof operation === 'function' ? operation(body) : path + '/commands/' + operation;
          await api('POST', url, body);
          if (!isCurrent()) return;
          noticesByBusiness.set(business.id, name + ' completed.');
          await render(ctx);
        } catch (error) { if (isCurrent()) fill(errorBox, el('div', {class: 'error', text: error.message})); }
        finally { button.disabled = false; }
      }}, el('h3', {text: name}), el('div', {class: 'logistics-form-grid'}, nodes), errorBox, button);
      return form;
    }
    function purchasing() {
      return el('div', null, el('div', {class: 'logistics-grid'},
        commandForm('Add supplier quote', {
          supplier_id: selectField('Supplier', refOptions('supplier')), item_id: selectField('Item', refOptions('item')),
          quantity_priced: qty('Priced quantity'), capacity_quantity: qty('Available quantity'), price_minor: field('Unit price', 'money'),
          currency: text('Currency', 'USD'), expected_date: date('Expected delivery'),
          expires_at: field('Quote expires', 'datetime-local'), quote_ref: text('Supplier quote reference', '', true),
        }, 'create_quote', payload => ({...payload, currency: payload.currency.toUpperCase(), pricing_kind: 'unit'})),
        commandForm('Create purchase draft', {supplier_id: selectField('Supplier', refOptions('supplier')), currency: text('Currency', 'USD')}, 'create_purchase_order', p => ({...p, currency: p.currency.toUpperCase()})),
        commandForm('Add purchase line', {
          purchase_order_id: selectField('Purchase order', optionRows(purchases.filter(p => p.status === 'draft'), p => '#' + identifier(p) + ' · ' + lookup(p.supplier_id))),
          quote_version_id: selectField('Quote', optionRows(quotes, q => '#' + identifier(q) + ' · ' + lookup(q.item_id) + ' · ' + money(q.price_minor) + ' ' + (q.currency || ''))), quantity: qty(),
        }, 'add_purchase_line'),
        commandForm('Allocate purchase to demand', {
          po_line_id: selectField('Purchase line', optionRows(lines, p => '#' + identifier(p) + ' · ' + lookup(p.item_id))),
          demand_line_id: selectField('Customer demand', optionRows(demands, d => '#' + identifier(d) + ' · ' + lookup(d.item_id) + ' · ' + (orderLabel(d.order_ref) || ''))), quantity: qty(),
          cost_share_minor: field('Allocated cost share', 'money', null, '', true),
        }, 'allocate_purchase_line'),
        commandForm('Approve purchase order', {purchase_order_id: selectField('Draft purchase order', optionRows(purchases.filter(p => p.status === 'draft'), p => '#' + identifier(p) + ' · ' + lookup(p.supplier_id)))}, 'approve_purchase_order')),
        table('Supplier quotes', quotes, [['Quote', identifier], ['Supplier', q => lookup(q.supplier_id)], ['Item', q => lookup(q.item_id)], ['Price', q => money(q.price_minor) + ' ' + q.currency], ['Delivery', q => q.expected_date], ['Expiry', q => q.expires_at]]),
        table('Purchase orders', purchases, [['Order', identifier], ['Supplier', p => lookup(p.supplier_id)], ['Status', p => p.status], ['Currency', p => p.currency]]),
        table('Purchase lines', lines, [['Line', identifier], ['Purchase order', p => p.purchase_order_id], ['Item', p => lookup(p.item_id)], ['Quantity', p => p.quantity], ['Received', p => p.received_quantity ?? p.quantity_received]]));
    }
    function receiving() {
      const allocationOptions = optionRows(allocations, a => '#' + identifier(a) + ' · line ' + a.po_line_id + ' → demand ' + a.demand_line_id + ' · ' + a.quantity + ' units');
      return el('div', null, el('p', {class: 'muted', text: 'Receive goods against an approved purchase allocation. Receiving and reserving for that customer demand happen together.'}),
        commandForm('Receive delivery', {
          delivery_ref: text('Delivery reference'), purchase_allocation_id: selectField('Purchase allocation', allocationOptions),
          location_id: selectField('Stock location', refOptions('location')), quantity: qty('Quantity received'), received_on: date('Received on'),
          lot_number: text('Lot number', '', true), expiry_date: field('Expiry date', 'date', null, '', true),
        }, 'receive_delivery', p => {
          const allocation = allocations.find(a => String(identifier(a)) === String(p.purchase_allocation_id));
          const line = lines.find(l => String(identifier(l)) === String(allocation?.po_line_id));
          if (!allocation || !line) throw new Error('Refresh and choose a current purchase allocation.');
          const receipt = {po_line_id: allocation.po_line_id, item_id: line.item_id, location_id: p.location_id, quantity: p.quantity,
            allocations: [{purchase_allocation_id: identifier(allocation), demand_line_id: allocation.demand_line_id, quantity: p.quantity}]};
          if (p.lot_number) receipt.lot_number = p.lot_number;
          if (p.expiry_date) receipt.expiry_date = p.expiry_date;
          return {delivery_ref: p.delivery_ref, received_on: p.received_on, lines: [receipt]};
        }),
        table('Purchase allocations', allocations, [['Allocation', identifier], ['Purchase line', a => a.po_line_id], ['Demand', a => a.demand_line_id], ['Quantity', a => a.quantity], ['Received', a => a.received_quantity ?? a.quantity_received]]));
    }
    function shipping() {
      const orderOptions = orders.map(order => [order.order_ref, order.order_number || order.order_ref]);
      return el('div', null, el('div', {class: 'logistics-grid'},
        commandForm('Reserve available stock', {
          demand_line_id: selectField('Customer demand', optionRows(demands, d => '#' + identifier(d) + ' · ' + lookup(d.item_id) + ' · ' + orderLabel(d.order_ref))), quantity: qty(),
          location_id: selectField('Location', refOptions('location')), as_of: date('Reservation date')}, 'reserve', p => {
            const demand = demands.find(d => String(identifier(d)) === String(p.demand_line_id));
            if (!demand?.external_key) throw new Error('Refresh and choose a current customer demand.');
            return {...p, item_id: demand.item_id, order_ref: demand.external_key};
          }),
        commandForm('Create shipment', {shipment_number: text('Shipment number'), order_ref: selectField('Customer order', orderOptions)}, 'create_shipment'),
        commandForm('Add shipment line', {shipment_id: selectField('Draft shipment', optionRows(shipments.filter(s => s.status === 'draft'), s => s.shipment_number || '#' + identifier(s))),
          reservation_id: selectField('Stock reservation', optionRows(reservations.filter(r => r.status === 'active' && (r.available_to_plan ?? r.remaining_quantity ?? r.quantity) > 0), r => '#' + identifier(r) + ' · ' + lookup(r.item_id) + ' · ' + (r.available_to_plan ?? r.remaining_quantity ?? r.quantity) + ' available')), quantity: qty()}, 'add_shipment_line'),
        commandForm('Dispatch shipment', {shipment_id: selectField('Draft shipment', optionRows(shipments.filter(s => s.status === 'draft'), s => s.shipment_number || '#' + identifier(s))),
          carrier: text('Carrier'), as_of: date('Dispatch date'), tracking_ref: text('Tracking reference'), label_ref: text('Label reference', '', true)}, 'dispatch'),
        commandForm('Record delivery update', {shipment_id: selectField('Shipment', optionRows(shipments.filter(s => s.status !== 'draft'), s => s.shipment_number || '#' + identifier(s))),
          event: selectField('Update', ['in_transit', 'out_for_delivery', 'delivery_failed', 'exception', 'delivered'].map(v => [v, title(v)])),
          occurred_at: field('Occurred at', 'datetime-local'), detail: text('Notes', '', true)}, 'record_tracking_event')),
        table('Shipments', shipments, [['Shipment', s => s.shipment_number || identifier(s)], ['Order', s => orderLabel(s.order_ref)], ['Status', s => title(s.status)], ['Carrier', s => s.carrier], ['Tracking', s => s.tracking_ref]]),
        table('Reservations', reservations, [['Reservation', identifier], ['Item', r => lookup(r.item_id)], ['Order', r => orderLabel(r.order_ref)], ['Quantity', r => r.quantity], ['Shipped', r => r.quantity_shipped], ['Status', r => title(r.status)]]));
    }
    async function orderView() {
      const binding = nerd.bindings?.order;
      if (!binding?.entity) return el('div', {class: 'warn', text: 'The customer-order mapping is missing. Refresh to check logistics setup.'});
      const result = Array.isArray(data.orders)
        ? {records: data.orders.filter(order => !order.submitted), total: data.orders.filter(order => !order.submitted).length}
        : await api('GET', `/businesses/${business.id}/records/${binding.entity}?limit=500`);
      if (!isCurrent()) return null;
      return el('div', null,
        el('p', {class: 'muted', text: 'Create customers, items, orders and order lines in your CRM. Submit a completed customer order here to create its logistics demand.'}),
        el('div', {class: 'row logistics-actions'},
          el('button', {text: 'Open customer orders', onclick: () => ctx.openRecords(binding.entity)}),
          nerd.bindings?.order_line?.entity ? el('button', {text: 'Open order lines', onclick: () => ctx.openRecords(nerd.bindings.order_line.entity)}) : null),
        commandForm('Submit customer order', {record_id: selectField('Customer order', cleanRows(result.records).map(r => [r.id, r.display || '#' + r.id]))}, p => path + '/orders/' + p.record_id + '/submit', p => ({record_id: p.record_id})),
        result.total > 500 ? el('p', {class: 'warn', text: 'Showing the first 500 customer orders. Use CRM to find and check additional records.'}) : null,
        table('Submitted orders', orders, [['Order', o => o.order_number || o.order_ref], ['Customer', o => o.customer_name], ['Lines', o => cleanRows(o.lines).length], ['Fulfillment', o => [...new Set(cleanRows(o.lines).map(line => title(line.status || 'pending')))].join(', ')]]),
        table('Customer demand', demands, [['Demand', identifier], ['Order', d => orderLabel(d.order_ref)], ['Item', d => lookup(d.item_id)], ['Quantity', d => d.quantity ?? d.requested_quantity], ['Required by', d => d.required_by]]));
    }
    function stockView() {
      return el('div', null,
        commandForm('Record opening stock', {item_id: selectField('Item', refOptions('item')), location_id: selectField('Location', refOptions('location')),
          quantity: qty('Opening quantity'), source_ref: text('Count sheet reference'), lot_number: text('Lot number', '', true), expiry_date: field('Expiry date', 'date', null, '', true)}, 'open_stock'),
        table('Stock balances', cleanRows(data.stock || fulfillment.stock), [['Item', r => lookup(r.item_id)], ['Location', r => lookup(r.location_id)], ['On hand', r => r.on_hand], ['Reserved', r => r.reserved], ['Available', r => r.available]]));
    }
    async function importView() {
      let state = importsByBusiness.get(business.id);
      if (!state) {
        state = {kind: 'customers', csv: '', preview: null, key: null, keyFor: null};
        importsByBusiness.set(business.id, state);
      }
      const previewBox = el('div'), errors = el('div', {role: 'alert'});
      const source = el('textarea', {rows: 10, value: state.csv, spellcheck: false, wrap: 'off', class: 'logistics-csv-source', 'aria-label': 'CSV contents'});
      const kind = el('select', {'aria-label': 'Import type'}, el('option', {value: 'customers', text: 'Customers'}), el('option', {value: 'orders', text: 'Customer orders and lines'}), el('option', {value: 'shipments', text: 'Draft shipments and lines'}));
      kind.value = state.kind;
      const download = el('a', {class: 'logistics-download', text: 'Download CSV template', download: state.kind + '-template.csv', href: '/api' + path + '/imports/' + state.kind + '/template'});
      const file = el('input', {type: 'file', accept: '.csv,text/csv', 'aria-label': 'Choose CSV file'});
      const previewButton = el('button', {type: 'button', class: 'primary', text: 'Preview import'});
      const kindHelp = el('p', {class: 'muted'});
      const updateHelp = () => { kindHelp.textContent = state.kind === 'shipments'
        ? 'Imported shipments stay as drafts. Review their lines, then dispatch them from Shipping. Importing a draft does not move stock.'
        : state.kind === 'orders' ? 'Imported orders stay as drafts in the CRM. Review them, then submit them from Orders to begin fulfillment.'
          : 'New customers are added to this business’s CRM after you approve the preview.'; };
      updateHelp();
      let previewSequence = 0;
      const invalidate = () => { ++previewSequence; state.preview = null; fill(previewBox); fill(errors); };
      source.addEventListener('input', () => { state.csv = source.value; invalidate(); });
      kind.addEventListener('change', () => {
        state.kind = kind.value; invalidate();
        download.href = '/api' + path + '/imports/' + state.kind + '/template';
        download.download = state.kind + '-template.csv';
        updateHelp(); drawReferencePanel();
      });
      file.addEventListener('change', async () => {
        const selected = file.files[0];
        if (!selected) return;
        invalidate();
        const sequence = previewSequence;
        try {
          if (selected.size > 1024 * 1024) throw new Error('Use a CSV file smaller than 1 MB.');
          const text = await selected.text();
          if (!isCurrent() || sequence !== previewSequence) return;
          source.value = text; state.csv = text;
        } catch (error) { fill(errors, el('div', {class: 'error', text: error.message})); }
      });
      function drawPreview(preview) {
        const valid = preview.summary?.valid === true && cleanRows(preview.errors).length === 0;
        const summary = preview.summary || {};
        const heading = el('div', {class: valid ? 'notice' : 'error', text: valid
          ? `${summary.total_rows} CSV row(s) checked. ${summary.create_count} to create; ${summary.skip_count} already imported.`
          : 'Correct the errors below, then preview the file again. Nothing has been imported.'});
        const commitStatus = el('div', {role: 'status'});
        const approve = el('button', {class: 'primary', type: 'button', text: 'Approve and import', disabled: !valid, onclick: async () => {
          if (state.preview !== preview || source.value !== state.csv) return;
          approve.disabled = true; kind.disabled = true; source.disabled = true; file.disabled = true; previewButton.disabled = true;
          fill(commitStatus);
          const frozen = state.csv;
          const frozenKind = state.kind;
          const fingerprint = [frozenKind, preview.base_version, preview.digest].join(':');
          if (!state.key || state.keyFor !== fingerprint) { state.key = requestKey(); state.keyFor = fingerprint; }
          try {
            const result = await api('POST', path + '/imports/' + frozenKind + '/commit', {
              csv_text: frozen, base_version: preview.base_version, digest: preview.digest, idempotency_key: state.key,
            });
            if (!isCurrent()) return;
            noticesByBusiness.set(business.id, `Import completed: ${result.created_count} created; ${result.skipped_count} already imported.`);
            state.preview = null; state.key = null; state.keyFor = null;
            await ctx.refreshBusiness();
          } catch (error) {
            if (isCurrent()) fill(commitStatus, el('div', {class: 'error', text: error.message}));
            approve.disabled = false;
          } finally { kind.disabled = false; source.disabled = false; file.disabled = false; previewButton.disabled = false; }
        }});
        const columns = state.kind === 'customers'
          ? [['Row', row => row.row], ['Reference', row => row.external_key], ['Customer', row => row.name], ['Address', row => row.address], ['Action', row => title(row.action)]]
          : state.kind === 'shipments' ? [['Row', row => row.row], ['Shipment', row => row.shipment_number], ['Order reference', row => row.order_ref], ['Reservation', row => row.reservation_id], ['Quantity', row => row.quantity], ['Action', row => title(row.action)]]
            : [['Row', row => row.row], ['Order', row => row.order_number], ['Customer record', row => row.customer_record_id], ['Item record', row => row.item_record_id], ['Quantity', row => row.quantity], ['Unit price', row => money(row.unit_price_minor) + ' ' + row.currency], ['Required by', row => row.required_by], ['Action', row => title(row.action)]];
        fill(previewBox, heading,
          cleanRows(preview.errors).length ? table('CSV errors', preview.errors, [['Row', row => row.row ?? 'File'], ['Column', row => row.field || 'General'], ['Error', row => row.message]]) : null,
          cleanRows(preview.rows).length ? table('Import preview', preview.rows, columns) : null,
          valid ? el('div', {class: 'logistics-actions'}, el('p', {class: 'muted', text: 'Review these changes before importing them into this business.'}), commitStatus, approve) : null);
      }
      previewButton.addEventListener('click', async () => {
        invalidate();
        const sequence = previewSequence, frozen = source.value, frozenKind = state.kind;
        state.csv = frozen;
        previewButton.disabled = true;
        try {
          const preview = await api('POST', path + '/imports/' + frozenKind + '/preview', {csv_text: frozen});
          if (!isCurrent() || sequence !== previewSequence) return;
          state.preview = preview; drawPreview(preview);
        } catch (error) { if (isCurrent() && sequence === previewSequence) fill(errors, el('div', {class: 'error', text: error.message})); }
        finally { previewButton.disabled = false; }
      });
      const sourcePanel = el('section', {class: 'logistics-card'}, el('h3', {text: 'Import business records'}),
        el('p', {class: 'muted', text: 'Use a template to add many records together. Orders use customer and item record IDs; shipments use reservation and order references. Enter prices as currency amounts, for example 10.00.'}), kindHelp,
        el('div', {class: 'logistics-form-grid'}, el('label', {class: 'logistics-field'}, 'Import type', kind), el('label', {class: 'logistics-field'}, 'Choose CSV file', file)),
        el('div', {class: 'logistics-actions'}, download), el('label', {class: 'logistics-field'}, 'CSV contents', source), errors,
        el('div', {class: 'logistics-actions'}, previewButton));
      const referencePanel = el('div', {class: 'logistics-grid'});
      const page = el('div', null, sourcePanel, previewBox, referencePanel);
      const roles = ['customer', 'item'];
      const recordPages = await Promise.all(roles.map(async role => {
        const binding = nerd.bindings?.[role];
        if (!binding) return {records: []};
        return api('GET', `/businesses/${business.id}/records/${binding.entity}?limit=500`);
      }));
      function drawReferencePanel() {
        referencePanel.classList.toggle('logistics-reference-wide', state.kind === 'shipments');
        fill(referencePanel, state.kind === 'shipments'
          ? table('Available shipment reservations', reservations.filter(row => row.status === 'active' && row.available_to_plan > 0),
            [['Reservation ID', row => row.reservation_id], ['Order', row => orderLabel(row.order_ref)], ['Order reference', row => row.order_ref], ['Item', row => lookup(row.item_id)], ['Available to plan', row => row.available_to_plan]])
          : roles.map((role, index) => table(role === 'customer' ? 'Customer record IDs' : 'Item record IDs', cleanRows(recordPages[index].records),
            [['Record ID', row => row.id], ['Name', row => row.values[nerd.bindings[role].fields.name] || row.display],
              ...(role === 'item' ? [['SKU', row => row.values[nerd.bindings.item.fields.sku]]] : [])])));
      }
      drawReferencePanel();
      if (state.preview) drawPreview(state.preview);
      return page;
    }
    async function reportView() {
      const selectedDate = reportDateByBusiness.get(business.id) || today();
      const report = await api('GET', path + '/reports?as_of=' + encodeURIComponent(selectedDate));
      const cutoff = el('input', {type: 'date', value: selectedDate, 'aria-label': 'Overdue comparison date'});
      const update = el('button', {text: 'Update reports', onclick: () => {
        if (!cutoff.value || !cutoff.checkValidity()) { cutoff.reportValidity(); return; }
        reportDateByBusiness.set(business.id, cutoff.value); choose('Reports');
      }});
      const totals = report.supplier_timeliness?.totals || {};
      const paymentTiming = report.customer_payment_timeliness;
      return el('div', null,
        el('section', {class: 'logistics-card'}, el('h3', {text: 'Operational reports'}),
          el('p', {class: 'muted', text: 'Stock balances are current. The selected date identifies overdue customer orders; it does not reconstruct historical inventory.'}),
          el('div', {class: 'row'}, el('label', {class: 'logistics-field'}, 'Overdue comparison date', cutoff), update)),
        table('Current inventory', cleanRows(report.inventory), [['Item', row => row.item_name || lookup(row.item_id)], ['Location', row => row.location_name || lookup(row.location_id)], ['On hand', row => row.on_hand], ['Reserved', row => row.reserved], ['Available', row => row.available]]),
        table('Outstanding purchases', cleanRows(report.outstanding_purchases), [['Purchase order', row => row.purchase_order_id], ['Line', row => row.po_line_id], ['Supplier', row => row.supplier_name], ['Item', row => row.item_name], ['Ordered', row => row.ordered_quantity], ['Received', row => row.received_quantity], ['Outstanding', row => row.outstanding_quantity], ['Expected', row => row.expected_date]]),
        table('Shipment status', cleanRows(report.shipments), [['Shipment', row => row.shipment_number || row.shipment_id], ['Order', row => row.order_number || orderLabel(row.order_ref)], ['Customer', row => row.customer_name], ['Status', row => title(row.status)], ['Carrier', row => row.carrier], ['Tracking', row => row.tracking_ref], ['Quantity', row => row.total_quantity]]),
        table('Overdue orders', cleanRows(report.overdue_orders), [['Order', row => row.order_number || orderLabel(row.order_ref)], ['Customer', row => row.customer_name], ['Item', row => row.item_name], ['Required by', row => row.required_by], ['Shipped', row => row.shipped_quantity], ['Delivered', row => row.delivered_quantity], ['Remaining', row => row.remaining_quantity], ['Days overdue', row => row.days_overdue]]),
        el('section', {class: 'logistics-card'}, el('h3', {text: 'Supplier receipt timing'}),
          el('p', {class: 'muted', text: `${totals.on_time_quantity ?? 0} on-time units out of ${totals.known_date_quantity ?? 0} units with comparable dates. ${totals.missing_date_quantity ?? 0} units have missing dates. Quantities retain the items’ configured units.`}),
          table('Received portions', cleanRows(report.supplier_timeliness?.portions), [['Supplier', row => row.supplier_name], ['Item', row => row.item_name], ['Purchase line', row => row.po_line_id], ['Quantity', row => row.quantity], ['Promised', row => row.promised_date], ['Received', row => row.received_on], ['Timing', row => row.on_time === true ? 'On time' : row.on_time === false ? 'Late' : 'Unknown']])),
        el('section', {class: 'logistics-card'}, el('h3', {text: 'Customer payment timing'}), paymentTiming
          ? el('div', null,
            el('p', {class: 'muted', text: 'Payment snapshot: ' + paymentTiming.as_of + '. This uses Accounting’s reporting date independently of the overdue-order comparison date.'}),
            el('p', {class: 'muted', text: money(paymentTiming.on_time_paid_minor) + ' ' + paymentTiming.currency + ' received on time out of ' + money(paymentTiming.eligible_paid_minor) + ' with comparable dates. ' + money(paymentTiming.late_paid_minor) + ' received late; ' + money(paymentTiming.unknown_date_paid_minor) + ' has unknown dates. Overdue unpaid: ' + money(paymentTiming.overdue_unpaid_minor) + '.'}),
            table('Recorded customer payments', cleanRows(paymentTiming.rows), [['Invoice', row => row.number], ['Customer', row => row.party_name], ['Due', row => row.due_date], ['Paid', row => row.payment_date], ['Amount', row => money(row.amount_minor) + ' ' + paymentTiming.currency], ['Timing', row => row.on_time === true ? 'On time' : row.on_time === false ? 'Late' : 'Unknown']]))
          : el('p', {class: 'muted', text: 'Enable Accounting and record customer invoice payments to view their timing here.'})),
        cleanRows(report.limitations).length ? el('div', {class: 'logistics-card'}, el('h3', {text: 'Report coverage'}), el('ul', null, report.limitations.map(text => el('li', {text})))) : null);
    }
    async function choose(tab) {
      tabByBusiness.set(business.id, tab);
      fill(nav, tabs.map((name, index) => el('button', {class: 'tab' + (name === tab ? ' active' : ''), role: 'tab', 'aria-label': name, 'aria-selected': String(name === tab), onclick: () => choose(name)},
        el('span', {class: 'logistics-tab-number', 'aria-hidden': 'true', text: String(index + 1)}), name)));
      fill(body, el('p', {class: 'muted', text: 'Loading…'}));
      try {
        const content = tab === 'Orders' ? await orderView() : tab === 'Purchasing' ? purchasing() : tab === 'Receiving' ? receiving() : tab === 'Shipping' ? shipping() : tab === 'Stock' ? stockView() : tab === 'Import' ? await importView() : await reportView();
        if (isCurrent() && tabByBusiness.get(business.id) === tab) fill(body, content);
      } catch (error) { if (isCurrent()) fill(body, el('div', {class: 'error', text: error.message})); }
    }
    await choose(tabByBusiness.get(business.id) || 'Orders');
  }

  function reset() {
    ++renderGeneration;
    tabByBusiness.clear(); noticesByBusiness.clear(); importsByBusiness.clear(); reportDateByBusiness.clear();
  }
  return {render, reset};
})();
