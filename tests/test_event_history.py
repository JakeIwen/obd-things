"""Offline event evidence acceptance cases, including the September 18 coolant watch."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from projects.vehicle_data.event_history import detail, EventReader, annotate, replay, parse_route, encode
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.early_warning import DEFAULT_WARNING_RULES, EarlyWarningEvaluator
from projects.vehicle_data.warning_chat import event_context, INSTRUCTIONS
from tests.test_vehicle_advisory_episodes import assessment
from tests.test_vehicle_historian import snapshot, available, definition

START = datetime(2026,9,18,0,33,13,tzinfo=timezone.utc)

def coolant(state='watch', value=215.6, offset=0, regime='engine_running:stationary:rpm_idle:warm'):
    a = assessment(state, rule='engine_coolant_temperature_relative_high', eligible=state=='warning')
    a.update(metric='engine.coolant_temperature', title='Coolant above comparable history', direction='high',
             regime=regime, baseline_regime='engine=engine_running|motion=stationary|rpm=rpm_idle',
             baseline={'median':190.4,'mad':3.6,'bucket_count':446,'trip_count':48,'unit':'°F'},
             deviation={'signed_from_median':value-190.4,'effect_in_rule_direction':value-190.4,'threshold':24.01812},
             persistence={'observed':1,'required':10,'window_seconds':60},
             rule_revision='test-v2',rule_snapshot={'max_age_seconds':10,'recovery_observations':3})
    a['current'].update(value=value,unit='°F',observed_at=(START+timedelta(seconds=offset)).isoformat(),
                        captured_at=(START+timedelta(seconds=offset)).isoformat(), source='ccan.broadcast.0x2ed',freshness='fresh',sample_id=123)
    return a


class EventEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'history.sqlite3'
        self.h=TelemetryHistorian(self.path);self.addCleanup(self.h.close)

    def record(self,a,offset=0):
        return self.h.record_advisory_assessments([a],evaluated_at=START+timedelta(seconds=offset))

    def event(self,id=1):
        code,result=detail(self.h._conn,id)
        self.assertEqual(code,200)
        return result['event']

    def test_coolant_opening_survives_stale_evidence_and_restart(self):
        self.record(coolant())
        a=coolant('unavailable',219.2,16);a.update(baseline=None,deviation=None,persistence={'observed':0,'required':10})
        self.record(a,27)
        e=self.event()
        self.assertEqual(e['first_assessment']['current']['value'],215.6)
        self.assertEqual(e['first_assessment']['baseline']['median'],190.4)
        self.assertAlmostEqual(e['first_assessment']['deviation']['signed_from_median'],25.2)
        self.assertIsNone(e['first_warning'])
        self.assertFalse(e['latest_assessment']['persistence']['evaluated'])
        self.assertEqual(e['latest_assessment']['current']['capture_freshness'],'fresh')
        self.assertNotIn('freshness',e['latest_assessment']['current'])
        self.assertEqual(e['status'],'open')
        with TelemetryHistorian(self.path) as reopened:
            e2=detail(reopened._conn,1)[1]['event']
            self.assertEqual(e2['first_assessment'],e['first_assessment'])

    def test_first_warning_separate_and_duplicate_evaluations_idempotent(self):
        self.record(coolant())
        a=coolant('warning',220,5);a['persistence']['observed']=10
        self.record(a,5);before=self.event()
        self.record(a,5);after=self.event()
        self.assertEqual(before['evidence'],after['evidence'])
        self.assertEqual(before['notifications'],after['notifications'])
        self.assertEqual(after['first_assessment']['state'],'watch')
        self.assertEqual(after['first_warning']['assessment']['state'],'warning')
        self.assertEqual(after['evidence']['observed_abnormal_seconds'],5)

    def test_regime_change_cannot_resolve_and_recovery_requires_distinct_samples(self):
        self.record(coolant())
        a=coolant('normal',150,5,regime='engine_running:stationary:rpm_low:cold');a['baseline_regime']='different'
        self.record(a,5)
        self.assertEqual(self.event()['evidence_state'],'not_applicable')
        for offset in (10,15,20):
            self.record(coolant('normal',195,10),offset)
        self.assertEqual(self.event()['status'],'open')
        self.assertEqual(self.event()['evidence']['recovery']['count'],1)
        self.record(coolant('normal',195,25),25) # gap resets recovery
        self.record(coolant('normal',195,30),30)
        self.assertEqual(self.event()['status'],'resolved')

    def test_gap_does_not_count_as_abnormal_duration(self):
        self.record(coolant())
        self.record(coolant('watch',218,3600),3600)
        e=self.event()
        self.assertEqual(e['evidence']['observed_abnormal_seconds'],0)
        self.assertEqual(e['evidence']['unobserved_seconds'],3600)
        self.assertEqual(e['evidence']['evaluator_gap_seconds'],3600)

    def test_window_survives_raw_deletion_and_budget_pruning_is_explicit(self):
        metric=definition('engine.coolant_temperature','°F')
        for n in range(3):
            at=START+timedelta(seconds=n*5)
            self.h.ingest_snapshot(snapshot(at,[metric],{'engine.coolant_temperature':available(metric,210+n,at)}),captured_at=at,ingest_key=f'p{n}')
        self.record(coolant('watch',215.6,10),10)
        e=self.event();self.assertEqual(len(e['sample_window']),3)
        with self.h._conn:
            self.h._conn.execute('DELETE FROM metric_samples')
        self.record(coolant('unavailable',215.6,10),20)
        self.assertEqual(len(self.event()['sample_window']),3)
        with patch('projects.vehicle_data.event_history.WINDOW_BUDGET',2):
            self.record(coolant('unavailable',215.6,10),25)
        self.assertEqual(self.event()['completeness']['sample_window'],'pruned_budget')
        self.assertEqual(self.event()['sample_window'],[])
        self.assertEqual(self.event()['first_assessment']['current']['value'],215.6)

    def test_annotations_are_idempotent_and_not_recovery(self):
        self.record(coolant())
        data={'kind':'dismissed','note':'Checked after parking','links':['maintenance:123'],'request_id':'note-1234567890'}
        with self.h._conn:
            annotate(self.h._conn,1,data);annotate(self.h._conn,1,data)
        e=self.event();self.assertEqual(e['status'],'open');self.assertEqual(len(e['annotations']),1)
        with self.assertRaises(ValueError):
            annotate(self.h._conn,1,{**data,'note':'Different'})

    def test_timeline_pagination_retains_opening_independently(self):
        self.record(coolant())
        for n in range(1,14):
            self.record(coolant('warning' if n%2 else 'watch',220+n,n*5),n*5)
        e=self.event();self.assertEqual(len(e['timeline']),10);self.assertIsNotNone(e['next_event_before'])
        old=detail(self.h._conn,1,e['next_event_before'])[1]['event']
        self.assertTrue(any(r['type']=='opened' for r in old['timeline']))
        self.assertEqual(old['first_assessment'],e['first_assessment'])

    def test_replay_never_writes_and_deduplicates_samples(self):
        self.record(coolant());e=self.event()
        e['sample_window']=[{'observed_at':(START+timedelta(seconds=n*5)).isoformat(),'value':215+n,'source':'x','freshness':'fresh','regime':'engine_running:x','evidence_ref':f'sample:{n}'} for n in range(3)]
        for point in e['sample_window']:
            point.update({k:e['first_assessment']['current'][k] for k in ('source','quality','provenance','unit')})
        e['sample_window'].insert(1,e['sample_window'][0])
        settings={'threshold':214,'operator':'above','persistence':3,'max_gap_seconds':10,'running_only':True}
        before=self.h._conn.total_changes
        result=replay(e,settings)
        self.assertEqual(len(result['points']),3);self.assertTrue(result['points'][-1]['would_warn'])
        self.assertTrue(result['counterfactual']);self.assertEqual(before,self.h._conn.total_changes)

    def test_reader_returns_pending_without_database_io_on_caller(self):
        self.record(coolant());reader=EventReader(str(self.path));self.addCleanup(reader.close)
        with patch.object(EventReader,'query',side_effect=lambda *args: (time.sleep(.15) or (200,{'available':True}))):
            start=time.monotonic();status,_=reader.request('GET','/v1/events/1')
            self.assertEqual(status,202);self.assertLess(time.monotonic()-start,.1)
            for _ in range(30):
                time.sleep(.02);status,result=reader.request('GET','/v1/events/1')
                if status!=202:break
            self.assertEqual(status,200)

    def test_invalid_routes_rejected(self):
        for route in ['/v1/events/../../x','/v1/events/0','/v1/events?before=-1','/v1/events?status=bad','/v1/events?q=a&q=b']:
            with self.subTest(route=route),self.assertRaises(ValueError):parse_route(route)

    def test_agent_receives_same_revision_and_understands_storage(self):
        self.record(coolant());packet=detail(self.h._conn,1)[1]
        class Client:
            def request(self,method,path):
                assert method=='GET'
                if path=='/v1/events/1':return 200,packet
                if path=='/v1/health':return 200,{'available':True}
                return 200,{}
        c=event_context(Client(),{'kind':'episode','id':'1'})
        self.assertEqual(c['event_revision'],packet['event']['revision'])
        self.assertEqual(c['event']['first_assessment']['baseline']['median'],190.4)
        self.assertIn('retention',c['system_guide'])
        self.assertIn('first_warning',INSTRUCTIONS)
        self.assertIn('counterfactual',INSTRUCTIONS)

    def test_event_backup_is_consistent_and_refuses_overwrite(self):
        from tools.telemetry_event_export import export
        self.record(coolant())
        out=Path(self.tmp.name)/'backup.jsonl'
        result=export(self.path,out)
        records=[json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(records[-1]['kind'],'complete')
        self.assertEqual(result['counts']['advisory_episodes'],1)
        self.assertEqual(next(r for r in records if r['kind']=='advisory_episodes')['record']['first_assessment_json'],encode(self.event()['first_assessment']))
        with self.assertRaises(FileExistsError):export(self.path,out)

    def test_rule_revision_closes_administratively_and_starts_separate_episode(self):
        self.record(coolant())
        changed=coolant('warning',220,5);changed['rule_revision']='changed'
        self.record(changed,5)
        old=self.event(1);new=self.event(2)
        self.assertEqual(old['status'],'resolved')
        self.assertIn('administrative',old['resolution_reason'])
        self.assertEqual(new['status'],'open')
        self.assertEqual(old['first_assessment']['rule_revision'],'test-v2')

    def test_coolant_early_running_and_engine_off_are_not_applicable(self):
        rule=next(r for r in DEFAULT_WARNING_RULES if r.metric=='engine.coolant_temperature')
        metric=definition('engine.coolant_temperature','°F')
        at=START
        self.h.ingest_snapshot(snapshot(at,[metric],{'engine.coolant_temperature':available(metric,215.6,at)},running=False),captured_at=at,ingest_key='asleep')
        result=EarlyWarningEvaluator(self.h,rules=[rule]).evaluate(at=at)['assessments'][0]
        self.assertEqual(result['state'],'not_applicable')
        self.assertEqual(result['applicability']['minimum_running_seconds'],300)

    def test_episode_evidence_rolls_back_when_outbox_fails(self):
        with patch.object(self.h,'_enqueue_advisory_notification_locked',side_effect=RuntimeError('storage failure')):
            with self.assertRaises(RuntimeError):self.record(coolant('warning'))
        for table in ('advisory_episodes','advisory_evidence','advisory_episode_events','advisory_notification_outbox'):
            self.assertEqual(self.h._conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0)

    def test_baseline_archives_retain_all_inputs_and_exclude_early_trip_buckets(self):
        at=START-timedelta(days=1)
        metric=definition('engine.coolant_temperature','°F')
        self.h.ingest_snapshot(snapshot(at,[metric],{'engine.coolant_temperature':available(metric,90,at)}),captured_at=at,ingest_key='prior-trip')
        source=metric['sources'][0]
        with self.h._conn:
            for n in range(140):
                us=int((at+timedelta(minutes=n)).timestamp()*1e6)
                value=90 if n<5 else 190
                self.h._conn.execute("INSERT INTO metric_rollups VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (us,60,1,metric['name'],'engine_running:stationary:rpm_idle:warm','°F',source['name'],'verified',source['provenance'],1,value,value,value,value,0,us,us))
        b=self.h.robust_baseline(metric['name'],'engine_running:stationary:rpm_idle:warm',before=START,unit='°F',quality='verified',source=source['name'],provenance=source['provenance'],regime_dimensions=('engine','motion','rpm'),minimum_trip_age_seconds=300)
        self.assertEqual(b.bucket_count,135);self.assertEqual(b.minimum,190)
        a=coolant();a['baseline']=b.as_dict();self.record(a)
        e=self.event();self.assertEqual(e['baseline_archives'][0]['bucket_count'],135)
        offset=0;inputs=[]
        while offset is not None:
            code,page=EventReader.query(self.h._conn,1,'baselines',{'digest':b.input_digest,'offset':str(offset)},None)
            self.assertEqual(code,200);inputs+=page['inputs'];offset=page['next_offset']
        self.assertEqual(len(inputs),135)
        self.assertEqual(inputs[-1]['median'],190)

    def test_interrupted_watch_archives_unconfirmed_after_its_quiet_window(self):
        self.record(coolant())
        unavailable=coolant('unavailable',219.2,16)
        unavailable.update(baseline=None,deviation=None)
        unavailable['rule_snapshot']['max_age_seconds']=10
        self.record(unavailable,27)
        paused=self.event()
        self.assertEqual(paused['status'],'open')
        self.assertEqual(paused['timeline'][0]['presentation'],'monitoring_note')
        self.assertEqual(paused['timeline'][0]['monitoring_note']['title'],'Monitoring paused')
        self.record(unavailable,60)
        archived=self.event()
        self.assertEqual(archived['outcome'],'unconfirmed')
        self.assertEqual(archived['status'],'resolved')
        self.assertEqual(archived['resolution_reason'],'unconfirmed_monitoring_ended')
        self.assertIsNone(archived['first_warning'])
        self.assertEqual(archived['first_assessment'],paused['first_assessment'])
        self.assertEqual(archived['timeline'][0]['type'],'watch_unconfirmed')
        self.assertFalse(self.h.list_advisory_episodes(active_only=True))
        self.assertFalse(self.h.pending_advisory_notifications())
        self.record(unavailable,300)
        self.assertEqual(len(self.event()['timeline']),len(archived['timeline']))
        self.record(coolant('watch',216,305),305)
        self.assertEqual(self.event(2)['status'],'open')
        self.assertEqual(self.event(1)['outcome'],'unconfirmed')

    def test_warning_that_deescalated_never_archives_as_unconfirmed(self):
        self.record(coolant('warning'))
        self.record(coolant('watch',217,5),5)
        self.record(coolant('unavailable',219,10),120)
        self.assertEqual(self.event()['status'],'open')
        self.assertIsNotNone(self.event()['first_warning'])
        self.assertNotIn('watch_unconfirmed',[v['type'] for v in self.event()['timeline']])

    def test_legacy_watch_archives_without_rewriting_saved_assessments(self):
        legacy=coolant();legacy.pop('rule_snapshot');legacy.pop('rule_revision')
        self.record(legacy)
        self.record(coolant('unavailable',219.2,16),27)
        original=self.event()['first_assessment']
        with self.h._conn:
            self.h._conn.execute('DELETE FROM advisory_evidence')
        self.record(coolant('unavailable',219.2,16),10000)
        e=self.event()
        self.assertEqual(e['outcome'],'unconfirmed')
        self.assertEqual(e['first_assessment'],original)
        self.assertEqual(e['completeness']['rule_revision'],'never_recorded_legacy')

    def test_usable_readings_reset_the_watch_quiet_window(self):
        self.record(coolant())
        self.record(coolant('unavailable',215.6,0),20)
        self.record(coolant('watch',216,50),50)
        self.record(coolant('unavailable',216,50),70)
        self.assertEqual(self.event()['status'],'open')
        self.record(coolant('unavailable',216,50),110)
        self.assertEqual(self.event()['outcome'],'unconfirmed')

    def test_late_restored_evidence_belongs_to_a_new_episode(self):
        self.record(coolant())
        self.record(coolant('unavailable',216,10),25)
        self.record(coolant('warning',225,100),100)
        self.assertEqual(self.event(1)['outcome'],'unconfirmed')
        self.assertIsNone(self.event(1)['first_warning'])
        self.assertEqual(self.event(2)['first_warning']['assessment']['state'],'warning')

    def test_coverage_note_preserves_age_and_saved_freshness_limit(self):
        from projects.vehicle_data.event_history import coverage_note
        a=coolant('unavailable',219.2,16)
        a['current']['effective_age_seconds']=11.798772
        a['rule_snapshot']['max_age_seconds']=10
        note=coverage_note(a)
        self.assertIn('11.8 seconds',note['detail'])
        self.assertIn('10 seconds',note['detail'])
        self.assertIn('does not',note['detail'])
        self.assertNotIn('shutdown',note['detail'])
        a.pop('rule_snapshot')
        self.assertIn('limit was not saved',coverage_note(a)['detail'])

    def test_coverage_advisory_requires_sustained_positive_running_evidence(self):
        from projects.vehicle_data.monitoring_coverage import coverage_assessments
        rpm=definition('engine.rpm','rpm')
        temperature=definition('engine.coolant_temperature','°F')
        primary=[coolant('unavailable')]
        at=START
        self.h.ingest_snapshot(snapshot(at,[rpm,temperature],{'engine.rpm':available(rpm,700,at),'engine.coolant_temperature':available(temperature,215,at)}),captured_at=at,ingest_key='coverage-0')
        for n in range(1,14):
            at=START+timedelta(seconds=5*n)
            self.h.ingest_snapshot(snapshot(at,[rpm,temperature],{'engine.rpm':available(rpm,700,at)}),captured_at=at,ingest_key=f'coverage-{n}')
            result=coverage_assessments(self.h,primary,at)[0]
            if n<12:self.assertNotEqual(result['state'],'warning')
        self.assertEqual(result['state'],'warning')
        self.assertEqual(result['category'],'telemetry_quality')
        self.assertFalse(result['notification_eligible'])
        self.h.record_advisory_assessments([result],evaluated_at=at)
        self.assertEqual(self.event()['category'] if 'category' in self.event() else self.h.list_advisory_episodes()[0]['category'],'telemetry_quality')
        no_rpm=coverage_assessments(self.h,primary,at+timedelta(seconds=20))[0]
        self.assertEqual(no_rpm['state'],'unavailable')
        at+=timedelta(seconds=25)
        self.h.ingest_snapshot(snapshot(at,[rpm,temperature],{'engine.rpm':available(rpm,0,at)},running=False),captured_at=at,ingest_key='coverage-stop')
        stopped=coverage_assessments(self.h,primary,at)[0]
        self.assertEqual(stopped['state'],'suppressed')
        self.h.record_advisory_assessments([stopped],evaluated_at=at)
        self.assertFalse(self.h.list_advisory_episodes(active_only=True))

    def test_silence_alone_creates_no_telemetry_gap_event(self):
        from projects.vehicle_data.monitoring_coverage import coverage_assessments
        observations=coverage_assessments(self.h,[coolant('unavailable')],START)
        self.assertTrue(all(a['state']=='unavailable' for a in observations))
        self.h.record_advisory_assessments(observations,evaluated_at=START)
        self.assertEqual(self.h.list_advisory_episodes(),[])

    def test_future_measurement_is_not_fresh_running_proof(self):
        from projects.vehicle_data.monitoring_coverage import age
        self.assertIsNone(age({'observed_at':(START+timedelta(seconds=5)).isoformat()},START))
