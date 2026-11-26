// Pages are stored newest first. Keep a separate cursor at a gap if the newest
// page jumps past previously expanded history while the client was offline.
export function pagedHistory() {
  let groups = [],
    expanded = false;
  function merge(rows, next, start) {
    rows = [...rows];
    let index = start;
    while (groups[index]) {
      const old = groups[index],
        ids = new Set(rows.map((row) => row.id));
      const overlap = old.rows.findLastIndex((row) => ids.has(row.id));
      if (overlap < 0) break;
      if (old.rows.some((row) => row.id === rows.at(-1)?.id)) {
        rows.push(...old.rows.slice(overlap + 1));
        next = old.next;
      }
      index++;
    }
    groups.splice(start, index - start, { rows, next });
  }
  return {
    get groups() {
      return groups;
    },
    latest(rows, next) {
      if (!expanded || !rows.length) groups = [{ rows, next }];
      else merge(rows, next, 0);
    },
    older(cursor, rows, next) {
      const at = groups.findIndex((group) => group.next === cursor);
      if (at < 0) return;
      expanded = true;
      merge(rows, next, at + 1);
      const following = groups.splice(at + 1, 1)[0];
      groups[at].rows.push(...following.rows);
      groups[at].next = following.next;
    },
    put(row) {
      const group = groups.find((group) => group.rows.some((item) => item.id === row.id));
      if (group) group.rows[group.rows.findIndex((item) => item.id === row.id)] = row;
      else if (groups.length) groups[0].rows.unshift(row);
      else groups = [{ rows: [row], next: null }];
    },
  };
}

export function controls(win) {
  const doc = win.document;
  const get = (id) => doc.getElementById(id);
  function element(tag, text, cls) {
    const node = doc.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function button(text, handler, cls) {
    const node = element('button', text, cls);
    node.type = 'button';
    node.onclick = handler;
    return node;
  }
  function picture(bot, cls) {
    const node = element('img', null, cls);
    node.alt = '';
    node.src = bot.photo || '/bot-art/figure-' + bot.face + '.webp';
    return node;
  }
  function folderChoices(prefix, folders, index = 0) {
    const select = get(prefix + 'folder');
    get(prefix + 'folderLabel').hidden = folders.length < 2;
    select.replaceChildren(
      ...folders.map((folder, i) => {
        const option = element('option', typeof folder === 'string' ? folder : folder.path);
        option.value = String(i);
        return option;
      }),
    );
    select.selectedIndex = Math.min(index, folders.length - 1);
  }
  function folderPayload(prefix) {
    const select = get(prefix + 'folder');
    return select.options.length > 1 ? { indices: [Number(select.value)] } : {};
  }
  return { win, get, element, button, picture, folderChoices, folderPayload };
}

const characters = [
  '서책을 든 학자',
  '두루마리 선비',
  '길 위의 탐방가',
  '도예 명장',
  '목수',
  '장구 연주자',
  '천문학자',
  '서신 전령',
  '요리사',
  '붓 든 고양이',
  '약초 토끼',
  '호랑이 선비',
];

// This accepts only output from our static sRGB canvas, never the uploaded file itself.
// Some encoders include an ICCP for that default colour space. WebP 1.0 portraits
// carry no profiles; retain the pixel and alpha chunks (including RIFF padding).
export function canvasPortrait(win, url, width, height) {
  const prefix = 'data:image/webp;base64,';
  const invalid = () => {
    throw Error('캔버스가 지원되는 정적 WebP 이미지를 만들지 못했습니다.');
  };
  if (!url.startsWith(prefix)) invalid();
  const raw = win.atob(url.slice(prefix.length));
  const uint = (at, bytes) => {
    let value = 0;
    for (let i = bytes - 1; i >= 0; i--) value = value * 256 + raw.charCodeAt(at + i);
    return value;
  };
  if (raw.slice(0, 4) !== 'RIFF' || raw.slice(8, 12) !== 'WEBP' || uint(4, 4) !== raw.length - 8) invalid();
  const chunks = [];
  for (let at = 12; at < raw.length; ) {
    if (at + 8 > raw.length) invalid();
    const tag = raw.slice(at, at + 4),
      size = uint(at + 4, 4),
      data = at + 8;
    const end = data + size + (size % 2);
    if (!size || end > raw.length || (size % 2 && raw.charCodeAt(end - 1) !== 0)) invalid();
    chunks.push({ tag, size, data, packed: raw.slice(at, end) });
    at = end;
  }
  const extended = chunks[0]?.tag === 'VP8X' ? chunks.shift() : null;
  let header = '';
  if (extended) {
    const { data, size, packed } = extended,
      flags = uint(data, 1);
    if (
      size !== 10 ||
      flags & ~0x30 ||
      uint(data + 1, 3) ||
      uint(data + 4, 3) + 1 !== width ||
      uint(data + 7, 3) + 1 !== height
    )
      invalid();
    if (flags & 0x20) {
      if (chunks.shift()?.tag !== 'ICCP') invalid();
    }
    if (flags & 0x10) header = packed.slice(0, 8) + String.fromCharCode(0x10) + packed.slice(9);
  }
  const tags = chunks.map((chunk) => chunk.tag).join(',');
  if (tags !== 'VP8L' && tags !== (header ? 'ALPH,VP8 ' : 'VP8 ')) invalid();
  const { data, size, tag } = chunks.at(-1);
  if (tag === 'VP8L') {
    const bits = uint(data + 1, 4);
    if (
      size <= 5 ||
      uint(data, 1) !== 0x2f ||
      bits >>> 29 ||
      (bits & 0x3fff) + 1 !== width ||
      ((bits >>> 14) & 0x3fff) + 1 !== height
    )
      invalid();
  } else if (
    size <= 10 ||
    uint(data, 1) & 1 ||
    uint(data + 3, 3) !== 0x2a019d ||
    (uint(data + 6, 2) & 0x3fff) !== width ||
    (uint(data + 8, 2) & 0x3fff) !== height
  )
    invalid();
  const body = 'WEBP' + header + chunks.map((chunk) => chunk.packed).join('');
  const n = body.length;
  if (n + 8 > 49152) return null;
  const length = String.fromCharCode(n & 255, (n >>> 8) & 255, (n >>> 16) & 255, n >>> 24);
  return prefix + win.btoa('RIFF' + length + body);
}

async function imageUpload(win, file) {
  if (!['image/png', 'image/webp', 'image/jpeg'].includes(file.type) || file.size > 8 * 1024 * 1024)
    throw Error('8 MB 이하의 PNG, WebP 또는 JPEG를 선택하세요.');
  const bitmap = await win.createImageBitmap(file);
  try {
    const canvas = win.document.createElement('canvas');
    for (const max of [256, 192, 128]) {
      const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height));
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext('2d', { colorSpace: 'srgb' }).drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const image = canvasPortrait(win, canvas.toDataURL('image/webp', 0.8), canvas.width, canvas.height);
      if (image) return { canvas, image };
    }
    throw Error('이미지를 48 KiB 이내의 WebP로 변환할 수 없습니다.');
  } finally {
    bitmap.close();
  }
}

