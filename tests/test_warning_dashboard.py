from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO = Path(__file__).resolve().parents[1]
APP = REPO / "projects" / "vehicle_data" / "static" / "app.js"


class WarningDashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for warning UI test")
    def test_unresolved_episode_is_labeled_and_recovered_events_are_separate(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
const node=()=>({textContent:'',dataset:{},children:[],hidden:false,open:false,
  append(...items){this.children.push(...items)},replaceChildren(){this.children=[]}});
const element=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id)};
global.document={getElementById:element,createElement:node};
global.window={VanDashboardProfiles:{loadSettings:()=>({})}};
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(0,source.indexOf('\nbyId("refresh").addEventListener')));
const health={available:true,episodes:{active:[{id:579,evidence_state:'unavailable',
  latest_assessment:{title:'Coolant above historical band',state:'unavailable',reason:'Fresh evidence unavailable'}}]},
  data_quality:{active:[],recent:[{status:'resolved',detail:'Recovered A'},{status:'resolved',detail:'Recovered B'}]}};
renderEarlyWarnings(health);
assert.equal(element('warning-state').textContent,'1 TO REVIEW');
const active=element('warning-list').children;
assert.equal(active.length,1);
assert.equal(active[0].dataset.state,'unavailable');
assert.equal(active[0].dataset.unresolved,'true');
assert.equal(active[0].children[0].textContent,'Unresolved advisory');
assert.equal(element('warning-recovered-list').children.length,2);
assert.equal(element('warning-recovered-title').textContent,'Recovered Events · 2');
assert.equal(element('warning-recovered').hidden,false);
assert.equal(element('warning-recovered').open,false);
element('warning-recovered').open=true;
health.data_quality.active=[{status:'active',detail:'Current filter'}];
renderEarlyWarnings(health);
assert.equal(element('warning-list').children.length,2);
assert.equal(element('warning-recovered-list').children.length,2);
assert.equal(element('warning-recovered').open,true);
health.episodes.active=[];health.data_quality.active=[];
renderEarlyWarnings(health);
assert(!element('warning-state').textContent.includes('TO REVIEW'));
assert.equal(element('warning-recovered-list').children.length,2);
health.data_quality.recent=[];renderEarlyWarnings(health);
assert.equal(element('warning-recovered').hidden,true);
'''
        result = subprocess.run(["node", "-e", script, str(APP)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is required for warning UI test")
    def test_only_real_watches_or_open_episodes_become_cards(self):
        script = r"""
const fs = require("fs");
const vm = require("vm");

global.document = {
  visibilityState: "visible",
  getElementById: () => ({dataset: {}, textContent: ""}),
};
global.window = {
  VanDashboardProfiles: {
    loadSettings: () => ({selected: "overview", customWidgets: []}),
  },
};
global.performance = {now: () => 1000};

const source = fs.readFileSync(process.argv[1], "utf8");
const definitionsOnly = source.slice(
  0,
  source.indexOf('\nbyId("refresh").addEventListener'),
);
vm.runInThisContext(definitionsOnly + `
  globalThis.warningDashboardUnderTest = {selectWarningCards};
`);

const assessments = [
  {rule: "normal", state: "normal"},
  {
    rule: "no-data",
    state: "unavailable",
    persistence: {observed: 0, required: 10},
  },
  {
    rule: "training",
    state: "insufficient_history",
    persistence: {observed: 0, required: 10},
  },
  {
    rule: "watch",
    state: "watch",
    persistence: {observed: 1, required: 10},
  },
  {
    rule: "warning",
    state: "warning",
    persistence: {observed: 10, required: 10},
  },
];
const openEpisode = [{
  rule: "open-but-inconclusive",
  state: "unavailable",
  episode_id: 7,
  persistence: {observed: 0, required: 10},
}];

process.stdout.write(JSON.stringify({
  fallback: warningDashboardUnderTest
    .selectWarningCards([], assessments)
    .map((item) => item.rule),
  persisted: warningDashboardUnderTest
    .selectWarningCards(openEpisode, assessments)
    .map((item) => item.rule),
}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(APP)],
            check=True,
            capture_output=True,
            text=True,
        )
        rendered = json.loads(completed.stdout)

        self.assertEqual(rendered["fallback"], ["watch", "warning"])
        self.assertEqual(rendered["persisted"], ["open-but-inconclusive"])


if __name__ == "__main__":
    unittest.main()
