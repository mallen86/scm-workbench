#!/usr/bin/env python3
"""Executable Custom fetch UI/transport contracts (also run by fetch-recents)."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(ROOT)],
        input=r'''
import fs from "node:fs";
import assert from "node:assert/strict";
const root = process.argv[2];
const source = name => fs.readFileSync(`${root}/${name}`, "utf8");
const dataUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
const calls = [];
let native = null;
const bridge = dataUrl(`export function getTauriInvoke(scope) { return globalThis.__native || null; }`);
const transport = await import(dataUrl(source("ui/js/custom-art-transport.js").replace('"./transport.js"', JSON.stringify(bridge))));
const response = value => ({ ok: true, json: async () => value });
const imported = (name = "card.png") => ({ ok: true, destination: "front", imported: 1, names: [name], failed: [] });
globalThis.fetch = async (...args) => { calls.push(args); return response(imported()); };
globalThis.__native = async (command, args) => { calls.push([command, args]); return imported(); };
await transport.importCustomArtDrop("front", "grant");
assert.deepEqual(calls.pop(), ["wb_custom_art_import", {destination:"front",token:"grant"}]);
await transport.openCustomArtFolder("double_sided");
assert.deepEqual(calls.pop(), ["wb_rpc", {method:"custom_art.open_folder",params:{destination:"double_sided"}}]);
globalThis.__native = async () => null;
assert.equal(await transport.chooseCustomArt("front"), null);
globalThis.__native = async () => { throw Error("native unavailable"); };
await assert.rejects(transport.importCustomArtDrop("front", "grant"), /native unavailable/);
await assert.rejects(transport.chooseCustomArt("front"), /native unavailable/);
await assert.rejects(transport.openCustomArtFolder("front"), /native unavailable/);
assert.equal(calls.length, 0, "native failure must not reach HTTP");
await assert.rejects(transport.importCustomArtFiles("front", []), /native drop/);
await assert.rejects(transport.openCustomArtFolder("../back"), /front, double-sided, or back/);
let callback, removed = 0, delivered;
const scope = {__TAURI_INTERNALS__: {
  transformCallback(fn) { callback=fn; return 42; },
  unregisterCallback(id) { assert.equal(id,42); removed++; },
}};
globalThis.__native = async (command,args) => {
  calls.push([command,args]);
  return command === "plugin:event|listen" ? 7 : undefined;
};
const off = await transport.listenCustomArtDrops(payload => delivered=payload, scope);
callback({payload:{token:"abc",x:12,y:23}});
assert.equal(delivered.token,"abc");
await off(); await off();
assert.equal(removed,1);
assert.equal(calls[0][1].event,"custom-art-drop");
assert.deepEqual(calls[1],["plugin:event|unlisten",{event:"custom-art-drop",eventId:7}]);
calls.length=0;
globalThis.__native = async () => { throw Error("listener failed"); };
await assert.rejects(transport.listenCustomArtDrops(()=>{},scope),/listener failed/);
assert.equal(removed,2,"failed subscription must release callback");
globalThis.__native = null;
const file = new File([new Uint8Array([137,80,78,71])], "My card.png");
const result = await transport.importCustomArtFiles("front", [file]);
assert.equal(result.imported,1);
assert.match(calls[0][0],/^\/api\/custom-art\/import\?destination=front&name=My\+card.png$/);
assert.equal(calls[0][1].body,file,"browser must upload selected bytes, not paths");
calls.length=0;
await assert.rejects(transport.importCustomArtFiles("front",Array(257).fill(file)),/256/);
await assert.rejects(transport.importCustomArtFiles("front",[{size:33*1024*1024}]),/32 MiB/);
await assert.rejects(transport.importCustomArtFiles("front",Array(17).fill({size:32*1024*1024})),/512 MiB/);
assert.equal(calls.length,0);
await assert.rejects(transport.importCustomArtFiles("back",[file,file]),/exactly one/);
await assert.rejects(transport.importCustomArtFiles("back",[{size:32*1024*1024+1}]),/32 MiB/);
assert.equal(calls.length,0,"invalid back batches cannot mutate");
assert.equal(transport.CUSTOM_BACK_MAX_FILE_BYTES,32*1024*1024);
assert.equal(transport.CUSTOM_BACK_MAX_FILE_BYTES,transport.CUSTOM_ART_MAX_FILE_BYTES);
const largeBack = new File([new Uint8Array(32*1024*1024)], "Large back.png", {type:"image/png"});
globalThis.fetch = async (...args) => {calls.push(args);return response({...imported("Large back.png"),destination:"back"});};
const largeBackResult = await transport.importCustomArtFiles("back",[largeBack]);
assert.equal(largeBackResult.imported,1,"32 MiB backs must pass frontend validation");
assert.equal(calls.length,1);assert.equal(calls[0][1].body,largeBack);
calls.length=0;
let uploads=0;
globalThis.fetch = async () => ++uploads === 1 ? response(imported("kept.png")) : ({ok:false,json:async()=>({ok:false,errors:["not an image"]})});
const partial = await transport.importCustomArtFiles("front",[file,file]);
assert.equal(partial.imported,1); assert.equal(partial.failed.length,1); assert.match(partial.failed[0].error,/not an image/);
let opened;
globalThis.fetch = async (url,options) => {opened=[url,JSON.parse(options.body)];return response({ok:true,errors:[]});};
await transport.openCustomArtFolder("double_sided");
assert.deepEqual(opened,["/api/custom-art/open-folder",{destination:"double_sided"}]);

const realNow = Date.now, realTimer = globalThis.setTimeout;
let now = 0, timeouts = [];
Date.now = () => now;
globalThis.setTimeout = (fn, ms) => { timeouts.push(ms); return realTimer(fn, ms); };
globalThis.fetch = async () => { now = 599999; return response(imported()); };
try {
  await transport.importCustomArtFiles("front", [file,file]);
  assert.deepEqual(timeouts, [60000,1], "each upload must be bounded by the remaining batch deadline");
} finally { Date.now = realNow; globalThis.setTimeout = realTimer; }

// Render the actual fetch page with a small DOM stand-in. Custom is not a
// manifest job, must stay selected with recent games, and keeps shared cleanup.
class Element {
  constructor(tag,attrs={},...kids) {this.tag=tag;this.attrs=attrs;this.children=[];this.dataset={destination:attrs['data-destination']};this.handlers={};this.classes=new Set();this.classList={add:value=>this.classes.add(value),remove:value=>this.classes.delete(value),contains:value=>this.classes.has(value)};this.open=false;Object.assign(this,attrs);this.append(...kids);}
  append(...kids) {this.children.push(...kids.flat(Infinity).filter(v=>v!==null&&v!==undefined&&v!==false));}
  addEventListener(name,handler) {this.handlers[name]=handler;}
  setAttribute(key,value) {this.attrs[key]=value;}
  replaceChildren(...kids) {this.children=[];this.append(...kids);}
  set innerHTML(value) {this.children=[];}
  get childElementCount() {return this.children.filter(v=>v instanceof Element).length;}
}
const el=(...args)=>new Element(...args);
const all=node=>[node,...(node.children||[]).flatMap(child=>child instanceof Element?all(child):[])];
const text=node=>(node.children||[]).map(child=>child instanceof Element?text(child):String(child)).join(" ");
const listeners=new Map();
globalThis.document={addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:(name,fn)=>{if(listeners.get(name)===fn)listeners.delete(name);},dispatchEvent:()=>{}};
const PAGES={};
const S={plugin:"__custom_art",jobs:[{kind:"fetch:mtg",ts:1}],forms:{},info:{scm:{found:true}},manifest:{"fetch:mtg":{game:"Magic",groups:[]}}};
let customMounted=0,customDisposed=0,cleared=0,formCount=0,strips=0,after=0;
const recents=await import(dataUrl(source("ui/js/fetch-recents.js")));
const deps={PAGES,S,el,ico:()=>el("i"),pageHead:(...args)=>el("header",{},args),$: (selector,element)=>all(element).find(n=>n.tag===selector),$$:()=>[],
  CUSTOM_ART_PLUGIN:"__custom_art",CUSTOM_ART_CHANGED:"custom-changed",customArtState:()=>({busy:false}),clearCustomArtStatus:()=>cleared++,
  renderCustomArt:()=>Object.assign(el("section",{},"CUSTOM DROP BOXES"),{__patch:()=>customMounted++,__dispose:()=>customDisposed++}),
  recentFetchLayout:recents.recentFetchLayout,JOBS_UPDATED_EVENT:"jobs-updated",uiMode:()=>"simple",go:()=>{},
  formCard:()=>{formCount++;return el("form");},jobStrip:()=>{strips++;return el("aside");},
  confirmModal:async()=>true,doRun:async()=>({id:"cleanup"}),watchJobDone:(_id,fn)=>fn(),afterFormChange:()=>after++,clearJobCompletion:()=>{},
};
const fetchPage=source("ui/js/pages/fetch.js").replace(/import\s+[^;]+?\s+from\s+["'][^"']+["'];/g,"").replace(/export function /g,"function ");
new Function(...Object.keys(deps),fetchPage)(...Object.values(deps));
let page=PAGES.fetch();
assert.match(text(page),/CUSTOM DROP BOXES/);
assert.match(text(page),/Starting a different deck\?/);
assert.match(text(page),/Clear card images/);
assert.equal(formCount,0);assert.equal(strips,0,"Custom must not launch or render a fake fetch job");
assert.equal(all(page).find(n=>n.tag==="details").open,true,"Custom stays visible with recent games");
page.__patch();assert.equal(customMounted,1);
await all(page).find(n=>n.tag==="button"&&text(n).includes("Clear card images")).onclick();
assert.equal(cleared,1);assert.equal(after,0,"Custom cleanup must not preview a missing manifest kind");
page.__dispose();assert.equal(customDisposed,1);assert.equal(listeners.size,0);
S.plugin="mtg";page=PAGES.fetch();assert.equal(formCount,1);assert.equal(strips,1);assert.match(text(page),/Starting a different deck\?/);page.__dispose();
const ui=source("ui/js/custom-art.js"), css=source("ui/theme.css");
for(const marker of ['["front", "double_sided", "back"]','openCustomArtFolder(destination)','if (hasNativeCustomArt()) return','refreshInfo({ keepForms: true })','getBoundingClientRect()','wrap.__dispose','confirmModal({ title: "Replace the card back image?"','Exactly one recognized image remains']) assert.ok(ui.includes(marker),`Missing Custom UI contract: ${marker}`);
assert.ok(css.includes('.custom-art-next[hidden] { display: none; }'));
const nextStyle=css.match(/\.custom-art-next\s*\{([^}]+)\}/)[1];
for(const rule of ['display: flex','justify-content: flex-end','flex-wrap: wrap','gap: 12px']) assert.ok(nextStyle.includes(rule),`Missing spaced, right-aligned actions: ${rule}`);
assert.match(css,/\.custom-art-zones\s*\{[^}]*gap: 16px/,'image areas must remain separated');
for(const marker of ['grid-template-columns: repeat(2, minmax(0, 1fr))','grid-template-rows: repeat(2, minmax(0, 1fr))','[data-destination="front"] { grid-column: 1; grid-row: 1 / 3; }','[data-destination="double_sided"] { grid-column: 2; grid-row: 1; }','[data-destination="back"] { grid-column: 2; grid-row: 2; }','grid-template-columns: 1fr; grid-template-rows: none;']) assert.ok(css.includes(marker),`Missing responsive zone layout: ${marker}`);
assert.ok(source("ui/js/custom-art-transport.js").includes('input.multiple = destination !== "back";'));
assert.ok(source("ui/js/custom-art-transport.js").includes('const destinations = new Set(["front", "double_sided", "back"])'));
let selections=[], confirmations=0, nativeDropHandler=null, nativeImports=[];
S.info.scm.back_images=[{name:"old.png"}];
const artDeps={S,el,ico:()=>el("i"),toast:()=>{},confirmModal:async()=>{confirmations++;return confirmations>1;},go:()=>{},
  chooseCustomArt:async dest=>{selections.push(dest);return null;},hasNativeCustomArt:()=>false,
  importCustomArtDrop:async(destination,token)=>{nativeImports.push([destination,token]);return {imported:0,names:[],failed:[]};},importCustomArtFiles:()=>{},listenCustomArtDrops:async handler=>{nativeDropHandler=handler;return ()=>{};},openCustomArtFolder:async()=>{}};
const artSource=ui.replace(/import\s+[^;]+?\s+from\s+["'][^"']+["'];/g,"").replace(/export (const|function) /g,"$1 ");
const art=new Function(...Object.keys(artDeps),`${artSource}; return {renderCustomArt};`)(...Object.values(artDeps));
const panel=art.renderCustomArt();
const zones=all(panel).filter(n=>n.attrs?.['data-destination']);
assert.deepEqual(zones.map(n=>n.dataset.destination),["front","double_sided","back"]);
assert.match(text(zones[2]),/Drop one image here/);
assert.match(text(zones[2]),/32 MiB/);
assert.doesNotMatch(text(panel),/8 MiB/);
await all(zones[2]).find(n=>n.tag==="button"&&n.attrs.class==="custom-art-drop").onclick();
assert.equal(confirmations,1);assert.deepEqual(selections,[],"declining back replacement must not open picker");
await all(zones[2]).find(n=>n.tag==="button"&&n.attrs.class==="custom-art-drop").onclick();
assert.deepEqual(selections,["back"]);
await all(zones[0]).find(n=>n.tag==="button"&&n.attrs.class==="custom-art-drop").onclick();
assert.deepEqual(selections,["back","front"]);assert.equal(confirmations,2);
S.customArt={busy:false,destination:"front",result:{imported:2,names:["successful-one.png","successful-two.png"],failed:[]}};
panel.__patch();
const status=all(panel).find(n=>n.attrs.class==="custom-art-status");
const actions=all(panel).find(n=>n.attrs.class==="actions custom-art-next");
assert.match(text(status),/2 images copied to front images/);
assert.doesNotMatch(text(status),/Imported filenames|successful-one|successful-two/);
assert.equal(all(status).filter(n=>n.tag==="details"||n.tag==="ul").length,0);
assert.equal(actions.hidden,false);
S.customArt.result.failed=[{name:"broken.png",error:"not an image"}];
listeners.get("workbench:custom-art-changed")();
assert.match(text(status),/Files needing attention/);
assert.match(text(status),/broken.png: not an image/,'failed filenames remain actionable');
assert.doesNotMatch(text(status),/successful-one|successful-two/);
S.customArt.busy=true;listeners.get("workbench:custom-art-changed")();assert.equal(actions.hidden,true);
S.customArt.busy=false;S.info.scm.back_images=[];
panel.isConnected=true;
zones.forEach((zone,index)=>zone.getBoundingClientRect=()=>({left:600+index*300,right:880+index*300,top:400,bottom:700}));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
for (let index=0;index<3;index++) {
  nativeDropHandler({phase:"over",x:720+index*300,y:540,token:"not-authorized"});
  assert.deepEqual(zones.map(zone=>zone.classList.contains("drag-over")),zones.map((_,i)=>i===index));
  assert.equal(nativeImports.length,0,"hover never authorizes an import");
}
let highlightMutations=0;
const originalAdd=zones[2].classList.add,originalRemove=zones[2].classList.remove;
zones[2].classList.add=value=>{highlightMutations++;originalAdd(value);};
zones[2].classList.remove=value=>{highlightMutations++;originalRemove(value);};
for (let i=0;i<100;i++) nativeDropHandler({phase:"over",x:1320,y:540});
assert.equal(highlightMutations,0,"repeated hover must not clear/reapply the class and restart painting");
assert.ok(zones[2].classList.contains("drag-over"));
assert.ok(css.includes('.custom-art-zone.drag-over .custom-art-drop { transition: none; }'),"drag feedback must paint immediately without animation");
nativeDropHandler({phase:"leave"});
assert.ok(zones.every(zone=>!zone.classList.contains("drag-over")));
nativeDropHandler({phase:"over",x:720,y:540});
nativeDropHandler({phase:"over",x:360,y:270});
assert.ok(zones.every(zone=>!zone.classList.contains("drag-over")),"outside clears target");
S.customArt.busy=true;
nativeDropHandler({phase:"over",x:720,y:540});
assert.ok(zones.every(zone=>!zone.classList.contains("drag-over")),"busy zones must not highlight");
S.customArt.busy=false;
for (let index=0;index<3;index++) {
  nativeDropHandler({phase:"over",x:720+index*300,y:540});
  nativeDropHandler({x:720+index*300,y:540,token:`grant-${index}`});
  await tick();
  assert.ok(zones.every(zone=>!zone.classList.contains("drag-over")),"drop clears highlight");
}
assert.deepEqual(nativeImports,[["front","grant-0"],["double_sided","grant-1"],["back","grant-2"]],"logical macOS Retina drop coordinates must select the intended zone");
nativeDropHandler({x:360,y:270,token:"wrong-scale"});await tick();
assert.equal(nativeImports.length,3,"a drop outside the zones must not import");
panel.__dispose();
nativeDropHandler({x:720,y:540,token:"disposed"});await tick();
assert.equal(nativeImports.length,3,"disposed page must not import dropped files");
console.log("ok: Custom source keeps cleanup, native grants never fall back, browser uploads are bounded, and partial results remain visible");
''', text=True, capture_output=True, timeout=30,
    )
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
