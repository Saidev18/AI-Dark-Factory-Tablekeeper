"""Stage 4 HTTP probes, with an independent exhaustive optimizer oracle."""
import argparse
import concurrent.futures
import copy
import itertools
import random
import time
import unittest
from datetime import datetime, timedelta, timezone
import test_http as http
from test_pairs import pair_fixture

LEGACY3 = None


def intersects(a, b):
    return datetime.fromisoformat(a['starts_at']) < datetime.fromisoformat(b['ends_at']) and datetime.fromisoformat(b['starts_at']) < datetime.fromisoformat(a['ends_at'])


def oracle(restaurant, bookings, closure, previous=()):
    considered = sorted([b for b in bookings if b['status']=='confirmed' and b['restaurant_id']==restaurant['id']
                         and datetime.fromisoformat(b['starts_at']) < datetime.fromisoformat(closure['to'])
                         and datetime.fromisoformat(closure['from']) < datetime.fromisoformat(b['ends_at'])], key=lambda b:b['reference'])
    fixed = [b for b in bookings if b['status']=='confirmed' and b not in considered]
    options = [[t['id']] for t in restaurant['tables']] + restaurant['combinable']
    best = None
    for ranks in itertools.product(range(len(options)), repeat=len(considered)):
        proposed = [{**b,'table_ids':options[rank]} for b,rank in zip(considered,ranks)]
        waste = 0
        valid = True
        for i,b in enumerate(proposed):
            capacity = sum(b['accepted_terms']['capacities'][t] for t in b['table_ids'])
            waste += capacity-b['party_size']
            if capacity < b['party_size']:valid=False;break
            if any(c['table_id'] in b['table_ids'] and datetime.fromisoformat(b['starts_at']) < datetime.fromisoformat(c['to']) and datetime.fromisoformat(c['from']) < datetime.fromisoformat(b['ends_at']) for c in [closure,*previous]):valid=False;break
            if any(other['restaurant_id']==b['restaurant_id'] and set(other['table_ids']) & set(b['table_ids']) and intersects(b,other) for other in [*fixed,*proposed[:i]]):valid=False;break
        if valid:
            changes = sum(set(a['table_ids'])!=set(b['table_ids']) for a,b in zip(considered,proposed))
            score = changes,waste,ranks
            if best is None or score < best[0]:best=score,proposed
    return best


