"""Stage 2 black-box pair, atomicity and legacy migration checks."""
import argparse
import concurrent.futures
import copy
import unittest
import test_http as http

LEGACY = 'http://tablekeeper-developer-s2-legacy:9095'


def pair_fixture():
    body = http.fixture()
    restaurant = body['restaurants'][0]
    restaurant['tables'].append({'id':'t3', 'label':'Garden', 'capacity':2})
    restaurant['combinable'] = [['t2','t1'], ['t2','t3']]
    return body


class PairTests(unittest.TestCase):
    error = http.HTTPTests.error

    def setUp(self):
        self.assertEqual(http.request('POST','/_test/reset',pair_fixture())[0],204)
        self.token = http.request('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']

    def body(self, ids=('t1','t2'), size=6, start='2036-09-24T19:00'):
        return {'restaurant_id':'r','table_ids':list(ids),'party_size':size,'starts_at_local':start}

    def create(self, body=None, key='pair'):
        return http.request('POST','/reservations',body or self.body(),self.token,key)

    def test_available_order_capacity_and_occupancy(self):
        slots = http.request('GET','/availability?restaurant_id=r&date=2036-09-24&party_size=2')[1]['slots']
        expected = [{'table_ids':['t1'],'capacity':2},{'table_ids':['t2'],'capacity':4},
                    {'table_ids':['t3'],'capacity':2},{'table_ids':['t2','t1'],'capacity':6},{'table_ids':['t2','t3'],'capacity':6}]
        self.assertEqual(slots[2]['available_options'],expected)
        status, pair = self.create()
        self.assertEqual(status,201)
        self.assertEqual(pair['table_ids'],['t1','t2'])
        self.assertNotIn('table_id',pair)
        self.error(self.create(self.body(['t2'],4),'single'),409,'table_unavailable')
        self.error(self.create(self.body(['t2','t3']),'other-pair'),409,'table_unavailable')
        occupied = http.request('GET','/availability?restaurant_id=r&date=2036-09-24&party_size=2')[1]['slots'][2]
        self.assertEqual(occupied['available_table_ids'],['t3'])
        self.assertEqual(occupied['available_options'],[{'table_ids':['t3'],'capacity':2}])
        self.assertEqual(self.create(self.body(start='2036-09-24T20:30'),'adjacent')[0],201)
        self.assertEqual(http.request('POST','/reservations/'+pair['reference']+'/cancel',{},self.token)[0],200)
        self.assertEqual(http.request('GET','/availability?restaurant_id=r&date=2036-09-24&party_size=6')[1]['slots'][2]['available_options'],expected[3:])

    def test_pair_validation_and_nontransitivity(self):
        cases = [({**self.body(),'table_id':'t1'},'validation_failed'),
                 (self.body(['t1','t1']),'validation_failed'),(self.body([]),'validation_failed'),
                 (self.body(['t1','t2','t3']),'combination_not_allowed'),(self.body(['t1','t3'],4),'combination_not_allowed'),
                 (self.body(size=7),'party_exceeds_capacity')]
        for body, code in cases:
            self.error(self.create(body,'failure'),422,code)
        self.error(self.create(self.body(['missing','t1']),'failure'),404,'not_found')
        self.error(self.create({**self.body(),'table_ids':'t1+t2'},'failure'),400,'malformed_request')
        self.assertEqual(self.create(key='failure')[0],201)

    def test_patch_pair_single_transitions_and_receipt_import(self):
        pair = self.create()[1]
        path = '/reservations/'+pair['reference']
        self.error(http.request('PATCH',path,{'table_ids':['t1','t3']},self.token),422,'combination_not_allowed')
        self.assertEqual(http.request('GET',path,token=self.token)[1],pair)
        single = http.request('PATCH',path,{'table_id':'t3','party_size':2},self.token)[1]
        self.assertEqual(single['table_ids'],['t3'])
        self.assertEqual(single['table_id'],'t3')
        snapshot = http.request('GET','/_test/export')[1]
        self.assertEqual(http.request('POST','/_test/import',snapshot,base=http.DESTINATION)[0],204)
        self.assertEqual(http.request('POST','/reservations',self.body(),self.token,'pair',http.DESTINATION),(200,pair))
        changed = http.request('PATCH',path,{'table_ids':['t2','t3'],'party_size':6},self.token)[1]
        self.assertNotIn('table_id',changed)
        self.assertEqual(changed['reservation_id'],pair['reservation_id'])

    def test_seeded_cancelled_pairs_leave_occupancy_free(self):
        body = pair_fixture()
        body['reservations'] = [{**self.body(),'id':'pair-seed','reference':'PAIR00','user_id':'ada','status':'cancelled'},
                                {**self.body(['t2'],4),'id':'single-seed','reference':'SINGLE','user_id':'ada'}]
        self.assertEqual(http.request('POST','/_test/reset',body)[0],204)
        self.token = http.request('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[1]['token']
        cancelled = http.request('GET','/reservations/PAIR00',token=self.token)[1]
        self.assertEqual(cancelled['status'],'cancelled')
        self.assertEqual(cancelled['table_ids'],['t1','t2'])
        self.assertNotIn('table_id',cancelled)
        available = http.request('GET','/availability?restaurant_id=r&date=2036-09-24&party_size=2')[1]['slots'][2]
        self.assertEqual(available['available_table_ids'],['t1','t3'])
        self.error(http.request('PATCH','/reservations/PAIR00',{},self.token),409,'reservation_cancelled')

    def test_atomic_moves_pair_overlap_and_rollback(self):
        a = self.create()[1]
        b = self.create(self.body(['t3'],2),'single')[1]
        body = {'moves':[{'reference':a['reference'],'table_ids':['t2','t3']},
                         {'reference':b['reference'],'table_ids':['t1']}]}
        status, swapped = http.request('POST','/reservation-moves',body,self.token,'swap')
        self.assertEqual(status,201)
        self.assertEqual([r['table_ids'] for r in swapped['reservations']],[['t2','t3'],['t1']])
        self.assertNotIn('table_id',swapped['reservations'][0])
        self.assertEqual(swapped['reservations'][1]['table_id'],'t1')
        bad = {'moves':[{'reference':a['reference'],'table_ids':['t1','t2']},
                        {'reference':b['reference'],'table_id':'t2'}]}
        before = http.request('GET','/reservations',token=self.token)[1]
        self.error(http.request('POST','/reservation-moves',bad,self.token,'bad'),409,'table_unavailable')
        self.assertEqual(http.request('GET','/reservations',token=self.token)[1],before)
        self.assertEqual(http.request('POST','/reservation-moves',{'moves':[{'reference':a['reference']}]},self.token,'bad')[0],201)
        http.request('POST','/reservations/'+a['reference']+'/cancel',{},self.token)
        self.assertEqual(http.request('POST','/reservation-moves',body,self.token,'swap'),(200,swapped))

    def test_50_simultaneous_pairs_and_singles_share_occupancy(self):
        import threading
        barrier = threading.Barrier(50)
        def worker(i):
            barrier.wait()
            return self.create(self.body() if i%2 else self.body(['t2'],4),'contend-'+str(i))
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(worker,range(50)))
        self.assertEqual(sum(s==201 for s,_ in results),1)
        self.assertEqual(sum(s==409 for s,_ in results),49)

    def test_stage1_export_preserves_tokens_identities_and_both_receipts(self):
        self.assertEqual(http.request('POST','/_test/reset',http.fixture(),base=LEGACY)[0],204)
        token = http.request('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'},base=LEGACY)[1]['token']
        body = http.HTTPTests.booking(self,table='t2',size=2)
        original = http.request('POST','/reservations',body,token,'legacy-create',LEGACY)[1]
        moves = {'moves':[{'reference':original['reference'],'table_id':'t1'}]}
        move_receipt = http.request('POST','/reservation-moves',moves,token,'legacy-move',LEGACY)[1]
        http.request('POST','/reservations/'+original['reference']+'/cancel',{},token,base=LEGACY)
        snapshot = http.request('GET','/_test/export',base=LEGACY)[1]
        self.assertEqual(http.request('POST','/_test/import',snapshot)[0],204)
        current = http.request('GET','/reservations/'+original['reference'],token=token)[1]
        self.assertEqual(current['table_ids'],['t1'])
        self.assertEqual(current['status'],'cancelled')
        self.assertEqual(current['reservation_id'],original['reservation_id'])
        self.assertEqual(current['created_at'],original['created_at'])
        self.assertEqual(http.request('POST','/reservations',body,token,'legacy-create'),(200,original))
        self.assertEqual(http.request('POST','/reservation-moves',moves,token,'legacy-move'),(200,move_receipt))
        self.assertEqual(http.request('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})[0],200)
        upgraded_snapshot = http.request('GET','/_test/export')[1]
        self.assertEqual(http.request('POST','/_test/import',upgraded_snapshot,base=http.DESTINATION)[0],204)
        self.assertEqual(http.request('POST','/reservations',body,token,'legacy-create',http.DESTINATION),(200,original))

    def test_invalid_pair_import_preserves_destination(self):
        booking = self.create()[1]
        original = http.request('GET','/_test/export')[1]
        for ids in ([],['t1','t1'],['t1','t2','t3']):
            invalid = copy.deepcopy(original)
            invalid['state']['reservations'][booking['reference']]['table_ids'] = ids
            self.error(http.request('POST','/_test/import',invalid),422,'validation_failed')
            self.assertEqual(http.request('GET','/reservations/'+booking['reference'],token=self.token)[1],booking)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--legacy',required=True)
    args = parser.parse_args()
    http.SOURCE,http.DESTINATION,LEGACY = args.source,args.destination,args.legacy
    unittest.main(argv=['test_pairs.py'],verbosity=2)
