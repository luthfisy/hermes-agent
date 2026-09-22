import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { bindApi, taskKey } from './api'
import { TaskDrawer } from './drawer'
import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail } from './types'

vi.mock('@/hermes', () => ({ setApiRequestProfile: vi.fn() }))

const legacyDetail: Omit<KanbanTaskDetail, 'attachments'> = {
  task: { id: 't_example', title: 'Example task', body: 'Keep this description readable.', status: 'todo' },
  comments: [{ id: 1, author: 'test', body: 'Keep this comment readable.', created_at: 0 }],
  events: [],
  links: { parents: [], children: [] },
  goal_configuration_locked: false,
  runs: []
}

let detail: object
let client: QueryClient
let disposeApi: () => void
let disposeLocales: () => void

const rest = vi.fn(async (path: string, options?: PluginRestOptions): Promise<unknown> => {
  if (path === '/tasks/t_example/attachments' && options?.method === 'POST') {
    detail = { ...legacyDetail, attachments: [{ id: 1, filename: options.upload?.filename }] }

    return { ok: true }
  }

  if (path === '/tasks/t_example') {
    if (options?.method === 'PATCH') {
      const patch = options.body as Record<string, unknown>
      const current = detail as KanbanTaskDetail
      detail = { ...current, task: { ...current.task, ...patch } }
      return { task: (detail as KanbanTaskDetail).task }
    }

    return detail
  }

  if (path.startsWith('/tasks/t_example/log?')) {
    return { exists: false, content: '', size_bytes: 0, truncated: false }
  }

  if (path === '/profiles') {
    return { profiles: [] }
  }

  if (path === '/orchestration') {
    return { default_assignee: '' }
  }

  throw new Error(`Unexpected REST request: ${path}`)
})

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  disposeApi = bindApi(
    async <T,>(path: string, options?: PluginRestOptions) => (await rest(path, options)) as T,
    { get: (_key, fallback) => fallback, set: vi.fn(), remove: vi.fn() },
    () => vi.fn()
  )
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeApi()
  disposeLocales()
  vi.clearAllMocks()
})

function openDrawer() {
  return render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['todo', 'ready', 'done']} id="t_example" onClose={vi.fn()} onOpen={vi.fn()} />
    </QueryClientProvider>
  )
}

describe('task attachment compatibility', () => {
  it.each([{}, { attachments: null }])(
    'keeps older task details usable without attachment controls (%j)',
    async extra => {
      detail = { ...legacyDetail, ...extra }
      openDrawer()

      expect(await screen.findByRole('heading', { name: legacyDetail.task.title })).toBeTruthy()
      expect(screen.getByText(legacyDetail.task.body!)).toBeTruthy()
      expect(screen.getByText(legacyDetail.comments[0].body)).toBeTruthy()
      expect(screen.queryByRole('button', { name: en.uploadAttachment })).toBeNull()
      expect(screen.queryByText(en.noAttachments)).toBeNull()

      // A later backend response restores the capability without remounting.
      detail = { ...legacyDetail, attachments: [] }
      await act(() => client.invalidateQueries({ queryKey: taskKey('local', '', legacyDetail.task.id) }))
      expect(await screen.findByRole('button', { name: en.uploadAttachment })).toBeTruthy()
      expect(screen.getByText(en.noAttachments)).toBeTruthy()
    }
  )

  it('keeps upload and attachment rendering working for a supported empty list', async () => {
    detail = { ...legacyDetail, attachments: [] }
    const { container } = openDrawer()
    const upload = await screen.findByRole('button', { name: en.uploadAttachment })
    expect(screen.getByText(en.noAttachments)).toBeTruthy()

    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!
    const click = vi.spyOn(input, 'click')
    fireEvent.click(upload)
    expect(click).toHaveBeenCalledOnce()

    const file = new File(['example'], 'example.txt', { type: 'text/plain' })
    const bytes = new ArrayBuffer(7)
    // jsdom's File lacks arrayBuffer; the upload still uses the real REST adapter.
    Object.defineProperty(file, 'arrayBuffer', { value: async () => bytes })
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/tasks/t_example/attachments', {
        method: 'POST',
        upload: { filename: file.name, contentType: file.type, bytes }
      })
    )
    expect(await screen.findByText(file.name)).toBeTruthy()
    expect(screen.queryByText(en.noAttachments)).toBeNull()
  })
})

