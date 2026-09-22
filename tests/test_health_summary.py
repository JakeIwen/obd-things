"""Oversized historical evidence must not break the overview's wire budget."""
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data.api import MAX_RESPONSE_BYTES
from projects.vehicle_data.health_summary import health_overview, OVERVIEW_BUDGET_BYTES
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.insights import TelemetryInsights
from projects.vehicle_data.event_history import detail
from tests.test_vehicle_advisory_episodes import assessment


def encoded_size(value):
    return len(json.dumps(value,sort_keys=True,separators=(',',':')).encode())


def large_assessment():
    a=assessment('warning',eligible=True)
    baseline={'median':190.4,'mad':3.6,'unit':'°F','input_digest':'a'*64,'bucket_count':446,'trip_count':48,
              'input_buckets':[{'bucket_us':i*60000000,'trip_key':i%48,'median':190.4,
                               'regime':'engine_running:stationary:rpm_idle:warm'} for i in range(128)]}
    a.update(baseline=baseline,corroborators=[{'baseline':copy.deepcopy(baseline)}],
             persistence={'observed':10,'required':10,'satisfied':True,'observations':[
                 {'sample_id':i,'observed_at':'2026-09-22T00:00:00+00:00','value':219.2,'source':'ccan.broadcast.0x2ed'} for i in range(10)]})
    return a


class HealthSummaryTests(unittest.TestCase):
    def test_large_overview_fits_without_losing_decision_facts_or_notification_state(self):
        a=large_assessment()
        episodes=[{'id':i,'first_assessment':copy.deepcopy(a),'latest_assessment':copy.deepcopy(a)} for i in range(25)]
        payload={'available':True,'assessments':[a], 'active':[a],
                 'episodes':{'active':episodes[:2],'recent':episodes,'notification_outbox':{'failed':2,'pending':1}},
                 'notification_delivery':{'enabled':True,'last_error':'delivery failure'}}
        original=copy.deepcopy(payload)
        self.assertGreater(encoded_size(payload),MAX_RESPONSE_BYTES)
        result=health_overview(payload)
        self.assertLess(encoded_size(result),OVERVIEW_BUDGET_BYTES)
        self.assertEqual(payload,original)
        self.assertTrue(result['available'])
        self.assertEqual(len(result['episodes']['recent']),25)
        self.assertEqual(result['notification_delivery'],payload['notification_delivery'])
        self.assertEqual(result['episodes']['notification_outbox'],payload['episodes']['notification_outbox'])
        projected=result['episodes']['active'][0]['first_assessment']
        self.assertEqual(projected['baseline']['median'],190.4)
        self.assertEqual(projected['baseline']['bucket_count'],446)
        self.assertEqual(projected['persistence']['observed'],10)
        self.assertNotIn('input_buckets',projected['baseline'])
        self.assertEqual(projected['baseline']['overview_omitted_arrays'],['input_buckets'])
        self.assertEqual(projected['persistence']['overview_omitted_arrays'],['observations'])

    def test_full_event_evidence_is_still_available_after_summary_projection(self):
        with tempfile.TemporaryDirectory() as tmp, TelemetryHistorian(Path(tmp)/'history.sqlite3') as historian:
            a=large_assessment()
            historian.record_advisory_assessments([a],evaluated_at=datetime(2026,9,22,tzinfo=timezone.utc))
            before=detail(historian._conn,1)[1]['event']['first_assessment']
            insight=TelemetryInsights(historian,dtc_cache_path=Path(tmp)/'unused.json',
                warning_evaluator=mock.Mock(evaluate=lambda:{'assessments':[copy.deepcopy(a)],'active':[copy.deepcopy(a)]}))
            overview=insight.health_response()
            self.assertEqual(overview['evidence_scope'],'overview')
            self.assertNotIn('input_buckets',overview['episodes']['active'][0]['first_assessment']['baseline'])
            after=detail(historian._conn,1)[1]['event']['first_assessment']
            self.assertEqual(before,after)
            self.assertEqual(len(after['baseline']['input_buckets']),128)
            self.assertEqual(len(after['persistence']['observations']),10)

    def test_secondary_history_omission_is_explicit_and_active_warnings_survive(self):
        payload={'available':True,'episodes':{'active':[{'id':42,'state':'warning'}],
                 'recent':[{'title':'x'*1000000}]},'notification_delivery':{'enabled':True}}
        result=health_overview(payload)
        self.assertTrue(result['available'])
        self.assertEqual(result['episodes']['active'],payload['episodes']['active'])
        self.assertEqual(result['overview_limits']['omitted_sections'],['episodes.recent'])
        self.assertLess(encoded_size(result),OVERVIEW_BUDGET_BYTES)

    def test_essential_overflow_fails_explicitly_instead_of_false_all_clear(self):
        result=health_overview({'available':True,'active':[{'reason':'x'*1000000}],
                              'notification_delivery':{'enabled':True}})
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'],'health_summary_too_large')
        self.assertTrue(result['notification_delivery']['enabled'])
        self.assertLess(encoded_size(result),OVERVIEW_BUDGET_BYTES)
