from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "node is required")
class RetainedDashboardTests(unittest.TestCase):
    def test_retention_policy_and_renderer_freshness_separation(self):
        app = Path(__file__).resolve().parents[1] / "projects/vehicle_data/static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},hidden:false});return elements.get(id)};
global.document={getElementById:element,querySelector:element};
global.window={VanDashboardProfiles:{loadSettings:()=>({})}};
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(0,source.indexOf('\nbyId("refresh").addEventListener')));
const def=(name,unit='°F',quality='verified')=>({name,unit,stale_after_seconds:5,minimum:0,maximum:300,sources:[{name:'source.'+name,quality}]});
const dated=(d,value=175)=>({available:true,stale:true,age_ms:86400000,value,unit:d.unit,source:d.sources[0].name,
  quality:d.sources[0].quality,observed_at:'2026-08-01T12:00:00Z'});
const temperature=def('engine.vvt_oil_temperature');
const saved=dated(temperature);
const stopped={available:false,reason:'engine_not_running',last_recorded:saved};
assert.equal(displayQuality('verified'),'');
assert.equal(displayQuality('candidate'),'CANDIDATE');
assert.equal(displayQuality('observed_alfa_scale'),'ALFA SCALE');
assert(!lastRecordedStatus(saved,stopped).includes('VERIFIED'));
let result=renderEngineMetric('oilTemperature',[temperature],{[temperature.name]:stopped});
assert.equal(element('engine-oil-temperature').textContent,'175');
assert(!element('engine-oil-temperature-status').textContent.includes('NOT LIVE'));
assert(element('engine-oil-temperature-status').textContent.includes('2026'));
assert.equal(element('engine-oil-temperature-status').dataset.retained,'false');
assert.equal(result.state.heroReady,false);assert.equal(result.retained,true);
assert.equal(element('engine-oil-temperature-card').dataset.state,'stale');
for(const name of ['engine.rpm','vehicle.speed','vehicle.ignition_on','transmission.gear','engine.oil_pressure',
  'engine.crankshaft_torque','engine.crankshaft_power','engine.target_crankshaft_torque',
  'generator.field_duty','transmission.output_speed','transmission.turbine_speed','diagnostics.cluster.did.0107.raw']) {
  const d=def(name);assert.equal(lastRecordedObservation(d,dated(d)),null,name);
}
for(const patch of [{value:NaN},{value:999},{unit:'psi'},{source:'unknown'},{quality:'candidate'},
  {observed_at:null},{observed_at:'garbage'},{observed_at:'2100-01-01T00:00:00Z'}]) {
  assert.equal(lastRecordedObservation(temperature,{...saved,...patch}),null);
}
const pressure=def('engine.oil_pressure','psi');
renderEngineMetric('oilPressure',[pressure],{[pressure.name]:dated(pressure,30)});
assert.equal(element('engine-oil-pressure').textContent,'—');
const oil=def('engine.oil_life_remaining','%');oil.maximum=100;
renderOilLife([],{});assert.equal(element('service-oil-life').textContent,'—');
renderOilLife([oil],{[oil.name]:dated(oil,72)});
assert.equal(element('service-oil-life').textContent,'72%');
assert(element('service-oil-life-detail').textContent.includes('2026'));
assert.equal(element('service-oil-life-detail').dataset.retained,'false');
renderOilLife([oil],{[oil.name]:dated(oil,125)});
assert.equal(element('service-oil-life').textContent,'—');
renderOilLife([oil],{[oil.name]:{...dated(oil,73),stale:false,age_ms:100}});
assert.equal(element('service-oil-life').textContent,'73%');
assert(!element('service-oil-life-detail').textContent.includes('NOT LIVE'));
const fresh={...saved,stale:false,age_ms:100};
result=renderEngineMetric('oilTemperature',[temperature],{[temperature.name]:fresh});
assert.equal(result.state.heroReady,true);assert.equal(result.retained,false);
const nodes={article:{dataset:{},classList:{contains:()=>false,toggle:()=>{}}},badge:element('badge'),value:element('value'),meta:element('meta')};
updateMetricCard(nodes,def('transmission.output_speed','rpm'),dated(def('transmission.output_speed','rpm'),1500));
assert.equal(nodes.value.textContent,'—');
updateMetricCard(nodes,temperature,stopped);assert(nodes.value.textContent.includes('175'));
assert(nodes.meta.textContent.includes('2026'));
assert.equal(nodes.meta.dataset.retained,'false');
assert.equal(sectionReadingStatus(0,1,1),'NOT LIVE');
assert.equal(sectionReadingStatus(1,1,1),'1/1 LIVE');
assert.equal(sectionReadingStatus(0,0,6),'NOT LIVE · 0/6 MAPPED');
assert.equal(sectionReadingStatus(2,6,6,3),'2/6 LIVE · 3/6 LAST READINGS');
assert(!lastRecordedStatus(saved,{available:false,reason:'stale'}).includes('latest attempt'));
assert.equal(lastRecordedStatus(saved,{available:false,reason:'stale'}),formatTimestamp(saved.observed_at));
assert.equal(metricStatus(temperature,{available:false,reason:'stale'},observationState(temperature,{available:false})),'stale');
'''
        result = subprocess.run(["node", "-e", script, str(app)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
