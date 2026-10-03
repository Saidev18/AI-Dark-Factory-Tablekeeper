'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const test = id => `[data-testid="${id}"]`;
let session = null;
try { session = JSON.parse(localStorage.getItem('tablekeeper.session')); } catch (_) { /* A damaged local session starts signed out. */ }
let results = null, selected = null, attempt = null, searchVersion = 0, bookingVersion = 0;
const main = $('#main');
const messages = {table_unavailable:'That seating option was just reserved. Choose another available table or time.',
  party_exceeds_capacity:'This party is too large for the selected seating. Choose a larger option or adjust your party size.',
  cutoff_passed:'This reservation is too close to its start time to cancel.',
  unauthenticated:'Please sign in to continue.', not_found:'We couldn’t find a reservation with that reference for your account.',
  email_taken:'That email already has an account. Try signing in.', validation_failed:'Please check the details and try again.',
  outside_opening_hours:'That time is outside the restaurant’s opening hours.', combination_not_allowed:'These tables cannot be reserved together.'};
const friendly = error => messages[error?.code] || error?.message || 'We couldn’t complete that request. Please try again.';
function notice(id, text, kind = 'error', target = main) {
  clearNotice(id);
  const node = document.createElement('div');
  if (id) node.dataset.testid = id;
  node.className = `notice ${kind}`; node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  node.textContent = text; target.append(node); return node;
}
function clearNotice(id) { if (id) $(test(id))?.remove(); }
async function api(path, options = {}) {
  const headers = {'Content-Type':'application/json', ...(session ? {Authorization:`Bearer ${session.token}`} : {}), ...options.headers};
  const response = await fetch(path, {...options, headers});
  const data = response.status === 204 ? null : await response.json();
  return {ok:response.ok, status:response.status, data};
}
function account() {
  $('#account-nav').innerHTML = session
    ? `<span data-testid="current-user" class="user-name">${esc(session.display_name)}</span><button data-testid="logout-button" class="link-button">Log out</button>`
    : '<a href="/login">Sign in</a><a class="nav-join" href="/signup">Join us</a>';
  $(test('logout-button'))?.addEventListener('click', () => {
    session = null; localStorage.removeItem('tablekeeper.session'); account();
    selected = null; attempt = null; bookingVersion++; $('#booking-area')?.replaceChildren();
    clearNotice('auth-error');
  });
}
function seatingLabel(restaurant, ids) {
  const labels = ids.map(id => restaurant?.tables?.find(t => t.id === id)?.label || id);
  return labels.map(label => /^table\b/i.test(label) ? label : `Table ${label}`).join(' + ');
}
function dateLabel(day) {
  const date = new Date(`${day}T12:00:00`);
  return Number.isNaN(date.getTime()) ? day : date.toLocaleDateString('en-GB', {weekday:'short', day:'numeric', month:'long'});
}
function localLabel(local) { return `${dateLabel(local.slice(0,10))} · ${local.slice(11,16)}`; }
function diningArt() {
  return `<div class="dining-art" aria-hidden="true"><svg viewBox="0 0 440 240" fill="none"><path d="M40 214V93a85 85 0 0 1 170 0v121M230 214V93a85 85 0 0 1 170 0v121" stroke="currentColor"/><path d="M55 214V93a70 70 0 0 1 140 0v121M245 214V93a70 70 0 0 1 140 0v121M16 214h408" stroke="currentColor" opacity=".35"/><circle cx="125" cy="110" r="33" stroke="currentColor"/><circle cx="315" cy="110" r="33" stroke="currentColor"/><path d="M125 144v55m-25 0h50M315 144v55m-25 0h50" stroke="currentColor"/></svg></div>`;
}
function searchScreen() {
  main.innerHTML = `<section class="hero"><div class="hero-copy"><p class="eyebrow">A considered dining experience</p><h1>An exceptional<br>evening.<br><span>A table for you.</span></h1><p class="hero-description">The right setting. Your favourite company.<br>Make space for an evening worth remembering.</p><p class="hero-signature"><span></span>Every great evening starts with a reservation.</p></div>
  <form id="search-form" class="search-panel concierge" novalidate><div class="panel-heading"><span class="step-mark">01</span><div><p class="eyebrow">Set the occasion</p><h2>Plan your evening</h2></div></div><div class="search-fields"><div class="field"><label for="restaurant">Restaurant</label><select id="restaurant" data-testid="restaurant-select" required><option value="">Loading restaurants…</option></select></div><div class="field"><label for="date">Dining date</label><input id="date" data-testid="date-input" type="date" required></div><div class="field"><label for="party">Number of guests</label><input id="party" data-testid="party-size-input" type="number" min="1" step="1" value="2" required></div><button data-testid="search-button" class="primary" type="submit">Explore availability <span class="arrow" aria-hidden="true">→</span></button></div><p class="search-caption">From an intimate dinner to a shared celebration.</p></form></section>
  <section id="results" class="results-section" aria-live="polite"><div class="empty-state welcome-empty"><span class="empty-icon" aria-hidden="true">✧</span><div><h3>A setting for your next story.</h3><p>Select your restaurant, date and guests to explore available seating.</p></div></div></section><div id="booking-area" class="booking-area"></div>`;
  const date = new Date(); date.setMinutes(date.getMinutes() - date.getTimezoneOffset()); $(test('date-input')).value = date.toISOString().slice(0,10);
  $('#search-form').addEventListener('submit', event => { event.preventDefault(); runSearch(); });
  api('/restaurants').then(response => {
    if (!response.ok) throw new Error(friendly(response.data?.error));
    const restaurants = response.data.restaurants;
    $(test('restaurant-select')).innerHTML = restaurants.length
      ? restaurants.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('')
      : '<option value="">No restaurants available</option>';
    if (!restaurants.length) $('#results').innerHTML = '<div class="empty-state"><h3>A table will be waiting.</h3><p>There are no restaurants to browse just yet. Please check back soon.</p></div>';
  }).catch(() => notice('search-error','Restaurants could not be loaded. Refresh the page to try again.'));
}
async function fetchSearch(parameters) {
  const query = new URLSearchParams(parameters);
  const [availability, restaurant] = await Promise.all([api(`/availability?${query}`), api(`/restaurants/${encodeURIComponent(parameters.restaurant_id)}`)]);
  if (!availability.ok) throw new Error(friendly(availability.data?.error));
  if (!restaurant.ok) throw new Error(friendly(restaurant.data?.error));
  for (const slot of availability.data.slots) {
    slot.available_options ??= restaurant.data.tables.filter(t => slot.available_table_ids.includes(t.id)).map(t => ({table_ids:[t.id], capacity:t.capacity}));
  }
  return {parameters, restaurant:restaurant.data, availability:availability.data};
}
async function runSearch() {
  const parameters = {restaurant_id:$(test('restaurant-select')).value, date:$(test('date-input')).value, party_size:$(test('party-size-input')).value};
  const version = ++searchVersion;
  selected = null; attempt = null; bookingVersion++; results = null;
  $('#booking-area').replaceChildren(); clearNotice('auth-error'); clearNotice('search-error');
  $('#results').innerHTML = '<div class="notice loading" role="status"><span class="spinner"></span>Finding a place for your gathering…</div><div class="availability-grid" aria-hidden="true" style="margin-top:18px"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>';
  try {
    const response = await fetchSearch(parameters);
    if (version !== searchVersion) return;
    results = response; renderResults();
  } catch (error) {
    if (version !== searchVersion) return;
    $('#results').replaceChildren(); notice('search-error', error.message, 'error', $('#results'));
  }
}
function renderResults() {
  const {restaurant, availability, parameters} = results;
  const slots = availability.slots;
  const count = slots.reduce((total, s) => total + s.available_options.length, 0);
  let html = `<div class="results-heading"><div><p class="eyebrow">02 / Select your setting</p><h2>${esc(restaurant.name)}</h2><p class="context-line">${esc(dateLabel(parameters.date))} <span>·</span> ${esc(parameters.party_size)} guests <span>·</span> ${esc(restaurant.timezone.replaceAll('_',' '))}</p></div><span class="result-count">${count} available ${count === 1 ? 'option' : 'options'}</span></div>`;
  if (!slots.length) {
    html += '<div data-testid="no-slots" class="empty-state"><div class="empty-icon" aria-hidden="true">☾</div><h3>A quiet day at the restaurant.</h3><p>There are no dining times on this date. Try another day for your gathering.</p></div>';
  } else {
    html += '<div class="legend"><span><i></i>Available</span><span><i class="reserved"></i>Unavailable for your party</span><span><i class="chosen"></i>Your choice</span></div><div data-testid="availability-grid" class="availability-grid">';
    // Published policies leave restaurant detail unchanged. Capacity claims come
    // from the authoritative searched options, never the original table detail.
    const capacityFor = ids => slots.flatMap(s => s.available_options).find(o => sameSet(o.table_ids,ids))?.capacity;
    const choices = restaurant.tables.map(t => ({ids:[t.id], capacity:capacityFor([t.id])}));
    for (const pair of restaurant.combinable || []) {
      if (slots.some(s => s.available_options.some(o => o.table_ids.length === 2 && sameSet(o.table_ids,pair)))) {
        choices.push({ids:pair, capacity:capacityFor(pair)});
      }
    }
    for (const [index, option] of choices.entries()) {
      const pair = option.ids.length === 2;
      html += `<article class="seating-card ${pair ? 'pair' : ''}"><div class="seating-top"><span class="seat-number" aria-hidden="true">${String(index+1).padStart(2,'0')}</span><div>${pair ? '<span class="pair-badge">Shared seating</span>' : '<span class="table-category">Individual table</span>'}<h3 class="seating-label">${esc(seatingLabel(restaurant, option.ids))}</h3><p class="capacity">${pair ? 'Two tables, one reservation' : 'Your own dining space'}${option.capacity ? ` · Up to ${option.capacity} guests` : ''}</p></div></div><div class="seating-times"><div class="time-grid">`;
      for (const slot of slots) {
        const available = slot.available_options.some(o => sameSet(o.table_ids, option.ids));
        if (pair && !available) continue;
        const chosen = selected && selected.local === slot.starts_at_local && sameSet(selected.ids,option.ids);
        html += `<button type="button" class="time-cell" data-testid="slot-${esc(option.ids.join('+'))}-${slot.starts_at_local.slice(11,16)}" data-available="${available}" aria-pressed="${Boolean(chosen)}" aria-label="${esc(seatingLabel(restaurant,option.ids))}, ${slot.starts_at_local.slice(11,16)}${available ? '' : ', unavailable'}" ${available ? '' : 'disabled'} data-local="${esc(slot.starts_at_local)}" data-tables="${esc(JSON.stringify(option.ids))}">${slot.starts_at_local.slice(11,16)}</button>`;
      }
      html += '</div><p class="card-footnote">Choose your arrival time</p></div></article>';
    }
    html += '</div><p class="browse-note"><span class="tiny-leaf"></span>All times are local to the restaurant.</p>';
  }
  $('#results').innerHTML = html;
  for (const button of $('#results').querySelectorAll('button[data-local]')) button.addEventListener('click', () => {
    if (!session) { notice('auth-error','Sign in or create an account to reserve your place.', 'error', $('#results')); return; }
    const ids = JSON.parse(button.dataset.tables);
    const option = slots.find(s => s.starts_at_local === button.dataset.local)?.available_options.find(o => sameSet(o.table_ids,ids));
    selected = {restaurant, ids, capacity:option?.capacity, local:button.dataset.local, party:Number(parameters.party_size), searchVersion};
    attempt = null; bookingVersion++; clearNotice('auth-error'); renderResults(); renderBooking();
    $(test('booking-form')).scrollIntoView({behavior:'smooth', block:'nearest'});
  });
}
function sameSet(a,b) { return a.length === b.length && a.every(id => b.includes(id)); }
function renderBooking() {
  $('#booking-area').innerHTML = `<form data-testid="booking-form" class="booking-card"><div class="booking-intent"><p class="eyebrow">03 / Make it yours</p><h2>Your evening,<br>in good hands.</h2><p data-testid="booking-summary" class="booking-summary">${esc(seatingLabel(selected.restaurant,selected.ids))}<br>${esc(localLabel(selected.local))}</p></div><div class="booking-action"><div class="booking-controls"><div class="field"><label for="booking-party">Guests at your table</label><input id="booking-party" data-testid="booking-party-size" type="number" min="1" step="1" value="${selected.party}" required></div><button data-testid="booking-submit" class="primary" type="submit">Confirm reservation <span aria-hidden="true">→</span></button></div><p class="booking-meta">${selected.capacity ? `Seating for up to ${selected.capacity} guests. ` : ''}Your reservation details will be confirmed below.</p><div id="booking-feedback" aria-live="polite"></div></div></form>`;
  $(test('booking-party-size')).addEventListener('input', () => {
    attempt = null; bookingVersion++; clearNotice('booking-error'); clearNotice('booking-uncertain'); $(test('confirmation'))?.remove();
  });
  $(test('booking-form')).noValidate = true;
  $(test('booking-form')).addEventListener('submit', submitBooking);
}
async function submitBooking(event) {
  event.preventDefault();
  if (!session) { notice('auth-error','Please sign in to reserve your place.', 'error', $('#booking-feedback')); return; }
  const body = {restaurant_id:selected.restaurant.id, ...(selected.ids.length === 1 ? {table_id:selected.ids[0]} : {table_ids:selected.ids.slice()}), starts_at_local:selected.local, party_size:Number($(test('booking-party-size')).value)};
  const serialized = JSON.stringify(body);
  if (!attempt || attempt.serialized !== serialized) attempt = {serialized, key:globalThis.crypto?.randomUUID?.() || `booking-${Date.now()}-${Math.random().toString(36).slice(2)}`};
  const pending = attempt, version = bookingVersion, context = selected;
  const button = $(test('booking-submit')); button.disabled = true; button.textContent = 'Reserving your place…';
  clearNotice('booking-error'); clearNotice('booking-uncertain'); $(test('confirmation'))?.remove();
  try {
    const response = await api('/reservations', {method:'POST', body:pending.serialized, headers:{'Idempotency-Key':pending.key}});
    if (version !== bookingVersion || selected !== context) return;
    if (!response.ok) {
      notice('booking-error', friendly(response.data?.error), 'error', $('#booking-feedback'));
      if (response.status === 409 && response.data.error.code === 'table_unavailable') await refreshAvailability(context.searchVersion);
      return;
    }
    confirmation(response.data, context.restaurant);
  } catch (_) {
    if (version !== bookingVersion || selected !== context) return;
    notice('booking-uncertain','We couldn’t confirm the response. Your table may be reserved. Keep these details and retry — we’ll check the same request without booking twice.', 'uncertain', $('#booking-feedback'));
  } finally {
    button.disabled = false; button.textContent = $(test('booking-uncertain')) ? 'Retry this reservation' : $(test('confirmation')) ? 'Check confirmation' : 'Reserve your table';
  }
}
async function refreshAvailability(version) {
  if (!results || version !== searchVersion) return;
  const parameters = {...results.parameters};
  try {
    const refreshed = await fetchSearch(parameters);
    if (version !== searchVersion) return;
    results = refreshed; renderResults();
  } catch (_) { /* Preserve the refused booking and inputs if refresh also loses connectivity. */ }
}
function confirmation(booking, restaurant) {
  const ids = booking.table_ids || [booking.table_id];
  const node = document.createElement('section'); node.dataset.testid = 'confirmation'; node.className = 'confirmation-card'; node.setAttribute('role','status');
  node.innerHTML = `<div class="confirmation-main"><p class="eyebrow"><span class="success-mark" aria-hidden="true">✓</span>Reservation confirmed</p><h2>We look forward to your evening.</h2><p data-testid="confirmation-details" class="confirmation-details">${esc(restaurant.name)}<br>${esc(seatingLabel(restaurant,ids))}<br>${esc(localLabel(booking.starts_at_local))}</p><p data-testid="confirmation-tables" class="confirmation-tables">${esc(seatingLabel(restaurant,ids))}</p>${booking.accepted_terms ? `<p class="accepted-duration">${booking.accepted_terms.reservation_duration_minutes} minutes reserved for your party</p>` : ''}</div><div class="confirmation-receipt"><p class="reference-label">Your confirmation reference</p><p data-testid="confirmation-reference" class="reference">${esc(booking.reference)}</p><a class="confirmation-link" href="/lookup?reference=${encodeURIComponent(booking.reference)}">Manage your reservation <span aria-hidden="true">→</span></a><p class="receipt-note">Keep this reference for your evening.</p></div>`;
  $('#booking-area').append(node);
  node.scrollIntoView({behavior:'smooth', block:'nearest'});
}
function authScreen(signup) {
  const prefix = signup ? 'signup' : 'login';
  main.innerHTML = `<div class="auth-layout"><section class="auth-card"><p class="eyebrow">${signup ? 'An invitation to great evenings' : 'Your personal dining space'}</p><h1>${signup ? 'Join Tablekeeper.' : 'Welcome back.'}</h1><p class="intro">${signup ? 'A few details. A world of memorable evenings.' : 'Sign in to reserve and manage your next evening.'}</p><form id="auth-form" class="stack">${signup ? '<div class="field"><label for="name">Your name</label><input id="name" data-testid="signup-display-name" autocomplete="name" required></div>' : ''}<div class="field"><label for="email">Email address</label><input id="email" data-testid="${prefix}-email" type="email" autocomplete="email" required></div><div class="field"><label for="password">Password</label><input id="password" data-testid="${prefix}-password" type="password" autocomplete="${signup ? 'new-password' : 'current-password'}" ${signup ? 'minlength="8"' : ''} required>${signup ? '<small>At least 8 characters.</small>' : ''}</div><button data-testid="${prefix}-submit" class="primary" type="submit">${signup ? 'Create your account' : 'Sign in'} <span class="arrow" aria-hidden="true">→</span></button></form><div id="auth-feedback" aria-live="polite"></div><p class="auth-switch">${signup ? 'Already a guest? <a href="/login">Sign in</a>' : 'Your first evening here? <a href="/signup">Create an account</a>'}</p></section><aside class="auth-story"><p class="eyebrow">The art of a good evening</p><h2>Some moments<br>deserve the<br><span>perfect setting.</span></h2><p>For quiet conversations, shared celebrations, and everything in between.</p>${diningArt()}<span class="story-detail">An evening well kept.</span></aside></div>`;
  $('#auth-form').noValidate = true;
  $('#auth-form').addEventListener('submit', async event => {
    event.preventDefault(); clearNotice('auth-error');
    const body = {email:$('#email').value, password:$('#password').value};
    if (signup) body.display_name = $('#name').value;
    const button = $(test(`${prefix}-submit`)); button.disabled = true;
    try {
      const response = await api(`/auth/${prefix}`, {method:'POST', body:JSON.stringify(body)});
      if (!response.ok) { notice('auth-error', friendly(response.data?.error), 'error', $('#auth-feedback')); return; }
      session = response.data; localStorage.setItem('tablekeeper.session', JSON.stringify(session));
      location.assign('/');
    } catch (_) { notice('auth-error','We couldn’t reach the restaurant service. Please try again.', 'error', $('#auth-feedback')); }
    finally { button.disabled = false; }
  });
}
let lookupVersion = 0;
function lookupScreen() {
  main.innerHTML = '<section class="lookup-layout"><div class="lookup-search"><p class="eyebrow">Your reservation</p><h1>The details<br>of your evening.</h1><p class="intro">Enter your confirmation reference to find your table and manage your reservation.</p><form id="lookup-form" class="search-panel"><div class="lookup-fields"><div class="field"><label for="reference">Confirmation reference</label><input id="reference" data-testid="lookup-reference-input" placeholder="e.g. K3P7QW" autocomplete="off" required></div><button data-testid="lookup-submit" class="primary" type="submit">Find my reservation <span aria-hidden="true">→</span></button></div></form><div id="lookup-feedback" aria-live="polite"></div></div><div id="lookup-detail"><div class="lookup-placeholder"><span aria-hidden="true">✧</span><p>Your evening, at a glance.</p><small>Your reservation details will appear here.</small></div></div></section>';
  $(test('lookup-reference-input')).value = new URLSearchParams(location.search).get('reference') || '';
  $('#lookup-form').addEventListener('submit', async event => {
    event.preventDefault(); const version = ++lookupVersion;
    clearNotice('reservation-error'); $('#lookup-detail').replaceChildren();
    const button = $(test('lookup-submit')); button.disabled = true; button.textContent = 'Finding your reservation…';
    try {
      const response = await api(`/reservations/${encodeURIComponent($(test('lookup-reference-input')).value.trim())}`);
      if (version !== lookupVersion) return;
      if (!response.ok) { notice('reservation-error', friendly(response.data?.error), 'error', $('#lookup-feedback')); return; }
      const restaurant = await api(`/restaurants/${encodeURIComponent(response.data.restaurant_id)}`);
      if (version !== lookupVersion) return;
      renderDetail(response.data, restaurant.data);
    } catch (_) { notice('reservation-error','Your reservation could not be loaded. Please try again.', 'error', $('#lookup-feedback')); }
    finally { button.disabled = false; button.innerHTML = 'Find my reservation <span aria-hidden="true">→</span>'; }
  });
}
function renderDetail(booking, restaurant) {
  const ids = booking.table_ids || [booking.table_id];
  $('#lookup-detail').innerHTML = `<section data-testid="reservation-detail" class="reservation-card"><div class="reservation-top"><p class="eyebrow">Your dining itinerary</p><span data-testid="reservation-status" class="reservation-status ${booking.status}">${esc(booking.status)}</span></div><h2>${esc(restaurant.name)}</h2><p class="context-line">${esc(booking.reference)} <span>·</span> ${booking.party_size} guests</p><div class="itinerary"><p class="reference-label">Date & arrival</p><p class="reservation-date">${esc(localLabel(booking.starts_at_local))}</p><p class="reference-label">Your setting</p><p data-testid="reservation-tables" class="reservation-tables">${esc(seatingLabel(restaurant,ids))}</p>${booking.accepted_terms ? `<p class="accepted-duration">${booking.accepted_terms.reservation_duration_minutes} minutes reserved for your party</p>` : ''}</div>${booking.status === 'confirmed' ? '<button data-testid="reservation-cancel-button" class="secondary">Cancel reservation</button>' : '<p class="context-line">This reservation is cancelled. We hope to welcome you another evening.</p>'}</section>`;
  $(test('reservation-cancel-button'))?.addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true; button.textContent = 'Cancelling…'; clearNotice('reservation-error');
    try {
      const response = await api(`/reservations/${encodeURIComponent(booking.reference)}/cancel`, {method:'POST', body:'{}'});
      if (!response.ok) { notice('reservation-error', friendly(response.data?.error), 'error', $('#lookup-feedback')); return; }
      renderDetail(response.data, restaurant);
    } catch (_) { notice('reservation-error','We couldn’t confirm the cancellation. Look up your reservation again to check its status.', 'error', $('#lookup-feedback')); }
    finally { button.disabled = false; button.textContent = 'Cancel reservation'; }
  });
}
account();
for (const link of document.querySelectorAll('[data-nav]')) if (link.dataset.nav === location.pathname) link.setAttribute('aria-current','page');
if (location.pathname === '/signup') authScreen(true);
else if (location.pathname === '/login') authScreen(false);
else if (location.pathname === '/lookup') lookupScreen();
else searchScreen();
