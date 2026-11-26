// Scene coordinates are feet on the painted floor; the camera and all actors scale together.
const study = {
  width: 850,
  height: 478,
  cameraX: 473,
  image: '/bot-art/study-seated-king.webp',
  king: { x: 498, feet: 178, height: 109, width: 114 },
  floor: { left: 273, top: 343, width: 393, height: 106 },
};
const gait = [0, 1, 2, 1];
const percent = (value, total) => (100 * value) / total + '%';

// Each actor keeps its own piece of the walkable floor, including stops for royal orders.
function floorRoute(floor, slot, count) {
  const rows = Math.ceil(Math.sqrt(count / 4));
  const row = slot % rows;
  const columns = Math.ceil((count - row) / rows);
  const width = floor.width / columns;
  const height = floor.height / rows;
  const cx = floor.left + (Math.floor(slot / rows) + 0.5) * width;
  const cy = floor.top + (row + 0.5) * height;
  const rx = Math.min(65, width * 0.29);
  const ry = Math.min(34, height * 0.30);
  return [
    [cx - rx, cy - ry, 0.6],
    [cx + rx, cy - ry * 0.8, 1.2],
    [cx + rx * 0.95, cy + ry, 0.4],
    [cx - rx * 0.9, cy + ry * 0.85, 1.8],
  ];
}

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

  const actors = new Map();
  let scale = 1;
  function resize() {
    if (!container.clientWidth || !container.clientHeight) return;
    scale = Math.min(
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
    for (const a of actors.values()) position(a);
  }
  const observer = win.ResizeObserver ? new win.ResizeObserver(resize) : null;
  observer?.observe(container);
  let live = false,
    frame = 0,
    previous = 0,
    cueTimer = 0;
  const reduced = win.matchMedia?.('(prefers-reduced-motion: reduce)');
  let playing = !reduced?.matches;
  const toggle = doc.getElementById('walk-toggle');
  function toggleLabel() {
    if (toggle) toggle.textContent = playing ? '이동 일시정지' : '이동 재생';
  }
  function position(a) {
    const depth = 0.76 + (0.48 * a.y) / scene.height;
    const feet = `translate(${a.x * scale}px, ${a.y * scale}px)`;
    a.node.style.transform = `${feet} translate(-50%, -100%) scale(${depth})`;
    a.node.style.zIndex = String(Math.round(a.y * 10));
  }
  function pose(a, walking) {
    if (a.bot.photo) {
      const swing = walking ? Math.sin((a.stride * Math.PI) / 2) : 0;
      a.body.style.transform = swing
        ? `translateY(${-1.3 * Math.abs(swing)}%) rotate(${1.5 * swing}deg)`
        : '';
      return;
    }
    const row = a.direction === 'back' ? 2 : a.direction === 'front' ? 0 : 1;
    const col = walking ? gait[Math.floor(a.stride) % gait.length] : 1;
    a.body.style.backgroundPosition = col * 50 + '% ' + row * 50 + '%';
    a.body.style.transform = a.direction === 'left' ? 'scaleX(-1)' : '';
  }
  function add(bot) {
    const node = element('button', 'game-actor'),
      body = element('span', 'actor-body'),
      label = element('span', 'game-name'),
      cue = element('span', 'royal-cue');
    node.type = 'button';
    cue.hidden = true;
    node.append(element('span', 'actor-shadow'), body, label, cue);
    world.append(node);
    const a = { node, body, label, cue, stride: 0, wait: 0, direction: 'front' };
    node.onclick = () => choose(a.bot);
    node.onpointerenter = (e) => {
      if (e.pointerType === 'mouse') a.hovered = true;
    };
    node.onpointerleave = () => {
      a.hovered = false;
    };
    actors.set(bot.id, a);
    return a;
  }
  function place(a, slot, count) {
    a.slot = slot;
    a.count = count;
    a.route = floorRoute(scene.floor, slot, count);
    const first = slot % a.route.length;
    [a.x, a.y] = a.route[first];
    a.next = (first + 1) % a.route.length;
    a.speed = 18 + (slot % 5);
  }
  function step(a, dt) {
    if (a.hearing || a.hovered || a.node === doc.activeElement || a.wait > 0) {
      a.wait = Math.max(0, a.wait - dt);
      pose(a, false);
      return;
    }
    const [x, y, delay] = a.route[a.station ? 0 : a.next];
    const dx = x - a.x,
      dy = y - a.y,
      length = Math.hypot(dx, dy),
      distance = Math.min(length, dt * a.speed);
    if (length > 0.001) {
      a.direction =
        Math.abs(dx) > Math.abs(dy) * 1.25 ? (dx > 0 ? 'right' : 'left') : dy > 0 ? 'front' : 'back';
      a.x += (distance * dx) / length;
      a.y += (distance * dy) / length;
      a.stride += distance / 3.75;
      position(a);
    }
    if (length <= distance + 0.001) {
      if (a.station) {
        a.station = false;
        a.next = 1;
        a.wait = 5;
        a.direction = 'back';
      } else {
        a.next = (a.next + 1) % a.route.length;
        a.wait = delay;
      }
    }
    pose(a, distance > 0 && a.wait <= 0);
  }
  function tick(now) {
    frame = 0;
    if (!live || doc.hidden || !playing) return;
    const dt = previous ? Math.min((now - previous) / 1000, 0.08) : 0;
    previous = now;
    for (const a of actors.values()) step(a, dt);
    frame = win.requestAnimationFrame(tick);
  }
  // Keep cue deadlines independent of walking so paused actors can still hear orders.
  function updateCues() {
    win.clearTimeout(cueTimer);
    if (!live || doc.hidden) return;
    const now = win.Date.now();
    let deadline = Infinity;
    for (const a of actors.values()) {
      const order = a.order;
      if (!order) continue;
      if (now >= order.ends) {
        a.hearing = false;
        a.cue.hidden = true;
        a.node.classList.remove('on-duty');
        a.order = null;
      } else if (now >= order.starts) {
        if (a.hearing) {
          a.hearing = false;
          a.cue.textContent = order.entry.action || '업무 접수';
          a.cue.title = order.entry.duty;
          a.node.classList.add('on-duty');
          a.wait = 0;
          a.station = true;
        }
        deadline = Math.min(deadline, order.ends);
      } else {
        deadline = Math.min(deadline, order.starts);
      }
    }
    if (Number.isFinite(deadline)) cueTimer = win.setTimeout(updateCues, deadline - now);
  }
  function schedule() {
    if (frame) win.cancelAnimationFrame(frame);
    frame = 0;
    previous = 0;
    updateCues();
    if (live && playing && !doc.hidden && actors.size) frame = win.requestAnimationFrame(tick);
    else for (const a of actors.values()) pose(a, false);
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
    bots.forEach((bot, slot) => {
      const a = actors.get(bot.id) ?? add(bot);
      if (!a.bot || a.bot.face !== bot.face || a.bot.photo !== bot.photo) {
        a.body.classList.toggle('atlas', !bot.photo);
        const imageUrl = bot.photo || '/bot-art/walk-' + bot.face + '.webp';
        a.body.style.backgroundImage = 'url("' + imageUrl + '")';
        a.body.style.backgroundPosition = '';
        const height = bot.photo ? 98 : bot.face >= 9 ? 76 : bot.face === 0 ? 104 : 98;
        a.node.style.height = percent(height, scene.height);
      }
      a.bot = bot;
      a.label.textContent = bot.name;
      a.node.setAttribute('aria-label', bot.name + ' · ' + bot.role + ' 대화 열기');
      if (a.slot !== slot || a.count !== bots.length) place(a, slot, bots.length);
      position(a);
      pose(a, false);
    });
    live = true;
    resize();
    if (!frame || !actors.size) schedule();
    else updateCues();
  }
  function respond(assignments) {
    const now = win.Date.now();
    for (const a of actors.values()) {
      a.order = null;
      a.hearing = false;
      a.station = false;
      a.cue.hidden = true;
      a.cue.removeAttribute('title');
      a.node.classList.remove('on-duty');
    }
    assignments.forEach((entry, i) => {
      const a = actors.get(entry.bot);
      if (!a) return;
      const offset = Math.min(i, 10) * 230;
      a.order = { entry, starts: now + 1300 + offset, ends: now + 12500 + offset };
      a.hearing = true;
      a.direction = 'back';
      pose(a, false);
      a.cue.hidden = false;
      a.cue.textContent = '명을 듣는 중';
    });
    updateCues();
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
    if (toggle) toggle.onclick = null;
    world.remove();
  }
  return { show, pause, respond, dispose };
}
