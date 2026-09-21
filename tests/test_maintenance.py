from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data.maintenance import MaintenanceStore
from projects.vehicle_data.models import success
from tests import test_vehicle_data as api_fixtures


def entry(**changes):
    return {"date": "2026-07-21", "mileage_mi": 51200.0,
            "mileage_source": "cluster_or_receipt", "notes": "Oil + filter",
            "request_id": "oil_test_1234567890", **changes}


class MaintenanceTests(unittest.TestCase):
    def test_service_log_survives_restart_and_retries_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance.json"
            store = MaintenanceStore(path)
            record = store.add_oil_change(entry())
            self.assertEqual(record, store.add_oil_change(entry()))
            restored = MaintenanceStore(path)
            self.assertEqual(restored.snapshot()["record_count"], 1)
            self.assertEqual(restored.snapshot()["last_oil_change"]["mileage_mi"], 51200)
            with self.assertRaises(ValueError):
                restored.add_oil_change(entry(notes="changed under same ID"))
            restored.add_oil_change(entry(date="2026-01-01", request_id="older_service_123"))
            self.assertEqual(restored.snapshot()["last_oil_change"]["date"], "2026-07-21")

    def test_invalid_inputs_and_failed_write_never_acknowledge_a_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MaintenanceStore(Path(directory) / "maintenance.json")
            for payload in (entry(date="2099-01-01"), entry(mileage_mi=True),
                            entry(mileage_mi=float("nan")), entry(mileage_mi=-1),
                            entry(mileage_source="unknown"), entry(notes="x"*801),
                            {**entry(), "reset_ecu": True}):
                with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                    store.add_oil_change(payload)
            self.assertEqual(store.snapshot()["record_count"], 0)

    def test_mileage_flush_failure_is_reported_without_stopping_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MaintenanceStore(Path(directory) / "maintenance.json")
            store.restore_odometer({"source": "ics.did.2001", "unit": "mi", "value": 53000,
                                    "observed_at": "2026-09-01T00:00:00+00:00"})
            with mock.patch("projects.vehicle_data.maintenance._atomic_json", side_effect=OSError("disk full")):
                store.flush()
            self.assertTrue(store.dirty)
            self.assertIn("disk full", store.snapshot()["storage_error"])
            store.flush()
            self.assertFalse(store.dirty)
            self.assertIsNone(store.snapshot()["storage_error"])
            with mock.patch("projects.vehicle_data.maintenance._atomic_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.add_oil_change(entry())
            self.assertEqual(store.snapshot()["record_count"], 0)

    def test_unknown_mileage_is_supported_and_corrupt_storage_preserved(self):
        store = MaintenanceStore()
        store.add_oil_change(entry(mileage_mi=None, mileage_source="unknown"))
        self.assertIsNone(store.snapshot()["last_oil_change"]["mileage_mi"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance.json"
            path.write_text("broken previous data")
            bad = MaintenanceStore(path)
            self.assertFalse(bad.snapshot()["available"])
            with self.assertRaises(OSError):
                bad.add_oil_change(entry())
            bad.flush()
            self.assertEqual(path.read_text(), "broken previous data")

    def test_last_known_odometer_is_retained_with_original_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance.json"
            store = MaintenanceStore(path)
            stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
            result = success(metric="vehicle.odometer", value=53800.5, unit="mi",
                             source="ics.did.2001", bus="b-can", quality="candidate",
                             acquisition="physical_read_data_by_identifier",
                             observed_at=stamp, observed_monotonic=1)
            store.observe_odometer(result)
            store.flush()
            last = MaintenanceStore(path).snapshot()["last_known_odometer"]
            self.assertEqual(last["value"], 53800.5)
            self.assertEqual(last["observed_at"], stamp.isoformat())
            store.restore_odometer({**last, "value": 40000, "observed_at": "2026-08-01T00:00:00+00:00"})
            self.assertEqual(store.snapshot()["last_known_odometer"]["value"], 53800.5)


class MaintenanceApiTests(unittest.TestCase):
    setUp = api_fixtures.ApiTests.setUp
    tearDown = api_fixtures.ApiTests.tearDown

    def test_service_record_only_route(self):
        path = Path(self.tmp.name) / "maintenance.json"
        self.broker.maintenance = MaintenanceStore(path)
        code, response = self.client.request("POST", "/v1/maintenance/oil-changes", entry())
        self.assertEqual(code, 201)
        code, response = self.client.request("GET", "/v1/maintenance")
        self.assertEqual(code, 200)
        self.assertEqual(response["record_count"], 1)
        self.assertFalse(response["oil_life"]["available"])
        self.assertEqual(self.acquirer.calls, [])


class MaintenanceWebTests(unittest.TestCase):
    setUp = api_fixtures.WebTests.setUp
    tearDown = api_fixtures.WebTests.tearDown
    request = api_fixtures.WebTests.request

    def test_same_origin_required_and_record_is_visible_on_reload(self):
        self.api.broker.maintenance = MaintenanceStore(Path(self.tmp.name) / "maintenance.json")
        path = "/v1/maintenance/oil-changes"
        status, _ = self.request("POST", path, entry())
        self.assertEqual(status, 403)
        status, _ = self.request("POST", path, entry(), headers={"Origin": "http://unrelated.example"})
        self.assertEqual(status, 403)
        origin = f"http://127.0.0.1:{self.web.server_port}"
        status, _ = self.request("POST", path, entry(), headers={"Origin": origin})
        self.assertEqual(status, 201)
        status, body = self.request("GET", "/v1/maintenance")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["last_oil_change"]["date"], "2026-07-21")
        self.assertEqual(self.acquirer.calls, [])


class MaintenanceUiTests(unittest.TestCase):
    def test_stale_mileage_and_unmapped_oil_life_are_honest(self):
        app = Path(__file__).resolve().parents[1] / "projects/vehicle_data/static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm');
const nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{textContent:'',dataset:{},disabled:false,children:[],replaceChildren(){this.children=[];},append(n){this.children.push(n);}});return nodes.get(id);}
global.document={getElementById:element,createElement:()=>({textContent:''})};
global.window={VanDashboardProfiles:{loadSettings:()=>({})}};
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source.slice(0,source.indexOf('\nbyId("refresh").addEventListener')));
supplemental.maintenance={available:true,persistent:true,last_known_odometer:{value:53191.86,observed_at:'2026-08-28T00:00:00Z'},
last_oil_change:{date:'2026-07-21',mileage_mi:50000,mileage_source:'cluster_or_receipt',notes:'<script>not executed</script>'},oil_changes:[]};
renderMaintenance(supplemental.maintenance);
process.stdout.write(JSON.stringify({odo:element('service-odometer').textContent,age:element('service-odometer').title,
oil:element('service-oil-life').textContent,distance:element('oil-distance').textContent,notes:element('oil-last-notes').textContent,disabled:element('oil-change-save').disabled}));
'''
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["odo"], "53,191 mi")
        self.assertNotIn("not live", payload["age"])
        self.assertIn("2026", payload["age"])
        self.assertEqual(payload["oil"], "—")
        self.assertIn("different mileage sources", payload["distance"])
        self.assertEqual(payload["notes"], "<script>not executed</script>")
        self.assertFalse(payload["disabled"])
