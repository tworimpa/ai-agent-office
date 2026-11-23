/** @typedef {(path: string) => Promise<any>} ReadApi */
/**
 * @typedef {object} RoomBundle
 * @property {object} room
 * @property {{run: object}} [run]
 * @property {{goal: object|null}} [goal]
 * @property {{requests: Array<object>}} [inbox]
 * @property {{directions: Array<object>}} [directions]
 * @property {object} [briefing]
 * @property {object} [github]
 * @property {object} [usage]
 */

/**
 * Keep projections local to this refresh. Button clicks, navigation, and
 * paginated reads must always issue a fresh request against their own host.
 * @param {ReadApi} api
 * @param {string} base
 * @param {string|null} id
 * @param {boolean} bundled
 * @param {boolean} slow
 * @param {boolean} usage
 * @param {() => boolean} current
 */
export async function roomRefresh(api, base, id, bundled, slow, usage, current) {
  if (bundled) {
    const mode = slow ? (usage ? 'full-usage' : 'full') : 'live';
    try {
      /** @type {RoomBundle} */
      const bundle = await api(base + '/bundle/' + (id ?? 'none') + '/' + mode);
      const run = '/runs/' + id;
      const sections = new Map([[run, bundle.run], [run + '/goal', bundle.goal], [run + '/inbox', bundle.inbox],
        [run + '/directions', bundle.directions], [base + '/briefing', bundle.briefing],
        [base + '/github', bundle.github], [base + '/usage/' + id, bundle.usage]]);
      /** @type {ReadApi} */
      const read = async path => {
        if (!current()) throw new Error('작업실 조회가 종료되었습니다.');
        return sections.get(path) ?? api(path);
      };
      return {room: bundle.room, read, retry: false};
    } catch (error) {
      // Large combined responses can exceed the peer limit even when all
      // individual reads fit. Each smaller projection retains its wire checks.
      if (!current() || error && typeof error === 'object' && 'code' in error && error.code === 'JOURNAL_MISMATCH') throw error;
    }
  }
  return {room: await api(base), read: api, retry: bundled};
}
