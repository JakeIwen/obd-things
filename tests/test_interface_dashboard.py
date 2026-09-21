import json
from pathlib import Path
import subprocess
import unittest


class InterfaceDashboardTests(unittest.TestCase):
    def test_roles_replace_single_channel_and_transient_owner(self):
        app = Path(__file__).resolve().parents[1] / "projects/vehicle_data/static/app.js"
        script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
function node() { return {textContent:'',dataset:{},hidden:false,children:[],
  append(...nodes){this.children.push(...nodes)},replaceChildren(){this.children=[]}}; }
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,node());return elements.get(id);}
global.document={getElementById:element,createElement:node};
global.window={VanDashboardProfiles:{loadSettings:()=>({})}};
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(0,source.indexOf('\nbyId("refresh").addEventListener')));
const role=(channel,bitrate,pair)=>({channel,safe:true,reason:'ready',
  expected:{bitrate,pair,board:'A',connector:'CAN1',usb_serial:'test-123456',dev_id:0},
  actual:{present:true,up:true,bitrate,listen_only:true,fd_enabled:false,controller_state:'ERROR-ACTIVE'}});
const roles={'c-can':role('can7',500000,'6/14'),'b-can':role('can5',125000,'3/11'),
  'can-ch':role('can9',500000,'12/13'),spare:{safe:true,channel:'can8',expected:{passive_required:false},actual:{up:false}}};
const status={interface:{channel:'can0',active_inhibits:[],role_interfaces:{roles}}};
renderInterface(status);
assert.equal(element('interface-links').textContent,'3/3');
assert.equal(element('interface-roles').children.length,3);
assert.deepEqual(element('interface-roles').children.map(c=>c.children[0].textContent),['C-CAN','B-CAN','CAN CH']);
assert(element('interface-identities').children[0].textContent.includes('can7'));
assert(!elements.has('current-owner'));assert(!elements.has('channel'));
const first=element('interface-roles').children[0];
status.current_owner={kind:'broker',operations:['read']};renderInterface(status);
status.current_owner=null;renderInterface(status);
assert.strictEqual(element('interface-roles').children[0],first);
roles['c-can'].actual.listen_only=false;roles['c-can'].operating_mode='armed_diagnostic';
renderInterface(status);
assert(element('interface-roles').children[0].children[2].textContent.includes('Diagnostics enabled'));
assert.equal(element('interface-roles').children[0].dataset.state,'ready');
roles['b-can'].safe=false;roles['b-can'].actual.controller_state='BUS-OFF';
roles['b-can'].detail='controller bus-off';status.interface.active_inhibits=['restore_failed'];
renderInterface(status);
assert.equal(element('inhibits').textContent,'restore_failed');
assert.equal(element('interface-roles').children[1].dataset.state,'unavailable');
assert(element('interface-roles').children[1].children[2].textContent.includes('BUS-OFF'));
roles.spare.safe=false;roles.spare.detail='spare unexpectedly up';
status.interface.role_interfaces.issues=[{detail:'USB identity ambiguity'}];renderInterface(status);
assert.equal(element('interface-issues').hidden,false);
assert(element('interface-issues').textContent.includes('spare unexpectedly up'));
assert(element('interface-issues').textContent.includes('USB identity ambiguity'));
delete roles['can-ch'];delete roles['b-can'].actual.bitrate;renderInterface(status);
assert.equal(element('interface-links').textContent,'2/3');
assert(element('interface-roles').children[1].children[1].textContent.includes('Bitrate unknown'));
assert.equal(element('interface-roles').children[2].dataset.state,'unavailable');
renderInterface({});
assert.equal(element('interface-links').textContent,'0/3');
assert.equal(element('inhibits').textContent,'Unknown');
assert(element('interface-roles').children.every(c=>c.dataset.state==='unavailable'));
process.stdout.write(JSON.stringify({passed:true}));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(app)], capture_output=True, text=True, check=True,
        )
        self.assertTrue(json.loads(completed.stdout)["passed"])
