/** @param {unknown} value */
export const validId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);

/** @param {Document} doc */
export function pageDom(doc) {
  /** @param {string} id */
  const get = id => /** @type {HTMLElement} */ (doc.getElementById(id));
  /** @param {string} id @param {string} value */
  const say = (id, value) => {
    const node = get(id);
    if (node.textContent !== value) node.textContent = value;
  };
  /** @param {string} tag @param {string} [value] @param {string} [cls] */
  const make = (tag, value, cls) => {
    const node = doc.createElement(tag);
    if (value !== undefined && value !== null) node.textContent = value;
    if (cls) node.className = cls;
    return node;
  };
  return {get, say, make};
}

/**
 * @param {Window & typeof globalThis} win
 * @param {string} path
 * @param {unknown} [body]
 * @param {{timeoutMs?: number, controllers?: Set<AbortController>, maskServerError?: boolean}} [options]
 */
export async function requestJson(win, path, body, {timeoutMs = 15000, controllers, maskServerError = true} = {}) {
  const controller = new win.AbortController();
  controllers?.add(controller);
  const timeout = win.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await win.fetch(path, {
      mode: 'same-origin', redirect: 'error', cache: 'no-store', signal: controller.signal,
      ...(body === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Agent-Office': 'studio'},
        body: JSON.stringify(body),
      }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      const message = (!maskServerError || response.status < 500) && data?.error?.message;
      throw Object.assign(new Error(message || '요청을 처리하지 못했습니다.'), {status: response.status, code: data?.error?.code});
    }
    return await response.json();
  } finally {
    win.clearTimeout(timeout);
    controllers?.delete(controller);
  }
}

/** @param {Window} win @param {() => void} refresh @param {() => void} invalidate */
export function visiblePoller(win, refresh, invalidate) {
  const doc = win.document;
  /** @type {number|undefined} */
  let timer;
  const cancel = () => { win.clearTimeout(timer); timer = undefined; };
  const visibility = () => {
    cancel();
    if (doc.hidden) invalidate();
    else refresh();
  };
  doc.addEventListener('visibilitychange', visibility);
  return {
    cancel,
    scheduled: () => timer !== undefined,
    /** @param {number} delay */
    schedule(delay) {
      cancel();
      if (!doc.hidden) timer = win.setTimeout(refresh, delay);
    },
    dispose() {
      cancel();
      doc.removeEventListener('visibilitychange', visibility);
    },
  };
}
