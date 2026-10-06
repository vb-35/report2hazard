// Exercise the real editor without a browser package or a frontend dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const element = () => ({value: '', handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; }, replaceChildren() {}});
const field = element(), input = element(), select = element(), submit = element();
field.value = 'event';
const form = element();
form.elements = {field, segment: element()};
form.querySelector = selector => selector.startsWith('input') ? input : selector.startsWith('select') ? select : submit;
const notice = element(), discard = element();
const elements = {'.candidate-edit': form, '#workspace': {dataset: {labels: '{"generalized_category":["Category A","Category B"]}'}}, '#edit-notice': notice, '#discard-edit': discard};
const listeners = {};
const window = {addEventListener(name, fn) { listeners[name] = fn; }, dispatchEvent(event) { listeners[event.type]?.(event); }};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../multi_hazard_pipeline/static/edit.js'), 'utf8'), {
  window, document: {querySelector: selector => elements[selector]},
  Option: function(text, value) { this.value = value; }, Event: function(type) { this.type = type; },
});
const editor = window.candidateEditor;
const row = {segment: 7, event: 'Original', generalized_category: 'Category A'};
editor.bind(row, 1, true);
assert.equal(input.value, 'Original');
input.value = 'Unsaved draft'; form.handlers.input();
editor.bind({...row, event: 'Background update'}, 1, true);
assert.equal(input.value, 'Unsaved draft');
assert.equal(submit.disabled, false);
editor.bind(row, 2, true);
assert.equal(input.value, 'Unsaved draft');
assert.equal(submit.disabled, true);
assert.match(notice.textContent, /retained/);
editor.bind(null, 2, false);
assert.equal(input.value, 'Unsaved draft');
assert.equal(form.elements.segment.value, 7);
let prevented = false;
listeners.beforeunload({preventDefault() { prevented = true; }});
assert.equal(prevented, true);
listeners['editor-reset'] = () => editor.bind(row, 2, true);
discard.handlers.click();
assert.equal(input.value, 'Original');
assert.equal(submit.disabled, false);
field.value = 'generalized_category'; field.handlers.change();
select.value = 'Category B'; form.handlers.input();
editor.bind(row, 2, true);
assert.equal(select.value, 'Category B');
assert.equal(input.disabled, true);
editor.bind({...row, segment: 8}, 2, true);
assert.equal(submit.disabled, true);
