const roleNames = new Map([['developer', '개발자'], ['reviewer', '리뷰어']]);
let nextInstance = 0;

function flowSource(tasks) {
  // Only our own Mermaid tokens and the backend's bounded task IDs enter the grammar.
  // Free-form descriptions, paths and revision labels are exclusively DOM text.
  if (!tasks.length || tasks.length > 12) throw new Error('Unsupported task count');
  const indices = new Map();
  const lines = ['flowchart TD'];
  for (const [index, task] of tasks.entries()) {
    if (typeof task?.id !== 'string' || !/^[A-Za-z][A-Za-z0-9_-]{0,31}$/.test(task.id)
        || indices.has(task.id) || !roleNames.has(task.owner) || !Array.isArray(task.depends_on)) {
      throw new Error('Invalid task');
    }
    lines.push(`n${index}[${task.id} · ${roleNames.get(task.owner)}]`);
    for (const id of new Set(task.depends_on)) {
      if (!indices.has(id)) throw new Error('Invalid dependency');
      lines.push(`n${indices.get(id)} --> n${index}`);
    }
    indices.set(task.id, index);
  }
  return lines.join('\n');
}

async function draw(doc, tasks, instance, caption) {
  const source = flowSource(tasks);
  const {renderMermaid} = await import('./mermaid-flow-renderer.js');
  // Chromium applies CSP while parsing even a detached XML document. The pinned
  // renderer emits a fixed inline style block (including a Google Fonts import)
  // and root style attribute. Replace both before the browser sees any markup.
  const markup = (await renderMermaid(source, {padding: 14}))
    .replace(/<style>[\s\S]*?<\/style>/g, '')
    .replace(/ style="[^"]*"/g, '');
  const svg = new doc.defaultView.DOMParser().parseFromString(markup, 'image/svg+xml').documentElement;
  if (svg.localName !== 'svg') throw new Error('Invalid diagram');
  for (const marker of svg.querySelectorAll('marker[id]')) {
    const previous = marker.id;
    marker.id = instance + '-' + previous;
    for (const attribute of ['marker-end', 'marker-start']) {
      for (const edge of svg.querySelectorAll('[' + attribute + ']')) {
        if (edge.getAttribute(attribute) === 'url(#' + previous + ')') {
          edge.setAttribute(attribute, 'url(#' + marker.id + ')');
        }
      }
    }
  }
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', caption);
  return doc.importNode(svg, true);
}

export function planFlow(doc, plan, revision) {
  const instance = 'plan-flow-' + (++nextInstance);
  const tasks = Array.isArray(plan?.tasks) ? plan.tasks : [];
  const caption = revision == null ? '작업 흐름' : '계획 ' + revision + ' · 작업 흐름';
  const section = doc.createElement('section');
  section.className = 'plan-flow';
  const heading = doc.createElement('h5');
  heading.textContent = '작업 순서와 담당';
  section.append(heading);
  if (!tasks.length) {
    const empty = doc.createElement('p');
    empty.textContent = '등록된 작업이 없습니다.';
    section.append(empty);
    return section;
  }

  const list = doc.createElement('ol');
  for (const task of tasks) {
    const item = doc.createElement('li');
    const title = doc.createElement('strong');
    title.textContent = [task?.id, roleNames.get(task?.owner) ?? task?.owner].filter(Boolean).join(' · ');
    item.append(title, doc.createTextNode(' ' + (task?.description ?? '')));
    const details = [];
    if (Array.isArray(task?.depends_on) && task.depends_on.length) details.push('선행 작업 ' + task.depends_on.join(', '));
    if (Array.isArray(task?.folders) && task.folders.length) details.push('작업 폴더 ' + task.folders.join(', '));
    if (details.length) {
      const meta = doc.createElement('small');
      meta.textContent = details.join(' · ');
      item.append(meta);
    }
    list.append(item);
  }

  const button = doc.createElement('button');
  button.type = 'button';
  button.textContent = '작업 흐름 보기';
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', instance);
  const panel = doc.createElement('div');
  panel.className = 'plan-flow-chart';
  panel.id = instance;
  panel.hidden = true;
  section.append(list, button, panel);
  let rendering;
  button.addEventListener('click', async () => {
    const open = panel.hidden;
    panel.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? '작업 흐름 닫기' : '작업 흐름 보기';
    if (!open || rendering) return;
    panel.setAttribute('aria-busy', 'true');
    rendering = draw(doc, tasks, instance, caption);
    try {
      panel.append(await rendering);
    } catch {
      const status = doc.createElement('p');
      status.setAttribute('role', 'status');
      status.textContent = '도식을 표시할 수 없습니다. 위 목록에서 작업 순서와 선행 작업을 확인하세요.';
      panel.append(status);
    } finally {
      panel.removeAttribute('aria-busy');
    }
  });
  return section;
}
