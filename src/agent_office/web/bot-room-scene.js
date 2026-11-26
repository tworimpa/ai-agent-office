// Scene coordinates are feet on the painted floor; the camera and all actors scale together.
const study = {
  width: 850,
  height: 478,
  cameraX: 473,
  image: '/bot-art/study-seated-king.webp',
  king: { x: 498, feet: 178, height: 109, width: 114 },
  resting: [
    [429, 348],
    [471, 429],
    [617, 399],
    [350, 432],
    [569, 446],
    [436, 399],
    [313, 408],
    [594, 345],
    [385, 378],
  ],
  routes: [
    [
      [321, 349, 0],
      [360, 351, 0],
      [391, 365, 1.4],
      [380, 394, 0],
      [345, 409, 2.2],
      [309, 385, 0],
      [321, 357, 0.8],
    ],
    [
      [559, 358, 0],
      [604, 354, 1],
      [636, 377, 0],
      [620, 408, 1.7],
      [565, 415, 0],
      [528, 399, 1],
      [542, 374, 0],
    ],
  ],
  walkers: { 0: { route: 0, speed: 19, height: 104 }, 11: { route: 1, speed: 22, height: 76 } },
};
const gait = [0, 1, 2, 1];
const percent = (value, total) => (100 * value) / total + '%';

