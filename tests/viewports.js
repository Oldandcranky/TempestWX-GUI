/* Geometry tests for the dashboard.
 *
 * Every card, at every viewport the display is ever likely to see, must fit
 * inside itself. That sounds trivial; it is the bug class that has actually
 * reached the wall. The Pollen face covered its own caption, and a hero word
 * ran off the side of a card, and both went unnoticed until a photograph
 * came back. Both would have failed here in about four seconds.
 *
 * Run through tests/run.sh, which starts the demo server first. Or directly,
 * against a server already up:
 *
 *     node tests/viewports.js                 # http://localhost:8444
 *     node tests/viewports.js http://host:8444
 *
 * What it checks, per card, per viewport:
 *
 *   - the card body does not overflow itself, on either axis. Both, because
 *     for a while it only measured height and a word running off the side
 *     went unseen.
 *   - no two things stacked in the body overlap each other.
 *   - nothing painted inside the card escapes the card. An SVG with
 *     `overflow: visible` can draw well outside the box it is laid out in,
 *     and the box measuring clean says nothing about that.
 *   - the page logged no errors while doing it.
 *
 * And then the ten-day outlook, opened the way a TV opens it — a tap on the
 * Forecast card's header — measured the same way, plus: the page does not
 * scroll sideways, the dashed normal band is labelled and its labels sit
 * inside the plot, clear of each other and of the forecast lines, and the
 * outlook's own header takes you back. The corner scale figures that people
 * misread as normals must stay gone.
 *
 * Then the alert banner at a phone, a tablet and the TV: each alert centred,
 * saying "until …" rather than NWS's sentence, naming the office, and never
 * cutting off the event or the end time.
 *
 * Last, once, in TV mode: every way back from the outlook — the Back button,
 * a tap anywhere on it, the idle return — and that two exits at once go back
 * one step, not two. Two would take the wall display off the dashboard. And
 * the way in: a tap anywhere on the Forecast card, on the TV and off it. And
 * the Internet card turning over, and back, from a tap anywhere on it.
 *
 * What it cannot check: whether any of it looks right. A card can pass every
 * assertion here and still be ugly, or say something untrue. Look at a
 * screenshot as well.
 */

const { chromium } = require(process.env.PLAYWRIGHT ||
                             '/opt/node22/lib/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://localhost:8444';

/* The page is measured against a fixed state, not against whatever the demo
   server happens to be generating this minute. Two reasons. The demo's
   numbers drift with the clock, so a card sitting one pixel inside its
   bounds passed or failed depending on when you ran it — a suite that
   answers differently on the same code is worse than no suite. And a
   captured state can be made hostile on purpose: the longest ISP name, the
   widest pollen word, a four-figure reading in every slot. A quiet Tuesday
   is not the case worth testing. */
const STATE = JSON.parse(fs.readFileSync(path.join(__dirname, 'state.json'), 'utf8'));

// Two desktops, an ultrawide, the short letterbox a TV in a corner ends up
// with, an old 4:3 panel, and a phone. The short one earns its place: it is
// the tightest for height and the first to overflow.
const VIEWPORTS = [
  [1920, 1080, '1920x1080'],
  [2560, 1080, '2560x1080'],
  [1280, 1024, '1280x1024'],
  [1920,  720, '1920x720 '],
  [1024,  768, '1024x768 '],
  [ 414,  896, '414x896  '],
];

const SETTLE_MS = 3000;     // two render ticks plus the fit pass

/* The captured forecast has placeholder dates. Lay them out from today, in
   UTC as the page does, so the first cell is always "Today" — the widest
   day label, and the highlighted one. */
const freshen = () => {
  const s = JSON.parse(JSON.stringify(STATE));
  s.now = Date.now() / 1000;
  // Every "when" moves with the clock, so a reading captured five minutes
  // before the state still reads "5m ago" however old the file is.
  const shift = s.now - STATE.now;
  for (const block of Object.values(s)) {
    if (!block || typeof block !== 'object' || Array.isArray(block)) continue;
    for (const k of ['at', 'fetched_at', 'observed_at'])
      if (typeof block[k] === 'number') block[k] += shift;
  }
  if (s.forecast && s.forecast.days) {
    s.forecast.fetched_at = s.now;
    const day0 = Date.UTC(...new Date().toISOString().slice(0, 10).split('-')
                           .map((v, i) => i === 1 ? v - 1 : +v));
    s.forecast.days.forEach((d, i) => {
      d.date = new Date(day0 + i * 864e5).toISOString().slice(0, 10);
    });
  }
  return s;
};

const measure = (root) => {
  const bad = [];
  for (const card of document.querySelector(root).querySelectorAll('.card[id]')) {
    const id = card.id.replace('card-', '');
    const body = card.querySelector('.card-body');
    if (!body) continue;

    const oy = body.scrollHeight - body.clientHeight;
    const ox = body.scrollWidth - body.clientWidth;
    if (oy > 1) bad.push([id, 'overflows down by ' + oy + 'px']);
    if (ox > 1) bad.push([id, 'overflows sideways by ' + ox + 'px']);

    // Stacked children must not sit on top of one another. This is the
    // Pollen overlap, stated as an assertion.
    const kids = [...body.children].filter(k => k.getBoundingClientRect().height > 0);
    for (let i = 1; i < kids.length; i++) {
      const above = kids[i - 1].getBoundingClientRect();
      const below = kids[i].getBoundingClientRect();
      const bite = above.bottom - below.top;
      if (bite > 1)
        bad.push([id, 'row ' + i + ' overlaps the one above by ' + bite.toFixed(0) + 'px']);
    }

    // svg.plot carries overflow:visible, so a plot can paint outside the
    // element that measured clean. Compare what is drawn against the card.
    const frame = card.getBoundingClientRect();
    for (const svg of card.querySelectorAll('svg')) {
      const r = svg.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const spill = Math.max(frame.top - r.top, r.bottom - frame.bottom,
                             frame.left - r.left, r.right - frame.right);
      if (spill > 1)
        bad.push([id, 'a drawing escapes the card by ' + spill.toFixed(0) + 'px']);
    }

    // The scale bands — Records, Air quality, Station. Their labels were
    // placed in em of their own font under a bar measured in another, so on
    // a large card they rode up onto the colours, and the month's outline
    // was a box over the labels as well as the bar.
    for (const band of card.querySelectorAll('.gauge-span')) {
      const track = band.querySelector('.track').getBoundingClientRect();
      const labels = [...band.querySelectorAll('.end,.nowlab')]
        .map(n => ({text: n.textContent, r: n.getBoundingClientRect()}));
      for (const {text, r} of labels) {
        if (r.top < track.bottom - 0.5)
          bad.push([id, 'band label "' + text + '" sits on the bar, ' +
                        (track.bottom - r.top).toFixed(0) + 'px up']);
        if (r.left < frame.left - 1 || r.right > frame.right + 1)
          bad.push([id, 'band label "' + text + '" leaves the card']);
      }
      for (let i = 0; i < labels.length; i++)
        for (let j = i + 1; j < labels.length; j++) {
          const a = labels[i].r, c = labels[j].r;
          if (a.left < c.right - 1 && c.left < a.right - 1)
            bad.push([id, 'band labels "' + labels[i].text + '" and "' + labels[j].text + '" overlap']);
        }
      const month = band.querySelector('.month');
      if (month) {
        const m = month.getBoundingClientRect();
        // Stopping just short of an end leaves a stub of bar past the outline,
        // which reads as the bar bleeding through it.
        for (const [gap, end] of [[m.left - track.left, 'left'], [track.right - m.right, 'right']])
          if (gap > 1.5 && gap < track.width * 0.03)
            bad.push([id, 'the month outline stops ' + gap.toFixed(0) + 'px short of the ' + end + ' end']);
        if (m.bottom > track.bottom + track.height || m.top < track.top - track.height)
          bad.push([id, 'the month outline is ' + m.height.toFixed(0) + 'px tall round a ' +
                        track.height.toFixed(0) + 'px bar']);
      }
    }

    const fit = parseFloat(getComputedStyle(card).getPropertyValue('--fit')) || 1;
    if (fit < 0.6)
      bad.push([id, 'type shrunk to ' + fit.toFixed(2) + ' — the card is too full']);
  }
  return bad;
};

/* The outlook page: what measure() cannot see. */
const measureOutlook = () => {
  const bad = [];
  const say = (why) => bad.push(['outlook', why]);
  if (!document.body.classList.contains('show-outlook'))
    return [['outlook', 'the Forecast header did not open it']];

  const doc = document.documentElement.scrollWidth - innerWidth;
  const host = document.getElementById('outlook');
  if (doc > 1) say('the page scrolls sideways by ' + doc + 'px');
  if (host.scrollWidth - host.clientWidth > 1)
    say('scrolls sideways by ' + (host.scrollWidth - host.clientWidth) + 'px');
  // A phone scrolls the outlook on purpose; anything wider must not.
  if (innerWidth > 720 && host.scrollHeight - host.clientHeight > 1)
    say('scrolls down by ' + (host.scrollHeight - host.clientHeight) + 'px');

  if (host.querySelector('.ax.hi, .ax.lo'))
    say('the corner scale figures are back — they read as normals');
  const periods = [...host.querySelectorAll('.nwswords .period')];
  if (periods.length !== 2) say('expected the forecaster\'s words for 2 periods, found ' + periods.length);
  for (const p of periods) {
    if (p.scrollWidth - p.clientWidth > 1) say('"' + p.querySelector('b').textContent + '" is cut off sideways');
    if (!/Tonight|Night|day/.test(p.querySelector('b').textContent)) say('a period with no name');
  }
  if (!/Words: NWS/.test(host.textContent)) say('the words carry no attribution');

  const plotEl = host.querySelector('.outplot');
  if (!plotEl) { say('no plot'); return bad; }
  const plot = plotEl.getBoundingClientRect();
  const labels = [...host.querySelectorAll('.ax.norm')];
  if (labels.length !== 2) { say('expected 2 normal labels, found ' + labels.length); return bad; }
  const rects = labels.map(l => l.getBoundingClientRect());

  rects.forEach((r, i) => {
    const out = Math.max(plot.top - r.top, r.bottom - plot.bottom,
                         plot.left - r.left, r.right - plot.right);
    if (out > 1) say('"' + labels[i].textContent + '" sits ' + out.toFixed(0) + 'px outside the plot');
  });
  const [a, b] = rects;
  if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom)
    say('the two normal labels overlap');

  // Walk the drawn high and low lines in screen space and see whether either
  // passes through a label. The SVG is stretched without keeping its aspect,
  // so each axis scales on its own.
  const svg = plotEl.querySelector('svg.plot');
  const box = svg.getBoundingClientRect();
  const [, , W, H] = svg.getAttribute('viewBox').split(' ').map(Number);
  for (const path of svg.querySelectorAll('path[stroke="var(--red)"], path[stroke="var(--cold)"]')) {
    const pts = (path.getAttribute('d').match(/-?[\d.]+/g) || []).map(Number);
    const xy = [];
    for (let i = 0; i + 1 < pts.length; i += 2)
      xy.push([box.left + pts[i] / W * box.width, box.top + pts[i + 1] / H * box.height]);
    for (let i = 1; i < xy.length; i++) {
      const [x0, y0] = xy[i - 1], [x1, y1] = xy[i];
      for (let t = 0; t <= 1; t += 0.02) {
        const x = x0 + (x1 - x0) * t, y = y0 + (y1 - y0) * t;
        rects.forEach((r, j) => {
          if (x > r.left + 1 && x < r.right - 1 && y > r.top + 1 && y < r.bottom - 1)
            bad.push(['outlook', 'a forecast line runs through "' + labels[j].textContent + '"']);
        });
      }
    }
  }
  return [...new Map(bad.map(x => [x.join('|'), x])).values()];
};

