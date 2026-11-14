'use strict';

BusinessModules.register('accounting', {
  roles: {
    customer: {label: 'Customers', names: ['client'], fields: {name: {label: 'Name', type: 'text'}, address: {label: 'Shipping address', type: 'text', optional: true}}},
    supplier: {label: 'Suppliers', names: ['vendor'], fields: {name: {label: 'Name', type: 'text'}, address: {label: 'Address', type: 'text', optional: true}}},
  },
  setupDescription: 'Accounting uses the existing customers and suppliers. Enabling it creates a USD prototype chart of accounts and a General fund. Bills, invoices, payments and journal entries are recorded only when you explicitly submit them.',
  renderWorkspace(ctx, w, h) {
    const {el, table, form, action, field, select, money, day, label, input} = h;
    const accounts = w.accounts || [], active = accounts.filter(account => account.is_active !== false), funds = w.funds || [];
    const accountOptions = rows => rows.map(account => [account.id, account.code + ' · ' + account.name]);
    const fundOptions = rows => rows.map(fund => [fund.id, fund.code + ' · ' + fund.name]);
    const cash = active.filter(account => account.role === 'cash');
    const text = (name, value, optional) => field(name, 'text', {value, optional});
    const date = name => field(name, 'date', {value: day()});
    const amount = name => field(name, 'money');
    function block(def) {
      const control = input(def);
      return {control, node: el('label', {class: 'logistics-field'}, def.label + (def.optional ? ' (optional)' : ''), control)};
    }
    function documentForm(kind) {
      const invoice = kind === 'invoice', parties = invoice ? w.references?.customers || [] : w.references?.suppliers || [];
      const controls = {
        number: block(text(invoice ? 'Invoice number' : 'Bill number')),
        party_record_id: block(select(invoice ? 'Customer' : 'Supplier', parties.map(party => [party.record_id, party.name]))),
        issue_date: block(date('Issue date')), due_date: block(date('Due date')),
        fund_id: block(select('Fund', fundOptions(funds), true)),
      };
      const lineBox = el('div'), rows = [];
      function addLine() {
        const row = {fields: {
          description: block(text('Description')), quantity: block(field('Quantity', 'integer', {value: 1})),
          unit_price_minor: block(amount('Unit price')),
          account_id: block(select(invoice ? 'Income account' : 'Expense or asset account', accountOptions(active.filter(account => invoice ? account.account_type === 'income' : ['expense', 'asset'].includes(account.account_type))))),
        }};
        row.node = el('fieldset', {class: 'module-line'}, el('legend', {text: 'Line ' + (rows.length + 1)}),
          el('div', {class: 'logistics-form-grid'}, Object.values(row.fields).map(field => field.node)),
          el('button', {type: 'button', text: 'Remove line', onclick: () => { rows.splice(rows.indexOf(row), 1); row.node.remove(); }}));
        rows.push(row); lineBox.append(row.node);
      }
      addLine();
      return action(invoice ? 'Create draft invoice' : 'Create draft bill', 'create_' + kind, () => {
        if (!rows.length) throw new Error('Add at least one document line.');
        const body = {number: controls.number.control.value.trim(), party_record_id: Number(controls.party_record_id.control.value), issue_date: controls.issue_date.control.value,
          due_date: controls.due_date.control.value, currency: 'USD', lines: rows.map(row => ({description: row.fields.description.control.value.trim(), quantity: Number(row.fields.quantity.control.value),
            unit_price_minor: h.minor(row.fields.unit_price_minor.control.value), account_id: Number(row.fields.account_id.control.value)}))};
        if (controls.fund_id.control.value) body.fund_id = Number(controls.fund_id.control.value);
        return body;
      }, el('div', null, el('div', {class: 'logistics-form-grid'}, Object.values(controls).map(field => field.node)), lineBox,
        el('div', {class: 'logistics-actions'}, el('button', {type: 'button', text: 'Add line', onclick: addLine}))),
      'Amounts are in USD. Review and post the draft before recording an explicit payment.');
    }
    function documents(kind) {
      const invoice = kind === 'invoice', rows = invoice ? w.invoices || [] : w.bills || [];
      const options = docs => docs.map(document => [document.id, document.number + ' · ' + (document.party_name || '') + ' · ' + money(document.remaining_minor) + ' USD remaining']);
      return el('div', null,
        table(invoice ? 'Customer invoices' : 'Supplier bills', rows, [['Number', row => row.number], [invoice ? 'Customer' : 'Supplier', row => row.party_name], ['Total (USD)', row => money(row.total_minor)], ['Paid', row => money(row.paid_minor)], ['Remaining', row => money(row.remaining_minor)], ['Due', row => row.due_date], ['Status', row => label(row.status)]]),
        documentForm(kind),
        el('div', {class: 'logistics-grid'},
          form(invoice ? 'Post invoice' : 'Post bill', {document_id: select('Draft document', options(rows.filter(document => document.status === 'draft')))}, 'post_' + kind, null,
            'Posting creates the accounting entry. It does not record a payment.'),
          form(invoice ? 'Record customer payment' : 'Record supplier payment', {
            document_id: select(invoice ? 'Open invoice' : 'Open bill', options(rows.filter(document => document.status !== 'draft' && !['void', 'voided'].includes(document.status) && document.remaining_minor > 0))),
            amount_minor: amount('Payment amount'), cash_account_id: select('Cash account', accountOptions(cash)), payment_date: date('Payment date'), reference: text('Payment reference'),
          }, invoice ? 'record_invoice_payment' : 'record_bill_payment', null, 'Record money already received or paid through your bank or cash account. The journal and remaining document balance update together.'),
          form(invoice ? 'Void invoice' : 'Void bill', {document_id: select('Document', options(rows.filter(document => !['void', 'voided'].includes(document.status) && !document.paid_minor))), entry_date: date('Reversal date'), reason: text('Reason')}, 'void_' + kind)));
    }
    function journalForm() {
      const entryDate = block(date('Entry date')), memo = block(text('Memo')), list = el('div'), rows = [];
      function addLine() {
        const row = {account: block(select('Account', accountOptions(active))), fund: block(select('Fund', fundOptions(funds), true)),
          debit: block(field('Debit', 'money', {optional: true})), credit: block(field('Credit', 'money', {optional: true}))};
        row.node = el('fieldset', {class: 'module-line'}, el('legend', {text: 'Journal line ' + (rows.length + 1)}),
          el('div', {class: 'logistics-form-grid'}, row.account.node, row.fund.node, row.debit.node, row.credit.node),
          el('button', {type: 'button', text: 'Remove line', onclick: () => { rows.splice(rows.indexOf(row), 1); row.node.remove(); }}));
        rows.push(row); list.append(row.node);
      }
      addLine(); addLine();
      return action('Post journal entry', 'post_journal_entry', () => {
        if (rows.length < 2) throw new Error('A journal entry needs at least two lines.');
        return {entry_date: entryDate.control.value, memo: memo.control.value.trim(), currency: 'USD', lines: rows.map(row => {
          const line = {account_id: Number(row.account.control.value), debit_minor: row.debit.control.value ? h.minor(row.debit.control.value) : 0, credit_minor: row.credit.control.value ? h.minor(row.credit.control.value) : 0};
          if (row.fund.control.value) line.fund_id = Number(row.fund.control.value); return line;
        })};
      }, el('div', null, el('div', {class: 'logistics-form-grid'}, entryDate.node, memo.node), list,
        el('div', {class: 'logistics-actions'}, el('button', {type: 'button', text: 'Add journal line', onclick: addLine}))), 'Enter balanced debits and credits. Each fund must balance independently.');
    }
    const journal = () => el('div', null,
      table('Posted journal', w.journal || [], [['Entry', row => row.id], ['Date', row => row.entry_date], ['Memo', row => row.memo], ['Source', row => label(row.source)], ['Amount (USD)', row => money(row.total_minor)], ['Lines', row => row.lines?.length]]),
      journalForm(), form('Reverse journal entry', {entry_id: select('Entry', (w.journal || []).map(entry => [entry.id, '#' + entry.id + ' · ' + entry.memo])), entry_date: date('Reversal date'), reason: text('Reason')}, 'reverse_entry'));
    const fundView = () => el('div', null,
      table('Funds', funds, [['Code', fund => fund.code], ['Fund', fund => fund.name], ['Restriction', fund => label(fund.restriction_type)], ['Available (USD)', fund => fund.available_minor === null ? 'General fund' : money(fund.available_minor)], ['Net assets (USD)', fund => money(fund.net_assets_minor)]]),
      el('div', {class: 'logistics-grid'},
        form('Create restricted fund', {code: text('Fund code'), name: text('Fund name'), restriction_type: select('Restriction', [['temporarily_restricted', 'Temporary restriction'], ['permanently_restricted', 'Permanent restriction']]), purpose: text('Purpose', '', true)}, 'create_fund'),
        form('Record grant receipt', {fund_id: select('Fund', fundOptions(funds.filter(fund => fund.restriction_type !== 'unrestricted'))), amount_minor: amount('Grant amount'),
          cash_account_id: select('Cash account', accountOptions(cash)), revenue_account_id: select('Grant revenue account', accountOptions(active.filter(account => account.account_type === 'income'))), entry_date: date('Receipt date'), memo: text('Memo')}, 'grant_receipt'),
        form('Release restricted funds', {fund_id: select('Temporary fund', fundOptions(funds.filter(fund => fund.restriction_type === 'temporarily_restricted'))), amount_minor: amount('Release amount'), entry_date: date('Release date'), memo: text('Memo')}, 'release_fund', null,
          'Release temporary restrictions when their conditions are satisfied. Permanent funds remain restricted.')));
    const accountView = () => el('div', null,
      table('Chart of accounts', accounts, [['Code', account => account.code], ['Account', account => account.name], ['Type', account => label(account.account_type)], ['Role', account => label(account.role)], ['Capital', account => account.is_capital ? 'Yes' : 'No'], ['Balance (USD)', account => money(account.balance_minor)], ['Active', account => account.is_active ? 'Yes' : 'No']]),
      el('div', {class: 'logistics-grid'},
        form('Create account', {code: text('Account code'), name: text('Account name'), account_type: select('Type', ['asset', 'liability', 'equity', 'income', 'expense'].map(type => [type, label(type)])),
          is_capital: select('Capital asset account', [['true', 'Yes'], ['false', 'No']]), role: text('Account role', '', true)}, 'create_account', payload => ({...payload, is_capital: payload.is_capital === 'true'}),
          'Designate capital accounts before their first posting so restricted-fund spending can be tracked consistently.'),
        form('Update account', {account_id: select('Account', accountOptions(accounts)), name: text('New account name', '', true), is_active: select('Active', [['true', 'Yes'], ['false', 'No']], true)}, 'update_account', payload => {
          if (payload.is_active !== undefined) payload.is_active = payload.is_active === 'true'; return payload;
        })));
    const paymentTiming = () => {
      const timing = w.payment_timeliness;
      if (!timing) return null;
      return el('section', {class: 'logistics-card'}, el('h3', {text: 'Customer payment timing'}),
        el('p', {class: 'muted', text: money(timing.on_time_paid_minor) + ' USD received on time out of ' + money(timing.eligible_paid_minor) + ' USD received with comparable dates. ' + money(timing.late_paid_minor) + ' USD received late; ' + money(timing.unknown_date_paid_minor) + ' USD has unknown dates.'}),
        el('p', {class: 'muted', text: 'Overdue unpaid invoices: ' + money(timing.overdue_unpaid_minor) + ' USD. Unpaid invoices are reported separately from the timing of payments already received.'}),
        table('Recorded customer payments', timing.rows || [], [['Invoice', row => row.number], ['Customer', row => row.party_name], ['Due', row => row.due_date], ['Paid', row => row.payment_date], ['Amount (USD)', row => money(row.amount_minor)], ['Timing', row => row.on_time === true ? 'On time' : row.on_time === false ? 'Late' : 'Unknown']]),
        table('Customer payment summary', timing.customers || [], [['Customer', row => row.party_name], ['Paid (USD)', row => money(row.paid_minor)], ['On time', row => money(row.on_time_paid_minor)], ['Late', row => money(row.late_paid_minor)], ['Known-date paid amount', row => money(row.eligible_paid_minor)], ['Overdue unpaid', row => money(row.overdue_unpaid_minor)]]));
    };
    const overview = () => el('div', null,
      el('div', {class: 'row logistics-actions'}, [['Assets', w.balance_sheet?.assets_minor], ['Liabilities', w.balance_sheet?.liabilities_minor], ['Net assets', w.balance_sheet?.net_assets_minor], ['Profit', w.income_statement?.profit_minor]].map(([name, amount]) =>
        el('div', {class: 'logistics-stat'}, el('strong', {text: money(amount)}), name + ' · USD'))),
      table('Trial balance', w.trial_balance?.accounts || [], [['Account', row => row.code + ' · ' + row.name], ['Debit (USD)', row => money(row.trial_debit_minor ?? Math.max(0, (row.debit_minor || 0) - (row.credit_minor || 0)))], ['Credit (USD)', row => money(row.trial_credit_minor ?? Math.max(0, (row.credit_minor || 0) - (row.debit_minor || 0)))]]),
      el('p', {class: 'muted', text: 'Trial balance totals: ' + money(w.trial_balance?.total_debits_minor) + ' USD debit; ' + money(w.trial_balance?.total_credits_minor) + ' USD credit.'}),
      table('Receivables aging', w.ar_aging?.rows || [], [['Invoice', row => row.number], ['Customer', row => row.party_name], ['Due', row => row.due_date], ['Remaining (USD)', row => money(row.remaining_minor)], ['Days overdue', row => row.days_overdue]]),
      table('Payables aging', w.ap_aging?.rows || [], [['Bill', row => row.number], ['Supplier', row => row.party_name], ['Due', row => row.due_date], ['Remaining (USD)', row => money(row.remaining_minor)], ['Days overdue', row => row.days_overdue]]));
    return el('div', null, el('p', {class: 'muted', text: 'USD bookkeeping for invoices, bills, journal entries and restricted funds. Payments record completed bank or cash movements.'}),
      h.tabs(['Overview', 'Invoices', 'Bills', 'Journal', 'Funds', 'Accounts'], tab => tab === 'Overview' ? el('div', null, overview(), paymentTiming()) : tab === 'Invoices' ? documents('invoice') : tab === 'Bills' ? documents('bill') : tab === 'Journal' ? journal() : tab === 'Funds' ? fundView() : accountView()),
      w.limitations?.length ? el('details', {class: 'logistics-settings'}, el('summary', {text: 'Accounting coverage'}), el('ul', null, w.limitations.map(text => el('li', {text})))) : null);
  },
});