export function mountRoomScene(container, { choose, openKing }, scene = study) {
  const doc = container.ownerDocument,
    win = doc.defaultView;
  const element = (tag, cls) => {
    const node = doc.createElement(tag);
    node.className = cls;
    return node;
  };
  const world = element('div', 'game-world');
  world.style.backgroundImage = 'url("' + scene.image + '")';
  container.replaceChildren(world);
  const king = element('button', 'court-king');
  king.type = 'button';
  king.setAttribute('aria-label', '왕에게 어명 전하기');
  king.append(element('span', 'king-label'));
  king.firstChild.textContent = '왕 · 어명';
  const seat = scene.king;
  Object.assign(king.style, {
    left: percent(seat.x, scene.width),
    top: percent(seat.feet, scene.height),
    width: percent(seat.width, scene.width),
    height: percent(seat.height, scene.height),
  });
  king.onclick = openKing;
  world.append(king);

  function resize() {
    if (!container.clientWidth || !container.clientHeight) return;
    const scale = Math.min(
      container.clientHeight / scene.height,
      container.clientWidth / (scene.width * 0.51),
    );
    const w = scale * scene.width,
      h = scale * scene.height;
    const offset =
      w <= container.clientWidth
        ? (container.clientWidth - w) / 2
        : Math.max(
            container.clientWidth - w,
            Math.min(0, container.clientWidth / 2 - scene.cameraX * scale),
          );
    Object.assign(world.style, {
      width: w + 'px',
      height: h + 'px',
      left: offset + 'px',
      top: (container.clientHeight - h) * 0.33 + 'px',
    });
  }
  const observer = win.ResizeObserver ? new win.ResizeObserver(resize) : null;
  observer?.observe(container);
  const actors = new Map();
  let live = false,
    frame = 0,
    previous = 0,
    timers = [];
  const reduced = win.matchMedia?.('(prefers-reduced-motion: reduce)');
  let playing = !reduced?.matches;
  const toggle = doc.getElementById('walk-toggle');
  function toggleLabel() {
    if (!toggle) return;
    toggle.textContent = playing ? '이동 일시정지' : '이동 재생';
    toggle.setAttribute('aria-pressed', String(!playing));
  }
  function paint(a, walking) {
    const height = (a.sprite?.height ?? 98) * (0.76 + (0.48 * a.y) / scene.height);
    Object.assign(a.node.style, {
      left: percent(a.x, scene.width),
      top: percent(a.y, scene.height),
      height: percent(height, scene.height),
      zIndex: String(Math.round(a.y * 10)),
    });
    if (!a.sprite) return;
    const row = a.direction === 'back' ? 2 : a.direction === 'front' ? 0 : 1;
    const col = walking ? gait[Math.floor(a.stride) % gait.length] : 1;
    a.body.style.backgroundPosition = col * 50 + '% ' + row * 50 + '%';
    a.body.style.transform = a.direction === 'left' ? 'scaleX(-1)' : '';
  }
  function add(bot, slot, ordinal) {
    const sprite = !bot.photo && scene.walkers[bot.face],
      route = sprite && scene.routes[sprite.route];
    const first = route ? (ordinal * 3) % route.length : 0;
    const [x, y] = route
      ? route[first]
      : (scene.resting[slot] ?? [310 + ((slot * 137) % 324), 343 + ((slot * 57) % 92)]);
    const node = element('button', 'game-actor');
    node.type = 'button';
    node.setAttribute('aria-label', bot.name + ' · ' + bot.role + ' 대화 열기');
    node.onclick = () => choose(bot);
    const body = element('span', 'actor-body' + (sprite ? ' atlas' : ''));
    const imageUrl = sprite
      ? '/bot-art/walk-' + bot.face + '.webp'
      : bot.photo || '/bot-art/figure-' + bot.face + '.webp';
    body.style.backgroundImage = 'url("' + imageUrl + '")';
    const label = element('span', 'game-name'),
      cue = element('span', 'royal-cue');
    label.textContent = bot.name;
    cue.hidden = true;
    node.append(element('span', 'actor-shadow'), body, label, cue);
    world.append(node);
    const a = {
      node,
      body,
      cue,
      sprite,
      route,
      bot,
      x,
      y,
      next: first + 1,
      wait: 0,
      stride: 0,
      direction: 'front',
      hovered: false,
    };
    node.onpointerenter = (e) => {
      if (e.pointerType === 'mouse') a.hovered = true;
    };
    node.onpointerleave = () => {
      a.hovered = false;
    };
    actors.set(bot.id, a);
    paint(a, false);
  }
  function step(a, dt) {
    if (!a.route) return;
    if (a.hearing || a.hovered || a.node === doc.activeElement || a.wait > 0) {
      a.wait = Math.max(0, a.wait - dt);
      paint(a, false);
      return;
    }
    const [x, y, delay] = a.station ?? a.route[a.next % a.route.length];
    const dx = x - a.x,
      dy = y - a.y,
      length = Math.hypot(dx, dy),
      distance = Math.min(length, dt * a.sprite.speed);
    if (length > 0.001) {
      a.direction =
        Math.abs(dx) > Math.abs(dy) * 1.25 ? (dx > 0 ? 'right' : 'left') : dy > 0 ? 'front' : 'back';
      a.x += (distance * dx) / length;
      a.y += (distance * dy) / length;
      a.stride += distance / 3.75;
    }
    if (length <= distance + 0.001) {
      if (a.station) {
        a.station = null;
        a.wait = 5;
        a.direction = 'back';
      } else {
        a.next = (a.next + 1) % a.route.length;
        a.wait = delay;
      }
    }
    paint(a, a.wait <= 0);
  }
  function tick(now) {
    frame = 0;
    if (!live || doc.hidden || !playing) return;
    const dt = previous ? Math.min((now - previous) / 1000, 0.08) : 0;
    previous = now;
    for (const a of actors.values()) step(a, dt);
    frame = win.requestAnimationFrame(tick);
  }
  function schedule() {
    if (frame) win.cancelAnimationFrame(frame);
    frame = 0;
    previous = 0;
    if (live && playing && !doc.hidden && [...actors.values()].some((a) => a.route))
      frame = win.requestAnimationFrame(tick);
  }
  function motionChanged() {
    playing = !reduced.matches;
    toggleLabel();
    schedule();
  }
  if (toggle)
    toggle.onclick = () => {
      playing = !playing;
      toggleLabel();
      schedule();
    };
  toggleLabel();
  reduced?.addEventListener('change', motionChanged);
  doc.addEventListener('visibilitychange', schedule);
  function show(bots) {
    const ids = new Set(bots.map((b) => b.id));
    for (const [id, a] of actors)
      if (!ids.has(id)) {
        a.node.remove();
        actors.delete(id);
      }
    let slot = 0;
    const counts = new Map();
    for (const b of bots) {
      const ordinal = counts.get(b.face) || 0;
      const a = actors.get(b.id);
      if (!a || ['face', 'photo', 'role', 'name'].some((key) => a.bot[key] !== b[key])) {
        a?.node.remove();
        add(b, slot, ordinal);
      } else {
        a.node.onclick = () => choose(b);
      }
      counts.set(b.face, ordinal + 1);
      if (b.photo || !scene.walkers[b.face]) slot++;
    }
    live = true;
    resize();
    if (!frame) schedule();
  }
  function respond(assignments) {
    timers.forEach((id) => win.clearTimeout(id));
    timers = [];
    for (const a of actors.values()) {
      a.hearing = false;
      a.station = null;
      a.cue.hidden = true;
      a.node.classList.remove('on-duty');
    }
    assignments.forEach((entry, i) => {
      const a = actors.get(entry.bot);
      if (!a) return;
      a.hearing = true;
      a.direction = 'back';
      paint(a, false);
      a.cue.hidden = false;
      a.cue.textContent = '명을 듣는 중';
      timers.push(
        win.setTimeout(
          () => {
            a.hearing = false;
            a.cue.textContent = entry.action || '업무 접수';
            a.cue.title = entry.duty;
            a.node.classList.add('on-duty');
            if (a.route) {
              a.wait = 0;
              a.station = [345 + ((i * 73) % 255), 350 + ((i * 41) % 70), 0];
            }
          },
          1300 + Math.min(i, 10) * 230,
        ),
      );
      timers.push(
        win.setTimeout(
          () => {
            a.cue.hidden = true;
            a.node.classList.remove('on-duty');
          },
          12500 + Math.min(i, 10) * 230,
        ),
      );
    });
  }
  function pause() {
    live = false;
    schedule();
  }
  function dispose() {
    pause();
    observer?.disconnect();
    reduced?.removeEventListener('change', motionChanged);
    doc.removeEventListener('visibilitychange', schedule);
    timers.forEach((id) => win.clearTimeout(id));
    world.remove();
  }
  return { show, pause, respond, dispose };
}
