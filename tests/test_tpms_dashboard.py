import json
from pathlib import Path
import subprocess
import unittest


class TpmsDashboardTests(unittest.TestCase):
    def test_last_pressures_remain_visible_without_becoming_live(self):
        app = Path(__file__).resolve().parents[1] / "projects/vehicle_data/static/app.js"
        script = r'''
const fs=require('fs'), vm=require('vm'), elements=new Map();
function element(id) {
  if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},hidden:false});
  return elements.get(id);
}
global.document={getElementById:element};
global.window={VanDashboardProfiles:{loadSettings:()=>({})}};
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(0,source.indexOf('\nbyId("refresh").addEventListener')));
const positions=['fl','fr','rl','rr'];
const catalog=positions.map(p=>({name:`tire.pressure.${p}`,unit:'psi',minimum:0,maximum:150,stale_after_seconds:30,sources:[{name:`tpms.${p}`,quality:'verified'}]}));
const metrics=Object.fromEntries(catalog.map(d=>[d.name,{available:true,stale:true,age_ms:86400000,value:57.7,unit:'psi',source:d.sources[0].name,quality:'verified',observed_at:'2026-09-19T12:00:00Z'}]));
renderTires(catalog,metrics);
const parked={badge:element('tires-state').textContent,value:element('tire-fl').textContent,
  status:element('tire-fl-status').textContent,style:element('tire-fl-card').dataset.state,
  wheelYellow:element('tire-fl-status').dataset.retained,panelYellow:element('tires-state').dataset.retained};
metrics['tire.pressure.fl'].stale=false;metrics['tire.pressure.fl'].age_ms=100;
renderTires(catalog,metrics);
const mixed=element('tires-state').textContent;
metrics['tire.pressure.fr'].quality='candidate';
metrics['tire.pressure.rl'].value=950.5;
metrics['tire.pressure.rr']={available:false,reason:'sensor_unavailable'};
renderTires(catalog,metrics);
const invalid=positions.slice(1).map(p=>element(`tire-${p}`).textContent);
process.stdout.write(JSON.stringify({parked,mixed,invalid}));
'''
        completed = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["parked"]["badge"], "NOT LIVE · 4/4 LAST READINGS")
        self.assertEqual(result["parked"]["value"], "57.7")
        self.assertEqual(result["parked"]["style"], "stale")
        self.assertEqual(result["parked"]["wheelYellow"], "false")
        self.assertEqual(result["parked"]["panelYellow"], "true")
        self.assertNotIn("Last recorded", result["parked"]["status"])
        self.assertNotIn("NOT LIVE", result["parked"]["status"])
        self.assertIn("2026", result["parked"]["status"])
        self.assertEqual(result["mixed"], "1/4 LIVE · 3/4 LAST READINGS")
        self.assertEqual(result["invalid"], ["—", "—", "—"])
