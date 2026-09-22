"""Presentation checks for the saved-event UI; no broker or model calls."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node is required for dashboard checks")
class EventDashboardTests(unittest.TestCase):
    def run_js(self, checks):
        script = r'''
const assert=require("assert"), fs=require("fs"), vm=require("vm");
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.attrs={}; this._text=""; this.classList={add(){}}; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent ?? c).join(""); }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children=children; this._text=""; }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener() {}
}
const elements=new Map();
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element("div"));return elements.get(id);},
  createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element("#text");n.textContent=text;return n;}};
const window={};
const source=fs.readFileSync(process.argv[1],"utf8").replace("window.EventHistory = {", "window.testUI = {overview,assessment,systemGuide,record,timeline}; window.EventHistory = {");
vm.runInNewContext(source,{document,window,Date,Number,URLSearchParams,Option:Element});
const ui=window.testUI;
const all=n=>[n,...n.children.flatMap(c=>typeof c==="object"?all(c):[])];
const event={id:579,status:"open",opened_at:"2026-09-18T00:33:13Z",last_evaluated_at:"2026-09-18T00:33:40Z",
  first_assessment:{state:"watch",current:{value:215.6,unit:"°F"},baseline:{median:190.4,unit:"°F"},
    deviation:{signed_from_median:25.2,threshold:24.01812},persistence:{observed:1,required:10}},
  first_warning:null,latest_assessment:{state:"unavailable",reason:"Reading is too old",current:{value:219.2,unit:"°F"},
    persistence:{observed:0,required:10,evaluated:false}}};
''' + checks
        result = subprocess.run(["node", "-e", script, str(ROOT / "projects/vehicle_data/static/event-history.js")],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overview_keeps_opening_and_unavailable_latest_distinct(self):
        self.run_js(r'''
const original=JSON.stringify(event), view=ui.overview(event);
assert(view.textContent.includes("215.6"));assert(view.textContent.includes("190.4"));
assert(view.textContent.includes("1 of 10"));assert(!view.textContent.includes("219.2"));
assert(view.textContent.includes("No warning escalation is recorded"));
assert(view.textContent.includes("Fresh data unavailable"));
assert.equal(JSON.stringify(event),original,"presentation must not mutate evidence");
''')

    def test_missing_evidence_is_not_reconstructed(self):
        self.run_js(r'''
const view=ui.assessment("Latest",event.latest_assessment,"event:1");
assert(view.textContent.includes("Not evaluated"));assert(!view.textContent.includes("0 of 10"));
assert(ui.assessment("Last usable",null).textContent.includes("No assessment was saved"));
''')

    def test_guide_and_structured_records_never_render_json_blocks(self):
        self.run_js(r'''
const guide=ui.systemGuide({version:2,retention:"Saved events retain their evidence.",architecture:"Cached reads only."});
assert(!all(guide).some(n=>n.tag==="pre"));
assert.equal(all(guide).filter(n=>n.className==="event-guide")[0].children.length,5);
assert(guide.textContent.includes("They do not resolve the event or silence its notifications"));
assert(guide.textContent.includes("Cached reads only"));
assert(all(guide).some(n=>n.tag==="strong" && n.textContent==="watch"));
assert(all(guide).some(n=>n.tag==="strong" && n.textContent==="do not resolve the event"));
assert(all(guide).some(n=>n.tag==="h4"),"technical topics have a lower heading level");
const rendered=ui.record({sample_window:"retained",nested:{value:215.6},enabled:true});
assert(!all(rendered).some(n=>n.tag==="pre"));assert(rendered.textContent.includes("Yes"));
''')

    def test_record_lists_are_bounded_without_discarding_export_context(self):
        self.run_js(r'''
const value=Array.from({length:100},(_,i)=>({value:i})), view=ui.record(value);
assert.equal(view.children.filter(n=>n.tag==="details").length,30);
assert(view.textContent.includes("Export includes the complete saved data"));
assert.equal(value.length,100);
''')

    def test_coverage_changes_are_collapsed_notes_and_unconfirmed_is_not_unresolved(self):
        self.run_js(r'''
event.outcome="unconfirmed";event.status="resolved";
event.timeline=[{type:"opened",assessment:event.first_assessment},
 {type:"evidence_inconclusive",assessment:{state:"unavailable",current:{value:219.2,unit:"°F",effective_age_seconds:11.8}}},
 {type:"watch_unconfirmed",assessment:event.latest_assessment}];
const view=ui.overview(event);
assert(view.textContent.includes("Unconfirmed · Monitoring ended"));
assert(!view.textContent.includes("Unresolved"));
assert(view.textContent.includes("Recovery was not established"));
const timeline=ui.timeline(event);
const notes=all(timeline).find(n=>n.tag==="details" && n.children[0]?.textContent==="Monitoring Notes");
assert(notes);assert(!notes.open,"monitoring notes are collapsed by default");
assert(notes.textContent.includes("11.8 seconds"));
assert(notes.textContent.includes("Watch archived as unconfirmed"));
assert(!timeline.textContent.includes("Evidence became inconclusive"));
const main=timeline.children.find(n=>n.tag==="ol");assert.equal(main.children.length,1);
''')
