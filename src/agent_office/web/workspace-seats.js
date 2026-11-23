import {validId} from './studio-shared.js';

export function mountSeats(doc, roles) {
  const template = doc.getElementById('agent-seat');
  const grid = template.parentNode;
  for (const role of Object.keys(roles)) {
    const button = template.content.firstElementChild.cloneNode(true);
    button.dataset.person = role;
    grid.append(button);
  }
  const children = new Map();
  return {
    showRuns(rows, address) {
      const visible = new Set();
      for (const row of rows) {
        if (!validId(row.id)) continue;
        const key = row.id + ':' + row.role;
        visible.add(key);
        let link = children.get(key);
        if (!link) {
          link = doc.createElement('a');
          link.className = 'seat child-seat';
          if (row.role === 'child') link.dataset.child = row.id;
          else link.dataset.visitor = row.role;
          for (const className of ['seat-name', 'seat-role', 'seat-line']) {
            const label = doc.createElement('span');
            label.className = className;
            link.append(label);
          }
          children.set(key, link);
          grid.append(link);
        }
        link.href = address(row.id);
        link.children[0].textContent = row.title;
        link.children[1].textContent = row.label;
        link.children[2].textContent = row.state;
      }
      for (const [id, link] of children) {
        if (!visible.has(id)) { link.remove(); children.delete(id); }
      }
    },
  };
}
