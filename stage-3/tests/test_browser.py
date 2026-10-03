"""Real Chromium flows, race/retry/upgrade checks and desktop/mobile visual capture."""
import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect
import test_http as http
from test_pairs import pair_fixture


def control(method, path, body=None, base=None, token=None, key=None):
    result = http.request(method, path, body, token, key, base)
    assert result[0] < 400, (result[0], result[1])
    return result[1]


def visual_fixture():
    body = pair_fixture()
    body['restaurants'][0]['name'] = 'The Olive Room'
    body['restaurants'][0]['tables'][0]['label'] = 'Window'
    body['restaurants'][0]['tables'][1]['label'] = 'Dining Room'
    return body


async def suite(source, destination, legacy, out, legacy2=None):
    http.SOURCE, http.DESTINATION = source, destination
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(viewport={'width':1440,'height':1050})
        page = await context.new_page()
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        async def reset(body=None):
            control('POST','/_test/reset',body or visual_fixture())
            await context.clear_cookies()
            await page.goto(source)
            await page.evaluate('localStorage.clear()')
            await page.reload()
            await expect(page.get_by_test_id('restaurant-select').locator('option')).to_have_count(1)

        async def login():
            await page.goto(source+'/login')
            await page.get_by_test_id('login-email').fill('ada@example.com')
            await page.get_by_test_id('login-password').fill('correct horse')
            await page.get_by_test_id('login-submit').click()
            await expect(page.get_by_test_id('current-user')).to_contain_text('Ada')
            await expect(page.get_by_test_id('restaurant-select')).to_be_visible()

        async def search(size='2', restaurant='r', day='2036-09-24'):
            await page.get_by_test_id('restaurant-select').select_option(restaurant)
            await page.get_by_test_id('date-input').fill(day)
            await page.get_by_test_id('party-size-input').fill(size)
            await page.get_by_test_id('search-button').click()
            await expect(page.get_by_test_id('availability-grid')).to_be_visible()

        async def screenshot(name):
            await page.screenshot(path=str(out/name),full_page=True)
            overflow = await page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not overflow, f'Horizontal overflow in {name}'

        await reset()
        for route, tid in (('/', 'search-button'),('/signup','signup-submit'),('/login','login-submit'),('/lookup','lookup-submit')):
            response = await page.goto(source+route)
            assert response.status == 200
            assert 'text/html' in response.headers['content-type']
            await expect(page.get_by_test_id(tid)).to_be_visible()
        await page.goto(source+'/signup')
        await page.get_by_test_id('signup-display-name').fill('Mila')
        await page.get_by_test_id('signup-email').fill('mila@example.com')
        await page.get_by_test_id('signup-password').fill('wonderful evening')
        await page.get_by_test_id('signup-submit').click()
        await expect(page.get_by_test_id('current-user')).to_contain_text('Mila')
        await page.goto(source+'/lookup')
        await expect(page.get_by_test_id('current-user')).to_contain_text('Mila')
        await page.get_by_test_id('logout-button').click()
        await expect(page.get_by_test_id('current-user')).to_have_count(0)
        print('PASS direct HTML routes, signup, navigation session and logout',flush=True)

        await reset()
        await search()
        await page.get_by_test_id('slot-t1-19:00').click()
        await expect(page.get_by_test_id('auth-error')).to_be_visible()
        await login()
        await search()
        assert await page.get_by_test_id('slot-t1-19:00').get_attribute('data-available') == 'true'
        await screenshot('desktop-availability.png')
        await page.get_by_test_id('slot-t1-19:00').click()
        await expect(page.get_by_test_id('booking-summary')).to_contain_text('Window')
        await expect(page.get_by_test_id('booking-summary')).to_contain_text('19:00')
        await page.get_by_test_id('booking-submit').click()
        await expect(page.get_by_test_id('confirmation-reference')).to_be_visible()
        reference = await page.get_by_test_id('confirmation-reference').inner_text()
        await page.get_by_test_id('booking-submit').click()
        await expect(page.get_by_test_id('confirmation-reference')).to_have_text(reference)
        await expect(page.get_by_test_id('booking-error')).to_have_count(0)
        await screenshot('desktop-confirmation.png')
        await page.goto(source+'/lookup')
        await page.get_by_test_id('lookup-reference-input').fill(reference)
        await page.get_by_test_id('lookup-submit').click()
        await expect(page.get_by_test_id('reservation-status')).to_have_text('confirmed')
        await expect(page.get_by_test_id('reservation-tables')).to_contain_text('Window')
        await page.get_by_test_id('reservation-cancel-button').click()
        await expect(page.get_by_test_id('reservation-status')).to_have_text('cancelled')
        await expect(page.get_by_test_id('reservation-cancel-button')).to_have_count(0)
        print('PASS signed-out guard, single booking/replay, lookup and cancellation',flush=True)

        for pair in (False, True):
            await reset(); await login(); await search('6' if pair else '2')
            cell = 'slot-t2+t1-19:00' if pair else 'slot-t1-19:00'
            await page.get_by_test_id(cell).click()
            token = control('POST','/auth/login',{'email':'bob@example.com','password':'correct horse'})['token']
            body = {'restaurant_id':'r','table_ids':['t2','t1'] if pair else ['t1'],'starts_at_local':'2036-09-24T19:00','party_size':6 if pair else 2}
            control('POST','/reservations',body,token=token,key='competitor')
            await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('booking-error')).to_be_visible()
            await expect(page.get_by_test_id('confirmation')).to_have_count(0)
            await expect(page.get_by_test_id('booking-form')).to_be_visible()
            await expect(page.get_by_test_id('booking-party-size')).to_have_value('6' if pair else '2')
            await expect(page.get_by_test_id('booking-summary')).to_contain_text('19:00')
            if not pair: await expect(page.get_by_test_id(cell)).to_have_attribute('data-available','false')
            await screenshot('desktop-refused-pair.png' if pair else 'desktop-refused-single.png')
            if pair:
                await page.set_viewport_size({'width':375,'height':850})
                await screenshot('mobile-refused-pair.png')
                await page.set_viewport_size({'width':1440,'height':1050})
        print('PASS stolen single/pair 409 refresh preserves form and inputs',flush=True)

        for pair in (False, True):
            await reset(); await login(); await search('6' if pair else '2')
            await page.get_by_test_id('slot-t2+t1-19:00' if pair else 'slot-t1-19:00').click()
            submissions = []
            async def lose_first(route):
                submissions.append((route.request.post_data,route.request.headers['idempotency-key']))
                if len(submissions)==1:
                    response = await route.fetch()
                    assert response.status == 201
                    await route.abort('failed')
                else:
                    await route.continue_()
            await page.route('**/reservations',lose_first)
            await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('booking-uncertain')).to_be_visible()
            await expect(page.get_by_test_id('booking-error')).to_have_count(0)
            await expect(page.get_by_test_id('confirmation')).to_have_count(0)
            await screenshot('desktop-uncertain-pair.png' if pair else 'desktop-uncertain-single.png')
            if pair:
                await page.set_viewport_size({'width':375,'height':850})
                await screenshot('mobile-uncertain-pair.png')
                await page.set_viewport_size({'width':1440,'height':1050})
            await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('confirmation-reference')).to_be_visible()
            await expect(page.get_by_test_id('booking-uncertain')).to_have_count(0)
            assert submissions[0] == submissions[1]
            recovered_reference = await page.get_by_test_id('confirmation-reference').inner_text()
            await page.get_by_test_id('booking-party-size').fill('5' if pair else '1')
            await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('booking-error')).to_be_visible()
            assert submissions[-1][1] != submissions[0][1]
            assert submissions[-1][0] != submissions[0][0]
            assert recovered_reference
            await page.unroute('**/reservations',lose_first)
        print('PASS committed lost single/pair response, same body/key retry, changed-field new key',flush=True)

        data = visual_fixture()
        second = json.loads(json.dumps(data['restaurants'][0])); second['id']='r-b'; second['name']='Garden House'
        second['tables']=[{'id':'b1','label':'Terrace','capacity':4}]; second['combinable']=[]
        data['restaurants'].append(second)
        control('POST','/_test/reset',data)
        await page.goto(source); await login()
        await expect(page.get_by_test_id('restaurant-select').locator('option')).to_have_count(2)
        release = asyncio.Event(); started = asyncio.Event(); finished = asyncio.Event()
        async def delayed(route):
            if 'restaurant_id=r&' in route.request.url:
                started.set(); await release.wait()
                response = await route.fetch(); await route.fulfill(response=response); finished.set()
            else: await route.continue_()
        await page.route('**/availability?**',delayed)
        await page.get_by_test_id('date-input').fill('2036-09-24')
        await page.get_by_test_id('party-size-input').fill('6')
        await page.get_by_test_id('search-button').click(); await started.wait()
        await screenshot('desktop-loading.png')
        await page.set_viewport_size({'width':375,'height':850})
        await screenshot('mobile-loading.png')
        await page.set_viewport_size({'width':1440,'height':1050})
        await page.get_by_test_id('restaurant-select').select_option('r-b')
        await page.get_by_test_id('party-size-input').fill('3')
        await page.get_by_test_id('date-input').fill('2036-09-25')
        await page.get_by_test_id('search-button').click()
        await expect(page.get_by_test_id('slot-b1-19:00')).to_be_visible()
        await page.get_by_test_id('slot-b1-19:00').click()
        release.set(); await finished.wait(); await page.wait_for_timeout(150)
        await expect(page.get_by_test_id('slot-t1-19:00')).to_have_count(0)
        await expect(page.get_by_test_id('booking-summary')).to_contain_text('Terrace')
        await expect(page.get_by_test_id('booking-party-size')).to_have_value('3')
        await expect(page.get_by_test_id('availability-grid')).to_contain_text('Terrace')
        await page.unroute('**/availability?**',delayed)
        print('PASS out-of-order search A cannot replace B grid/labels/form',flush=True)

        control('POST','/_test/reset',http.fixture(),base=legacy)
        control('POST','/_test/reset',http.fixture())
        await page.goto(source); await page.evaluate('localStorage.clear()')
        async def legacy_login(route):
            response = await route.fetch(url=legacy+'/auth/login'); await route.fulfill(response=response)
        await page.route('**/auth/login',legacy_login)
        await login(); await page.unroute('**/auth/login',legacy_login)
        await search(); await page.get_by_test_id('slot-t1-19:00').click()
        legacy_attempts = []; legacy_receipt = None
        async def legacy_lost(route):
            nonlocal legacy_receipt
            legacy_attempts.append((route.request.post_data,route.request.headers['idempotency-key']))
            response = await route.fetch(url=legacy+'/reservations')
            assert response.status==201
            legacy_receipt = await response.json()
            await route.abort('failed')
        await page.route('**/reservations',legacy_lost)
        await page.get_by_test_id('booking-submit').click()
        await expect(page.get_by_test_id('booking-uncertain')).to_be_visible()
        snapshot = control('GET','/_test/export',base=legacy)
        control('POST','/_test/import',snapshot)
        await page.unroute('**/reservations',legacy_lost)
        async def record_retry(route):
            legacy_attempts.append((route.request.post_data,route.request.headers['idempotency-key']))
            await route.continue_()
        await page.route('**/reservations',record_retry)
        await expect(page.get_by_test_id('current-user')).to_contain_text('Ada')
        await page.get_by_test_id('booking-submit').click()
        await expect(page.get_by_test_id('confirmation-reference')).to_have_text(legacy_receipt['reference'])
        assert legacy_attempts[0] == legacy_attempts[1]
        await expect(page.get_by_test_id('booking-uncertain')).to_have_count(0)
        await page.unroute('**/reservations',record_retry)
        await page.goto(source+'/lookup')
        await page.get_by_test_id('lookup-reference-input').fill(legacy_receipt['reference'])
        await page.get_by_test_id('lookup-submit').click()
        await expect(page.get_by_test_id('reservation-status')).to_have_text('confirmed')
        print('PASS Stage 1→3 import retains browser identity, pending key/body and original confirmation/lookup',flush=True)

        if legacy2:
            control('POST','/_test/reset',visual_fixture(),base=legacy2)
            await reset()
            async def prior_login(route):
                response=await route.fetch(url=legacy2+'/auth/login');await route.fulfill(response=response)
            await page.route('**/auth/login',prior_login);await login();await page.unroute('**/auth/login',prior_login)
            await search('6');await page.get_by_test_id('slot-t2+t1-19:00').click()
            attempts=[];old_receipt=None
            async def prior_lost(route):
                nonlocal old_receipt
                attempts.append((route.request.post_data,route.request.headers['idempotency-key']))
                response=await route.fetch(url=legacy2+'/reservations');assert response.status==201
                old_receipt=await response.json();await route.abort('failed')
            await page.route('**/reservations',prior_lost);await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('booking-uncertain')).to_be_visible()
            control('POST','/_test/import',control('GET','/_test/export',base=legacy2))
            await page.unroute('**/reservations',prior_lost)
            async def prior_retry(route):
                attempts.append((route.request.post_data,route.request.headers['idempotency-key']));await route.continue_()
            await page.route('**/reservations',prior_retry);await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('confirmation-reference')).to_have_text(old_receipt['reference'])
            assert attempts[0]==attempts[1]
            await expect(page.get_by_test_id('current-user')).to_contain_text('Ada')
            await expect(page.get_by_test_id('confirmation-tables')).to_contain_text('Window')
            await expect(page.get_by_test_id('confirmation-tables')).to_contain_text('Dining Room')
            await page.unroute('**/reservations',prior_retry)
            print('PASS Stage 2→3 import retains pair pending request, token and original receipt',flush=True)

        await reset(); await login(); await search('6')
        await page.get_by_test_id('slot-t2+t1-19:00').click()
        await page.get_by_test_id('booking-submit').click()
        await expect(page.get_by_test_id('confirmation-tables')).to_contain_text('Dining Room')
        await expect(page.get_by_test_id('confirmation-tables')).to_contain_text('Window')
        await screenshot('desktop-pair-confirmation.png')
        await page.set_viewport_size({'width':375,'height':850})
        await screenshot('mobile-pair-confirmation.png')
        for route, name in (('/login','mobile-login.png'),('/signup','mobile-signup.png'),('/lookup','mobile-lookup.png')):
            await page.goto(source+route); await screenshot(name)
            await page.set_viewport_size({'width':1440,'height':1050});await screenshot(name.replace('mobile','desktop'))
            await page.set_viewport_size({'width':375,'height':850})
        await page.goto(source); await search('2'); await screenshot('mobile-availability.png')
        await page.get_by_test_id('party-size-input').fill('100'); await page.get_by_test_id('search-button').click()
        await expect(page.get_by_test_id('slot-t1-19:00')).to_have_attribute('data-available','false')
        await screenshot('mobile-unavailable.png')
        empty = visual_fixture(); empty['restaurants'][0]['opening_hours']=[]
        control('POST','/_test/reset',empty)
        await page.goto(source); await page.get_by_test_id('date-input').fill('2036-09-24'); await page.get_by_test_id('search-button').click()
        await expect(page.get_by_test_id('no-slots')).to_be_visible(); await screenshot('mobile-empty.png')
        await page.set_viewport_size({'width':1440,'height':1050});await screenshot('desktop-empty.png')

        policy_fixture=visual_fixture();policy_fixture['restaurants'][0]['manager_user_ids']=['ada']
        await reset(policy_fixture);await login()
        token=control('POST','/auth/login',{'email':'ada@example.com','password':'correct horse'})['token']
        policy={'effective_from':'2000-01-01','slot_minutes':15,'reservation_duration_minutes':45,
                'cancellation_cutoff_minutes':0,'opening_hours':policy_fixture['restaurants'][0]['opening_hours'],
                'capacities':{'t1':5,'t2':8,'t3':2}}
        control('POST','/restaurants/r/policies',policy,token=token,key='browser-policy')
        await search('10')
        await expect(page.locator('.seating-card.pair .capacity').first).to_contain_text('Up to 13 guests')
        await page.get_by_test_id('slot-t2+t1-19:00').click()
        await expect(page.get_by_test_id('booking-form')).not_to_contain_text('90 minutes')
        await expect(page.get_by_test_id('booking-form')).to_contain_text('up to 13 guests')
        await page.get_by_test_id('booking-submit').click()
        await expect(page.get_by_test_id('confirmation')).to_contain_text('45 minutes reserved')
        ref=await page.get_by_test_id('confirmation-reference').inner_text()
        await screenshot('desktop-policy-confirmation.png')
        await page.set_viewport_size({'width':375,'height':850});await screenshot('mobile-policy-confirmation.png')
        control('POST','/restaurants/r/policies',{**policy,'reservation_duration_minutes':120},token=token,key='new-policy')
        await page.goto(source+'/lookup');await page.get_by_test_id('lookup-reference-input').fill(ref)
        await page.get_by_test_id('lookup-submit').click()
        await expect(page.get_by_test_id('reservation-detail')).to_contain_text('45 minutes reserved')
        await screenshot('mobile-lookup-found.png')
        await page.set_viewport_size({'width':1440,'height':1050});await screenshot('desktop-lookup-found.png')
        await page.get_by_test_id('reservation-cancel-button').click()
        await expect(page.get_by_test_id('reservation-status')).to_have_text('cancelled')
        await screenshot('desktop-lookup-cancelled.png')
        await page.set_viewport_size({'width':375,'height':850});await screenshot('mobile-lookup-cancelled.png')
        await page.goto(source+'/login');await page.get_by_test_id('login-email').focus()
        assert await page.get_by_test_id('login-email').evaluate("e=>getComputedStyle(e).outlineStyle")!='none'
        await screenshot('mobile-keyboard-focus.png')
        print('PASS policy-derived capacity and accepted duration remain truthful after publication; visible focus',flush=True)
        assert not errors, errors
        print('PASS desktop and 375px visual states; no page overflow or JavaScript errors',flush=True)
        await browser.close()
    print('ALL BROWSER CHECKS PASSED',flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--legacy',required=True)
    parser.add_argument('--legacy2')
    parser.add_argument('--out',required=True,help='External directory for transient screenshots')
    args = parser.parse_args()
    asyncio.run(suite(args.source,args.destination,args.legacy,Path(args.out),args.legacy2))
