"""Synthetic saved events and fake Codex, for local browser acceptance only."""
from pathlib import Path
import argparse
import json
import signal
import sys
import tempfile
import threading
from datetime import timedelta
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(Path(__file__).parent)]
from test_event_history import coolant, START
from test_vehicle_historian import definition, available, snapshot
from projects.vehicle_data.historian import TelemetryHistorian
from projects.vehicle_data.event_history import EventReader
from projects.vehicle_data.web import TelemetryWebServer
from projects.vehicle_data.warning_chat import WarningChatManager, ChatServer, ChatHandler
from warning_chat_preview import FixtureClient, FixtureRunner

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interrupted",action="store_true",help="include archived watch and unresolved confirmed warning")
    args=parser.parse_args()
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    with tempfile.TemporaryDirectory(prefix='event-preview-',dir=ROOT/'tmp') as temp:
        h=TelemetryHistorian(Path(temp)/'history.sqlite3')
        metric=definition('engine.coolant_temperature','°F')
        metric['sources'][0].update(name='ccan.broadcast.0x2ed',quality='observed_alfa_scale',provenance='exact test provenance')
        for n in range(10):
            at=START+timedelta(seconds=(n-5)*5)
            h.ingest_snapshot(snapshot(at,[metric],{'engine.coolant_temperature':available(metric,210+n,at)}),captured_at=at,ingest_key=f'preview-{n}')
        h.record_advisory_assessments([coolant()],evaluated_at=START)
        last=coolant('unavailable',219.2,15.2);last.update(baseline=None,deviation=None)
        last['current']['effective_age_seconds']=11.8
        h.record_advisory_assessments([last],evaluated_at=START+timedelta(seconds=27))
        if args.interrupted:
            last['current']['effective_age_seconds']=54.8
            h.record_advisory_assessments([last],evaluated_at=START+timedelta(seconds=70))
            confirmed=coolant('warning',225,80)
            confirmed.update(rule='confirmed_coolant_example',title='Confirmed coolant example')
            h.record_advisory_assessments([confirmed],evaluated_at=START+timedelta(seconds=80))
            missing={**last,'rule':confirmed['rule'],'title':confirmed['title']}
            h.record_advisory_assessments([missing],evaluated_at=START+timedelta(seconds=200))
        reader=EventReader(h.database)
        class Client(FixtureClient):
            def request(self,method,path,payload=None,**kwargs):
                if path.startswith('/v1/events'):return reader.request(method,path,payload)
                if path=='/v1/health':return 200,{'available':True,'episodes':h.advisory_summary(),'assessments':[]}
                return super().request(method,path,payload,**kwargs)
        client=Client()
        manager=WarningChatManager(Path(temp)/'advisor',client,FixtureRunner())
        advisor=ChatServer(str(Path(temp)/'advisor.sock'),ChatHandler)
        advisor.manager=manager
        web=TelemetryWebServer(('127.0.0.1',0),socket_path='/unused',allow_acquisitions=False,
            stream_interval_seconds=1,stream_max_seconds=30,warning_chat_socket=str(Path(temp)/'advisor.sock'))
        web.telemetry_client=client
        for server in (advisor,web):threading.Thread(target=server.serve_forever,daemon=True).start()
        print(json.dumps({'url':f'http://127.0.0.1:{web.server_port}','fixture_code':(Path(temp)/'advisor'/'access-code').read_text().strip()}),flush=True)
        stop.wait()
        for server in (web,advisor):server.shutdown();server.server_close()
        reader.close();h.close()
if __name__=='__main__':main()
