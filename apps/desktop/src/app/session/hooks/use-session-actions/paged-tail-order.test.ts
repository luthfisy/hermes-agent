import { describe, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, textPart } from '@/lib/chat-messages'

import { appendLiveSessionProjection, preserveLocalPendingTurnMessages } from './utils'

const msg = (id: string, role: ChatMessage['role'], text: string, timestamp: number): ChatMessage => ({
  id, role, parts: [textPart(text)], timestamp
})

const prompt = () => msg('user-local', 'user', 'Inspect', 100)
const reply = () => ({ ...msg('assistant-stream-local', 'assistant', 'Done.', 110), pending: false })
const queued = (text = 'Next') => msg('user-queued-session', 'user', text, 105)
const ids = (messages: ChatMessage[]) => messages.map(message => message.id)

const folded = (): ChatMessage => ({
  id: 'row-folded', role: 'assistant', timestamp: 101,
  parts: [
    textPart('Checking the file.'),
    { type: 'tool-call', toolCallId: 'call-1', toolName: 'read_file', result: 'contents' },
    textPart('Done.')
  ]
})

const runningPage = (): ChatMessage[] => [{ ...folded(), interim: true }]
const localStream = (): ChatMessage => ({ ...folded(), id: 'assistant-stream-local', pending: true })

describe('page-omitted warm prompts', () => {
  it('anchors a text-only final reply folded into a tool bubble, including repeated hydration and backfill', () => {
    const user = prompt()
    const page = [folded()]
    const result = preserveLocalPendingTurnMessages(page, [user, reply()])
    expect(ids(result)).toEqual([user.id, page[0].id])
    expect(ids(preserveLocalPendingTurnMessages(page, result))).toEqual(ids(result))
    const backfilled = [msg('row-user', 'user', 'Inspect', 100), ...page]
    expect(ids(preserveLocalPendingTurnMessages(backfilled, result))).toEqual(ids(backfilled))
  })

  it('restores a completed prompt while leaving a genuinely queued prompt after the reply', () => {
    const user = prompt()
    const next = queued()
    const page = [msg('row-reply', 'assistant', 'Done.', 110)]
    const result = preserveLocalPendingTurnMessages(page, [user, reply(), next])
    expect(ids(result)).toEqual([user.id, page[0].id, next.id])
  })

  it('does not let a projected queued row block the completed-turn anchor', () => {
    const user = prompt()
    const next = queued()
    const page = [msg('row-reply', 'assistant', 'Done.', 110), next]
    expect(ids(preserveLocalPendingTurnMessages(page, [user, reply(), next])))
      .toEqual([user.id, page[0].id, next.id])
  })

  it.each(['Next', 'Inspect'])('keeps a running prompt before tool activity with queued text %s', text => {
    const user = prompt()
    const next = queued(text)
    const page = runningPage()
    const projection = {
      session_id: 'session',
      inflight: { user: 'Inspect', assistant: 'Working.', streaming: true },
      queued: { user: text }
    }
    let cached = [user, localStream(), next]
    for (let resume = 0; resume < 3; resume += 1) {
      cached = appendLiveSessionProjection(page, projection, cached)
      expect(ids(cached)).toEqual([user.id, page[0].id, next.id])
      expect(cached[0]).toBe(user)
    }
    expect(ids(page)).toEqual(['row-folded'])
  })

  it.each(['Inspect', ''])('does not restore a different attachment with the same visible text %j', text => {
    const cached = { ...prompt(), parts: [textPart(text)], attachmentRefs: ['@image:/old.png'] }
    const wireText = `@image:/new.png\n${text}`.trim()
    const result = appendLiveSessionProjection(runningPage(), {
      session_id: 'session', inflight: { user: wireText, streaming: true }
    }, [cached, localStream()])
    expect(result.some(message => message.id === cached.id)).toBe(false)
    expect(chatMessageText(result.find(message => message.id === 'user-inflight-session')!)).toBe(wireText)
  })

  it('does not treat reordered image attachments as the same prompt', () => {
    const user = { ...prompt(), attachmentRefs: ['@image:/first.png', '@image:/second.png'] }
    const result = appendLiveSessionProjection(runningPage(), {
      session_id: 'session',
      inflight: { user: '@image:/second.png\n@image:/first.png\nInspect', streaming: true }
    }, [user, localStream()])
    expect(result.some(message => message.id === user.id)).toBe(false)
  })

  it('matches equivalent reference chips and wire lines without duplicating the prompt', () => {
    const user = { ...prompt(), attachmentRefs: ['@image:/same.png'] }
    const page = runningPage()
    const result = appendLiveSessionProjection(page, {
      session_id: 'session', inflight: { user: '@image:/same.png\nInspect', streaming: true }
    }, [user, localStream()])
    expect(ids(result)).toEqual([user.id, page[0].id])
    expect(result[0].attachmentRefs).toEqual(user.attachmentRefs)
  })

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])('rejects invalid cached send boundary %s', timestamp => {
    const page = runningPage()
    const result = appendLiveSessionProjection(page, {
      session_id: 'session', inflight: { user: 'Inspect', streaming: true }
    }, [{ ...prompt(), timestamp }, localStream()])
    expect(result[0]).toBe(page[0])
  })

  it('rejects a non-finite hydrated timestamp as evidence of a newer page', () => {
    const page = [{ ...folded(), timestamp: Number.POSITIVE_INFINITY, interim: true }]
    const result = appendLiveSessionProjection(page, {
      session_id: 'session', inflight: { user: 'Inspect', streaming: true }
    }, [prompt(), localStream()])
    expect(result[0]).toBe(page[0])
  })

  it('keeps an uncommitted prompt after an older page', () => {
    const page = [msg('row-old', 'assistant', 'Done.', 90)]
    const result = appendLiveSessionProjection(page, {
      session_id: 'session', inflight: { user: 'Inspect', streaming: true }
    }, [prompt()])
    expect(ids(result)).toEqual(['row-old', 'user-inflight-session', 'assistant-stream-session'])
  })

  it('does not anchor to an older identical final reply', () => {
    const page = [msg('row-old', 'assistant', 'Done.', 90), msg('row-other', 'assistant', 'Other', 115)]
    expect(ids(preserveLocalPendingTurnMessages(page, [prompt(), reply()])))
      .toEqual([...ids(page), 'user-local'])
  })

  it('does not cross a later persisted user to find an identical reply', () => {
    const page = [msg('row-other-user', 'user', 'Other', 102), msg('row-reply', 'assistant', 'Done.', 110)]
    expect(ids(preserveLocalPendingTurnMessages(page, [prompt(), reply()])))
      .toEqual([...ids(page), 'user-local'])
  })

  it('does not use a later local turn as evidence for an earlier omitted prompt', () => {
    const later = msg('user-later', 'user', 'Other', 200)
    const page = [msg('row-later-reply', 'assistant', 'Done.', 210)]
    expect(ids(preserveLocalPendingTurnMessages(page, [prompt(), reply(), later])))
      .toEqual([...ids(page), 'user-local', later.id])
  })
})
