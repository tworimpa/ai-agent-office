import assert from 'node:assert/strict'

const historicalVersions = new Set(['0.8.1', '0.9.0'])

export function verifyUpgradeSeedVersion(installedVersion, expectedVersion = '0.8.1') {
  assert.ok(historicalVersions.has(expectedVersion), 'Unsupported desktop upgrade baseline: ' + expectedVersion)
  assert.equal(installedVersion, expectedVersion, 'Seed receipt must match the selected baseline version')
}

export function meetingRevisionRoles(installedVersion) {
  return historicalVersions.has(installedVersion) ? ['developer', 'reviewer'] : ['coordinator']
}

export function verifiesCatalogHandoff(installedVersion) {
  return !historicalVersions.has(installedVersion)
}

export function verifiesPublicCatalog(installedVersion, enabled) {
  return installedVersion === '0.10.0' && enabled === '1'
}
