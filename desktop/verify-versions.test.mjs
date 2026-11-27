import assert from 'node:assert/strict'
import test from 'node:test'
import {
  meetingRevisionRoles, verifiesCatalogHandoff, verifiesPublicCatalog, verifyUpgradeSeedVersion,
} from './verify-versions.mjs'

test('upgrade receipts must match an allowed, explicitly selected baseline', () => {
  assert.doesNotThrow(() => verifyUpgradeSeedVersion('0.8.1'))
  assert.doesNotThrow(() => verifyUpgradeSeedVersion('0.9.0', '0.9.0'))
  assert.throws(() => verifyUpgradeSeedVersion('0.8.1', '0.9.0'))
  assert.throws(() => verifyUpgradeSeedVersion('0.9.0'))
  assert.throws(() => verifyUpgradeSeedVersion('0.10.0', '0.10.0'))
})

test('install smoke checks each baseline against its own third plan revision', () => {
  assert.deepEqual(meetingRevisionRoles('0.8.1'), ['developer', 'reviewer'])
  assert.deepEqual(meetingRevisionRoles('0.9.0'), ['developer', 'reviewer'])
  assert.deepEqual(meetingRevisionRoles('0.10.0'), ['coordinator'])
})

test('only catalog-capable releases test the desktop link handoff', () => {
  assert.equal(verifiesCatalogHandoff('0.8.1'), false)
  assert.equal(verifiesCatalogHandoff('0.9.0'), false)
  assert.equal(verifiesCatalogHandoff('0.10.0'), true)
})

test('the public sample is checked only for the opted-in candidate', () => {
  assert.equal(verifiesPublicCatalog('0.10.0', '1'), true)
  assert.equal(verifiesPublicCatalog('0.10.0', undefined), false)
  assert.equal(verifiesPublicCatalog('0.10.0', '0'), false)
  assert.equal(verifiesPublicCatalog('0.9.0', '1'), false)
  assert.equal(verifiesPublicCatalog('0.8.1', '1'), false)
})
