import crypto from 'node:crypto'
import fs from 'node:fs'

interface HostSpawnGateClaim {
  claim?: string
  pid?: number
  startedAt?: number
}

/**
 * Atomically claim the host-level backend spawn gate.
 *
 * Exclusive creation makes one contender win when several Desktop processes
 * start together. The release checks its opaque claim before unlinking, so a
 * delayed cleanup cannot remove a later process's gate.
 */
export function claimHostSpawnGate(
  gatePath: string,
  { pid = process.pid, startedAt = Date.now(), claim = crypto.randomUUID() }: HostSpawnGateClaim = {}
): (() => void) | null {
  try {
    fs.writeFileSync(gatePath, JSON.stringify({ claim, pid, startedAt }), {
      flag: 'wx',
      mode: 0o600
    })
  } catch {
    return null
  }

  return () => {
    try {
      const record = JSON.parse(fs.readFileSync(gatePath, 'utf8'))

      if (record?.claim === claim) {
        fs.unlinkSync(gatePath)
      }
    } catch {
      // Already gone, unreadable, or replaced.
    }
  }
}
