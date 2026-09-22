/**
 * TaskDrawer worker-log-tail ANSI stripping.
 *
 * Kanban workers' stdout is piped (not a TTY) but still carries ANSI SGR/OSC
 * styling. The drawer's plain-text LogView has no terminal emulator, so raw
 * escape codes render as literal garbage. The drawer must hand the log tail to
 * LogView already stripped (the shared `stripAnsi` from `@hermes/shared/ansi`).
 *
 * This test pins the CALL SITE: render TaskDrawer with an ANSI-laced log tail
 * and assert the rendered log region is clean plain text (no escape bytes).
 * It fails if the `stripAnsi(...)` wrapper is removed from the render.
 */

import { cleanup, render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

// ── fixtures ─────────────────────────────────────────────────────────────────
// The exact classes of sequences Hermes workers emit on piped stdout:
// SGR dim/faint, SGR reset, an OSC hyperlink (\x1b]8;;url\x07), plain lines.
const RAW_LOG = 'step 1 ok\x1b[2;3m dimmed line\x1b[0m\n\x1b]8;;https://example.invalid/x\x07link\x1b[0m\nplain line'
// What the plain-text LogView should actually show (every escape removed).
const EXPECTED_LOG = 'step 1 ok dimmed line\nlink\nplain line'

// ── mocks ────────────────────────────────────────────────────────────────────
// Only the plugin-sdk surface TaskDrawer touches is stubbed. LogView is a
// faithful plain-text stub (a <pre>), which is exactly the contract the real
// component provides: children rendered as plain text, no terminal emulator.
vi.mock('@hermes/plugin-sdk', async () => {
  // The drawer gets stripAnsi through the SDK (the fence forbids a static
  // `@hermes/shared/ansi` import in plugin code, tests included), so the
  // mock supplies the same real implementation the SDK re-exports.
  const { stripAnsi } = await import('@hermes/shared/ansi')
  const host = { notify: vi.fn(), navigate: vi.fn() }

  const stub = (name: string) => (props: { children?: ReactNode }) => <div data-stub={name}>{props.children}</div>

  return {
    host,
    stripAnsi,
    cn: (...parts: unknown[]) => parts.filter(Boolean).join(' '),
    compactNumber: (n: number) => String(n),
    isSubmitEnter: () => false,
    Codicon: () => null,
    Badge: stub('Badge'),
    Button: stub('Button'),
    ErrorState: stub('ErrorState'),
    Loader: () => null,
    Textarea: () => null,
    Tip: stub('Tip'),
    DropdownMenu: stub('DropdownMenu'),
    DropdownMenuTrigger: stub('DropdownMenuTrigger'),
    DropdownMenuContent: stub('DropdownMenuContent'),
    DropdownMenuItem: stub('DropdownMenuItem'),
    DropdownMenuSeparator: () => null,
    // Faithful to the real LogView: plain text, selectable, no terminal.
    LogView: ({ children, className }: { children?: ReactNode; className?: string }) => (
      <pre className={className} data-testid="worker-log">
        {children}
      </pre>
    ),
    useValue: (atom: unknown) => atom, // $boardSlug -> 'smoke' (mocked below)
    useQuery: (opts: { queryKey: unknown }) => {
      const key = Array.isArray(opts.queryKey) ? opts.queryKey.join('|') : String(opts.queryKey)

      if (key === 'LOG') {
        return { data: { exists: true, content: RAW_LOG, truncated: false }, isPending: false }
      }

      if (key === 'PROFILES') {
        return { data: { profiles: [] }, isPending: false }
      }

      // TASK
      return {
        data: {
          task: {
            id: 't1',
            title: 'T',
            status: 'done',
            assignee: 'w',
            priority: 1,
            created_at: 1,
            result: null,
            latest_summary: null,
            diagnostics: null
          },
          links: { parents: [], children: [] },
          comments: [],
          events: [],
          runs: []
        },
        isPending: false
      }
    },
    useMutation: () => ({ mutate: vi.fn(), isPending: false }),
    useQueryClient: () => ({
      invalidateQueries: vi.fn(),
      getQueryData: () => undefined,
      setQueryData: vi.fn(),
      cancelQueries: vi.fn(async () => {})
    })
  }
})

// Key helpers return distinguishable literals so the useQuery mock can branch
// on queryKey; the fetchers are stubs (never awaited by the mock useQuery).
vi.mock('./api', () => ({
  $boardSlug: 'smoke',
  logKey: () => 'LOG',
  taskKey: () => 'TASK',
  profilesKey: () => 'PROFILES',
  boardKeyPrefix: () => 'B',
  routedToScope: () => true,
  useKanbanScope: () => 'scope',
  fetchTask: vi.fn(),
  fetchLog: vi.fn(),
  fetchProfiles: vi.fn(),
  addComment: vi.fn(),
  patchTask: vi.fn(),
  deleteTask: vi.fn(),
  reassignTask: vi.fn(),
  reclaimTask: vi.fn(),
  uploadAttachment: vi.fn(),
  estimateTask: vi.fn()
}))

vi.mock('./ui', () => {
  // k.* are mostly strings (safe as React children/props); a few are called
  // as functions (k.comments(n), k.activity(n), ...). The Proxy returns a
  // no-op '' for anything not explicitly a function, so an unknown key is a
  // harmless empty string rather than a crash.
  const fnKeys = new Set([
    'comments',
    'activity',
    'runs',
    'attachments',
    'diagnosticsN',
    'artifacts',
    'copiedId',
    'evtCreated',
    'evtMovedTo',
    'evtAssignedTo',
    'evtUnassigned',
    'evtCommentBy',
    'evtClaimedReview',
    'evtClaimedWorker',
    'evtWorkerStarted',
    'evtCompleted',
    'evtBlocked',
    'evtUnblocked',
    'evtReclaimed',
    'evtSpecified',
    'evtPromoted',
    'evtScheduled',
    'evtArchived',
    'evtReprioritized'
  ])

  const kanbanText: unknown = new Proxy(
    {},
    {
      get: (_target, prop: string) => {
        if (typeof prop !== 'string') {
          return undefined
        }

        return fnKeys.has(prop) ? () => '' : ''
      }
    }
  )

  return {
    ago: () => '',
    duration: () => '',
    shortId: (id: string) => id.slice(0, 8),
    columnLabel: (_k: unknown, c: string) => c,
    errText: (e: unknown) => String(e),
    isLockedTarget: () => false,
    lockedReason: () => '',
    Avatar: () => null,
    Callout: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    Section: ({ children, label }: { children?: ReactNode; label?: string }) => (
      <section data-label={label}>{children}</section>
    ),
    ScrollFade: ({ children }: { children?: ReactNode }) => <>{children}</>,
    StatusMenu: () => null,
    useDefaultAssignee: () => 'w',
    useKanban: () => kanbanText
  }
})

vi.mock('./model-override', () => ({
  ModelOverrideField: () => null,
  overridePatch: () => ({})
}))

vi.mock('./types', () => ({
  SEVERITY_TONE: { info: 'info', warning: 'warning', error: 'error', critical: 'critical' }
}))

afterEach(cleanup)

it('strips ANSI from the worker log tail before it reaches LogView', async () => {
  const { TaskDrawer } = await import('./drawer')

  const { container, getByTestId } = render(
    <TaskDrawer columns={['todo', 'done']} id="t1" onClose={() => {}} onOpen={() => {}} />
  )

  const log = getByTestId('worker-log')

  // The rendered log region is clean plain text.
  expect(log.textContent).toBe(EXPECTED_LOG)
  // No escape byte anywhere in the rendered drawer output.
  expect(container.textContent ?? '').not.toContain('\u001b')
})
