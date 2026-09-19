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

const measure = () => {
  const bad = [];
  for (const card of document.querySelectorAll('.card[id]')) {
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
      body: JSON.stringify(Object.assign({}, STATE, { now: Date.now() / 1000 })),
    }));

    try {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await page.waitForSelector('.card[id]', { state: 'attached' });
      await page.waitForTimeout(SETTLE_MS);

      const bad = await page.evaluate(measure);
      const cards = await page.evaluate(() => document.querySelectorAll('.card[id]').length);
      checked += cards;

      for (const line of noise) bad.push(['(page)', line]);

      if (bad.length === 0) {
        console.log('  ok   ' + label + '  ' + cards + ' cards');
      } else {
        failures += bad.length;
        console.log('  FAIL ' + label + '  ' + cards + ' cards');
        for (const [id, why] of bad) console.log('         ' + id.padEnd(10) + why);
      }
    } catch (e) {
      failures++;
      console.log('  FAIL ' + label + '  ' + e.message.split('\n')[0]);
    }
    await page.close();
  }

  await browser.close();
  console.log(failures
    ? '\n  ' + failures + ' problem(s) across ' + checked + ' card renders'
    : '\n  ' + checked + ' card renders, all clean');
  process.exit(failures ? 1 : 0);
})();
