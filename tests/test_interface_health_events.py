"""Offline event classification: initialization, active ownership, real failures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import copy
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.early_warning import InfrastructureHealthEvaluator
from projects.vehicle_data.historian import TelemetryHistorian
from tests.test_vehicle_data import FakeAcquirer, FakeClock
from tests.test_vehicle_historian import snapshot, role_aware_interface, role_status

START=datetime(2026,9,21,tzinfo=timezone.utc)


def role_snapshot():
    roles={key:role_status(key,channel,rate) for key,channel,rate in
           [('c-can','can0',500000),('b-can','can1',125000),('can-ch','can2',500000)]}
    for role in roles.values():
        role['actual'].update(present=True,fd_enabled=False,one_shot=False,restart_ms=0)
        role['reason']='ready'
    roles['b-can']['expected'].update(dev_id=1,bitrate=125000)
    return role_aware_interface(roles=roles)


class InterfaceHealthEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.h=TelemetryHistorian(Path(self.tmp.name)/'history.sqlite3');self.addCleanup(self.h.close)
        self.evaluator=InfrastructureHealthEvaluator(self.h)

    def ingest(self, offset, probe='ready', roles=True, override=None):
        at=START+timedelta(seconds=offset)
        p=snapshot(at,[],{},running=False)
        p['status']['interface']=role_snapshot()
        if not roles:p['status']['interface']['role_interfaces']['roles']={}
        p['status']['interface_probe']={'state':probe,'elapsed_seconds':offset,'startup_grace_seconds':30,'producer_instance':'test'}
        if override:override(p)
        saved=self.h.ingest_snapshot(p,captured_at=at)
        result=self.evaluator.evaluate(saved.snapshot_id,at=at)
        self.h.record_advisory_assessments(result['assessments'],evaluated_at=at)
        return {a['rule']:a for a in result['assessments']}

    def test_startup_is_unknown_and_not_three_unhealthy_episodes(self):
        for t in (0,5,10):
            report=self.ingest(t,probe='awaiting_first_probe',roles=False)
            for role in ('c_can','b_can','can_ch'):
                a=report['can_interface_role_'+role]
                self.assertEqual(a['state'],'unavailable')
                self.assertNotIn('unhealthy',a['title'])
        self.assertEqual(self.h.list_advisory_episodes(),[])
        report=self.ingest(15)
        self.assertEqual(self.h.list_advisory_episodes(),[])
        self.assertEqual(report['can_interface_role_b_can']['state'],'normal')

    def test_discovery_timeout_is_one_cause_specific_status_event(self):
        self.ingest(0,probe='awaiting_first_probe',roles=False)
        self.ingest(35,probe='awaiting_first_probe',roles=False)
        episodes=self.h.list_advisory_episodes(active_only=True)
        self.assertEqual(len(episodes),1)
        self.assertEqual(episodes[0]['rule'],'can_interface_status_probe')
        self.assertEqual(episodes[0]['title'],'Interface discovery delayed')
        self.ingest(40)
        self.assertFalse(self.h.list_advisory_episodes(active_only=True))

    def test_probe_failure_is_not_a_claim_of_three_broken_adapters(self):
        report=self.ingest(0,probe='failed',roles=False)
        episodes=self.h.list_advisory_episodes(active_only=True)
        self.assertEqual(len(episodes),1)
        self.assertEqual(episodes[0]['title'],'Interface status probe failed')
        # System items are dashboard System notes only; they never notify.
        self.assertFalse(report['can_interface_status_probe']['notification_eligible'])

    def test_actual_missing_role_still_persists_to_warning(self):
        self.ingest(0)
        def missing(p):
            p['status']['interface']['role_interfaces']['roles']['b-can'].update(resolution='missing',passive_ready=False,reason='role_missing')
        first=self.ingest(5,override=missing)['can_interface_role_b_can']
        # The first observation confirms nothing and opens no System event.
        self.assertEqual(first['state'],'normal')
        self.assertEqual(first['current']['pending'],{'observed':1,'required':2})
        self.assertIn('first observation',first['reason'])
        self.assertFalse([e for e in self.h.list_advisory_episodes(active_only=True)
                          if e['rule']=='can_interface_role_b_can'])
        second=self.ingest(10,override=missing)['can_interface_role_b_can']
        self.assertEqual(second['state'],'warning')
        self.assertEqual(second['title'],'b-can adapter is missing')
        self.assertNotIn('pending',second['current'])
        episode=[e for e in self.h.list_advisory_episodes(active_only=True)
                 if e['rule']=='can_interface_role_b_can']
        self.assertEqual([e['state'] for e in episode],['warning'])
        # A missing adapter notifies only after about five minutes (60 samples).
        self.assertFalse(second['notification_eligible'])
        self.assertEqual(second['severity'],'info')
        self.assertEqual((second['tier'],second['group']),(3,'system'))

    def test_controller_error_even_with_topology_failure_is_immediate(self):
        def failed(p):
            b=p['status']['interface']['role_interfaces']['roles']['b-can']
            b.update(passive_ready=False,reason='controller_not_error_active')
            b['actual']['controller_state']='BUS-OFF'
        a=self.ingest(0,probe='awaiting_first_probe',override=failed)['can_interface_role_b_can']
        self.assertEqual(a['state'],'warning')
        self.assertEqual(a['title'],'b-can controller error')
        self.assertEqual(a['severity'],'warning')
        self.assertFalse(a['notification_eligible'])
        self.assertEqual(a['persistence']['required'],1)

    def test_deaf_adapter_is_an_immediate_system_note(self):
        """2026-09-22: Board A stopped receiving with every link check passing."""
        def deaf(p):
            c=p['status']['interface']['role_interfaces']['roles']['c-can']
            c['receive_watch']={'receive_silent':True,'silent_seconds':180.0,
                                'reference_role':'can-ch','reference_active_seconds':180.0}
        a=self.ingest(0,override=deaf)['can_interface_role_c_can']
        self.assertEqual(a['state'],'warning')
        self.assertEqual(a['title'],'c-can adapter hears nothing while CAN-CH is active')
        self.assertEqual(a['action'],InfrastructureHealthEvaluator.DEAF_ACTION)
        self.assertFalse(a['notification_eligible'])
        self.assertEqual(a['tier'],3)

    def test_system_items_never_enqueue_a_notification(self):
        """Controller error, failed probe, restoration inhibit and a role missing
        for 60 observations: every System item stays off the phone (owner
        decision 2026-09-24).  Iteration 1 enqueued three of these."""

        from projects.vehicle_data.early_warning import InfrastructureHealthEvaluator as Evaluator
        from tests.test_warning_rules_v3 import StubHistorian
        roles={role:{'health':'healthy','resolution':'resolved','reason':'ready',
                     'active_gap':{'observation_count':0}} for role in ('c-can','b-can','can-ch')}
        roles['c-can']={'health':'unhealthy','resolution':'resolved','reason':'controller_unhealthy',
                        'active_gap':{'observation_count':0}}
        roles['b-can']={'health':'unhealthy','resolution':'missing','reason':'role_missing',
                        'active_gap':{'observation_count':60}}
        context={'roles':roles,'active_interface_gaps':{},'topology_changed':True,
                 'topology_generation':'b','previous_topology_generation':'a','issues':[],
                 'active_inhibits':[],'restoration_failed':True,
                 'interface_probe':{'state':'failed','elapsed_seconds':5}}
        usb={'available':True,'new_removal_events':[],'active_incidents':[],'removal_event_count_24h':0}
        report=Evaluator(StubHistorian(context,usb)).evaluate(1,at=START)
        items={a['rule']:a for a in report['assessments']}
        # Severity still ranks the hard faults in the System notes.
        self.assertEqual(items['can_interface_role_c_can']['severity'],'warning')
        self.assertEqual(items['can_interface_role_b_can']['severity'],'warning')
        self.assertEqual(items['can_interface_status_probe']['state'],'warning')
        self.assertEqual(items['can_restoration_inhibit']['severity'],'critical')
        for a in report['assessments']:
            self.assertIs(a['notification_eligible'],False,a['rule'])
        self.h.record_advisory_assessments(report['assessments'],evaluated_at=START)
        self.assertTrue(self.h.list_advisory_episodes(active_only=True))
        with self.h._lock:
            rows=self.h._conn.execute('SELECT COUNT(*) FROM advisory_notification_outbox').fetchone()[0]
        self.assertEqual(rows,0)

    def test_restart_initialization_does_not_resolve_a_prior_real_failure(self):
        def failed(p):
            b=p['status']['interface']['role_interfaces']['roles']['b-can']
            b.update(passive_ready=False,reason='controller_not_error_active')
            b['actual']['controller_state']='BUS-OFF'
        self.ingest(0,override=failed)
        self.ingest(5,probe='awaiting_first_probe',roles=False)
        episodes=self.h.list_advisory_episodes(active_only=True)
        self.assertEqual(len(episodes),1)
        self.assertEqual(episodes[0]['rule'],'can_interface_role_b_can')
        self.assertEqual(episodes[0]['evidence_state'],'unavailable')

    def broker(self):
        delegate=SimpleNamespace(channel='can1',expected_usb_serial='test-serial-b-can',expected_dev_id=1)
        # The fixture role registry uses this exact serial spelling.
        delegate.expected_usb_serial=role_snapshot()['role_interfaces']['roles']['b-can']['expected']['usb_serial']
        wrapper=SimpleNamespace(_delegate=delegate,stop=lambda:None)
        b=TelemetryBroker(acquirer=FakeAcquirer(),auxiliary_drive_supervisor=wrapper,auxiliary_drive_enabled=True)
        self.addCleanup(b.close)
        b._interface_status=role_snapshot()
        b.handle_auxiliary_drive_event({'type':'status','state':'armed_diagnostic','reason':'running_gate_satisfied',
            'detail':'exact fixed helper active','interface_mode':'armed_diagnostic','pid':1234})
        return b

    def health(self,b):
        payload={'status':b.status_response()}
        return TelemetryHistorian._interface_health(TelemetryHistorian._interface_payloads(payload)['b-can'])

    def test_verified_bcan_active_owner_is_healthy_without_passive_claim(self):
        b=self.broker()
        result=b.status_response()['interface']['role_interfaces']['roles']['b-can']
        self.assertTrue(result['topology_usable'])
        self.assertFalse(result['passive_ready'])
        self.assertFalse(result['actual']['listen_only'])
        self.assertEqual(self.health(b),('healthy','armed_diagnostic'))
        self.assertTrue(b._interface_status['role_interfaces']['roles']['b-can']['actual']['listen_only'])

    def test_verified_bcan_owner_is_named(self):
        role=self.broker().status_response()['interface']['role_interfaces']['roles']['b-can']
        self.assertEqual(role['armed_owner'],'broker_auxiliary_drive')

    def ccan_broker(self, *, reason='interface_armed'):
        """Broker whose active-drive helper holds C-CAN after a status refresh.

        Episodes 643/644 (2026-09-25 00:00:05Z and 02:00:05Z): the 18:00 and
        20:00 MDT voltage_mon acquisitions refreshed interface status mid-drive;
        the re-probed role said interface_armed / passive_ready false, the
        top-level topology.usable copied that, and the historian recorded the
        broker's own armed channel as topology_unusable until the trip ended.
        """
        b=TelemetryBroker(acquirer=FakeAcquirer(),active_drive_enabled=True)
        self.addCleanup(b.close)
        status=role_snapshot()
        c=status['role_interfaces']['roles']['c-can']
        c.update(passive_ready=False,reason=reason,detail='can0 is not listen-only')
        c['actual']['listen_only']=False
        status.update(listen_only=False,topology={'bus':'c-can','usable':False,'reason':'can0 is not listen-only'})
        b._interface_status=status
        b.handle_active_drive_event({'type':'status','state':'armed_diagnostic','reason':'running_gate_satisfied',
            'detail':'coordinated owner is armed','interface_mode':'armed_diagnostic','pid':4321})
        return b

    def ccan_health(self,b):
        payload={'status':b.status_response()}
        return TelemetryHistorian._interface_health(TelemetryHistorian._interface_payloads(payload)['c-can'])

    def test_own_armed_ccan_after_a_status_refresh_is_healthy(self):
        b=self.ccan_broker()
        self.assertEqual(self.ccan_health(b),('healthy','armed_diagnostic'))
        payload=TelemetryHistorian._interface_payloads({'status':b.status_response()})['c-can']
        self.assertEqual(payload['armed_owner'],'broker_active_drive')
        # Two snapshots, as live: no System note opens.
        def armed(p):
            p['status']['interface']=b.status_response()['interface']
        for t in (0,6):
            report=self.ingest(t,override=armed)
            self.assertEqual(report['can_interface_role_c_can']['state'],'normal')
        self.assertFalse([e for e in self.h.list_advisory_episodes(active_only=True)
                          if e['rule']=='can_interface_role_c_can'])

    def test_armed_ccan_with_a_route_fault_still_flags(self):
        b=self.ccan_broker(reason='bitrate_mismatch')
        self.assertEqual(self.ccan_health(b),('unhealthy','topology_unusable'))

    def test_ccan_armed_by_someone_else_still_flags(self):
        """No broker owner: a non-listen-only C-CAN is a configuration mismatch."""
        def external(p):
            c=p['status']['interface']['role_interfaces']['roles']['c-can']
            c.update(passive_ready=False,reason='interface_armed',detail='can0 is not listen-only')
            c['actual']['listen_only']=False
        self.ingest(0,override=external)
        a=self.ingest(6,override=external)['can_interface_role_c_can']
        self.assertEqual(a['state'],'warning')
        self.assertEqual(a['title'],'c-can interface configuration mismatch')
        self.assertIn('topology_unusable',a['reason'])

    def test_active_owner_override_cannot_hide_health_or_identity_faults(self):
        b=self.broker();original=copy.deepcopy(b._interface_status)
        for field,value in [('controller_state','ERROR-PASSIVE'),('present',False),('up',False),('fd_enabled',True),('one_shot',True),('restart_ms',100),('bitrate',500000)]:
            with self.subTest(field=field):
                b._interface_status=copy.deepcopy(original)
                b._interface_status['role_interfaces']['roles']['b-can']['actual'][field]=value
                self.assertEqual(self.health(b)[0],'unhealthy')
        for field,value in [('channel','can9'),('resolution','ambiguous')]:
            b._interface_status=copy.deepcopy(original)
            b._interface_status['role_interfaces']['roles']['b-can'][field]=value
            self.assertEqual(self.health(b)[0],'unhealthy')
        b._interface_status=copy.deepcopy(original)
        b._auxiliary_drive['owner_route']['usb_serial']='wrong-serial'
        self.assertEqual(self.health(b)[0],'unhealthy')

    def test_unverified_owner_or_failed_restoration_cannot_get_override(self):
        b=self.broker()
        for change in ({'owner_route':None},{'state':'failed'},{'helper_pid':None},{'restoration_failed':True}):
            saved=copy.deepcopy(b._auxiliary_drive)
            b._auxiliary_drive.update(change)
            self.assertEqual(self.health(b)[0],'unhealthy')
            b._auxiliary_drive=saved
        b._interface_status['active_inhibits']=['restoration-failed']
        self.assertEqual(self.health(b)[0],'unhealthy')

    def test_probe_status_is_explicit_and_cache_only(self):
        clock=FakeClock();acquirer=FakeAcquirer()
        b=TelemetryBroker(acquirer=acquirer,monotonic=clock);self.addCleanup(b.close)
        with mock.patch.object(acquirer,'status_snapshot',side_effect=RuntimeError('test')) as probe:
            status=b.status_response()['interface_probe']
            self.assertEqual(status['state'],'awaiting_first_probe');probe.assert_not_called()
            b._refresh_interface_status()
            self.assertEqual(b.status_response()['interface_probe']['state'],'failed')
        b._refresh_interface_status()
        self.assertEqual(b.status_response()['interface_probe']['state'],'ready')

    def test_legacy_display_title_is_clarified_without_rewriting_evidence(self):
        from projects.vehicle_data.event_history import detail
        from tests.test_vehicle_advisory_episodes import assessment
        a=assessment('watch',rule='can_interface_role_b_can')
        a.update(title='b-can interface role is unhealthy',category='can_infrastructure',metric=None,
                 current={'role':'b-can','reason':'interface_role_absent','health':'missing'})
        self.h.record_advisory_assessments([a],evaluated_at=START)
        e=detail(self.h._conn,1)[1]['event']
        self.assertEqual(e['title'],'b-can interface status was unavailable')
        self.assertEqual(e['original_title'],a['title'])
        self.assertEqual(e['first_assessment']['title'],a['title'])
        self.assertEqual(e['timeline'][0]['assessment']['title'],a['title'])