class ReplanTests(unittest.TestCase):
    error = http.HTTPTests.error

    def setUp(self):
        self.data=pair_fixture();self.data['restaurants'][0]['manager_user_ids']=['ada']
        self.reset()

    def reset(self, base=None):
        self.assertEqual(http.request('POST','/_test/reset',self.data,base=base)[0],204)
        self.token=self.login('ada',base);self.bob=self.login('bob',base)

    def login(self,user,base=None):
        return http.request('POST','/auth/login',{'email':user+'@example.com','password':'correct horse'},base=base)[1]['token']

    def create(self,ids=('t1',),clock='19:00',day='2036-09-24',size=2,key='b',base=None):
        body={'restaurant_id':'r','table_ids':list(ids),'party_size':size,'starts_at_local':day+'T'+clock}
        result=http.request('POST','/reservations',body,self.token,key,base)
        self.assertEqual(result[0],201,result)
        return result[1],body

    def closure(self,tid='t1',day='2036-09-24',start='18:00',end='23:00'):
        return {'table_id':tid,'from':day+'T'+start+':00+02:00','to':day+'T'+end+':00+02:00'}

    def preview(self,body=None,key='p',token=None):
        return http.request('POST','/restaurants/r/replans',body or self.closure(),token or self.token,key)

    def apply(self,plan,key='a',base=None):
        return http.request('POST','/restaurants/r/replans/'+plan['plan_id']+'/apply',{},self.token,key,base)

    def snapshot(self,base=None):return http.request('GET','/_test/export',base=base)[1]

    def series(self,count=3,day='2036-09-24',clock='19:00'):
        anchor,_=self.create(day=day,clock=clock)
        body={'anchor_reference':anchor['reference'],'count':count,'interval_weeks':1}
        result=http.request('POST','/series',body,self.token,'s')
        self.assertEqual(result[0],201,result)
        return result[1],body

    def amend_series(self,series,clock='20:00',index=0,key='am',revision=None):
        body={'expected_revision':series['revision'] if revision is None else revision,'from_index':index,'local_time':clock}
        return http.request('POST','/series/'+series['series_id']+'/amend',body,self.token,key),body

    def test_preview_purity_apply_history_cutoff_and_replays(self):
        b,_=self.create(day='2020-09-24')
        before=self.snapshot();closure=self.closure(day='2020-09-24')
        status,p=self.preview(closure);self.assertEqual(status,201)
        after=self.snapshot()
        for key in ('reservations','histories','restaurant_revisions','closures','series'):self.assertEqual(before['state'][key],after['state'][key])
        self.assertEqual(p['moved_count'],1);self.assertEqual(p['restaurant_revision'],1)
        applied=self.apply(p);self.assertEqual(applied[0],201,applied)
        moved=applied[1]['reservations'][0]
        self.assertEqual(moved['table_ids'],['t3']);self.assertEqual(moved['revision'],2)
        for key in ('starts_at','ends_at','starts_at_local','party_size','accepted_terms','reference','reservation_id','created_at'):self.assertEqual(moved[key],b[key])
        history=http.request('GET','/reservations/'+b['reference']+'/history',token=self.token)[1]['entries']
        self.assertEqual(history[-1]['event'],'reassigned');self.assertEqual(history[-1]['plan_id'],p['plan_id'])
        self.assertEqual(history[-1]['changes'],[{'field':'table_ids','from':['t1'],'to':['t3']}])
        self.assertEqual(self.apply(p), (200,applied[1]))
        self.error(self.apply(p,'other'),409,'plan_already_applied')
        self.assertEqual(self.preview(closure),(200,p))
        self.error(self.preview({**closure,'invalid':1}),409,'idempotency_key_reuse')
        self.assertEqual(http.request('POST','/_test/import',self.snapshot(),base=http.DESTINATION)[0],204)
        self.assertEqual(self.apply(p,base=http.DESTINATION),(200,applied[1]))

    def test_closure_half_open_explanations_and_all_write_paths(self):
        closure=self.closure(start='19:30',end='20:30');p=self.preview(closure)[1];self.assertEqual(self.apply(p)[0],201)
        slots=http.request('GET','/availability?restaurant_id=r&date=2036-09-24&party_size=2&explain=true')[1]['slots']
        by={s['starts_at_local'][-5:]:s for s in slots}
        self.assertIn('t1',by['18:00']['available_table_ids']);self.assertIn('t1',by['20:30']['available_table_ids'])
        self.assertNotIn('t1',by['19:00']['available_table_ids'])
        self.assertEqual(by['19:00']['explain'][0]['rules'],[{'rule':'capacity','holds':True},{'rule':'no_overlap','holds':False}])
        self.assertTrue(all('t1' not in o['table_ids'] for o in by['19:00']['available_options']))
        for ids in (['t1'],['t2','t1']):
            self.error(http.request('POST','/reservations',{'restaurant_id':'r','table_ids':ids,'party_size':2,'starts_at_local':'2036-09-24T19:00'},self.token,'closed'+str(ids)),409,'table_unavailable')
        b,_=self.create(ids=['t3']);before=self.snapshot()
        self.error(http.request('PATCH','/reservations/'+b['reference'],{'table_id':'t1'},self.token),409,'table_unavailable')
        self.error(http.request('POST','/reservation-moves',{'moves':[{'reference':b['reference'],'table_id':'t1'}]},self.token,'move'),409,'table_unavailable')
        self.assertEqual(before,self.snapshot())

    def test_stale_plan_permissions_validation_and_failed_key_reuse(self):
        self.error(self.preview(token=self.bob),403,'forbidden')
        self.error(http.request('POST','/restaurants/r/replans',self.closure(),key='p'),401,'unauthenticated')
        for c in ({**self.closure(),'from':'2036-09-24T18:00:00'}, {**self.closure(),'to':self.closure()['from']}, {**self.closure(),'from':False}):self.error(self.preview(c),422,'validation_failed')
        self.error(self.preview({**self.closure(),'table_id':'missing'}),404,'not_found')
        p=self.preview()[1];self.create()
        before=self.snapshot();self.error(self.apply(p),409,'stale_plan');self.assertEqual(before,self.snapshot())
        p2=self.preview(key='p2')[1];self.assertEqual(self.apply(p2)[0],201)
        self.error(http.request('POST','/restaurants/r/replans/missing/apply',{},self.token,'x'),404,'not_found')
        self.error(self.preview({},key=''),400,'missing_idempotency_key')
        self.error(self.preview(key='x'*256),422,'validation_failed')

    def test_infeasible_and_planning_limit_are_pure(self):
        self.data['restaurants'][0]['tables']=self.data['restaurants'][0]['tables'][:1];self.data['restaurants'][0]['combinable']=[];self.reset()
        self.create();before=self.snapshot();self.error(self.preview(),409,'no_feasible_plan');self.assertEqual(before,self.snapshot())
        self.data['restaurants'][0]['tables'] += [{'id':'t'+str(i),'label':str(i),'capacity':2} for i in range(2,8)];self.reset()
        before=self.snapshot();self.error(self.preview(),422,'planning_limit');self.assertEqual(before,self.snapshot())

    def test_optimizer_exhaustive_oracle_and_all_tie_breaks(self):
        randomizer=random.Random(401)
        for case in range(24):
            self.data=pair_fixture();r=self.data['restaurants'][0];r['manager_user_ids']=['ada']
            r['tables']=[{'id':'t'+str(i),'label':str(i),'capacity':randomizer.randint(2,6)} for i in range(1,5)]
            r['combinable']=[['t2','t1'],['t3','t4']]
            self.data['reservations']=[{'id':'res'+str(i),'reference':'BOOK0'+str(i),'user_id':'ada','restaurant_id':'r','table_id':'t'+str(i+1),'party_size':randomizer.randint(1,r['tables'][i]['capacity']),'starts_at_local':'2036-09-24T'+randomizer.choice(['18:00','19:00','20:30'])} for i in range(3)]
            self.reset();bookings=http.request('GET','/reservations',token=self.token)[1]['reservations'];closure=self.closure(tid=randomizer.choice(['t1','t2','t3']),start='18:30',end='20:30')
            expected=oracle(r,bookings,closure);before=self.snapshot();result=self.preview(closure)
            if expected is None:self.error(result,409,'no_feasible_plan');self.assertEqual(before,self.snapshot())
            else:
                self.assertEqual(result[0],201,result);score,proposed=expected
                self.assertEqual((result[1]['moved_count'],result[1]['unused_seats']),score[:2])
                self.assertEqual([a['table_ids'] for a in result[1]['assignments']],[b['table_ids'] for b in proposed],case)

    def test_optimizer_uses_accepted_capacities_and_previous_closures(self):
        b,_=self.create();r=self.data['restaurants'][0]
        policy={'effective_from':'2036-01-01','slot_minutes':30,'reservation_duration_minutes':90,'cancellation_cutoff_minutes':0,'opening_hours':r['opening_hours'],'capacities':{'t1':2,'t2':1,'t3':1}}
        self.assertEqual(http.request('POST','/restaurants/r/policies',policy,self.token,'policy')[0],201)
        p=self.preview()[1];self.assertEqual(p['assignments'][0]['table_ids'],['t3']);self.assertEqual(self.apply(p)[0],201)
        # The old accepted terms still allow t2, although the current policy does not.
        p2=self.preview(self.closure(tid='t3'),key='p2')[1];self.assertEqual(p2['assignments'][0]['table_ids'],['t2'])
        self.assertEqual(self.apply(p2,'a2')[0],201)
        self.assertEqual(http.request('GET','/reservations/'+b['reference'],token=self.token)[1]['accepted_terms'],b['accepted_terms'])

    def test_concurrent_applications_and_other_restaurant_isolation(self):
        other=copy.deepcopy(self.data['restaurants'][0]);other['id']='other';self.data['restaurants'].append(other);self.reset()
        self.create();p=self.preview()[1]
        op=http.request('POST','/restaurants/other/replans',self.closure(),self.token,'op')[1]
        self.assertEqual(http.request('POST','/restaurants/other/replans/'+op['plan_id']+'/apply',{},self.token,'oa')[0],201)
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:results=list(pool.map(lambda i:self.apply(p),range(30)))
        self.assertEqual(sum(r[0]==201 for r in results),1);self.assertEqual(sum(r[0]==200 for r in results),29)
        self.assertTrue(all(r[1]==results[0][1] for r in results));self.assertEqual(self.snapshot()['state']['restaurant_revisions']['r'],2)

    def test_series_collective_amend_noop_exceptions_and_original_dates(self):
        series,_=self.series(count=4);sid=series['series_id'];refs=[o['reference'] for o in series['occurrences']]
        self.assertEqual(http.request('PATCH','/reservations/'+refs[1],{'starts_at_local':'2036-10-01T18:00'},self.token)[0],200)
        self.assertEqual(http.request('POST','/reservations/'+refs[2]+'/cancel',{},self.token)[0],200)
        series=http.request('GET','/series/'+sid,token=self.token)[1]
        result,body=self.amend_series(series);self.assertEqual(result[0],201,result);current=result[1]
        self.assertEqual(current['revision'],4);self.assertEqual([o['exception'] for o in current['occurrences']],[False,True,False,False])
        self.assertEqual([o['reservation']['starts_at_local'][-5:] for o in current['occurrences']],['20:00','18:00','19:00','20:00'])
        before=self.snapshot();noop,_=self.amend_series(current,key='noop');self.assertEqual(noop,(201,current))
        after=self.snapshot();after['state']['receipts'].pop(next(k for k in after['state']['receipts'] if 'noop' in k));self.assertEqual(before,after)
        changed,body2=self.amend_series(current,clock='20:30',key='next');self.assertEqual(changed[0],201)
        self.assertEqual(http.request('POST','/series/'+sid+'/amend',body,self.token,'am'),(200,current))
        self.assertEqual(http.request('POST','/_test/import',self.snapshot(),base=http.DESTINATION)[0],204)
        self.assertEqual(self.snapshot(http.DESTINATION),self.snapshot())

    def test_series_validation_precedence_rollback_and_concurrent_revision(self):
        series,_=self.series();path='/series/'+series['series_id']+'/amend'
        self.error(http.request('POST',path,{},key='am'),401,'unauthenticated');self.error(http.request('POST',path,{},self.bob,'am'),404,'not_found')
        for patch in ({'expected_revision':True},{'from_index':True},{'from_index':3},{'local_time':'24:00'},{'local_time':'20:00:00'},{'local_time':2}):
            self.error(http.request('POST',path,{'expected_revision':1,'from_index':0,'local_time':'20:00',**patch},self.token,'am'),422,'validation_failed')
        self.error(http.request('POST',path,{'expected_revision':9,'from_index':False,'local_time':'bad'},self.token,'am'),409,'stale_revision')
        before=self.snapshot();self.error(self.amend_series(series,clock='22:30')[0],422,'outside_opening_hours');self.assertEqual(before,self.snapshot())
        self.create(clock='20:30',day='2036-10-01',key='block')
        before=self.snapshot();self.error(self.amend_series(series,clock='20:00')[0],409,'table_unavailable');self.assertEqual(before,self.snapshot())
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda i:self.amend_series(series,clock=['18:00','18:30'][i],key='race'+str(i))[0],range(2)))
        self.assertEqual(sorted(r[0] for r in results),[201,409]);self.error(next(r for r in results if r[0]==409),409,'stale_revision')

    def test_series_policy_adoption_and_nonoccupancy_error_order(self):
        series,_=self.series();r=self.data['restaurants'][0]
        policy={'effective_from':'2036-10-01','slot_minutes':60,'reservation_duration_minutes':60,'cancellation_cutoff_minutes':0,'opening_hours':r['opening_hours'],'capacities':{'t1':2,'t2':4,'t3':2}}
        self.assertEqual(http.request('POST','/restaurants/r/policies',policy,self.token,'policy')[0],201)
        before=self.snapshot();self.error(self.amend_series(series,clock='19:30')[0],422,'not_on_slot_grid');self.assertEqual(before,self.snapshot())
        result,_=self.amend_series(series,clock='20:00');self.assertEqual(result[0],201,result)
        self.assertEqual([o['reservation']['accepted_terms']['policy_version'] for o in result[1]['occurrences']],[0,1,1])
        self.assertEqual([o['exception'] for o in result[1]['occurrences']],[False]*3)

    def test_repair_series_roundtrip_and_impossible_state_rejection(self):
        series,_=self.series();sid=series['series_id']
        p=self.preview()[1];self.assertEqual(self.apply(p)[0],201)
        current=http.request('GET','/series/'+sid,token=self.token)[1];self.assertEqual(current['revision'],2);self.assertFalse(current['occurrences'][0]['exception'])
        result,_=self.amend_series(current);self.assertEqual(result[0],201,result)
        exported=self.snapshot();self.assertEqual(http.request('POST','/_test/import',exported,base=http.DESTINATION)[0],204)
        self.assertEqual(self.snapshot(http.DESTINATION),exported)
        invalids=[]
        wrong=copy.deepcopy(exported);wrong['state']['series'][sid]['revision']=20;invalids.append(wrong)
        wrong=copy.deepcopy(exported);wrong['state']['series'][sid]['occurrences'][1]['exception']=True;invalids.append(wrong)
        wrong=copy.deepcopy(exported);wrong['state']['mutation_sources']={};invalids.append(wrong)
        wrong=copy.deepcopy(exported);wrong['state']['closures']={};invalids.append(wrong)
        for invalid in invalids:
            self.error(http.request('POST','/_test/import',invalid,base=http.DESTINATION),422,'validation_failed');self.assertEqual(self.snapshot(http.DESTINATION),exported)

    def test_stage3_import_series_edits_cancelled_anchor_and_exact_receipts(self):
        self.reset(LEGACY3)
        b,create_body=self.create(base=LEGACY3)
        # A pre-adoption anchor amendment is legal and must not become an exception.
        b=http.request('PATCH','/reservations/'+b['reference'],{'party_size':1},self.token,base=LEGACY3)[1]
        adoption={'anchor_reference':b['reference'],'count':4,'interval_weeks':1}
        old=http.request('POST','/series',adoption,self.token,'series',LEGACY3)[1];refs=[o['reference'] for o in old['occurrences']]
        http.request('PATCH','/reservations/'+refs[1],{'table_id':'t3'},self.token,base=LEGACY3)
        http.request('POST','/reservations/'+refs[2]+'/cancel',{},self.token,base=LEGACY3)
        exported=self.snapshot(LEGACY3);self.assertEqual(http.request('POST','/_test/import',exported)[0],204)
        self.assertEqual(http.request('POST','/series',adoption,self.token,'series'),(200,old))
        self.assertEqual(http.request('POST','/reservations',create_body,self.token,'b')[1],http.request('POST','/reservations',create_body,self.token,'b',LEGACY3)[1])
        series=http.request('GET','/series/'+old['series_id'],token=self.token)[1]
        result,_=self.amend_series(series);self.assertEqual(result[0],201,result)
        self.assertEqual([o['exception'] for o in result[1]['occurrences']],[False,True,False,False])
        current=self.snapshot();self.assertEqual(http.request('POST','/_test/import',current,base=http.DESTINATION)[0],204);self.assertEqual(self.snapshot(http.DESTINATION),current)

    def test_six_bookings_four_pairs_fixed_overlap_and_rank_order(self):
        r=self.data['restaurants'][0]
        r['tables']=[{'id':'t'+str(i),'label':str(i),'capacity':2} for i in range(1,7)]
        r['combinable']=[['t1','t2'],['t2','t3'],['t3','t4'],['t5','t6']]
        # Every listed booking is considered, even when its table stays available.
        self.data['reservations']=[{'id':'res'+str(i),'reference':'BOOK0'+str(i),'user_id':'ada','restaurant_id':'r','table_id':'t'+str(i+1),'party_size':2,'starts_at_local':'2036-09-24T'+('18:00' if i<3 else '20:00')} for i in range(6)]
        self.reset();bookings=http.request('GET','/reservations',token=self.token)[1]['reservations'];closure=self.closure(start='18:30',end='20:30')
        expected=oracle(r,bookings,closure);started=time.monotonic();result=self.preview(closure)
        self.assertLess(time.monotonic()-started,5);self.assertEqual(result[0],201,result)
        self.assertEqual(len(result[1]['assignments']),6)
        self.assertEqual([a['table_ids'] for a in result[1]['assignments']],[b['table_ids'] for b in expected[1]])
        # A booking starting at the closure's end is fixed but constrains a
        # considered booking extending beyond it.
        self.data=pair_fixture();self.data['restaurants'][0]['manager_user_ids']=['ada'];self.reset()
        self.create(ids=['t1'],clock='19:00');self.create(ids=['t3'],clock='20:30',key='fixed')
        p=self.preview(self.closure(start='19:30',end='20:30'))[1]
        self.assertEqual(len(p['assignments']),1);self.assertEqual(p['assignments'][0]['table_ids'],['t3'])
        # End == next start is half-open and therefore permits this assignment.
        self.assertEqual(self.apply(p)[0],201)

    def test_series_dst_uses_scheduled_dates_and_skips_empty_set(self):
        r=self.data['restaurants'][0];r['timezone']='America/New_York'
        r['opening_hours']=[{'weekday':d,'opens':'00:00','closes':'05:00'} for d in ('mon','tue','wed','thu','fri','sat','sun')]
        self.reset();series,_=self.series(day='2036-10-26',clock='00:30')
        amended,_=self.amend_series(series,clock='01:30');self.assertEqual(amended[0],201,amended)
        records=[o['reservation'] for o in amended[1]['occurrences']]
        self.assertEqual([b['starts_at_local'][:10] for b in records],['2036-10-26','2036-11-02','2036-11-09'])
        self.assertTrue(records[1]['starts_at'].endswith('-04:00'));self.assertTrue(records[1]['ends_at'].endswith('-05:00'))
        self.assertTrue(records[2]['starts_at'].endswith('-05:00'))
        current=amended[1]
        for occurrence in current['occurrences']:
            self.assertEqual(http.request('POST','/reservations/'+occurrence['reference']+'/cancel',{},self.token)[0],200)
        current=http.request('GET','/series/'+current['series_id'],token=self.token)[1]
        before=self.snapshot();result,_=self.amend_series(current,key='empty');self.assertEqual(result,(201,current))
        self.assertEqual(self.snapshot()['state']['restaurant_revisions'],before['state']['restaurant_revisions'])

    def test_series_nonoccupancy_before_earlier_occupancy_and_closure_rollback(self):
        series,_=self.series();r=self.data['restaurants'][0]
        self.create(clock='20:30',key='blocking')
        policy={'effective_from':'2036-10-01','slot_minutes':30,'reservation_duration_minutes':90,'cancellation_cutoff_minutes':0,'opening_hours':[{'weekday':d,'opens':'18:00','closes':'20:00'} for d in ('mon','tue','wed','thu','fri','sat','sun')],'capacities':{'t1':2,'t2':4,'t3':2}}
        http.request('POST','/restaurants/r/policies',policy,self.token,'policy')
        before=self.snapshot();self.error(self.amend_series(series,clock='20:00')[0],422,'outside_opening_hours');self.assertEqual(before,self.snapshot())
        self.reset();series,_=self.series()
        p=self.preview(self.closure(day='2036-10-01',start='20:30',end='22:00'))[1];self.assertEqual(self.apply(p)[0],201)
        # This plan did not move any member; restaurant changes don't stale the
        # series revision, but its new closure still blocks collective amendments.
        before=self.snapshot();self.error(self.amend_series(series,clock='20:00')[0],409,'table_unavailable');self.assertEqual(before,self.snapshot())

    def test_series_old_cutoff_noop_bypass_and_multi_member_repair_counter(self):
        day=(datetime.now(timezone.utc)+timedelta(days=2)).strftime('%Y-%m-%d')
        r=self.data['restaurants'][0];r['cancellation_cutoff_minutes']=0;self.reset()
        series,_=self.series(day=day)
        policy={'effective_from':day,'slot_minutes':30,'reservation_duration_minutes':90,'cancellation_cutoff_minutes':10080,'opening_hours':r['opening_hours'],'capacities':{'t1':2,'t2':4,'t3':2}}
        self.assertEqual(http.request('POST','/restaurants/r/policies',policy,self.token,'policy')[0],201)
        result,_=self.amend_series(series,clock='19:30');self.assertEqual(result[0],201,result)
        current=result[1];before=self.snapshot()
        noop,_=self.amend_series(current,clock='19:30',key='noop');self.assertEqual(noop,(201,current))
        self.assertEqual(self.snapshot()['state']['restaurant_revisions'],before['state']['restaurant_revisions'])
        before=self.snapshot();self.error(self.amend_series(current,clock='20:00',key='cutoff')[0],409,'cutoff_passed');self.assertEqual(before,self.snapshot())
        self.error(self.amend_series(current,clock='bad',revision=1,key='cutoff')[0],409,'stale_revision')
        closure={'table_id':'t1','from':day+'T00:00:00+02:00','to':(datetime.strptime(day,'%Y-%m-%d')+timedelta(days=15)).strftime('%Y-%m-%d')+'T00:00:00+02:00'}
        p=self.preview(closure)[1];self.assertEqual(p['moved_count'],3)
        result=self.apply(p);self.assertEqual(result[0],201,result)
        after=http.request('GET','/series/'+series['series_id'],token=self.token)[1]
        self.assertEqual(after['revision'],current['revision']+1)
        self.assertTrue(all(not o['exception'] for o in after['occurrences']))
        self.assertEqual(self.snapshot()['state']['restaurant_revisions']['r'],before['state']['restaurant_revisions']['r']+1)
        self.assertEqual(http.request('POST','/_test/import',self.snapshot(),base=http.DESTINATION)[0],204)

    def test_import_rejects_impossible_preview_and_unlinked_repair_atomically(self):
        r=self.data['restaurants'][0]
        r['tables'].append({'id':'t4','label':'Four','capacity':2});self.reset();self.create()
        p=self.preview(self.closure(tid='t4'))[1];exported=self.snapshot()
        self.assertEqual(http.request('POST','/_test/import',exported,base=http.DESTINATION)[0],204)
        for field,value in (('table_ids',['t1','t2','t3']),('changed',True)):
            invalid=copy.deepcopy(exported);plan=invalid['state']['plans'][p['plan_id']]['preview']
            plan['assignments'][0][field]=value
            if field=='table_ids':plan['assignments'][0]['changed']=True;plan['moved_count']=1;plan['unused_seats']=6
            else:plan['moved_count']=1
            for receipt in invalid['state']['receipts'].values():
                if receipt['response'].get('plan_id')==p['plan_id']:receipt['response']=copy.deepcopy(plan)
            self.error(http.request('POST','/_test/import',invalid,base=http.DESTINATION),422,'validation_failed');self.assertEqual(self.snapshot(http.DESTINATION),exported)
        repaired=self.preview(key='repair')[1];self.assertEqual(self.apply(repaired)[0],201)
        valid=self.snapshot();self.assertEqual(http.request('POST','/_test/import',valid,base=http.DESTINATION)[0],204)
        invalid=copy.deepcopy(valid);invalid['state']['mutation_sources']={}
        self.error(http.request('POST','/_test/import',invalid,base=http.DESTINATION),422,'validation_failed');self.assertEqual(self.snapshot(http.DESTINATION),valid)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','destination','legacy3'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();http.SOURCE,http.DESTINATION=args.source,args.destination;LEGACY3=args.legacy3
    unittest.main(argv=['test_replans.py'],verbosity=2)