describe('goal configuration', () => {
  it('sends controlled goal and budget patches, and follows a refreshed task', async () => {
    detail = {
      ...legacyDetail,
      attachments: [],
      task: { ...legacyDetail.task, goal_mode: false, goal_max_turns: 7 }
    }
    openDrawer()

    const toggle = await screen.findByRole('switch', { name: en.goalMode })
    const budget = screen.getByRole<HTMLInputElement>('spinbutton', { name: en.goalTurnBudget })
    expect(budget.value).toBe('7')
    fireEvent.click(toggle)
    await waitFor(() => expect(rest).toHaveBeenCalledWith('/tasks/t_example', { method: 'PATCH', body: { goal_mode: true } }))

    fireEvent.change(budget, { target: { value: '12' } })
    fireEvent.blur(budget)
    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/tasks/t_example', { method: 'PATCH', body: { goal_max_turns: 12 } })
    )

    fireEvent.change(budget, { target: { value: '' } })
    fireEvent.blur(budget)
    await waitFor(() =>
      expect(rest).toHaveBeenCalledWith('/tasks/t_example', { method: 'PATCH', body: { goal_max_turns: null } })
    )

    detail = { ...detail, task: { ...(detail as KanbanTaskDetail).task, goal_mode: true, goal_max_turns: 18 } }
    await act(() => client.invalidateQueries({ queryKey: taskKey('local', '', legacyDetail.task.id) }))
    await waitFor(() => expect(budget.value).toBe('18'))
  })

  it('rejects invalid budget input locally and disables goal controls after a run', async () => {
    detail = {
      ...legacyDetail,
      attachments: [],
      task: { ...legacyDetail.task, goal_mode: true, goal_max_turns: 7 }
    }
    openDrawer()

    const budget = await screen.findByRole<HTMLInputElement>('spinbutton', { name: en.goalTurnBudget })
    fireEvent.change(budget, { target: { value: '0' } })
    fireEvent.blur(budget)
    expect(budget.value).toBe('7')
    expect(rest).not.toHaveBeenCalledWith('/tasks/t_example', { method: 'PATCH', body: { goal_max_turns: 0 } })

    detail = {
      ...(detail as KanbanTaskDetail),
      goal_configuration_locked: true,
      runs: [{ id: 1, status: 'completed' }]
    }
    await act(() => client.invalidateQueries({ queryKey: taskKey('local', '', legacyDetail.task.id) }))
    const toggle = await screen.findByRole('switch', { name: en.goalMode })
    expect((toggle as HTMLButtonElement).disabled).toBe(true)
    expect(budget.disabled).toBe(true)
  })

  it('visibly disables goal mode while preserving its budget after the PATCH succeeds', async () => {
    detail = {
      ...legacyDetail,
      attachments: [],
      task: { ...legacyDetail.task, goal_mode: true, goal_max_turns: 50 }
    }
    openDrawer()

    const toggle = await screen.findByRole('switch', { name: en.goalMode })
    fireEvent.click(toggle)
    await waitFor(() => expect(rest).toHaveBeenCalledWith('/tasks/t_example', { method: 'PATCH', body: { goal_mode: false } }))
    await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByRole<HTMLInputElement>('spinbutton', { name: en.goalTurnBudget }).value).toBe('50')
  })

  it('restores authoritative goal values when a PATCH is rejected', async () => {
    detail = {
      ...legacyDetail,
      attachments: [],
      task: { ...legacyDetail.task, goal_mode: true, goal_max_turns: 50 }
    }
    openDrawer()

    const toggle = await screen.findByRole('switch', { name: en.goalMode })
    rest.mockRejectedValueOnce(new Error('goal settings are locked'))
    fireEvent.click(toggle)

    await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('true'))
    expect(screen.getByRole<HTMLInputElement>('spinbutton', { name: en.goalTurnBudget }).value).toBe('50')
  })
})
