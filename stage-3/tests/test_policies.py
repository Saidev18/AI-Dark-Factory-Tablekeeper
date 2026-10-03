"""HTTP checks derived from the Stage 3 specification, including rollback snapshots."""
import argparse
import calendar
import concurrent.futures
import copy
import threading
import unittest
from datetime import datetime, timedelta, timezone
import test_http as http
from test_pairs import pair_fixture

LEGACY1 = LEGACY2 = None


class PolicyTests(unittest.TestCase):
    error = http.HTTPTests.error

    def setUp(self):
        self.data = pair_fixture()
        self.data['restaurants'][0]['manager_user_ids'] = ['ada']
        self.reset()

    def reset(self):
        self.assertEqual(http.request('POST','/_test/reset',self.data)[0],204)
        self.token = self.login('ada')
        self.bob = self.login('bob')

    def login(self, user, base=None):
        return http.request('POST','/auth/login',{'email':user+'@example.com','password':'correct horse'},base=base)[1]['token']

    def policy(self, date='2036-09-01', **patch):
        restaurant = self.data['restaurants'][0]
        return {'effective_from':date,'slot_minutes':30,'reservation_duration_minutes':60,
                'cancellation_cutoff_minutes':0,'opening_hours':copy.deepcopy(restaurant['opening_hours']),
                'capacities':{'t1':3,'t2':5,'t3':3},**patch}

    def publish(self, body=None, key='policy', token=None):
        return http.request('POST','/restaurants/r/policies',body or self.policy(),token or self.token,key)

    def create(self, day='2036-09-24', ids=('t1',), size=2, clock='19:00', key='booking'):
        body = {'restaurant_id':'r','table_ids':list(ids),'party_size':size,'starts_at_local':day+'T'+clock}
        result = http.request('POST','/reservations',body,self.token,key)
        self.assertEqual(result[0],201,result)
        return result[1], body

    def get(self, booking, suffix=''):
        return http.request('GET','/reservations/'+booking['reference']+suffix,token=self.token)[1]

    def amend(self, booking, body):
        return http.request('PATCH','/reservations/'+booking['reference'],body,self.token)

    def snapshot(self):
        return http.request('GET','/_test/export')[1]

    def adopt(self, anchor, count=3, interval=1, key='series'):
        return http.request('POST','/series',{'anchor_reference':anchor['reference'],'count':count,'interval_weeks':interval},self.token,key)

    def test_explanations_are_independent_ordered_and_opt_in(self):
        booking,_ = self.create()
        path = '/availability?restaurant_id=r&date=2036-09-24&party_size=4'
        plain = http.request('GET',path)[1]
        self.assertTrue(all('explain' not in slot for slot in plain['slots']))
        for value in ('false','1','','TRUE'):
            self.error(http.request('GET',path+'&explain='+value),422,'validation_failed')
        explained = http.request('GET',path+'&explain=true')[1]
        for slot in explained['slots']:
            items = slot['explain']
            self.assertEqual([e['table_id'] for e in items],['t1','t2','t3'])
            self.assertEqual([e['table_id'] for e in items if e['available']],slot['available_table_ids'])
            for item in items:
                self.assertEqual([r['rule'] for r in item['rules']],['capacity','no_overlap'])
                self.assertEqual(item['available'],all(r['holds'] for r in item['rules']))
                self.assertEqual(item['policy_version'],0)
        self.assertEqual(explained['slots'][2]['explain'][0]['rules'],[{'rule':'capacity','holds':False},{'rule':'no_overlap','holds':False}])
        self.publish()
        self.assertEqual(http.request('GET',path+'&explain=true')[1]['slots'][0]['explain'][0]['policy_version'],1)

    def test_policy_validation_permissions_and_retry_key_ordering(self):
        path = '/restaurants/r/policies'
        self.error(http.request('POST',path,self.policy(),key='k'),401,'unauthenticated')
        self.error(self.publish(token=self.bob),403,'forbidden')
        self.error(http.request('POST','/restaurants/missing/policies',self.policy(),self.token,'k'),404,'not_found')
        before = self.snapshot()
        invalid = [{k:v for k,v in self.policy().items() if k!='capacities'}, self.policy(date='2036-02-30'),
                   self.policy(capacities={'t1':3,'t2':5}),self.policy(capacities={'t1':True,'t2':5,'t3':3}),
                   self.policy(opening_hours=[{'weekday':'mon','opens':'19:00','closes':'18:00'}]),
                   self.policy(opening_hours=self.policy()['opening_hours']*2)]
        for field, values in [('slot_minutes',[True,'30',0,1441,1.5]),('reservation_duration_minutes',[False,0,1441]),
                              ('cancellation_cutoff_minutes',[-1,10081,True]),('capacities',[[],None])]:
            invalid.extend(self.policy(**{field:v}) for v in values)
        for body in invalid:
            self.error(self.publish(body,key='reuse-after-fail'),422,'validation_failed')
            self.assertEqual(self.snapshot(),before)
        body = self.policy(unknown={'a':True})
        status,receipt = self.publish(body,key='reuse-after-fail')
        self.assertEqual(status,201); self.assertEqual(receipt['policy_version'],1)
        self.assertNotIn('unknown',receipt)
        self.assertEqual(self.publish(dict(reversed(list(body.items()))),key='reuse-after-fail'),(200,receipt))
        self.error(self.publish({'invalid':True},key='reuse-after-fail'),409,'idempotency_key_reuse')
        self.assertEqual(http.request('GET',path)[1],{'policies':[receipt]})

    def test_effective_dates_ties_and_original_detail(self):
        first = self.publish(self.policy(date='2036-10-01'),key='one')[1]
        second = self.publish(self.policy(date='2036-09-01',reservation_duration_minutes=120),key='two')[1]
        third = self.publish(self.policy(date='2036-09-01',reservation_duration_minutes=30),key='three')[1]
        self.assertEqual(http.request('GET','/restaurants/r/policies')[1]['policies'],[first,second,third])
        self.assertEqual(http.request('GET','/restaurants/r')[1]['reservation_duration_minutes'],90)
        for index,(date,version,duration) in enumerate([('2036-08-24',0,90),('2036-09-24',3,30),('2036-10-24',1,60)]):
            booking,_ = self.create(day=date,key=str(index))
            self.assertEqual(booking['accepted_terms']['policy_version'],version)
            self.assertEqual(booking['accepted_terms']['reservation_duration_minutes'],duration)

    def test_snapshot_noops_pair_order_and_history(self):
        booking,body = self.create(ids=('t1','t2'),size=6)
        self.assertEqual(booking['table_ids'],['t2','t1'])
        history = self.get(booking,'/history')['entries']
        self.assertEqual([c['field'] for c in history[0]['changes']],['table_ids','starts_at_local','party_size'])
        self.assertEqual(history[0]['changes'][0]['to'],['t2','t1'])
        self.publish(self.policy(capacities={'t1':1,'t2':1,'t3':1}))
        before = self.snapshot()
        self.assertEqual(self.amend(booking,{'table_ids':['t1','t2'],'expected_revision':1,'ignored':True}),(200,booking))
        self.assertEqual(self.snapshot(),before)
        self.error(self.amend(booking,{'party_size':5}),422,'party_exceeds_capacity')
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(http.request('POST','/reservations',body,self.token,'booking'),(200,booking))
        changed = self.amend(booking,{'party_size':2})[1]
        self.assertEqual(changed['revision'],2)
        self.assertEqual(changed['accepted_terms']['policy_version'],1)
        self.assertEqual(changed['ends_at'],'2036-09-24T20:00:00+02:00')
        entries = self.get(booking,'/history')['entries']
        self.assertEqual(entries[1]['changes'],[{'field':'party_size','from':6,'to':2}])
        self.assertEqual(entries[0],history[0])
        self.assertEqual(self.get(booking,'/decision'),{'reference':booking['reference'],'revision':2,'accepted_terms':changed['accepted_terms']})

    def test_old_accepted_cutoff_and_stale_revision_precedence(self):
        day = (datetime.now(timezone.utc)+timedelta(days=2)).strftime('%Y-%m-%d')
        self.data['restaurants'][0]['cancellation_cutoff_minutes']=10080
        self.reset(); booking,_ = self.create(day=day)
        self.publish(self.policy(date='2000-01-01',cancellation_cutoff_minutes=0))
        before = self.snapshot()
        self.error(self.amend(booking,{'expected_revision':2,'party_size':'bad'}),409,'stale_revision')
        for value in (True,0,'1',1.2):
            self.error(self.amend(booking,{'expected_revision':value}),422,'validation_failed')
        self.error(self.amend(booking,{}),409,'cutoff_passed')
        self.error(http.request('POST','/reservations/'+booking['reference']+'/cancel',{},self.token),409,'cutoff_passed')
        self.assertEqual(self.snapshot(),before)
        self.data['restaurants'][0]['cancellation_cutoff_minutes']=0
        self.reset(); booking,_=self.create(day=day)
        self.publish(self.policy(date='2000-01-01',cancellation_cutoff_minutes=10080))
        updated=self.amend(booking,{'party_size':1})
        self.assertEqual(updated[0],200)
        self.error(self.amend(booking,{}),409,'cutoff_passed')

    def test_history_cancel_private_reads_and_terminal_event(self):
        booking,_=self.create()
        for suffix in ('/history','/decision'):
            for token in (None,self.bob,'unknown'):
                self.error(http.request('GET','/reservations/'+booking['reference']+suffix,token=token),404,'not_found')
        changed=self.amend(booking,{'table_id':'t2','starts_at_local':'2036-09-25T19:30','party_size':3})[1]
        entries=self.get(booking,'/history')['entries']
        self.assertEqual([c['field'] for c in entries[1]['changes']],['table_id','starts_at_local','party_size'])
        cancelled=http.request('POST','/reservations/'+booking['reference']+'/cancel',{},self.token)[1]
        self.assertEqual(cancelled['revision'],3)
        self.assertEqual(http.request('POST','/reservations/'+booking['reference']+'/cancel',{},self.token),(200,cancelled))
        self.assertEqual([e['seq'] for e in self.get(booking,'/history')['entries']],[1,2,3])
        self.assertEqual(self.get(booking,'/history')['entries'][-1]['changes'],[])
        self.assertEqual(self.get(booking,'/decision')['accepted_terms'],changed['accepted_terms'])

    def test_concurrent_expected_revision_and_policy_replays(self):
        booking,_=self.create()
        barrier=threading.Barrier(50)
        def worker(_):
            barrier.wait(); return self.amend(booking,{'party_size':1,'expected_revision':1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results=list(pool.map(worker,range(50)))
        self.assertEqual(sum(s==200 for s,_ in results),1)
        self.assertEqual(sum(s==409 and b['error']['code']=='stale_revision' for s,b in results),49)
        self.assertEqual(len(self.get(booking,'/history')['entries']),2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results=list(pool.map(lambda _:self.publish(),range(50)))
        self.assertEqual(sum(s==201 for s,_ in results),1)
        self.assertTrue(all(b==results[0][1] for _,b in results))
        self.assertEqual(len(http.request('GET','/restaurants/r/policies')[1]['policies']),1)

    def test_series_anchor_identity_policies_replay_and_exceptions(self):
        anchor,body=self.create(ids=('t1','t2'),size=6)
        history=self.get(anchor,'/history')
        self.publish(self.policy(date='2036-10-01',reservation_duration_minutes=60))
        status,series=self.adopt(anchor)
        self.assertEqual(status,201,series)
        self.assertEqual(series['occurrences'][0]['reservation'],anchor)
        self.assertEqual(self.get(anchor,'/history'),history)
        self.assertEqual(http.request('POST','/reservations',body,self.token,'booking'),(200,anchor))
        self.assertEqual([o['reservation']['accepted_terms']['policy_version'] for o in series['occurrences']],[0,1,1])
        self.assertEqual([o['reservation']['starts_at_local'] for o in series['occurrences']],['2036-09-24T19:00','2036-10-01T19:00','2036-10-08T19:00'])
        path='/series/'+series['series_id']
        for token in (None,self.bob): self.error(http.request('GET',path,token=token),404,'not_found')
        occurrence=series['occurrences'][1]['reservation']
        self.assertEqual(self.amend(occurrence,{'table_ids':['t1','t2']})[0],200)
        self.assertEqual(http.request('GET',path,token=self.token)[1],series)
        self.amend(occurrence,{'party_size':5})
        current=http.request('GET',path,token=self.token)[1]
        self.assertEqual(current['revision'],2); self.assertTrue(current['occurrences'][1]['exception'])
        self.amend(occurrence,{'party_size':6})
        http.request('POST','/reservations/'+anchor['reference']+'/cancel',{},self.token)
        current=http.request('GET',path,token=self.token)[1]
        self.assertEqual(current['revision'],4)
        self.assertTrue(current['occurrences'][1]['exception']); self.assertFalse(current['occurrences'][0]['exception'])
        self.assertEqual(current['occurrences'][1]['reservation']['status'],'confirmed')
        self.assertEqual(self.adopt(anchor),(200,series))

    def test_series_failure_rolls_back_all_state_and_keys(self):
        anchor,_=self.create()
        self.publish(self.policy(date='2036-10-01',capacities={'t1':1,'t2':4,'t3':2}))
        before=self.snapshot()
        self.error(self.adopt(anchor),422,'party_exceeds_capacity')
        self.assertEqual(self.snapshot(),before)
        self.publish(self.policy(date='2036-10-01'),key='fix')
        self.assertEqual(self.adopt(anchor)[0],201)
        self.error(self.adopt(anchor,key='new'),409,'already_in_series')
        for count in (True,1,13,'3',2.5):
            self.error(self.adopt(anchor,count=count,key='invalid'),422,'validation_failed')

    def test_series_dst_calendar_gaps_and_absolute_duration(self):
        year=2036
        for zone,month,weekday in [('Europe/Berlin',3,-1),('America/New_York',3,1)]:
            sundays=[d for d in range(1,calendar.monthrange(year,month)[1]+1) if datetime(year,month,d).weekday()==6]
            transition=sundays[-1] if weekday==-1 else sundays[1]
            day=datetime(year,month,transition)-timedelta(days=7)
            self.data=pair_fixture(); r=self.data['restaurants'][0];r['timezone']=zone
            r['opening_hours']=[{'weekday':d,'opens':'00:00','closes':'05:00'} for d in ('mon','tue','wed','thu','fri','sat','sun')]
            self.reset(); anchor,_=self.create(day=day.strftime('%Y-%m-%d'),clock='02:30')
            before=self.snapshot();self.error(self.adopt(anchor,count=2),422,'invalid_local_time');self.assertEqual(self.snapshot(),before)
        for zone,month in [('Europe/Berlin',10),('America/New_York',11)]:
            sundays=[d for d in range(1,calendar.monthrange(year,month)[1]+1) if datetime(year,month,d).weekday()==6]
            transition=sundays[-1] if month==10 else sundays[0]
            day=datetime(year,month,transition)-timedelta(days=7)
            self.data['restaurants'][0]['timezone']=zone;self.reset()
            anchor,_=self.create(day=day.strftime('%Y-%m-%d'),clock='02:30' if month==10 else '01:30')
            occurrence=self.adopt(anchor,count=2)[1]['occurrences'][1]['reservation']
            start=datetime.fromisoformat(occurrence['starts_at']);end=datetime.fromisoformat(occurrence['ends_at'])
            self.assertEqual((end-start).total_seconds(),5400)
            self.assertNotEqual(start.utcoffset(),end.utcoffset())

    def test_collective_move_policy_history_counters_and_rollback(self):
        anchor,_=self.create()
        series=self.adopt(anchor)[1]
        a,b=[o['reservation'] for o in series['occurrences'][:2]]
        self.publish()
        before=self.snapshot();rid_revision=before['state']['restaurant_revisions']['r']
        moves={'moves':[{'reference':a['reference'],'party_size':1,'expected_revision':1},
                        {'reference':b['reference'],'party_size':1,'expected_revision':2}]}
        self.error(http.request('POST','/reservation-moves',moves,self.token,'batch'),409,'stale_revision')
        self.assertEqual(self.snapshot(),before)
        moves['moves'][1]['expected_revision']=1
        status,response=http.request('POST','/reservation-moves',moves,self.token,'batch')
        self.assertEqual(status,201)
        after=self.snapshot()
        self.assertEqual(after['state']['restaurant_revisions']['r'],rid_revision+1)
        current=http.request('GET','/series/'+series['series_id'],token=self.token)[1]
        self.assertEqual(current['revision'],2)
        self.assertEqual([o['exception'] for o in current['occurrences']],[True,True,False])
        self.assertEqual([r['revision'] for r in response['reservations']],[2,2])
        self.assertTrue(all(r['accepted_terms']['policy_version']==1 for r in response['reservations']))
        self.assertEqual(http.request('POST','/reservation-moves',moves,self.token,'batch'),(200,response))
        self.assertEqual(self.snapshot(),after)

    def test_portable_new_state_and_both_legacy_upgrades(self):
        for base in (LEGACY1,LEGACY2):
            http.request('POST','/_test/reset',http.fixture(),base=base)
            token=self.login('ada',base)
            body={'restaurant_id':'r','table_id':'t2','party_size':2,'starts_at_local':'2036-09-24T19:00'}
            receipt=http.request('POST','/reservations',body,token,'old',base)[1]
            moves={'moves':[{'reference':receipt['reference'],'table_id':'t1'}]}
            batch=http.request('POST','/reservation-moves',moves,token,'old',base)[1]
            snapshot=http.request('GET','/_test/export',base=base)[1]
            self.assertEqual(http.request('POST','/_test/import',snapshot)[0],204)
            self.assertEqual(http.request('POST','/reservations',body,token,'old'),(200,receipt))
            self.assertEqual(http.request('POST','/reservation-moves',moves,token,'old'),(200,batch))
            self.token=token
            anchor=http.request('GET','/reservations/'+receipt['reference'],token=token)[1]
            self.assertEqual(anchor['revision'],1)
            self.assertEqual(anchor['created_at'],receipt['created_at'])
            self.assertEqual(self.adopt(anchor,count=2)[0],201)
            exported=self.snapshot()
            self.assertEqual(http.request('POST','/_test/import',exported,base=http.DESTINATION)[0],204)
            self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],exported)
            self.assertEqual(http.request('POST','/reservations',body,token,'old',http.DESTINATION),(200,receipt))

    def test_series_first_failing_occurrence_and_concurrent_replay(self):
        anchor,_=self.create()
        taken,_=self.create(day='2036-10-01',key='taken')
        self.publish(self.policy(date='2036-10-08',capacities={'t1':1,'t2':4,'t3':2}))
        before=self.snapshot()
        self.error(self.adopt(anchor,count=3),409,'table_unavailable')
        self.assertEqual(self.snapshot(),before)
        http.request('POST','/reservations/'+taken['reference']+'/cancel',{},self.token)
        self.publish(self.policy(date='2036-10-08'),key='fix')
        before=self.snapshot();barrier=threading.Barrier(50)
        def worker(_):
            barrier.wait();return self.adopt(anchor,count=3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:results=list(pool.map(worker,range(50)))
        self.assertEqual(sum(s==201 for s,_ in results),1)
        self.assertEqual(sum(s==200 for s,_ in results),49)
        self.assertTrue(all(b==results[0][1] for _,b in results))
        after=self.snapshot()
        self.assertEqual(len(after['state']['reservations']),len(before['state']['reservations'])+2)
        self.assertEqual(after['state']['restaurant_revisions']['r'],before['state']['restaurant_revisions']['r']+1)

    def test_multi_series_swap_occupancy_and_one_increment_per_series(self):
        a,_=self.create();b,_=self.create(ids=('t2',),key='b')
        sa=self.adopt(a,count=2,key='sa')[1];sb=self.adopt(b,count=2,key='sb')[1]
        before=self.snapshot()
        refs=[o['reference'] for s in (sa,sb) for o in s['occurrences']]
        moves={'moves':[{'reference':ref,'table_id':'t2' if index<2 else 't1','expected_revision':1} for index,ref in enumerate(refs)]}
        status,response=http.request('POST','/reservation-moves',moves,self.token,'swap')
        self.assertEqual(status,201,response)
        after=self.snapshot()
        self.assertEqual(after['state']['restaurant_revisions']['r'],before['state']['restaurant_revisions']['r']+1)
        for series in (sa,sb):
            current=http.request('GET','/series/'+series['series_id'],token=self.token)[1]
            self.assertEqual(current['revision'],2);self.assertTrue(all(o['exception'] for o in current['occurrences']))
        bad={'moves':[{'reference':refs[0],'table_id':'t1'},{'reference':refs[2],'party_size':1}]}
        self.error(http.request('POST','/reservation-moves',bad,self.token,'bad'),409,'table_unavailable')
        self.assertEqual(self.snapshot(),after)
        noop={'moves':[{'reference':ref} for ref in refs]}
        self.assertEqual(http.request('POST','/reservation-moves',noop,self.token,'bad')[0],201)
        self.assertEqual(self.snapshot()['state']['restaurant_revisions'],after['state']['restaurant_revisions'])

    def test_policy_duration_changes_new_occupancy_without_editing_old_end(self):
        old,_=self.create()
        self.publish(self.policy(reservation_duration_minutes=30))
        self.assertEqual(self.get(old),old)
        body={'restaurant_id':'r','table_id':'t1','starts_at_local':'2036-09-24T20:00','party_size':2}
        self.error(http.request('POST','/reservations',body,self.token,'later'),409,'table_unavailable')
        amended=self.amend(old,{'party_size':1})[1]
        self.assertEqual(amended['ends_at'],'2036-09-24T19:30:00+02:00')
        self.assertEqual(http.request('POST','/reservations',body,self.token,'later')[0],201)
        self.publish(self.policy(opening_hours=[]),key='closed')
        path='/availability?restaurant_id=r&date=2036-09-24&party_size=2&explain=true'
        self.assertEqual(http.request('GET',path)[1]['slots'],[])
        self.assertEqual(self.amend(old,{}),(200,amended))
        self.error(self.amend(old,{'party_size':2}),422,'outside_opening_hours')

    def test_portable_new_state_rejects_invalid_metadata(self):
        anchor,_=self.create();self.publish();series=self.adopt(anchor)[1]
        self.amend(series['occurrences'][1]['reservation'],{'party_size':1})
        http.request('POST','/reservations/'+anchor['reference']+'/cancel',{},self.token)
        exported=self.snapshot()
        self.assertEqual(http.request('POST','/_test/import',exported,base=http.DESTINATION)[0],204)
        self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],exported)
        for mutation in ('revision','terms','history','series','policy'):
            bad=copy.deepcopy(exported);state=bad['state']
            if mutation=='revision':state['reservations'][anchor['reference']]['revision']=0
            if mutation=='terms':state['reservations'][anchor['reference']]['accepted_terms']['reservation_duration_minutes']=1
            if mutation=='history':state['histories'][anchor['reference']][0]['seq']=2
            if mutation=='series':state['series'][series['series_id']]['occurrences'][1]['reference']='MISSING'
            if mutation=='policy':state['policies']['r'][0]['policy_version']=9
            self.error(http.request('POST','/_test/import',bad,base=http.DESTINATION),422,'validation_failed')
            self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],exported)


    def test_import_rejects_generated_exception_history_mismatch(self):
        anchor,_=self.create();series=self.adopt(anchor)[1]
        changed=series['occurrences'][1]['reservation']
        self.amend(changed,{'party_size':1})
        exported=self.snapshot()
        self.assertEqual(http.request('POST','/_test/import',exported,base=http.DESTINATION)[0],204)
        for index,flag in ((1,False),(2,True)):
            invalid=copy.deepcopy(exported)
            invalid['state']['series'][series['series_id']]['occurrences'][index]['exception']=flag
            self.error(http.request('POST','/_test/import',invalid,base=http.DESTINATION),422,'validation_failed')
            self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],exported)

    def test_import_series_revision_bounds_preserve_valid_batches_and_anchor_history(self):
        anchor,_=self.create()
        anchor=self.amend(anchor,{'party_size':1})[1]
        series=self.adopt(anchor,count=4)[1]
        adopted=self.snapshot()
        self.assertFalse(series['occurrences'][0]['exception'])
        self.assertEqual(http.request('POST','/_test/import',adopted,base=http.DESTINATION)[0],204)
        generated=[o['reservation'] for o in series['occurrences'][1:]]
        moves={'moves':[{'reference':b['reference'],'party_size':2} for b in generated]}
        self.assertEqual(http.request('POST','/reservation-moves',moves,self.token,'group')[0],201)
        current=http.request('GET','/series/'+series['series_id'],token=self.token)[1]
        self.assertEqual(current['revision'],2)
        exported=self.snapshot()
        self.assertEqual(http.request('POST','/_test/import',exported,base=http.DESTINATION)[0],204)
        self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],exported)
        for revision in (1,5):
            invalid=copy.deepcopy(exported);invalid['state']['series'][series['series_id']]['revision']=revision
            self.error(http.request('POST','/_test/import',invalid,base=http.DESTINATION),422,'validation_failed')
            self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],exported)
        for booking in generated:
            http.request('POST','/reservations/'+booking['reference']+'/cancel',{},self.token)
        cancelled=self.snapshot()
        self.assertEqual(cancelled['state']['series'][series['series_id']]['revision'],5)
        self.assertEqual(http.request('POST','/_test/import',cancelled,base=http.DESTINATION)[0],204)
        invalid=copy.deepcopy(cancelled);invalid['state']['series'][series['series_id']]['revision']=4
        self.error(http.request('POST','/_test/import',invalid,base=http.DESTINATION),422,'validation_failed')
        self.assertEqual(http.request('GET','/_test/export',base=http.DESTINATION)[1],cancelled)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','destination','legacy1','legacy2'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();http.SOURCE,http.DESTINATION=args.source,args.destination;LEGACY1,LEGACY2=args.legacy1,args.legacy2
    unittest.main(argv=['test_policies.py'],verbosity=2)
