import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { attachOrReserveSpawn, attachToHostBackend } from './host-backend-attach'
import { claimHostSpawnGate } from './host-spawn-gate'
import { runPrimaryBackendStartup } from './primary-backend-startup'

const LEDGER = JSON.stringify([
  {
    argv: 'hermes serve --host 127.0.0.1 --port 0',
    create_time: 1_000,
    host: '127.0.0.1',
    install: 'abc',
    pid: 4711,
    port: 65_238,
    profile: 'ops',
    purpose: 'serve',
    registered_at: 2_000
  }
])

function attachDeps(ledger: string | null) {
  return {
    log: () => {},
    probeWebSocket: async () => ({ ok: true }),
    readLedger: () => ledger,
    resolveServedToken: async () => 'served-token',
    waitForReady: async () => undefined
  }
}

/**
 * Multiplex-only invariant: a backend already running on the HOST is THE
 * backend — even one registered by another profile. Startup attaches to it and
 * spawns nothing.
 */
test('a running backend record makes startup attach and spawn zero processes', async () => {
  let spawns = 0

  const setup = await runPrimaryBackendStartup({
    assertCurrentAttempt: () => {},
    attachHostBackend: () => attachToHostBackend({ isolated: false, ledgerPath: '/ledger.json' }, attachDeps(LEDGER)),
    connectRemote: async () => ({ mode: 'remote' }),
    ensureLocalRuntime: async backend => backend,
    prepareLocalBackend: () => {
      spawns += 1

      return { label: 'spawned' }
    },
    resolveRemote: async () => null,
    waitForDecision: async () => 'continue-local' as const,
    waitForLocalStart: async () => undefined
  })

  assert.equal(setup.kind, 'attached')
  assert.equal(spawns, 0, 'startup must not prepare/spawn a backend when the host already has one')
  assert.deepEqual(setup.kind === 'attached' ? setup.attached : null, {
    baseUrl: 'http://127.0.0.1:65238',
    pid: 4711,
    port: 65238,
    token: 'served-token',
    wsUrl: 'ws://127.0.0.1:65238/api/ws?token=served-token'
  })
})

/** The only case that may start a process: the host has no backend. */
test('no backend record spawns exactly one backend', async () => {
  let spawns = 0

  const setup = await runPrimaryBackendStartup({
    assertCurrentAttempt: () => {},
    attachHostBackend: () => attachToHostBackend({ isolated: false, ledgerPath: '/ledger.json' }, attachDeps(null)),
    connectRemote: async () => ({ mode: 'remote' }),
    ensureLocalRuntime: async backend => backend,
    prepareLocalBackend: () => {
      spawns += 1

      return { label: 'spawned' }
    },
    resolveRemote: async () => null,
    waitForDecision: async () => 'continue-local' as const,
    waitForLocalStart: async () => undefined
  })

  assert.equal(setup.kind, 'local')
  assert.equal(spawns, 1)
})

/** A record that fails validation is not a backend: fall through to spawning. */
test('a record whose backend rejects the session token does not attach', async () => {
  const attached = await attachToHostBackend(
    { isolated: false, ledgerPath: '/ledger.json' },
    { ...attachDeps(LEDGER), probeWebSocket: async () => ({ ok: false, reason: 'unauthorized' }) }
  )

  assert.equal(attached, null)
})

/** A process that loses the atomic gate race waits for the winner's backend. */
test('a lost spawn-gate race attaches instead of spawning a second backend', async () => {
  let ledger: string | null = null
  let takeAttempts = 0

  const outcome = await attachOrReserveSpawn(
    { isolated: false, ledgerPath: '/ledger.json' },
    { ...attachDeps(null), readLedger: () => ledger },
    {
      now: () => 0,
      read: () => null,
      take: () => {
        takeAttempts += 1

        return null
      },
      sleep: async () => {
        ledger = LEDGER
      }
    },
    { pollMs: 0, waitBudgetMs: 1 }
  )

  assert.equal(takeAttempts, 1)
  assert.equal('attached' in outcome, true)
})

/** Gate creation is exclusive and an old release cannot remove a replacement. */
test('only one process owns the spawn gate file', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-spawn-gate-'))
  const gatePath = path.join(directory, 'gate.json')

  try {
    const releaseFirst = claimHostSpawnGate(gatePath, {
      claim: 'first',
      pid: 1,
      startedAt: 1
    })

    const releaseLoser = claimHostSpawnGate(gatePath, {
      claim: 'loser',
      pid: 2,
      startedAt: 2
    })

    assert.equal(typeof releaseFirst, 'function')
    assert.equal(releaseLoser, null)

    fs.unlinkSync(gatePath)

    const releaseReplacement = claimHostSpawnGate(gatePath, {
      claim: 'replacement',
      pid: 3,
      startedAt: 3
    })

    releaseFirst?.()

    assert.equal(typeof releaseReplacement, 'function')
    assert.equal(fs.existsSync(gatePath), true)
    releaseReplacement?.()
  } finally {
    fs.rmSync(directory, { force: true, recursive: true })
  }
})