/* A budget, and a clock on every line.

   The suite prints nothing while a section runs, so from outside a slow run
   and a hung one look the same — and a hung headless browser will sit there
   until someone notices. TEST_BUDGET_S is the most this may take; past it
   the run says which section it was in, shuts the browser and exits 3. Each
   result line carries how long its section took, so when the budget does go
   it is obvious which section ate it. */
const BUDGET_S = Number(process.env.TEST_BUDGET_S) || 240;
const LANES = Math.max(1, Number(process.env.TEST_LANES) || 4);
const T0 = Date.now();
const { AsyncLocalStorage } = require('async_hooks');
const clock = new AsyncLocalStorage();      // which section a line came from
const running = new Set();
const say = console.log.bind(console);
console.log = (...args) => {
  const line = String(args[0] == null ? '' : args[0]);
  const mine = clock.getStore();
  if (mine && /^  (ok  |FAIL) /.test(line))
    return say(line.padEnd(44) + '  ' + ((Date.now() - mine.start) / 1000).toFixed(0).padStart(3) + 's');
  return say(...args);
};

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  let failures = 0, checked = 0;
  setTimeout(() => {
    say('\n  FAIL out of time: ' + BUDGET_S + 's spent. Still running: ' + [...running].join('; '));
    say('       raise TEST_BUDGET_S if the suite has honestly grown; otherwise something hung');
    // Straight out. Closing the browser politely first made every section
    // still waiting fail on a closed browser, and buried this message under
    // their complaints. Playwright takes its browser down with the process.
    process.exit(3);
  }, BUDGET_S * 1000);

  /* Sections are independent — each opens its own pages and answers its own
     requests — and nearly all of their time is spent waiting for a page to
     settle, not computing. One after another they took eight minutes, which
     is not a suite anyone runs before a push. They go LANES at a time. A
     section's result and its reasons are printed in one breath, so lanes
     cannot interleave them. The one marked solo runs alone at the end: it
     measures how long a press was held, and a busy machine would blur it. */
  const sections = [];
  const section = (name, fn, solo) => sections.push({name, fn, solo});
  // The same idea inside a section: a list of pages that do not depend on
  // each other, a few at a time rather than in a row.
  const abreast = async (items, fn, n = 5) => {
    const todo = items.slice();
    await Promise.all(Array.from({length: n}, async () => {
      while (todo.length) await fn(todo.shift());
    }));
  };

  for (const [width, height, label] of VIEWPORTS) section(label.trim() + ' cards', async () => {
    const page = await browser.newPage({ viewport: { width, height } });
    const noise = [];
    page.on('pageerror', e => noise.push('pageerror: ' + e.message));
    page.on('console', m => { if (m.type() === 'error') noise.push('console: ' + m.text()); });

    // Keep the clock moving so anything derived from "now" stays sane, but
    // hold everything else still.
    await page.route('**/api/state', route => route.fulfill({
      status: 200,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify(freshen()),
    }));

    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.card[id]', { state: 'attached' });
      await page.waitForTimeout(SETTLE_MS);

      const bad = await page.evaluate(measure, '#grid');
      const cards = await page.evaluate(() => document.querySelectorAll('#grid .card[id]').length);
      checked += cards;

      // In by the Forecast header, as on the TV; out by the outlook's own.
      await page.$eval('#card-fc .card-head', el => el.click());
      await page.waitForTimeout(SETTLE_MS);
      bad.push(...await page.evaluate(measure, '#outlook'));
      bad.push(...await page.evaluate(measureOutlook));
      checked += 1;
      // Only try the way out if the way in worked, or the failure to open
      // is reported as a missing element instead of as itself.
      if (await page.evaluate(() => document.body.classList.contains('show-outlook'))) {
        await page.$eval('#outlook .card-head', el => el.click());
        await page.waitForTimeout(1200);
        if (await page.evaluate(() => document.body.classList.contains('show-outlook')))
          bad.push(['outlook', 'its header did not close it']);
      }

      for (const line of noise) bad.push(['(page)', line]);

      if (bad.length === 0) {
        console.log('  ok   ' + label + '  ' + cards + ' cards + outlook');
      } else {
        failures += bad.length;
        console.log('  FAIL ' + label + '  ' + cards + ' cards + outlook');
        for (const [id, why] of bad) console.log('         ' + id.padEnd(10) + why);
      }
    } catch (e) {
      failures++;
      console.log('  FAIL ' + label + '  ' + e.message.split('\n')[0]);
    }
    await page.close();
  });

  // ── the manifest, and that every icon it names is served at its size ──
  section("the manifest, and that every icon it names is served at its size", async () => {
    const page = await browser.newPage();
    const bad = [];
    try {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      const r = await page.evaluate(async () => {
        const link = document.querySelector('link[rel="manifest"]');
        if (!link) return { error: 'no <link rel="manifest">' };
        const res = await fetch(link.href);
        const type = res.headers.get('content-type') || '';
        let m; try { m = await res.json(); } catch (e) { return { error: 'manifest is not JSON' }; }
        const icons = [];
        for (const ic of m.icons || []) {
          const url = new URL(ic.src, link.href).href;
          const img = new Image(); img.src = url;
          const ok = await img.decode().then(() => true, () => false);
          icons.push({ src: ic.src, sizes: ic.sizes, ok, w: img.naturalWidth, h: img.naturalHeight });
        }
        return { status: res.status, type, name: m.name, icons };
      });
      if (r.error) bad.push(r.error);
      else {
        if (r.status !== 200) bad.push('manifest answered ' + r.status);
        if (!/manifest\+json/.test(r.type)) bad.push('manifest served as ' + r.type);
        if (!r.icons.some(i => i.sizes === '512x512')) bad.push('no 512px icon');
        for (const i of r.icons) {
          if (!i.ok) bad.push(i.src + ' does not load');
          const m = /^(\d+)x(\d+)$/.exec(i.sizes);
          if (i.ok && m && (i.w !== +m[1] || i.h !== +m[2]))
            bad.push(i.src + ' says ' + i.sizes + ' but is ' + i.w + 'x' + i.h);
        }
      }
    } catch (e) { bad.push(e.message.split('\n')[0]); }
    failures += bad.length;
    console.log(bad.length ? '  FAIL manifest and icons' : '  ok   manifest and icons');
    for (const why of bad) console.log('         ' + why);
    await page.close();
  });

  // ── the Internet card's way out to Speedtest Tracker ──────────────────
  section("the Internet card's way out to Speedtest Tracker", async () => {
    const bad = [];
    for (const [w, h, tv] of [[1920, 1080, false], [414, 896, false], [1920, 1080, true]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8',
        body: JSON.stringify((() => { const s = freshen();
          s.internet = Object.assign({}, s.internet, {url: 'http://127.0.0.1:8080'});
          return s; })()),
      }));
      await page.goto(BASE + (tv ? '/?tv' : '/'), { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      await page.$eval('#card-internet .face.front .card-body', el => el.click());
      await page.waitForTimeout(900);
      const at = w + 'x' + h + (tv ? ' TV' : '');
      const r = await page.evaluate(() => {
        const a = document.querySelector('#card-internet .face.back .linkbtn.out');
        if (!a) return {missing: true};
        const ar = a.getBoundingClientRect();
        const cr = document.getElementById('card-internet').getBoundingClientRect();
        return {href: a.href, target: a.target, rel: a.rel,
                shown: ar.width > 0 && ar.height > 0,
                inside: ar.left >= cr.left - 1 && ar.right <= cr.right + 1 &&
                        ar.top >= cr.top - 1 && ar.bottom <= cr.bottom + 1};
      });
      if (r.missing) { bad.push(at + ': no link'); await page.close(); continue; }
      if (tv) {
        if (r.shown) bad.push(at + ': the link is showing on the TV');
      } else {
        if (!r.shown) bad.push(at + ': the link is hidden');
        if (!r.inside) bad.push(at + ': the link sits outside the card');
        // 127.0.0.1 is the server talking to itself; a browser needs this host.
        const want = new URL(BASE).hostname;
        if (!r.href.includes(want + ':8080')) bad.push(at + ': link points at ' + r.href);
        if (r.target !== '_blank' || !/noopener/.test(r.rel))
          bad.push(at + ': link opens unsafely (' + r.target + ', ' + r.rel + ')');
        // Tapping it must not turn the card over.
        await page.$eval('#card-internet .face.back .linkbtn.out', a => {
          a.addEventListener('click', e => e.preventDefault(), {once: true});
          a.click();
        });
        await page.waitForTimeout(700);
        if (!await page.evaluate(() => document.querySelector('#card-internet.flipped')))
          bad.push(at + ': tapping the link turned the card back');
      }
      await page.close();
    }
    failures += bad.length;
    console.log(bad.length ? '  FAIL internet link' : '  ok   internet link');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the Internet page: a week of tests, fetched when it is opened ─────
  section("the Internet page: a week of tests, fetched when it is opened", async () => {
    const bad = [];
    const week = (() => {                     // a week of hourly tests
      const now = Date.now() / 1000, pts = [];
      for (let i = 0; i < 168; i++) {
        const ok = i % 40 !== 7;
        pts.push({at: now - (168 - i) * 3600, ok,
          down: ok ? 300 + (i % 11) * 60 : null, up: ok ? 90 + (i % 7) * 30 : null,
          ping: ok ? 9 + (i % 5) * 2 : null, loaded: ok ? 20 + (i % 9) * 18 : null,
          jitter: ok ? 1.5 : null, loss: ok ? (i === 44 ? 4.2 : 0) : null,
          server: 'EZEE Fiber',
          url: ok ? 'https://www.speedtest.net/result/c/abc' + i : null});
      }
      return {available: true, error: '', days: 7, fetched_at: now, points: pts};
    })();
    for (const [w, h] of [[1920, 1080], [1400, 860], [1920, 720], [414, 896]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      const errs = [];
      let asked = 0;
      page.on('pageerror', e => errs.push(e.message));
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8',
        body: JSON.stringify((() => { const s = freshen();
          s.internet = Object.assign({}, s.internet, {url: 'http://127.0.0.1:8080'});
          return s; })()),
      }));
      await page.route('**/api/internet*', route => {
        asked++;
        return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8',
                               body: JSON.stringify(week) });
      });
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      const at = w + 'x' + h;
      // Nothing should have been fetched before the page is opened.
      if (asked) bad.push(at + ': the week was fetched before the page was opened');
      await page.$eval('#netBtn', el => el.click());
      await page.waitForTimeout(2200);
      if (!asked) bad.push(at + ': opening the page fetched nothing');
      const r = await page.evaluate(() => {
        const host = document.getElementById('netpage');
        const body = host.querySelector('.card-body');
        const plots = [...host.querySelectorAll('.netplot svg')];
        const rows = host.querySelectorAll('.tests .row');
        const card = host.querySelector('.card').getBoundingClientRect();
        const out = host.querySelector('.linkbtn.out');
        const spill = plots.map(v => { const b = v.getBoundingClientRect();
          return Math.max(card.top - b.top, b.bottom - card.bottom,
                          card.left - b.left, b.right - card.right); });
        return {open: document.body.classList.contains('show-net'),
                top: host.getBoundingClientRect().top,
                overflow: body.scrollHeight - body.clientHeight,
                sideways: body.scrollWidth - body.clientWidth,
                plots: plots.length, rows: rows.length,
                paths: plots.map(v => v.querySelectorAll('path').length),
                worstSpill: spill.length ? Math.max(...spill) : 0,
                link: out ? out.href : null,
                caption: host.textContent.includes('173') || host.textContent.includes('168')};
      });
      if (!r.open) bad.push(at + ': the page did not open');
      if (r.top > 80) bad.push(at + ': the page starts ' + Math.round(r.top) + 'px down the screen');
      if (r.plots !== 2) bad.push(at + ': ' + r.plots + ' plots, expected 2');
      // The charts are the page: between them, at least a third of the card.
      const share = await page.evaluate(() => {
        const c = document.querySelector('#netpage .card').getBoundingClientRect();
        const p = document.querySelector('#netpage .netplots').getBoundingClientRect();
        return p.height / c.height; });
      if (w > 720 && share < 0.33) bad.push(at + ': the charts get only ' + Math.round(share * 100) + '% of the card');
      if (r.paths.some(n => n < 3)) bad.push(at + ': a plot drew almost nothing (' + r.paths + ')');
      if (r.worstSpill > 1) bad.push(at + ': a plot paints ' + r.worstSpill.toFixed(0) + 'px outside the card');
      if (r.rows < 5) bad.push(at + ': the test list has ' + r.rows + ' rows');
      if (r.overflow > 1) bad.push(at + ': overflows down by ' + r.overflow + 'px');
      if (r.sideways > 1) bad.push(at + ': overflows sideways by ' + r.sideways + 'px');
      if (!r.link || !r.link.includes(':8080')) bad.push(at + ': no link to the tracker');
      // Reopening must not fetch again — the answer is held for a few minutes.
      const before = asked;
      await page.$eval('#netpage .card-head', el => el.click());
      await page.waitForTimeout(900);
      await page.$eval('#netBtn', el => el.click());
      await page.waitForTimeout(1200);
      if (asked > before) bad.push(at + ': reopening fetched the week again');
      // And the Back button closes it, as everywhere else.
      await page.goBack();
      await page.waitForTimeout(900);
      if (await page.evaluate(() => document.body.classList.contains('show-net')))
        bad.push(at + ': Back did not close the page');
      for (const m of errs) bad.push(at + ': pageerror: ' + m);
      await page.close();
    }
    failures += bad.length;
    console.log(bad.length ? '  FAIL internet page' : '  ok   internet page');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the Records band, with today at each end of it and in the middle ──
  // On a wall of six large cards as well as the crowded thirteen: the large
  // card is where the labels came adrift, and no other test draws one.
  section("the Records band, with today at each end of it and in the middle", async () => {
    const bad = [];
    const SIX = ['temperature', 'wind', 'records', 'air', 'hardware', 'forecast'];
    const jobs = [];
    for (const [w, h] of [[1920, 1080], [1280, 1024], [414, 896]])
      for (const slots of [SIX, null])
        for (const where of ['lo', 'mid', 'hi', 'month-is-everything', 'month-a-degree-short']) jobs.push([w, h, slots, where]);
    // Side by side, six at a time: two dozen page loads in a row is a minute
    // and a half of a suite that is meant to be run before every push.
    for (let i = 0; i < jobs.length; i += 6)
      await Promise.all(jobs.slice(i, i + 6).map(async ([w, h, slots, where]) => {
          const page = await browser.newPage({ viewport: { width: w, height: h } });
          const s = freshen();
          if (slots) s.slots = slots;
          const all = s.records.all;
          s.obs.temp_c = where === 'lo' ? all.lo[0] : where === 'hi' ? all.hi[0]
                       : (all.lo[0] + all.hi[0]) / 2;
          if (where === 'month-a-degree-short') {     // what the owner's card showed
            const span = all.hi[0] - all.lo[0];
            s.records.month = Object.assign({}, s.records.month,
              {lo: [all.lo[0] + span * 0.015, all.lo[1]], hi: [all.hi[0] - span * 0.018, all.hi[1]]});
          }
          if (where === 'month-is-everything')       // a station in its first month
            s.records.month = s.records.year = Object.assign({}, all);
          await page.route('**/api/state', route => route.fulfill({
            status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(s),
          }));
          await page.goto(BASE, { waitUntil: 'networkidle' });
          await page.waitForTimeout(SETTLE_MS);
          const at = w + 'x' + h + (slots ? ' six cards' : ' thirteen') + ', today at ' + where;
          for (const [id, why] of await page.evaluate(measure, '#grid'))
            if (id === 'records') bad.push(at + ': ' + why);
          if (!await page.evaluate(() => !!document.querySelector('#card-records .nowlab')))
            bad.push(at + ': today is not marked on the band');
          await page.close();
      }));
    failures += bad.length;
    console.log(bad.length ? '  FAIL records band' : '  ok   records band');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the daily record: a failed save is said, and a record can be struck ─
  section("the daily record: warnings, settings and the way out", async () => {
    const bad = [];
    const RECORD = {
      records: STATE.records.station ? [
        {field: 'hi', label: 'Hottest', v: 42.3, date: '2024-08-27'},
        {field: 'gust', label: 'Strongest gust', v: 38.9, date: '2024-12-15'},
        {field: 'rain', label: 'Wettest day', v: 131.06, date: '2025-09-27'},
        {field: 'pmin', label: 'Lowest pressure', v: 961.7, date: '2024-11-30'}] : [],
      struck: [{date: '2025-03-11', field: 'swing', label: 'Widest swing'}],
      storage: Object.assign({}, STATE.storage, {
        ok: false, error: 'Cannot save tempest_days.json — No space left on device',
        notices: ['tempest_days.json was damaged (not valid JSON); kept as tempest_days.json.damaged-20260921-151741']}),
    };
    await abreast([[1920, 1080], [414, 896]], async ([w, h]) => {
      const at = w + 'x' + h;
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      const errs = [], posts = [];
      page.on('pageerror', e => errs.push(e.message));
      const s = freshen();
      s.storage = Object.assign({}, s.storage, {ok: false, failing_since: s.now - 7200,
        error: 'Cannot save tempest_days.json — No space left on device'});
      s.hardware.rejected_today = 3;
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(s) }));
      await page.route('**/api/record', route => {
        if (route.request().method() === 'POST') {
          posts.push({body: route.request().postDataJSON(), header: route.request().headers()['x-tempest-config']});
          return route.fulfill({ status: 200, contentType: 'application/json',
            body: JSON.stringify(Object.assign({ok: true, error: ''}, RECORD,
              {records: RECORD.records.filter(r => r.field !== 'gust'),
               struck: RECORD.struck.concat([{date: '2024-12-15', field: 'gust', label: 'Strongest gust'}])})) });
        }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(RECORD) });
      });
      try {
        await page.goto(BASE, { waitUntil: 'networkidle' });
        await page.waitForTimeout(SETTLE_MS);
        const seen = await page.evaluate(() => ({
          banner: document.getElementById('banner').classList.contains('show')
                  ? document.getElementById('banner').textContent : '',
          station: (document.querySelector('#card-hardware') || {textContent: ''}).textContent,
          sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth }));
        if (!/not being saved/.test(seen.banner) || !/No space left/.test(seen.banner))
          bad.push(at + ': a failed save is not on the banner: "' + seen.banner + '"');
        if (!/2h/.test(seen.banner)) bad.push(at + ': the banner does not say for how long');
        if (!/History is not being saved/.test(seen.station))
          bad.push(at + ': the Station card does not say history is not being saved');
        if (seen.sideways > 1) bad.push(at + ': the banner pushes the page ' + seen.sideways + 'px wide');
        for (const [id, why] of await page.evaluate(measure, '#grid'))
          if (id === 'hardware') bad.push(at + ': station card ' + why);

        await page.$eval('#setBtn', el => el.click());
        await page.waitForTimeout(800);
        await page.$eval('.settabs button[data-tab="record"]', el => el.click());
        await page.waitForTimeout(1500);
        await page.$eval('#reviewRecords summary', el => el.click());
        await page.waitForTimeout(300);
        const sec = await page.evaluate(() => {
          const el = document.getElementById('recordsec');
          const link = el && el.querySelector('a.linkbtn');
          return { text: el ? el.textContent : '', rows: el ? el.querySelectorAll('.recrow').length : 0,
                   href: link ? link.getAttribute('href') : '',
                   wide: el ? [...el.querySelectorAll('.recrow')].filter(r => r.scrollWidth > r.clientWidth + 1).length : 0 };
        });
        if (sec.rows !== 5) bad.push(at + ': ' + sec.rows + ' record rows in settings, expected 5');
        for (const want of ['1213 days kept', '34 backups', 'Not being saved', 'damaged', 'Marked as not real'])
          if (!sec.text.includes(want)) bad.push(at + ': settings does not say "' + want + '"');
        if (!/api\/days\.csv\?temp=F&wind=mph&pres=inHg&rain=in/.test(sec.href))
          bad.push(at + ': the CSV link is "' + sec.href + '"');
        if (sec.wide) bad.push(at + ': ' + sec.wide + ' record row(s) too wide for the sheet');
        // Striking the gust record: the right request, with the header, and the list redrawn.
        await page.$$eval('#recordsec .recrow', rows => rows.find(r => /Strongest gust/.test(r.textContent))
          .querySelector('button').click());
        await page.waitForTimeout(1200);
        const p0 = posts[0] || {};
        if (!p0.body || !p0.body.strike || p0.body.strike.date !== '2024-12-15' || p0.body.strike.field !== 'gust')
          bad.push(at + ': striking sent ' + JSON.stringify(p0.body));
        if (p0.header !== '1') bad.push(at + ': the write went without its header');
        const after = await page.evaluate(() => [...document.querySelectorAll('#recordsec .recrow')]
          .map(r => r.textContent));
        if (!after.some(t => /Strongest gust/.test(t) && /Put back/.test(t)))
          bad.push(at + ': the struck gust is not offered back');
      } catch (e) { bad.push(at + ': ' + e.message.split('\n')[0]); }
      for (const m of errs) bad.push(at + ': pageerror: ' + m);
      await page.close();
    });
    // And the real thing, from the real server: a CSV with a header and rows.
    try {
      const r = await fetch(BASE + '/api/days.csv?temp=C&wind=km%2Fh');
      const text = await r.text();
      const lines = text.trim().split('\r\n');
      if (!/text\/csv/.test(r.headers.get('content-type') || '')) bad.push('the CSV is served as ' + r.headers.get('content-type'));
      if (!/attachment; filename="tempest-daily-record-/.test(r.headers.get('content-disposition') || ''))
        bad.push('the CSV is not a download');
      if (!lines[0].startsWith('date,high_C,low_C,') || !lines[0].includes('peak_gust_kmh'))
        bad.push('the CSV header is "' + lines[0].slice(0, 60) + '"');
      if (lines.length < 300) bad.push('the demo year exported only ' + lines.length + ' lines');
      const cols = lines[0].split(',').length;
      if (lines.some(l => l.split(',').length !== cols)) bad.push('a CSV row has the wrong number of columns');
    } catch (e) { bad.push('CSV: ' + e.message); }
    failures += bad.length;
    console.log(bad.length ? '  FAIL daily record' : '  ok   daily record');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the settings page ─────────────────────────────────────────────────
  // Its faults were all of a kind a screenshot does not show: Back left the
  // dashboard, a key with an "s" in it closed the page, a change was lost
  // without its button, nothing was large enough for a remote to press.
  section("the settings page: tabs, autosave, live status and the way out", async () => {
    const bad = [];
    await abreast([[1920, 1080], [1024, 768], [414, 896]], async ([w, h]) => {
      const at = w + 'x' + h;
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      const errs = [], posts = [];
      page.on('pageerror', e => errs.push(e.message));
      const s = freshen();
      s.pollen = Object.assign({}, s.pollen, {error: 'Pollen key rejected (HTTP 403)'});
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(s) }));
      const CFG = {slots: ['temperature', 'wind', 'pollen'], cards: STATE.slots.slice(),
                   token_set: true, pollen_key_set: true, speedtest_token_set: false,
                   backfill: {status: 'ok', added: 12, error: ''},
                   server: [{what: 'Station name', value: '', env: 'TEMPEST_NAME'},
                            {what: 'Internet plan', value: '1000 down · 40 up Mbps', env: 'TEMPEST_PLAN_DOWN / _UP'}]};
      await page.route('**/api/config', route => {
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON();
          posts.push(body);
          if (body.slots) CFG.slots = body.slots;
          return route.fulfill({ status: 200, contentType: 'application/json',
                                 body: JSON.stringify(Object.assign({ok: true, changed: []}, CFG)) });
        }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CFG) });
      });
      const tab = async (id) => { await page.$eval('.settabs button[data-tab="' + id + '"]', el => el.click());
                                  await page.waitForTimeout(500); };
      const open = () => page.evaluate(() => document.body.classList.contains('show-settings'));
      try {
        await page.goto(BASE, { waitUntil: 'networkidle' });
        await page.waitForTimeout(SETTLE_MS);
        await page.$eval('#setBtn', el => el.click());
        await page.waitForTimeout(900);
        await tab('layout');

        // Everything pressable is big enough to press, on every tab.
        for (const id of ['layout', 'screen', 'sources', 'record']) {
          await tab(id);
          const small = await page.evaluate(() => [...document.querySelectorAll(
              '#settings button, #settings label.tick, #settings summary, #settings a.linkbtn, #settings input[type=password]')]
            .filter(e => e.offsetParent && !e.classList.contains('needs'))
            .map(e => ({t: (e.getAttribute('aria-label') || e.textContent).trim().slice(0, 24), r: e.getBoundingClientRect()}))
            .filter(x => x.r.height < 39.5 || x.r.width < 39.5).map(x => x.t + ' ' + Math.round(x.r.width) + 'x' + Math.round(x.r.height)));
          if (small.length) bad.push(at + ' ' + id + ': too small to press: ' + small.slice(0, 4).join(', '));
          const wide = await page.evaluate(() => ({
            page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            sheets: [...document.querySelectorAll('#settings .sheet')].filter(e => e.scrollWidth > e.clientWidth + 1).length }));
          if (wide.page > 1) bad.push(at + ' ' + id + ': the page scrolls sideways by ' + wide.page + 'px');
          if (wide.sheets) bad.push(at + ' ' + id + ': ' + wide.sheets + ' panel(s) wider than themselves');
        }
        await tab('record');
        const cols = await page.evaluate(() => new Set([...document.querySelectorAll('#settings .sheet')]
          .map(e => Math.round(e.getBoundingClientRect().left))).size);
        if (w >= 1900 && cols < 3) bad.push(at + ': ' + cols + ' column(s) on a screen this wide');
        if (w < 720 && cols !== 1) bad.push(at + ': ' + cols + ' columns on a phone');

        // Layout: the names the cards use, and saved without a button.
        await tab('layout');
        const names = await page.$$eval('.cardrow .nm > span', els => els.map(e => e.textContent));
        if (!names.includes('Station') || !names.includes('Air quality') || names.includes('hardware'))
          bad.push(at + ': the card list says ' + names.slice(-3).join(', '));
        if (await page.$('#settings button.primary:not(#setDone)')) bad.push(at + ': there is still a save button on the layout');
        await page.$eval('.cardrow[data-card="wind"] .up', el => el.click());
        await page.waitForTimeout(1100);
        const moved = posts[posts.length - 1] || {};
        if (JSON.stringify(moved.slots) !== JSON.stringify(['wind', 'temperature', 'pollen']))
          bad.push(at + ': moving Wind up saved ' + JSON.stringify(moved.slots));
        const shape = await page.$eval('#gridShape', el => el.textContent);
        if (!/3 cards · 2 across, 2 rows/.test(shape)) bad.push(at + ': the preview says "' + shape + '"');
        if (await page.$$eval('.gridpreview div', els => els.length) !== 3) bad.push(at + ': the preview does not have three boxes');
        // The last card cannot be switched off.
        for (const c of ['wind', 'temperature', 'pollen'])
          await page.$eval('.cardrow[data-card="' + c + '"] input', el => el.click());
        await page.waitForTimeout(1100);
        const left = await page.$$eval('.cardrow input:checked', els => els.length);
        if (left !== 1) bad.push(at + ': ' + left + ' cards left ticked after unticking all three');
        if ((posts[posts.length - 1].slots || []).length !== 1) bad.push(at + ': an empty layout was saved');
        const needs = await page.$$eval('.cardrow .needs', els => els.map(e => e.closest('.cardrow').dataset.card));
        if (needs.join() !== 'internet') bad.push(at + ': "needs a key" is on [' + needs + '], expected internet only');
        await page.$eval('.cardrow .needs', el => el.click());
        await page.waitForTimeout(500);
        if (!await page.$eval('.source[data-source="internet"]', el => el.open).catch(() => false))
          bad.push(at + ': "needs a key" did not open the Internet source');

        // Data sources: what is working, not what was once typed.
        const lines = await page.$$eval('.source', els => Object.fromEntries(els.map(e =>
          [e.dataset.source, e.querySelector('.line').textContent + ' [' + e.querySelector('.dot').className + ']'])));
        if (!/key rejected/.test(lines.pollen || '') || !/stale/.test(lines.pollen || ''))
          bad.push(at + ': a rejected pollen key reads "' + lines.pollen + '"');
        if (!/Working/.test(lines.air || '') || !/live/.test(lines.air || '')) bad.push(at + ': air quality reads "' + lines.air + '"');
        if (!/Not set up/.test(lines.internet || '')) bad.push(at + ': an unset token reads "' + lines.internet + '"');
        if (/configured/i.test(Object.values(lines).join(' '))) bad.push(at + ': it still says "configured"');
        // A key with shortcut letters in it must not close the page or change the theme.
        const theme = await page.evaluate(() => document.documentElement.dataset.theme);
        await page.focus('.source[data-source="internet"] input');
        await page.keyboard.type('stfwdna-token');
        if (!await open()) bad.push(at + ': typing a key closed the settings page');
        if (await page.evaluate(() => document.documentElement.dataset.theme) !== theme) bad.push(at + ': typing a key changed the theme');
        if (await page.$eval('.source[data-source="internet"] input', el => el.value) !== 'stfwdna-token')
          bad.push(at + ': the field did not keep what was typed');

        // This screen: a unit chosen here is the unit on the cards.
        await tab('screen');
        await page.$eval('.seg[aria-label="Temperature"] button[data-value="°C"]', el => el.click());
        await page.$eval('.seg[aria-label="Rain"] button[data-value="mm"]', el => el.click());
        const stored = await page.evaluate(() => [localStorage.getItem('unit.temp'), localStorage.getItem('unit.rain')]);
        if (stored.join() !== '°C,mm') bad.push(at + ': units stored as ' + stored);

        // Out: Done, and Back, and neither leaves the dashboard.
        await page.$eval('#setDone', el => el.click());
        await page.waitForTimeout(900);
        if (await open()) bad.push(at + ': Done did not close it');
        const hero = await page.$eval('#card-temp .hero .u', el => el.textContent).catch(() => 'nothing');
        if (hero !== '°C') bad.push(at + ': after choosing °C the Temperature card says ' + hero);
        await page.keyboard.press('s');
        await page.waitForTimeout(900);
        if (!await open()) bad.push(at + ': s did not open it');
        if (await page.$eval('.settabs button[aria-selected="true"]', el => el.dataset.tab) !== 'screen')
          bad.push(at + ': it did not come back on the tab it was left on');
        await page.goBack();
        await page.waitForTimeout(900);
        if (await open()) bad.push(at + ': Back did not close it');
        if (!/localhost|127\.0\.0\.1/.test(page.url())) bad.push(at + ': Back left the dashboard for ' + page.url());
        if (!await page.evaluate(() => document.querySelectorAll('#grid .card[id]').length)) bad.push(at + ': the cards did not come back');
      } catch (e) { bad.push(at + ': ' + e.message.split('\n')[0]); }
      for (const m of errs) bad.push(at + ': pageerror: ' + m);
      await page.close();
    });
    failures += bad.length;
    console.log(bad.length ? '  FAIL settings page' : '  ok   settings page');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the almanac page ──────────────────────────────────────────────────
  // Three records of a station's life: a full and violent year, which is the
  // case for width; a station three days old, which is the case for every
  // "none yet"; and nothing at all.
  section("the almanac page", async () => {
    const bad = [];
    const iso = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
                       String(d.getDate()).padStart(2, '0');
    const back = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };
    const year = (days) => {
      const plot = [], months = new Map();
      for (let i = days - 1; i >= 0; i--) {
        const d = new Date(); d.setDate(d.getDate() - i);
        const season = Math.cos(2 * Math.PI * ((d - new Date(d.getFullYear(), 0, 0)) / 864e5 - 201) / 365);
        const mean = 7 + 27 * season + 6 * Math.sin(i * 1.7);
        const wet = i % 3 === 0 ? 8 + (i % 41) * 2.9 : 0;
        if (i % 97 === 50) { plot.push([iso(d), null, null, 0, null]); continue; }   // a day lost
        plot.push([iso(d), +(mean - 9).toFixed(2), +(mean + 9).toFixed(2), wet, 9 + (i % 23)]);
        const key = iso(d).slice(0, 7);
        const m = months.get(key) || {month: key, days: 0, rain: 0, wet_days: 0, strikes: 0,
                                       gust: 31.7, hi: 41.7, lo: -28.9, hi_avg: 0, lo_avg: 0};
        m.days++; m.rain += wet; m.hi_avg = +(mean + 9).toFixed(2); m.lo_avg = +(mean - 9).toFixed(2);
        months.set(key, m);
      }
      return {plot, months: [...months.values()].slice(-12)};
    };
    const full = year(366);
    const HOSTILE = Object.assign({
      available: true, backfill: 'ok', date: back(0), from: '2023-05-27', days: 1213,
      today: {date: back(0), hi: 39.6, lo: -24.8, rain: 112.27, gust: 31.74,
              gust_t: Date.now() / 1000 - 3600},
      year_ago: {date: back(365), hi: 38.2, lo: -23.4, rain: 104.9, gust: 28.0},
      warmest_since: {date: null, days: 1212, record: true},
      coldest_since: {date: '2025-01-21', days: 608, record: false},
      gustiest_since: {date: '2024-12-15', days: 645, record: false},
      dry_days: 0, wet_days: 17,
      last_rain: {date: back(0), days: 0, mm: 112.27},
      first_frost: {date: back(12), days: 12, lo: -11.2, hi: 3.0},
      last_frost: {date: '2026-05-27', days: 117, lo: -0.4, hi: 12.0},
      frost_days: 11, last_warm: {date: back(0), days: 0, lo: 20, hi: 39.6},
      warm_days: 148, hot_days: 112,
      thresholds: {warm_c: 26.67, hot_c: 32.22, frost_c: 0},
      records: {all: STATE.records.station.all, year: STATE.records.station.year},
    }, full);
    const young = year(3);
    const YOUNG = {
      available: true, backfill: 'off', date: back(0), from: back(2), days: 3,
      today: {date: back(0), hi: 21.0, lo: 12.0, rain: 0},
      year_ago: null, warmest_since: null, coldest_since: null, gustiest_since: null,
      dry_days: 3, wet_days: 0, last_rain: null, first_frost: null, last_frost: null,
      frost_days: 0, last_warm: null, warm_days: 0, hot_days: 0,
      thresholds: {warm_c: 26.67, hot_c: 32.22, frost_c: 0},
      records: {all: {days: 3}, year: {days: 3}},
      plot: young.plot.map(r => [r[0], r[1], r[2], 0, null]), months: young.months,
    };
    const CASES = [['a full year', HOSTILE, [[1920, 1080], [1400, 860], [1920, 720], [1024, 768], [414, 896]]],
                   ['three days old', YOUNG, [[1920, 720], [414, 896]]],
                   ['nothing yet', {available: false, days: 0, plot: [], months: []}, [[1024, 768]]]];
    await abreast(CASES.flatMap(([name, payload, sizes]) => sizes.map(([w, h]) => [name, payload, w, h])),
                  async ([name, payload, w, h]) => {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      const errs = [];
      let asked = 0, query = '';
      page.on('pageerror', e => errs.push(e.message));
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
      }));
      await page.route('**/api/almanac*', route => {
        asked++; query = route.request().url().split('?')[1] || '';
        return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8',
                               body: JSON.stringify(payload) });
      });
      const at = name + ' at ' + w + 'x' + h;
      try {
        await page.goto(BASE, { waitUntil: 'networkidle' });
        await page.waitForTimeout(SETTLE_MS);
        if (asked) bad.push(at + ': the almanac was fetched before the page was opened');
        // In by a tap anywhere on the Records card, as on the TV.
        await page.$eval('#card-records .card-body', el => el.click());
        await page.waitForTimeout(2200);
        if (!asked) bad.push(at + ': opening the page fetched nothing');
        if (!/warm=26\.67/.test(query) || !/hot=32\.22/.test(query))
          bad.push(at + ': a Fahrenheit reader asked for "' + query + '"');
        const r = await page.evaluate(() => {
          const host = document.getElementById('almanac');
          const card = host.querySelector('.card');
          const body = host.querySelector('.card-body');
          const frame = card.getBoundingClientRect();
          const plot = host.querySelector('.yearplot');
          const svg = host.querySelector('.yearplot svg');
          const b = svg ? svg.getBoundingClientRect() : null;
          // Anything that holds text and is narrower than the text it holds.
          const clipped = [...host.querySelectorAll('.k,.v,.sub,.fc-hl,.fc-pop,.caption,.ax,.tag')]
            .filter(n => n.scrollWidth > n.clientWidth + 1 && n.clientWidth > 0)
            .map(n => n.textContent.trim().slice(0, 28));
          const kids = [...body.children].filter(k => k.getBoundingClientRect().height > 0);
          let overlap = 0;
          for (let i = 1; i < kids.length; i++)
            overlap = Math.max(overlap, kids[i - 1].getBoundingClientRect().bottom -
                                        kids[i].getBoundingClientRect().top);
          const labels = [...host.querySelectorAll('.yearplot .ax, .yearplot .tag')]
            .map(n => n.getBoundingClientRect());
          const p = plot ? plot.getBoundingClientRect() : null;
          return {open: document.body.classList.contains('show-almanac'),
                  overflow: body.scrollHeight - body.clientHeight,
                  sideways: body.scrollWidth - body.clientWidth,
                  pageSideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                  fit: parseFloat(getComputedStyle(card).getPropertyValue('--fit')) || 1,
                  share: p ? p.height / frame.height : 0,
                  paths: svg ? svg.querySelectorAll('path').length : 0,
                  bars: svg ? svg.querySelectorAll('rect').length : 0,
                  spill: b ? Math.max(frame.top - b.top, b.bottom - frame.bottom,
                                      frame.left - b.left, b.right - frame.right) : 0,
                  stray: p ? labels.filter(l => l.left < p.left - 1 || l.right > p.right + 1).length : 0,
                  top: host.getBoundingClientRect().top,
                  ghosts: ['outlook', 'netpage', 'map', 'settings'].filter(id =>
                    document.getElementById(id).getBoundingClientRect().height > 0),
                  months: host.querySelectorAll('.months .fc-day').length,
                  cells: host.querySelectorAll('.cell').length,
                  clipped, overlap, text: host.textContent};
        });
        if (!r.open) bad.push(at + ': a tap on the Records card did not open it');
        // A phone once kept an empty, screen-high outlook above every other
        // page, so this one opened a screen below where anyone was looking.
        if (r.top > 80) bad.push(at + ': the page starts ' + Math.round(r.top) + 'px down the screen');
        if (r.ghosts.length) bad.push(at + ': hidden but taking room: ' + r.ghosts.join(', '));
        if (r.sideways > 1) bad.push(at + ': overflows sideways by ' + r.sideways + 'px');
        if (r.pageSideways > 1) bad.push(at + ': the page scrolls sideways by ' + r.pageSideways + 'px');
        if (w > 720 && r.overflow > 1) bad.push(at + ': overflows down by ' + r.overflow + 'px');
        if (r.overlap > 1) bad.push(at + ': two rows overlap by ' + r.overlap.toFixed(0) + 'px');
        if (r.fit < 0.6) bad.push(at + ': type shrunk to ' + r.fit.toFixed(2));
        if (r.clipped.length) bad.push(at + ': cut off: ' + r.clipped.join(' | '));
        if (r.spill > 1) bad.push(at + ': the plot paints ' + r.spill.toFixed(0) + 'px outside the card');
        if (r.stray) bad.push(at + ': ' + r.stray + ' plot label(s) outside the plot');
        if (payload.available === false) {
          if (!/Collecting/.test(r.text)) bad.push(at + ': an empty record does not say so');
        } else {
          if (r.cells !== 14) bad.push(at + ': ' + r.cells + ' cells, expected 14');
          if (r.months !== payload.months.length) bad.push(at + ': ' + r.months + ' month columns');
          if (w > 720 && r.share < 0.25)
            bad.push(at + ': the year gets only ' + Math.round(r.share * 100) + '% of the card');
          if (r.paths < 3) bad.push(at + ': the plot drew almost nothing (' + r.paths + ' paths)');
        }
        if (payload === HOSTILE) {
          if (!r.bars) bad.push(at + ': no rain on the plot');
          for (const want of ['Wet streak', 'Records began', 'First frost', 'last year', 'normal'])
            if (!r.text.includes(want)) bad.push(at + ': does not say "' + want + '"');
        }
        if (payload === YOUNG)
          for (const want of ['Dry streak', 'None yet', 'Not yet', 'no rain on record'])
            if (!r.text.includes(want)) bad.push(at + ': does not say "' + want + '"');
        // Held for a few minutes: opening it again must not fetch again.
        const before = asked;
        await page.$eval('#almanac .card-body', el => el.click());     // a tap anywhere is the way back
        await page.waitForTimeout(900);
        if (await page.evaluate(() => document.body.classList.contains('show-almanac')))
          bad.push(at + ': a tap on the page did not close it');
        await page.keyboard.press('a');
        await page.waitForTimeout(1200);
        if (asked > before) bad.push(at + ': reopening fetched it again');
        await page.goBack();
        await page.waitForTimeout(900);
        if (await page.evaluate(() => document.body.classList.contains('show-almanac')))
          bad.push(at + ': Back did not close the page');
        if (!await page.evaluate(() => document.querySelectorAll('#grid .card[id]').length))
          bad.push(at + ': the cards did not come back');
      } catch (e) { bad.push(at + ': ' + e.message.split('\n')[0]); }
      for (const m of errs) bad.push(at + ': pageerror: ' + m);
      await page.close();
    });
    // A Celsius reader's round numbers are not a Fahrenheit reader's.
    {
      const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
      let query = '';
      await page.addInitScript(() => { try { localStorage.setItem('unit.temp', '°C'); } catch (e) {} });
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
      }));
      await page.route('**/api/almanac*', route => {
        query = route.request().url().split('?')[1] || '';
        return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8',
                               body: JSON.stringify(HOSTILE) });
      });
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      await page.$eval('#almBtn', el => el.click());
      await page.waitForTimeout(1500);
      if (!/warm=25\.00/.test(query) || !/hot=30\.00/.test(query))
        bad.push('in Celsius it asked for "' + query + '"');
      const text = await page.evaluate(() => document.getElementById('almanac').textContent);
      if (!text.includes('30° days')) bad.push('in Celsius the hot days are not named for 30°');
      // After a deploy the page reloads itself where it stood. Restored, it is
      // drawn before the first state arrives, and has to catch up when one does.
      await page.reload({ waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      const again = await page.evaluate(() => ({
        open: document.body.classList.contains('show-almanac'),
        text: document.getElementById('almanac').textContent }));
      if (!again.open) bad.push('a reload did not put the almanac back');
      else if (!again.text.includes('normal')) bad.push('restored by a reload, it never got its normals');
      await page.close();
    }
    failures += bad.length;
    console.log(bad.length ? '  FAIL almanac page' : '  ok   almanac page');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the lightning card's storm, in each of its states ─────────────────
  section("the lightning card's storm, in each of its states", async () => {
    const bad = [];
    const SPEC = {
      overhead: {words: 'Overhead', sheet: true, kite: false},
      nearby:   {words: 'Storm nearby', sheet: false, kite: false},
      quiet:    {words: 'No activity', sheet: false, kite: false},
      kite:     {words: 'Nothing since', sheet: false, kite: true},
    };
    for (const kind of Object.keys(SPEC)) {
      for (const [w, h] of [[1920, 1080], [1920, 720], [414, 896]]) {
        const page = await browser.newPage({ viewport: { width: w, height: h } });
        const errs = [];
        page.on('pageerror', e => errs.push(e.message));
        await page.route('**/api/state', route => route.fulfill({
          status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
        }));
        await page.goto(BASE + '/?teststorm=' + kind, { waitUntil: 'networkidle' });
        await page.waitForTimeout(SETTLE_MS);
        const at = kind + ' at ' + w + 'x' + h;
        for (const [id, why] of await page.evaluate(measure, '#grid'))
          if (id === 'bolt') bad.push(at + ': ' + why);
        const r = await page.evaluate(() => {
          const card = document.getElementById('card-bolt');
          const sky = card.querySelector('.stormsky');
          const band = sky && sky.querySelector('.band').getBoundingClientRect();
          const cr = card.getBoundingClientRect();
          const cells = [...card.querySelectorAll('.card-body .row')].map(x => x.getBoundingClientRect());
          return {
            storm: sky ? sky.dataset.storm : null,
            rings: sky ? sky.querySelectorAll('circle').length : 0,
            bolt: !!(sky && sky.querySelector('.bolt')),
            sheet: !!(sky && sky.querySelector('.sheet')),
            // the kite's own red diamond, drawn nowhere else
            kite: !!(sky && /d9483b/.test(sky.innerHTML)),
            caption: [...card.querySelectorAll('.caption')].map(c => c.textContent).join(' | '),
            // A backdrop may sit behind the figures, as the wind card's tree
            // does, but never behind the headline number.
            overHero: (() => { const h = card.querySelector('.hero');
              return h && band ? h.getBoundingClientRect().bottom - band.top : 0; })(),
            inside: band ? (band.top >= cr.top - 1 && band.bottom <= cr.bottom + 1) : false,
          };
        });
        const sp = SPEC[kind];
        if (!r.storm) bad.push(at + ': no storm picture');
        if (r.rings < 5) bad.push(at + ': ' + r.rings + ' rings');
        if (!r.inside) bad.push(at + ': the picture escapes the card');
        if (r.overHero > 2) bad.push(at + ': the picture runs ' + Math.round(r.overHero) + 'px into the headline');
        if (sp.sheet !== r.sheet) bad.push(at + ': the flash is ' + (r.sheet ? 'on' : 'off'));
        if (sp.kite !== r.kite) bad.push(at + ': the kite is ' + (r.kite ? 'there' : 'missing'));
        if ((kind === 'overhead' || kind === 'nearby') && !r.bolt) bad.push(at + ': no bolt');
        if (!r.caption.includes(sp.words)) bad.push(at + ': caption says "' + r.caption + '"');
        if (kind === 'overhead' && /—\s*mi/.test(r.caption)) bad.push(at + ': overhead without a distance');
        for (const m of errs) bad.push(at + ': pageerror: ' + m);
        await page.close();
      }
    }
    // A wall display asked for less motion must not be lit up by lightning.
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, reducedMotion: 'reduce' });
    await page.route('**/api/state', route => route.fulfill({
      status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
    }));
    await page.goto(BASE + '/?teststorm=overhead', { waitUntil: 'networkidle' });
    await page.waitForTimeout(SETTLE_MS);
    const still = await page.evaluate(() => [...document.querySelectorAll('#card-bolt .bolt, #card-bolt .sheet')]
      .map(e => getComputedStyle(e).animationName));
    if (still.some(n => n !== 'none')) bad.push('reduced motion: the storm still flashes');
    await page.close();
    failures += bad.length;
    console.log(bad.length ? '  FAIL lightning storm' : '  ok   lightning storm');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the slow half: series and Internet history come every 30 s, not 2 ─
  section("the slow half: series and Internet history come every 30 s, not 2", async () => {
    const bad = [];
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    let states = 0, slows = 0;
    await page.route('**/api/state', route => {
      states++;
      const s = freshen();
      delete s.series;                      // as the live server now sends it
      delete s.internet.history;
      delete s.nws;
      return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8',
                             body: JSON.stringify(s) });
    });
    await page.route('**/api/series', route => {
      slows++;
      const s = freshen();
      return route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8',
        body: JSON.stringify({ series: s.series, internet_history: s.internet.history, nws: s.nws }) });
    });
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.waitForTimeout(SETTLE_MS + 4000);
    const r = await page.evaluate(() => ({
      trace: !!document.querySelector('#card-temp svg path'),
      presTrace: !!document.querySelector('#card-pres svg path'),
      backHistory: (() => { const c = [...document.querySelectorAll('#card-internet .face.back .cell')]
        .find(x => /Failed/i.test(x.textContent)); return c ? c.textContent : ''; })(),
    }));
    if (!r.trace) bad.push('the temperature trace did not draw from the slow half');
    if (!r.presTrace) bad.push('the pressure trace did not draw from the slow half');
    if (!/of 25/.test(r.backHistory)) bad.push('the Internet back lacks its history: "' + r.backHistory + '"');
    await page.evaluate(() => toggleOutlook(true));
    await page.waitForTimeout(1500);
    const words = await page.evaluate(() => document.querySelectorAll('#outlook .nwswords .period').length);
    if (words !== 2) bad.push('the outlook lacks the NWS words from the slow half (' + words + ')');
    await page.evaluate(() => toggleOutlook(false));
    if (slows !== 1) bad.push('the slow half was fetched ' + slows + ' times in 7 s, expected once');
    if (states < 3) bad.push('the snapshot was fetched only ' + states + ' times in 7 s');
    await page.close();
    failures += bad.length;
    console.log(bad.length ? '  FAIL slow half' : '  ok   slow half');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the wind tree in a gale, which must stay inside its card ──────────
  section("the wind tree in a gale, which must stay inside its card", async () => {
    const bad = [];
    for (const [w, h] of [[1920, 720], [1920, 1080], [1024, 768], [414, 896], [2560, 1080]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8',
        body: JSON.stringify((() => { const s = freshen();
          s.obs = Object.assign({}, s.obs, {wind_avg_ms: 24, wind_gust_ms: 33});
          return s; })()),
      }));
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      const at = w + 'x' + h;
      for (const [id, why] of await page.evaluate(measure, '#grid'))
        if (id === 'wind') bad.push(at + ' in a gale: ' + why);
      const lean = await page.evaluate(() => {
        const t = document.querySelector('#card-wind .tree');
        return t ? parseFloat(t.style.getPropertyValue('--lean')) : null;
      });
      if (!(lean > 9)) bad.push(at + ': a 24 m/s wind leans the tree ' + lean + '°');
      await page.close();
    }
    failures += bad.length;
    console.log(bad.length ? '  FAIL wind tree' : '  ok   wind tree');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the pollen card's in-season list, which used to be cut off ────────
  section("the pollen card's in-season list, which used to be cut off", async () => {
    const bad = [];
    for (const [w, h] of [[1920, 720], [1920, 1080], [1024, 768], [414, 896], [2560, 1080]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
      }));
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      const r = await page.evaluate(() => {
        const line = [...document.querySelectorAll('#card-pollen .caption')]
          .find(c => /In season/i.test(c.textContent));
        if (!line) return {missing: true};
        return {text: line.textContent, cutSide: line.scrollWidth - line.clientWidth,
                cutBelow: line.scrollHeight - line.clientHeight};
      });
      const at = w + 'x' + h;
      if (r.missing) bad.push(at + ': no in-season line');
      else {
        // The captured state has eight plants; the last must still be there.
        if (!/Juniper/.test(r.text)) bad.push(at + ': the list stops early — "' + r.text + '"');
        if (r.cutSide > 1) bad.push(at + ': cut off sideways by ' + r.cutSide + 'px');
        if (r.cutBelow > 1) bad.push(at + ': cut off below by ' + r.cutBelow + 'px');
      }
      await page.close();
    }
    failures += bad.length;
    console.log(bad.length ? '  FAIL pollen in season' : '  ok   pollen in season');
    for (const why of bad) console.log('         ' + why);
  });

  // ── /testall: every step shows what it says, and nothing is saved ──────
  section("/testall: every step shows what it says, and nothing is saved", async () => {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const bad = [], errs = [];
    let posted = 0;
    page.on('pageerror', e => errs.push(e.message));
    await page.route('**/api/state', route => route.fulfill({
      status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
    }));
    await page.route('**/api/config', route => {
      if (route.request().method() === 'POST') posted++;
      return route.continue();
    });
    try {
      // The trailing-slash form must land on /testall, or its fetches break.
      await page.goto(BASE + '/testall/?tv', { waitUntil: 'networkidle' });
      if (!/\/testall\?/.test(page.url())) bad.push('/testall/ did not redirect to /testall: ' + page.url());
      await page.waitForTimeout(SETTLE_MS);
      // The tag must not sit over any card.
      const covers = await page.evaluate(() => {
        const t = document.getElementById('testtag').getBoundingClientRect();
        return [...document.querySelectorAll('#grid .card[id]')].filter(c => {
          const r = c.getBoundingClientRect();
          return t.left < r.right && r.left < t.right && t.top < r.bottom && r.top < t.bottom;
        }).map(c => c.id);
      });
      if (covers.length) bad.push('the TEST tag covers ' + covers.join(', '));
      const total = await page.evaluate(() => TESTALL_STEPS.length);
      // No timer: left alone, it stays on step 1.
      await page.waitForTimeout(2500);
      if (await page.evaluate(() => testStep) !== 0) bad.push('it moved on by itself');
      const leans = [];
      for (let i = 0; i < total; i++) {
        if (i) { await page.$eval('#testtag', el => el.click()); await page.waitForTimeout(1300); }
        const r = await page.evaluate(() => {
          const st = TESTALL_STEPS[testStep];
          const sky = document.querySelector('#card-rain .rainsky');
          return {
            label: st.label, tag: document.getElementById('testtag').textContent,
            want: { rain: st.rain || null, alerts: st.alerts ? Math.min(3, st.alerts.length) : 0,
                    pollen: st.pollen, wind: st.wind, flip: /Internet/.test(st.label),
                    outlook: /outlook/.test(st.label), almanac: /Almanac/.test(st.label) },
            rain: sky ? sky.dataset.tier : null,
            hail: !!document.querySelector('#card-rain .drop.hail'),
            alerts: document.querySelectorAll('#alerts .alert').length,
            pollenCalm: document.querySelector('#card-pollen .pollenface.calm') ? true
                      : document.querySelector('#card-pollen .pollenface') ? false : null,
            lean: (document.querySelector('#card-wind .tree') || {style: {getPropertyValue: () => ''}})
                    .style.getPropertyValue('--lean'),
            flipped: !!document.querySelector('#card-internet.flipped'),
            outlook: document.body.classList.contains('show-outlook'),
            almanac: document.body.classList.contains('show-almanac'),
            cards: [...document.querySelectorAll('#grid .card[id]')].map(c => c.draggable),
          };
        });
        const at = 'step ' + (i + 1) + ' (' + r.label + ')';
        if (!r.tag.includes(r.label)) bad.push(at + ': tag says "' + r.tag + '"');
        const w = r.want;
        if (w.rain === 'hail' ? !r.hail : w.rain && r.rain !== w.rain) bad.push(at + ': rain is ' + r.rain);
        if (!w.rain && r.rain) bad.push(at + ': rain showing outside a rain step');
        if (r.alerts !== w.alerts) bad.push(at + ': ' + r.alerts + ' alerts, expected ' + w.alerts);
        if (w.pollen != null && r.pollenCalm !== (w.pollen === 0)) bad.push(at + ': pollen face is wrong');
        if (w.wind != null) { if (!r.lean) bad.push(at + ': no tree'); else leans.push(parseFloat(r.lean)); }
        if (w.flip !== r.flipped) bad.push(at + ': Internet card ' + (r.flipped ? 'turned' : 'not turned'));
        if (w.outlook !== r.outlook) bad.push(at + ': outlook ' + (r.outlook ? 'open' : 'closed'));
        if (w.almanac !== r.almanac) bad.push(at + ': almanac ' + (r.almanac ? 'open' : 'closed'));
        if (r.cards.some(Boolean)) bad.push(at + ': cards are draggable on the test page');
      }
      // One more click wraps to the start; the left arrow goes back to the end.
      await page.$eval('#testtag', el => el.click()); await page.waitForTimeout(1300);
      if (await page.evaluate(() => testStep) !== 0) bad.push('the click after the last step did not wrap to 1');
      await page.keyboard.press('ArrowLeft'); await page.waitForTimeout(800);
      if (await page.evaluate(() => testStep) !== total - 1) bad.push('the left arrow did not go back');
      // The wind steps rise, so the tree should lean further at each.
      if (leans.length !== 4 || !leans.every((v, i) => !i || v > leans[i - 1]))
        bad.push('the tree does not lean further in stronger wind: ' + leans.join(', '));
    } catch (e) { bad.push(e.message.split('\n')[0]); }
    if (posted) bad.push('the test page saved settings ' + posted + ' time(s)');
    for (const m of errs) bad.push('pageerror: ' + m);
    failures += bad.length;
    console.log(bad.length ? '  FAIL /testall' : '  ok   /testall');
    for (const why of bad) console.log('         ' + why);
    await page.close();
  });

  // ── rain and snow behind the Rainfall card ────────────────────────────
  section("rain and snow behind the Rainfall card", async () => {
    const bad = [];
    // What each preview must draw. img: a moving picture (an <img>, so it can
    // leave the card and be clipped); svg: a fixed one inside the card.
    const SPEC = {
      light: {}, moderate: {}, hail: {hail: true},
      heavy: {img: 'duck', words: 'Duck weather'},
      violent: {img: 'ark', words: 'Consider building an ark'},
      snow: {flakes: true, words: 'Snowing', caveat: true},
      heavysnow: {flakes: true, svg: 'snowman', words: 'Snowman weather', caveat: true},
      blizzard: {flakes: true, img: 'yeti', words: 'Stay inside', caveat: true},
      freezing: {icy: true, svg: 'icicles', words: 'Freezing rain'},
      sleet: {hail: true, words: 'Sleet'},
    };
    const cases = [
      ['heavy', 414, 896], ['heavy', 1920, 720], ['heavy', 1024, 768],
      ['violent', 414, 896], ['violent', 1920, 720], ['violent', 1024, 768],
      ['heavysnow', 414, 896], ['heavysnow', 1920, 720], ['heavysnow', 1024, 768],
      ['blizzard', 414, 896], ['blizzard', 1920, 720], ['blizzard', 1024, 768],
      ['light', 1920, 1080], ['hail', 1920, 1080], ['snow', 1920, 720],
      ['freezing', 1920, 720], ['sleet', 1920, 1080], [null, 1920, 1080],
    ];
    const look = async (page) => page.evaluate(() => {
      const card = document.getElementById('card-rain');
      const sky = card.querySelector('.rainsky');
      const cr = card.getBoundingClientRect();
      const img = sky && sky.querySelector('img');
      const ir = img && img.getBoundingClientRect();
      return {
        tier: sky ? sky.dataset.tier : null,
        drops: sky ? sky.querySelectorAll('.drop').length : 0,
        flakes: sky ? sky.querySelectorAll('.flake').length : 0,
        hail: sky ? sky.querySelectorAll('.drop.hail').length : 0,
        icy: sky ? sky.querySelectorAll('.drop.icy').length : 0,
        img: img ? [...img.classList].find(c => c !== 'floater') : null,
        loaded: img ? img.complete && img.naturalWidth > 0 : null,
        above: ir ? cr.top - ir.top : 0, below: ir ? ir.bottom - cr.bottom : 0,
        svgs: sky ? [...sky.querySelectorAll('svg')].map(v => v.getAttribute('class')) : [],
        caption: [...card.querySelectorAll('.caption, .sub')].map(c => c.textContent).join(' | '),
      };
    });
    const open = async (w, h, url, state) => {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      const errs = [];
      page.on('pageerror', e => errs.push(e.message));
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8',
        body: JSON.stringify(state || freshen()),
      }));
      await page.goto(BASE + url, { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      return { page, errs };
    };

    await abreast(cases, async ([tier, w, h]) => {
      const { page, errs } = await open(w, h, tier ? '/?testrain=' + tier : '/');
      const where = (tier || 'dry') + ' at ' + w + 'x' + h;
      for (const [id, why] of await page.evaluate(measure, '#grid'))
        if (id === 'rain') bad.push(where + ': ' + why);
      const r = await look(page);
      if (!tier) { if (r.tier) bad.push(where + ': something is falling on a dry day'); }
      else {
        const sp = SPEC[tier];
        if (tier !== 'hail' && r.tier !== tier) bad.push(where + ': drew ' + r.tier);
        if (sp.flakes ? !r.flakes : !r.drops) bad.push(where + ': nothing falling');
        if (sp.flakes && r.drops) bad.push(where + ': rain drops in snow');
        if (sp.hail && !r.hail) bad.push(where + ': no pellets');
        if (sp.icy && !r.icy) bad.push(where + ': drops are not icy');
        if (sp.img) {
          if (r.img !== sp.img) bad.push(where + ': expected the ' + sp.img + ', found ' + r.img);
          else if (!r.loaded) bad.push(where + ': the ' + sp.img + ' image did not load');
          if (r.above > 1 || r.below > 1) bad.push(where + ': the ' + sp.img + ' sits outside the card');
        } else if (r.img) bad.push(where + ': a ' + r.img + ' where none belongs');
        if (sp.svg && !r.svgs.includes(sp.svg)) bad.push(where + ': no ' + sp.svg);
        if (sp.words && !r.caption.includes(sp.words)) bad.push(where + ': caption says "' + r.caption + '"');
        if (!!sp.caveat !== r.caption.includes('under-counts snow'))
          bad.push(where + ': the snow caveat is ' + (sp.caveat ? 'missing' : 'showing'));
      }
      for (const m of errs) bad.push(where + ': pageerror: ' + m);
      await page.close();
    });

    // The rules, on real data rather than previews. The nearest NWS station
    // says what is falling; it is only believed when it is cold here.
    const winter = (tempC, obs, extra) => {
      const s = freshen();
      s.obs = Object.assign({}, s.obs, { temp_c: tempC, wind_gust_ms: 3 });
      s.precip_obs = Object.assign({ available: true, error: '', blowing: false,
        station: 'KDPA', station_name: 'Dupage Airport', observed_at: s.now - 600 }, obs);
      return Object.assign(s, extra || {});
    };
    const stale = { observed_at: freshen().now - 4 * 3600 };
    const rules = [
      ['light snow reported, but 50°F here', winter(10, { kind: 'snow', intensity: 'light' }), null, null],
      ['light snow reported, 28°F here', winter(-2, { kind: 'snow', intensity: 'light' }), 'snow', 'Dupage Airport'],
      ['heavy snow reported, 25°F', winter(-4, { kind: 'snow', intensity: 'heavy' }), 'heavysnow', 'Snowman weather'],
      ['snow under a Blizzard Warning', winter(-6, { kind: 'snow', intensity: 'light' },
        { alerts: { checked: true, error: '', alerts: [{ event: 'Blizzard Warning', severity: 'Severe', rank: 3 }] } }),
        'blizzard', 'Stay inside'],
      ['rain reported at 33°F', winter(0.5, { kind: 'rain', intensity: 'light' }), null, null],
      ['freezing rain reported, 30°F', winter(-1, { kind: 'freezing_rain', intensity: 'light' }), 'freezing', 'Dupage Airport'],
      ['station quiet, forecast says heavy snow', winter(-3, Object.assign({ kind: 'none' }, stale),
        { forecast: Object.assign(freshen().forecast, { current: Object.assign({}, freshen().forecast.current, { code: 75 }) }) }),
        'heavysnow', 'Snowman weather'],
      ['station reports nothing, forecast says snow', winter(-3, { kind: 'none' },
        { forecast: Object.assign(freshen().forecast, { current: Object.assign({}, freshen().forecast.current, { code: 73 }) }) }),
        null, null],
    ];
    await abreast(rules, async ([what, state, tier, words]) => {
      const { page, errs } = await open(1920, 1080, '/', state);
      const r = await look(page);
      if (r.tier !== tier) bad.push(what + ': drew ' + r.tier + ', expected ' + tier);
      if (words && !r.caption.includes(words)) bad.push(what + ': caption says "' + r.caption + '"');
      for (const m of errs) bad.push(what + ': pageerror: ' + m);
      await page.close();
    });

    // Reduced motion: no falling drops, and the scene holds still.
    await abreast(['violent', 'heavysnow', 'blizzard', 'freezing'], async (tier) => {
      const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, reducedMotion: 'reduce' });
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(freshen()),
      }));
      await page.goto(BASE + '/?testrain=' + tier, { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      const still = await page.evaluate(() => ({
        falling: [...document.querySelectorAll('#card-rain .drop, #card-rain .flake')]
          .some(d => d.getBoundingClientRect().height > 0),
        moving: [...document.querySelectorAll('#card-rain .rainscene *, #card-rain .snowscene *, #card-rain .snowman g, #card-rain .icicles *')]
          .some(e => getComputedStyle(e).animationName !== 'none'),
        // Held still, the snowman must be the finished one, not a heap.
        dressed: (() => { const g = document.querySelector('#card-rain .snowman .sm-dress');
                          return !g || getComputedStyle(g).opacity === '1'; })(),
      }));
      if (still.falling) bad.push('reduced motion (' + tier + '): still falling');
      if (still.moving) bad.push('reduced motion (' + tier + '): the scene still moves');
      if (!still.dressed) bad.push('reduced motion (' + tier + '): the snowman is undressed');
      await page.close();
    });

    failures += bad.length;
    console.log(bad.length ? '  FAIL rain and snow' : '  ok   rain and snow');
    for (const why of bad) console.log('         ' + why);
  });

  // ── the alert banner: centred, and the end time never cut off ─────────
  section("the alert banner: centred, and the end time never cut off", async () => {
    const soon = (h) => new Date(Date.now() + h * 36e5).toISOString();
    const alerts = { checked: true, error: '', alerts: [
      { event: 'Tornado Warning', severity: 'Extreme', rank: 4, ends: soon(1),
        expires: soon(1), sender: 'NWS Chicago IL', headline: 'x' },
      { event: 'Severe Thunderstorm Warning', severity: 'Severe', rank: 3,
        ends: soon(20), expires: soon(3), sender: 'NWS Chicago IL', headline: 'x' },
      { event: 'Flood Watch', severity: 'Moderate', rank: 2, ends: null,
        expires: soon(50), sender: 'NWS Quad Cities IA IL', headline: 'x' },
    ]};
    const bad = [];
    for (const [w, h, tv] of [[414, 896, false], [1024, 768, false], [1920, 1080, true]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      await page.route('**/api/state', route => route.fulfill({
        status: 200, contentType: 'application/json; charset=utf-8',
        body: JSON.stringify(Object.assign(freshen(), { alerts })),
      }));
      await page.goto(BASE + (tv ? '/?tv' : '/'), { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);
      const found = await page.evaluate(() => [...document.querySelectorAll('#alerts .alert')].map(a => {
        const r = a.getBoundingClientRect(), cs = getComputedStyle(a);
        const inner = [r.left + parseFloat(cs.paddingLeft), r.right - parseFloat(cs.paddingRight)];
        const kids = [...a.children].map(k => k.getBoundingClientRect());
        const lines = {};
        for (const k of kids) (lines[Math.round(k.top)] ||= []).push(k);
        const offCentre = Math.max(...Object.values(lines).map(row => {
          const mid = (Math.min(...row.map(k => k.left)) + Math.max(...row.map(k => k.right))) / 2;
          return Math.abs(mid - (inner[0] + inner[1]) / 2);
        }));
        const cut = [...a.children].filter(k => k.scrollWidth > k.clientWidth + 1)
                                   .map(k => k.className);
        const hl = a.querySelector('.hl');
        return { ev: a.querySelector('.ev').textContent, hl: hl ? hl.textContent : '',
                 src: (a.querySelector('.src') || {}).textContent || '',
                 overflow: a.scrollWidth - a.clientWidth, offCentre, cut };
      }));
      const where = w + 'px' + (tv ? ' TV' : '');
      if (w <= 720) {
        // On a phone the banner rides just above the footer on every card:
        // iOS draws its address bar over the top of the page, so the top is
        // where it was being missed.
        for (const i of [0, 2]) {
          await page.evaluate(i => {
            const c = document.querySelectorAll('#grid .card')[i];
            window.scrollTo(0, c.offsetTop);
          }, i);
          await page.waitForTimeout(600);
          const g = await page.evaluate(i => {
            const a = document.getElementById('alerts').getBoundingClientRect();
            const f = document.querySelector('footer').getBoundingClientRect();
            const c = document.querySelectorAll('#grid .card')[i].getBoundingClientRect();
            return { aTop: a.top, aBottom: a.bottom, fTop: f.top, cBottom: c.bottom, vh: innerHeight };
          }, i);
          const at = where + ' on card ' + (i + 1);
          if (Math.abs(g.aBottom - g.fTop) > 1) bad.push(at + ': banner is not sitting on the footer');
          if (g.aTop < 0 || g.aBottom > g.vh + 1) bad.push(at + ': banner is not fully on screen');
          if (g.cBottom > g.aTop + 1) bad.push(at + ': the card runs ' + Math.round(g.cBottom - g.aTop) + 'px under the banner');
        }
        await page.evaluate(() => window.scrollTo(0, 0));
      }
      if (found.length !== 3) bad.push(where + ': expected 3 alerts, found ' + found.length);
      for (const f of found) {
        if (f.overflow > 1) bad.push(where + ': "' + f.ev + '" overflows by ' + f.overflow + 'px');
        if (f.cut.includes('ev') || f.cut.includes('hl'))
          bad.push(where + ': "' + f.ev + '" has its ' + f.cut.join('+') + ' cut off');
        if (!/^until /.test(f.hl)) bad.push(where + ': "' + f.ev + '" says "' + f.hl + '", not an end time');
        if (!/^NWS /.test(f.src)) bad.push(where + ': "' + f.ev + '" does not name the office');
        if (f.offCentre > 3) bad.push(where + ': "' + f.ev + '" sits ' + f.offCentre.toFixed(0) + 'px off centre');
      }
      await page.close();
    }
    failures += bad.length;
    console.log(bad.length ? '  FAIL alert banner' : '  ok   alert banner');
    for (const why of bad) console.log('         ' + why);
  });

  // ── ways back from the outlook, once, in TV mode ──────────────────────
  section("ways back from the outlook, once, in TV mode", async () => {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const errs = [];
    page.on('pageerror', e => errs.push(e.message));
    await page.route('**/api/state', route => route.fulfill({
      status: 200, contentType: 'application/json; charset=utf-8',
      body: JSON.stringify(freshen()),
    }));
    const bad = [];
    const isOpen = () => page.evaluate(() => document.body.classList.contains('show-outlook'));
    const open = async () => {
      await page.$eval('#card-fc .card-head', el => el.click());
      await page.waitForTimeout(800);
      return isOpen();
    };
    const flipped = () => page.evaluate(() =>
      document.getElementById('card-internet').classList.contains('flipped'));
    const onDashboard = () => page.evaluate(
      (base) => location.href.startsWith(base) && !!document.getElementById('grid'), BASE);
    try {
      await page.goto(BASE + '/?tv', { waitUntil: 'networkidle' });
      await page.waitForTimeout(SETTLE_MS);

      const shown = () => page.evaluate(() => [...document.querySelectorAll('#grid .flipbtn')]
        .some(b => b.getBoundingClientRect().width > 0));
      if (!await shown()) bad.push('flip icons are hidden on the TV — the owner wants them');

      if (!await open()) bad.push('the Forecast header did not open it');
      await page.goBack();
      await page.waitForTimeout(800);
      if (await isOpen()) bad.push('the Back button did not close it');
      if (!await onDashboard()) bad.push('Back left the dashboard');

      await open();
      await page.$eval('#outlook .card-body', el => el.click());
      await page.waitForTimeout(800);
      if (await isOpen()) bad.push('a tap on the body did not close it in TV mode');

      await open();
      await page.evaluate(() => {           // header and body in the same instant
        document.querySelector('#outlook .card-head').click();
        document.querySelector('#outlook .card-body').click();
      });
      await page.waitForTimeout(1200);
      if (await isOpen()) bad.push('a double exit left it open');
      if (!await onDashboard()) bad.push('a double exit went back two steps and left the dashboard');

      await open();
      await page.evaluate(() => { outlookSince = 0; });   // as if idle for hours
      await page.waitForTimeout(SETTLE_MS);
      if (await isOpen()) bad.push('the idle return did not bring the cards back');

      // The way in, the same way round: the whole Forecast card on the TV.
      await page.$eval('#card-fc .card-body', el => el.click());
      await page.waitForTimeout(800);
      if (!await isOpen()) bad.push('a tap on the Forecast card body did not open it in TV mode');
      await page.goBack();
      await page.waitForTimeout(800);

      // Off the TV, both whole-card taps work the same way, and the flip
      // icons come back — as hints, and for the keyboard.
      await page.evaluate(() => setTv(false));
      await page.waitForTimeout(SETTLE_MS);
      if (!await shown()) bad.push('flip icons are missing off the TV');
      await page.focus('#card-fc .flipbtn');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(800);
      if (!await isOpen()) bad.push('Enter on the Forecast flip button did not open the outlook');
      await page.goBack(); await page.waitForTimeout(800);
      if (await isOpen()) bad.push('Back after a keyboard open did not close it');
      await page.$eval('#card-fc .card-body', el => el.click());
      await page.waitForTimeout(800);
      if (!await isOpen()) bad.push('a tap on the Forecast card body did not open it outside TV mode');
      await page.$eval('#outlook .card-body', el => el.click());
      await page.waitForTimeout(800);
      if (await isOpen()) bad.push('a tap on the body did not close it outside TV mode');
      if (!await onDashboard()) bad.push('closing outside TV mode left the dashboard');
      // A real press: a quick one opens the outlook, a held one does not,
      // and dragging the card moves it without opening anything.
      const centre = (sel) => page.evaluate(s => {
        const r = document.querySelector(s).getBoundingClientRect();
        return [r.left + r.width / 2, r.top + r.height / 2]; }, sel);
      const press = async (sel, ms) => {
        const [x, y] = await centre(sel);
        await page.mouse.move(x, y); await page.mouse.down();
        await page.waitForTimeout(ms); await page.mouse.up();
        await page.waitForTimeout(800);
      };
      await press('#card-fc .card-body', 60);
      if (!await isOpen()) bad.push('a quick click on the Forecast card did not open it');
      await page.goBack(); await page.waitForTimeout(800);
      await press('#card-fc .card-body', 800);
      if (await isOpen()) bad.push('a press held on the Forecast card opened it — a hold is a grab, not a tap');
      await press('#card-internet .face.front .card-body', 800);
      if (await flipped()) bad.push('a press held on the Internet card turned it over');
      await page.evaluate(() => { window.__dragged = false;
        document.addEventListener('dragend', () => { window.__dragged = true; }, { once: true }); });
      const [x0, y0] = await centre('#card-fc'), [x1, y1] = await centre('#card-temp');
      await page.mouse.move(x0, y0); await page.mouse.down();
      await page.mouse.move(x0 + 10, y0 + 10, { steps: 3 });
      await page.mouse.move(x1, y1, { steps: 15 }); await page.mouse.up();
      await page.waitForTimeout(800);
      if (!await page.evaluate(() => window.__dragged)) bad.push('the Forecast card could not be dragged');
      if (await isOpen()) bad.push('dragging the Forecast card opened the outlook');

      // The Internet card turns over from a tap anywhere on either face.
      await page.$eval('#card-internet .face.front .card-body', el => el.click());
      await page.waitForTimeout(600);
      if (!await flipped()) bad.push('a tap on the Internet card front did not turn it over');
      await page.$eval('#card-internet .face.back .card-body', el => el.click());
      await page.waitForTimeout(600);
      if (await flipped()) bad.push('a tap on the Internet card back did not turn it back');
      await page.$eval('#card-internet .face.front .card-head', el => el.click());
      await page.waitForTimeout(600);
      if (!await flipped()) bad.push('the Internet header turned it twice, or not at all');
    } catch (e) {
      bad.push(e.message.split('\n')[0]);
    }
    for (const m of errs) bad.push('pageerror: ' + m);
    failures += bad.length;
    console.log(bad.length ? '  FAIL ways back from the outlook' : '  ok   ways back from the outlook');
    for (const why of bad) console.log('         ' + why);
    await page.close();
  }, true);

  const runOne = (s) => clock.run({start: Date.now()}, async () => {
    running.add(s.name);
    try { await s.fn(); }
    catch (e) { failures++; console.log('  FAIL ' + s.name + ': ' + e.message.split('\n')[0]); }
    running.delete(s.name);
  });
  // Longest first, or the whole run waits on whichever long section happened
  // to be queued last. These are the ones the clock says are long.
  const LONG = /rain and snow|almanac|lightning|testall|Internet page/;
  // TEST_ONLY=almanac runs just the sections whose name contains that, for
  // checking one fix without waiting for everything else.
  const ONLY = (process.env.TEST_ONLY || '').toLowerCase();
  const wanted = sections.filter(s => !ONLY || s.name.toLowerCase().includes(ONLY));
  if (ONLY) say('  only: ' + (wanted.map(s => s.name).join('; ') || 'nothing matches "' + ONLY + '"'));
  const queue = wanted.filter(s => !s.solo)
    .sort((a, b) => LONG.test(b.name) - LONG.test(a.name));
  await Promise.all(Array.from({length: LANES}, async () => {
    while (queue.length) await runOne(queue.shift());
  }));
  for (const s of wanted.filter(s => s.solo)) await runOne(s);

  await browser.close();
  console.log(failures
    ? '\n  ' + failures + ' problem(s) across ' + checked + ' renders'
    : '\n  ' + checked + ' renders, all clean');
  say('  ' + ((Date.now() - T0) / 1000).toFixed(0) + 's of a ' + BUDGET_S + 's budget');
  process.exit(failures ? 1 : 0);
})();
