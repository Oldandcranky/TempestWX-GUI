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

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  let failures = 0, checked = 0;

  for (const [width, height, label] of VIEWPORTS) {
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
  }

  // ── the manifest, and that every icon it names is served at its size ──
  {
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
  }

  // ── the alert banner: centred, and the end time never cut off ─────────
  {
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
  }

  // ── ways back from the outlook, once, in TV mode ──────────────────────
  {
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

      // Off the TV, both whole-card taps work the same way.
      await page.evaluate(() => setTv(false));
      await page.waitForTimeout(SETTLE_MS);
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
  }

  await browser.close();
  console.log(failures
    ? '\n  ' + failures + ' problem(s) across ' + checked + ' renders'
    : '\n  ' + checked + ' renders, all clean');
  process.exit(failures ? 1 : 0);
})();