export function mountCharacterPicker(ui, { uuid, api, changed }) {
  const { get, element, button, picture, win } = ui;
  let owner,
    digest,
    id,
    face,
    image,
    preview,
    group,
    epoch = 0,
    saving = false;
  function faces() {
    get('characterFilters').replaceChildren(
      ...['전체', '인물', '동물'].map((label) => {
        const item = button(label, () => {
          group = label;
          faces();
          get('characterFilters').querySelector('[aria-pressed=true]').focus();
        });
        item.setAttribute('aria-pressed', String(group === label));
        return item;
      }),
    );
    const choices = [];
    characters.forEach((label, index) => {
      if ((group === '인물' && index >= 9) || (group === '동물' && index < 9)) return;
      const tile = button('', () => {
        epoch++;
        face = index;
        image = '';
        preview = null;
        get('upload').value = '';
        get('uploadStatus').textContent = '';
        get('submitBot').disabled = false;
        faces();
        get('choices').querySelector('[aria-pressed=true]').focus();
      });
      tile.setAttribute('aria-pressed', String(!image && face === index));
      tile.setAttribute('aria-label', label);
      tile.append(picture({ face: index }), element('span', label));
      choices.push(tile);
    });
    get('choices').replaceChildren(...choices);
    const custom = get('customPreview');
    custom.replaceChildren();
    custom.hidden = !image;
    if (image) custom.append(preview || picture(owner), element('span', '내 이미지'));
  }
  get('cancel').onclick = () => get('dialog').close();
  get('dialog').addEventListener('close', () => {
    epoch++;
  });
  get('upload').onchange = async () => {
    const token = ++epoch,
      file = get('upload').files[0];
    if (!file) return;
    get('submitBot').disabled = true;
    try {
      const result = await imageUpload(win, file);
      if (token !== epoch) return;
      preview = result.canvas;
      image = result.image;
      get('uploadStatus').textContent = file.name;
      faces();
    } catch (err) {
      if (token === epoch) get('uploadStatus').textContent = err.message;
    } finally {
      if (token === epoch) get('submitBot').disabled = false;
    }
  };
  get('botForm').onsubmit = async (event) => {
    event.preventDefault();
    if (saving || get('submitBot').disabled) return;
    saving = true;
    get('submitBot').disabled = true;
    const token = epoch,
      appearance = { preset: face, ...(image ? { image } : {}) };
    try {
      if (owner && image === (owner.appearance?.image || '') && face === owner.face) {
        get('dialog').close();
        return;
      }
      if (owner) await api('/bots/' + owner.id + '/appearance', { appearance, digest });
      else
        await api('/bots', {
          id,
          name: get('name').value.trim(),
          role: get('job').value.trim(),
          instructions: get('soul').value,
          appearance,
        });
      if (token === epoch) get('dialog').close();
      await changed();
    } catch (err) {
      if (token === epoch) get('uploadStatus').textContent = err.message;
    } finally {
      saving = false;
      get('submitBot').disabled = false;
    }
  };
  return (bot) => {
    if (saving) return;
    epoch++;
    owner = bot;
    id = 'bot-' + uuid();
    digest = bot?.digest;
    face = bot?.face ?? 0;
    image = bot?.appearance?.image || '';
    preview = null;
    group = '전체';
    get('botForm').reset();
    get('identity').hidden = !!bot;
    for (const name of ['name', 'job', 'soul']) get(name).required = !bot;
    get('dialogTitle').textContent = bot ? '캐릭터 교체' : '새 봇 만들기';
    get('submitBot').textContent = bot ? '적용' : '회의실에 추가';
    get('submitBot').disabled = false;
    get('uploadStatus').textContent = '';
    faces();
    get('dialog').showModal();
  };
}
