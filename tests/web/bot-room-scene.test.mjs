import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { mountRoomScene } from '../../src/agent_office/web/bot-room-scene.js';

function fixture(t, initiallyReduced = false) {
  const { window: win } = new JSDOM('<button id="walk-toggle"></button><div id="figures"></div>', {
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  const doc = win.document;
  const reduced = new win.EventTarget();
  reduced.matches = initiallyReduced;
  win.matchMedia = () => reduced;
  const frames = new Map();
  const timers = new Map();
  let now = 10000;
  let key = 0;
  win.Date.now = () => now;
  win.requestAnimationFrame = (fn) => { frames.set(++key, fn); return key; };
  win.cancelAnimationFrame = (id) => frames.delete(id);
  win.setTimeout = (fn, delay) => { timers.set(++key, { fn, due: now + delay }); return key; };
  win.clearTimeout = (id) => timers.delete(id);
  function advance(ms = 80) {
    now += ms;
    for (const [id, { fn, due }] of [...timers]) {
      if (now < due) continue;
      timers.delete(id);
      fn();
    }
    for (const [id, fn] of [...frames]) {
      frames.delete(id);
      fn(now);
    }
  }
  function hidden(value) {
    Object.defineProperty(doc, 'hidden', { value, configurable: true });
    doc.dispatchEvent(new win.Event('visibilitychange'));
  }
  const chosen = [];
  let royal = 0;
  const scene = mountRoomScene(doc.getElementById('figures'), {
    choose: (bot) => chosen.push(bot),
    openKing: () => royal++,
  });
  t.after(() => { scene.dispose(); win.close(); });
  return {
    scene, doc, win, frames, timers, advance, hidden, reduced, chosen,
    royal: () => royal,
    toggle: doc.getElementById('walk-toggle'),
    actors: () => [...doc.querySelectorAll('.game-actor')],
  };
}

function member(slot, face = slot) {
  return Object.freeze({ id: 'bot-' + slot, name: '봇 ' + slot, role: '조사', face, photo: '' });
}

function feet(node) {
  const [, x, y] = node.style.transform.match(/^translate\(([^,]+)px, ([^)]+)px\)/);
  return [Number(x), Number(y)];
}

test('each preset walks in four directions; duplicate presets and uploads have separate floor space', (t) => {
  const room = fixture(t);
  const bots = Array.from({ length: 12 }, (_, index) => member(index));
  bots.push(member(12, 2), Object.freeze({ ...member(13, 2), photo: '/my-portrait' }));
  room.scene.show(bots);
  const actors = room.actors();
  const bodies = actors.map((a) => a.querySelector('.actor-body'));
  const starts = actors.map(feet);
  const directions = bodies.map(() => new Set());
  const columns = bodies.map(() => new Set());
  const moved = new Set();
  const portraitGait = new Set();
  for (let i = 0; i < 450; i++) {
    room.advance();
    const points = actors.map(feet);
    for (const [slot, [x, y]] of points.entries()) {
      if (x !== starts[slot][0] || y !== starts[slot][1]) moved.add(slot);
      assert.ok(x >= 273 && x <= 666 && y >= 343 && y <= 449, 'feet stay on the visible floor');
      for (const [ox, oy] of points.slice(0, slot)) assert.ok(Math.hypot(x - ox, y - oy) > 18);
      const body = bodies[slot];
      if (slot === 13) {
        portraitGait.add(body.style.transform);
        continue;
      }
      const [col, row] = body.style.backgroundPosition.split(' ');
      directions[slot].add(row === '50%' ? (body.style.transform ? 'left' : 'right') : row);
      columns[slot].add(col);
    }
  }
  assert.equal(moved.size, bots.length);
  for (let i = 0; i < 13; i++) {
    assert.equal(bodies[i].style.backgroundImage, 'url("/bot-art/walk-' + bots[i].face + '.webp")');
    assert.deepEqual(directions[i], new Set(['0%', '100%', 'left', 'right']));
    assert.deepEqual(columns[i], new Set(['0%', '50%', '100%']));
  }
  assert.equal(bodies[13].style.backgroundImage, 'url("/my-portrait")');
  assert.ok(!bodies[13].classList.contains('atlas'));
  assert.ok(portraitGait.size > 3);
  assert.ok([...portraitGait].every((pose) => !pose.includes('scaleX')));
});

test('pointer, keyboard, roster refresh and reduced motion retain usable standing characters', (t) => {
  const room = fixture(t, true);
  const bot = member(0, 3);
  const photo = { ...member(1, 1), photo: '/private/workspace/photo' };
  room.scene.show([bot, photo]);
  const [first, second] = room.actors();
  const body = first.querySelector('.actor-body');
  assert.equal(room.frames.size, 0);
  assert.equal(room.toggle.textContent, '이동 재생');
  assert.equal(body.style.backgroundPosition, '50% 0%');
  room.toggle.click();
  room.advance();
  room.advance();
  const moving = feet(first);
  first.onpointerenter({ pointerType: 'mouse' });
  room.advance();
  assert.deepEqual(feet(first), moving);
  first.focus();
  first.onpointerleave();
  room.advance();
  assert.deepEqual(feet(first), moving);
  const renamed = { ...bot, name: '다른 이름', face: 7, digest: 'new' };
  room.scene.show([renamed, photo]);
  assert.equal(room.doc.activeElement, first);
  assert.deepEqual(feet(first), moving);
  assert.match(first.getAttribute('aria-label'), /다른 이름/);
  assert.match(body.style.backgroundImage, /walk-7/);
  first.click();
  second.click();
  room.doc.querySelector('.court-king').click();
  assert.deepEqual(room.chosen, [renamed, photo]);
  assert.equal(room.royal(), 1);
  first.blur();
  room.advance();
  assert.notDeepEqual(feet(first), moving);
  room.toggle.click();
  assert.equal(room.frames.size, 0);
  assert.match(body.style.backgroundPosition, /^50% /);
  assert.equal(second.querySelector('.actor-body').style.transform, '');
  room.reduced.matches = false;
  room.reduced.dispatchEvent(new room.win.Event('change'));
  assert.equal(room.frames.size, 1);
  room.reduced.matches = true;
  room.reduced.dispatchEvent(new room.win.Event('change'));
  assert.equal(room.frames.size, 0);
});

test('royal cues, hidden tabs, empty rosters and disposal release scheduled work', (t) => {
  const room = fixture(t);
  const bot = member(0);
  room.scene.show([bot, member(1)]);
  const [first] = room.actors();
  const cue = first.querySelector('.royal-cue');
  const assignment = { bot: bot.id, duty: '자료 조사', action: '조사 중' };
  room.scene.respond([assignment]);
  assert.equal(cue.textContent, '명을 듣는 중');
  const listening = feet(first);
  room.advance(1000);
  assert.deepEqual(feet(first), listening);
  room.hidden(true);
  assert.equal(room.frames.size + room.timers.size, 0);
  room.advance(600);
  room.hidden(false);
  assert.equal(cue.textContent, assignment.action);
  assert.equal(cue.title, assignment.duty);
  assert.equal(room.frames.size, 1);
  assert.equal(room.timers.size, 1);
  room.scene.pause();
  assert.equal(room.frames.size + room.timers.size, 0);
  room.advance(14000);
  room.scene.show([bot, member(1)]);
  assert.ok(cue.hidden);
  room.scene.respond([assignment]);
  room.scene.show([]);
  assert.equal(room.frames.size + room.timers.size, 0);
  room.scene.show([bot]);
  room.scene.respond([assignment]);
  room.scene.dispose();
  room.hidden(true);
  room.hidden(false);
  room.toggle.click();
  assert.equal(room.frames.size + room.timers.size, 0);
  assert.equal(room.actors().length, 0);
});
