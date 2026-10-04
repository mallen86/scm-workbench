import fs from 'node:fs';
import assert from 'node:assert/strict';
const url = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
class Node {
  constructor(tag, attrs, kids) { this.tag = tag; this.attrs = attrs; Object.assign(this, attrs); this.value = attrs.value || ''; this.kids = kids.filter(Boolean); }
  append(...kids) { this.kids.push(...kids.filter(Boolean)); }
  replaceChildren(...kids) { this.kids = kids.filter(Boolean); }
}
globalThis.pickerEl = (tag, attrs = {}, ...kids) => new Node(tag, attrs, kids);
const source = fs.readFileSync(process.argv[2], 'utf8')
  .replace('from "./core.js"', `from "${url('export const el = globalThis.pickerEl;')}"`)
  .replace('from "./artifacts.js"', `from "${url('export const listFiles = () => { throw Error("unexpected transport"); };')}"`);
const { createImageSelection, selectionPage, selectionKey } = await import(url(source));
const all = root => [root, ...(root.kids || []).filter(k => typeof k === 'object').flatMap(all)];
const text = node => typeof node === 'string' ? node : (node.textContent || '') + (node.kids || []).map(text).join(' ');
const button = (root, label) => all(root).find(n => n.tag === 'button' && text(n) === label);
const rows = root => all(root).filter(n => n.class === 'pp-selection-row');
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
let calls = 0, changes = 0;
const names = Array.from({length:100}, (_, i) => ({name:`Card ${String(i+1).padStart(3,'0')}, artist.png`, dir:false}));
for (const mode of ['simple','advanced']) {
  const args = {scope:'both', selected_images:[]};
  const picker = createImageSelection({args:()=>args, changed:()=>changes++, inventory: async (path, images) => {
    calls++; assert.equal(images, true); return {items:path==='game/front'?names:[], truncated:false};
  }});
  picker.sync(); assert.equal(picker.root.hidden, true);
  const before = calls;
  args.scope = 'selected'; picker.sync(); await tick();
  assert.equal(calls-before,3); assert.equal(picker.root.hidden,false);
  assert.equal(rows(picker.root).length,12);
  assert.match(text(picker.root),/Page 1 of 9/);
  const check = rows(picker.root)[0].kids[0]; check.checked = true; check.onchange();
  assert.deepEqual(args.selected_images,['game/front/Card 001, artist.png']);
  button(picker.root,'Next').onclick(); assert.match(text(picker.root),/Page 2 of 9/);
  assert.equal(rows(picker.root).length,12);
  const search = all(picker.root).find(n=>n.type==='search');
  search.value = 'Card 09'; search.oninput();
  assert.equal(rows(picker.root).length,10);
  button(picker.root,'Select all matches').onclick(); assert.equal(args.selected_images.length,11);
  search.value = ''; search.oninput();
  const only = all(picker.root).find(n=>n.tag==='label' && n.class==='pp-selection-filter').kids[0];
  only.checked = true; only.onchange(); assert.equal(rows(picker.root).length,11);
  assert.match(text(picker.root),/11 selected/);
  args.scope='front'; picker.sync(); assert.equal(picker.root.hidden,true);
  args.scope='selected'; picker.sync(); assert.equal(args.selected_images.length,11);
  const restored = structuredClone(args); assert.equal(selectionKey(args),selectionKey(restored));
  button(picker.root,'Clear selection').onclick(); assert.equal(args.selected_images.length,0);
  assert.match(text(picker.root),/No matching cards/);
  picker.dispose();
}
const state = {scope:'selected', selected_images:[]};
for (const result of [{items:names,truncated:true},new Error('Native failed')]) {
  const picker=createImageSelection({args:()=>state,changed:()=>{},inventory:async()=>{if(result instanceof Error)throw result;return result;}});
  picker.sync(); await tick(); assert.equal(rows(picker.root).length,0);
  assert.equal(button(picker.root,'Select all matches').disabled,true);
  assert.match(text(picker.root),result instanceof Error?/Native failed/:/Too many/); picker.dispose();
}
let resolve;
const stale=createImageSelection({args:()=>state,changed:()=>{throw Error('late update');},inventory:()=>new Promise(r=>{resolve=r;})});
stale.sync(); stale.dispose(); resolve({items:names,truncated:false}); await tick();
assert.equal(rows(stale.root).length,0);
const malicious=createImageSelection({args:()=>state,changed:()=>{},inventory:async()=>({items:[{name:'../escape.png'},{name:'x\\y.png'},{name:'<img onerror=evil>.png'}],truncated:false})});
malicious.sync(); await tick(); assert.equal(rows(malicious.root).length,3); // Same legal name in three roles.
assert.equal(rows(malicious.root)[0].kids[1].kids[0],'<img onerror=evil>');
assert.ok(changes>0);
console.log('ok: manual card selection is bounded, searchable, persistent, text-only and fail-closed in both modes');
