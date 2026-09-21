from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "projects/vehicle_data/static"


@unittest.skipUnless(shutil.which("node"), "node is required for layout tests")
class DashboardLayoutTests(unittest.TestCase):
    def run_js(self, checks):
        script = r'''
const assert = require("assert"), fs = require("fs");
const data = new Map();
global.window = {localStorage:{getItem:key=>data.get(key) ?? null,
  setItem:(key,value)=>data.set(key,value)}};
require(process.argv[1]);
const p=window.VanDashboardProfiles;
const rows=s=>p.resolve({...s,selected:"custom"}).rows;
const ids=s=>rows(s).map(row=>row.map(tile=>tile.id));
''' + checks
        result = subprocess.run(["node", "-e", script, str(STATIC / "profiles.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registry_matches_every_tile_and_profiles(self):
        self.run_js(r'''
const html=fs.readFileSync(process.argv[1].replace("profiles.js","index.html"),"utf8");
const sections=[...html.matchAll(/<section class="panel[^>]*>/g)].map(m=>m[0]);
const panelIds=sections.map(tag=>tag.match(/data-widget="([^"]+)"/)?.[1]);
assert(panelIds.every(Boolean),"unregistered panel");
assert.deepEqual(panelIds.slice().sort(),p.widgets.map(w=>w.id).sort());
assert.equal(new Set(panelIds).size,panelIds.length);
for(const [id,profile] of Object.entries(p.profiles)) {
  assert.equal(new Set(profile.widgets).size,profile.widgets.length);
  assert(profile.widgets.every(id=>panelIds.includes(id)));
  const view=p.resolve({selected:id});
  assert.deepEqual(view.widgets,view.rows.flat().map(t=>t.id));
  assert(view.rows.filter(r=>r.length===1&&r[0].width==="half").length<=1);
}
assert(!p.resolve({selected:"driving"}).widgets.includes("maintenance"));
assert(!p.resolve({selected:"diagnostics"}).widgets.includes("maintenance"));
assert(p.resolve({selected:"overview"}).widgets.includes("maintenance"));
assert(Object.keys(p.profiles).every(id=>p.resolve({selected:id}).widgets.includes("radar")));
''')

    def test_v1_v2_migration_corruption_and_denied_storage(self):
        self.run_js(r'''
data.set("van-telemetry.dashboard.v2",JSON.stringify({selected:"custom",customWidgets:["source","controls","collector"]}));
let migrated=p.loadSettings();
assert.equal(migrated.version,3);
assert.deepEqual(new Set(p.resolve(migrated).widgets),new Set(["maintenance","radar","battery","collector"]));
assert.equal(migrated.customLayout.filter(t=>t.id==="battery").length,1);
assert(data.has("van-telemetry.dashboard.v3"));
assert(data.has("van-telemetry.dashboard.v2"));
data.set("van-telemetry.dashboard.v3","broken json");
assert.equal(p.loadSettings().selected,"custom");
data.clear();data.set("van-telemetry.dashboard.v1",JSON.stringify({selected:"auto",customWidgets:["tires"]}));
assert.equal(p.loadSettings().selected,"overview");
data.clear();data.set("van-telemetry.dashboard.v2",JSON.stringify({selected:"auto",customWidgets:["tires"]}));
assert.equal(p.loadSettings().selected,"auto");
data.clear();data.set("van-telemetry.dashboard.v2",JSON.stringify({selected:"custom",customWidgets:["tires"]}));
window.localStorage.setItem=()=>{throw Error("denied")};
migrated=p.loadSettings();assert.equal(migrated.selected,"custom");
assert(p.resolve(migrated).widgets.includes("tires"));
assert.equal(p.saveSettings(migrated).selected,"custom");
''')

    def test_width_validation_empty_layout_and_packing(self):
        self.run_js(r'''
const base=p.defaultSettings();
let clean=p.normalizeSettings({selected:"bogus",customLayout:[null,{id:"bogus"},
  {id:"battery",width:"bogus",visible:false},{id:"battery",width:"full"},
  {id:"catalog",width:"half"}]});
assert.equal(clean.selected,"overview");assert.equal(clean.customLayout.length,p.widgets.length);
assert.deepEqual(clean.customLayout.find(t=>t.id==="battery"),{id:"battery",width:"half",visible:false});
assert.equal(clean.customLayout.find(t=>t.id==="catalog").width,"full");
const empty={...base,selected:"custom",customLayout:base.customLayout.map(t=>({...t,visible:false}))};
assert.equal(p.resolve(empty).widgets.length,0);
assert.equal(p.resolve(p.saveSettings(empty)).widgets.length,0);
const packed=p.packRows([{id:"a",width:"half",visible:true},{id:"b",width:"full",visible:true},
  {id:"c",width:"half",visible:true},{id:"d",width:"half",visible:true}]);
assert.deepEqual(packed.map(r=>r.map(t=>t.id)),[["a","c"],["b"],["d"]]);
for(let mask=0;mask<128;mask++) {
  let sample={...base,selected:"custom",customLayout:base.customLayout.map((t,i)=>({
    ...t,visible:!!(mask & (1<<(i%7))),width:mask%2&&i%3===0?"full":t.width}))};
  let layout=rows(sample);
  assert(layout.filter(r=>r.length===1&&r[0].width==="half").length<=1);
  assert(layout.every(r=>r.length<=2 && (r.length===1||r.every(t=>t.width==="half"))));
  assert.equal(new Set(layout.flat().map(t=>t.id)).size,layout.flat().length);
}
''')

    def test_editing_profiles_and_row_operations_are_stable(self):
        self.run_js(r'''
let custom=p.customize({selected:"overview"});
const before=ids(custom);
assert.deepEqual(before,p.resolve({selected:"overview"}).rows.map(r=>r.map(t=>t.id)));
const soloIndex=rows(custom).findIndex(r=>r.length===1&&r[0].width==="half");
const soloMoved=p.moveRow(custom,soloIndex,-1);
const expectedSoloMove=before.map(r=>r.slice());
[expectedSoloMove[soloIndex],expectedSoloMove[soloIndex-1]]=[expectedSoloMove[soloIndex-1],expectedSoloMove[soloIndex]];
assert.deepEqual(ids(soloMoved),expectedSoloMove,"moving solo row must not split existing pairs");
custom=p.moveRow(custom,0,1);
assert.deepEqual(ids(custom).slice(0,2),[before[1],before[0]]);
custom=p.swapRow(custom,1);
assert.deepEqual(ids(custom)[1],before[0].slice().reverse());
// Pair a tile from the right side with one from a later pair.
custom=p.pairTiles(custom,"maintenance","radar");
assert(ids(custom).some(r=>r[0]==="maintenance"&&r[1]==="radar"));
custom=p.changeTile(custom,"battery",{width:"full"});
assert(rows(custom).some(r=>r.length===1&&r[0].id==="battery"&&r[0].width==="full"));
custom=p.changeTile(custom,"maintenance",{visible:false});
assert(!p.resolve(custom).widgets.includes("maintenance"));
custom=p.changeTile(custom,"maintenance",{visible:true});
assert(p.resolve(custom).widgets.includes("maintenance"));
const saved=p.saveSettings(custom);assert.deepEqual(p.loadSettings(),saved);
assert.deepEqual(p.resolve({selected:"overview"}).rows.map(r=>r.map(t=>t.id)),before);
assert.equal(p.defaultSettings().selected,"overview");
''')

    def test_customizer_visible_only_on_custom_view(self):
        self.run_js(r'''
const app=fs.readFileSync(process.argv[1].replace("profiles.js","app.js"),"utf8");
const html=fs.readFileSync(process.argv[1].replace("profiles.js","index.html"),"utf8");
assert(/<details class="panel customizer" hidden>/.test(html),"hidden before first render");
const profileManager=p, lastSnapshot={status:{}};
let settings=p.defaultSettings(), lastProfileRenderKey=null, lastAppliedLayoutSignature=null;
let editorRenders=0;
const customizer={hidden:true,parentElement:{insertBefore(){}}}, picker={};
const panels=p.widgets.map(w=>({dataset:{widget:w.id}}));
global.document={body:{dataset:{}},querySelector:()=>customizer,querySelectorAll:()=>panels};
function byId(){return picker;}
function text(){}
function updateMetricPanelSummary(){}
function renderLayoutEditor(){editorRenders++;}
eval(app.slice(app.indexOf("function setProfile()"),app.indexOf("function editLayout(")));
const savedLayout=JSON.stringify(settings.customLayout);
for(const selection of ["overview","parked","driving","diagnostics","auto","custom","overview","custom"]) {
  settings={...settings,selected:selection};
  const before=editorRenders;
  setProfile();
  assert.equal(customizer.hidden,selection!=="custom",selection);
  assert.equal(editorRenders-before,selection==="custom"?1:0);
  assert.equal(JSON.stringify(settings.customLayout),savedLayout,"switching must preserve saved edits");
}
''')

    def test_tile_layout_has_no_independent_css_visibility_or_order(self):
        css = (STATIC / "style.css").read_text()
        self.assertNotIn("row dense", css)
        self.assertNotRegex(css, r'body\[data-profile=[^\n]+maintenance-panel')
        self.assertNotIn('.radar-panel { grid-column:', css)
        self.assertIn('[data-widget][data-width="half"]', css)
        app = (STATIC / "app.js").read_text()
        self.assertIn("lastLayoutEditorSignature", app)
        self.assertIn("Dashboard panels and widget registry must match exactly", app)
