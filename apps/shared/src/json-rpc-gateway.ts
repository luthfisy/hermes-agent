import type { GatewayEvent, GatewayEventName } from './gateway-events.js'
import {
  DEFAULT_HEARTBEAT_DEADLINE_MS,
  DEFAULT_HEARTBEAT_INTERVAL_MS,
  type GatewayRequestId,
  JsonRpcRequestChannel,
  type JsonRpcRequestChannelOptions,
  type JsonRpcTransport,
  type ServerRequestHandler,
  wireFrameText
} from './json-rpc-channel.js'

export type { GatewayEvent, GatewayEventName } from './gateway-events.js'
export type ConnectionState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

/** Connection-handshake failure with optional WebSocket close metadata. */
export class GatewayConnectError extends Error {
  readonly wsCloseCode?: number
  readonly needsOauthLogin?: boolean

  constructor(message: string, options?: { wsCloseCode?: number; needsOauthLogin?: boolean }) {
    super(message)
    this.name = 'GatewayConnectError'
    this.wsCloseCode = options?.wsCloseCode
    this.needsOauthLogin = options?.needsOauthLogin
  }
}

export type WebSocketLike = WebSocket

type ConnectAttempt = {
  socket: WebSocketLike
  url: string
  promise: Promise<void>
  resolve: () => void
  reject: (error: Error) => void
  timer?: ReturnType<typeof setTimeout>
  settled: boolean
}
export interface GatewayClientOptions {
  authRejectedErrorMessage?: string
  closedErrorMessage?: string
  connectErrorMessage?: string
  connectTimeoutMs?: number
  /**
   * When `connect()` settles. `'gateway.ready'` (default) waits for that first
   * frame; any other first frame is a protocol failure. `'open'` settles on
   * the WebSocket `open` event — for sockets such as `/api/events` that never
   * send `gateway.ready`.
   */
  handshake?: 'gateway.ready' | 'open'
  createRequestId?: (nextId: number) => GatewayRequestId
  heartbeatDeadlineMs?: number
  heartbeatIntervalMs?: number
  /** A server→client request handler threw; the channel already answered `-32603`. */
  onRequestHandlerError?: JsonRpcRequestChannelOptions['onRequestHandlerError']
  /** No handler accepted a server→client request; the channel already answered `-32601`. */
  onUnhandledRequest?: JsonRpcRequestChannelOptions['onUnhandledRequest']
  /** Return true to intercept the default closed-state transition. */
  onSocketClose?: (event: { code: number }) => boolean | void
  /** Fetch `session.events.since` after a reconnect (default). Off for notification-only feeds whose peer never answers RPCs. */
  replay?: boolean
  requestIdPrefix?: string
  requestTimeoutMs?: number
  socketFactory?: (url: string) => WebSocketLike
  notConnectedErrorMessage?: string
}

const ANY = '*'
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000

const isGatewayReady = (event: GatewayEvent): event is GatewayEvent<'gateway.ready'> => event.type === 'gateway.ready'
// Replay fetch after reconnect: bounded so a wedged backend can't hold the
// guard open; generous enough for a 512-frame ring to drain.
const REPLAY_REQUEST_TIMEOUT_MS = 10_000
// A reconnect after sleep/wake must not hang forever in 'connecting' (which
// keeps the composer disabled and stuck on "Starting Hermes..."). If the open
// handshake doesn't land in this window, fail to 'error' so callers can retry.
const DEFAULT_CONNECT_TIMEOUT_MS = 15_000

/** True for a `ws://` / `wss://` URL string — the only thing `JsonRpcGatewayClient.connect()` will dial. */
export function isGatewayWebSocketUrl(value: unknown): value is string {
  if (typeof value !== 'string') {return false}

  try {
    const protocol = new URL(value).protocol

    return protocol === 'ws:' || protocol === 'wss:'
  } catch {
    return false
  }
}

/**
 * Typed fan-out of gateway `event` notifications: per-type handlers plus a
 * `*` wildcard. Shared by the WebSocket client below and the Ink TUI's stdio
 * client so both dispatch the same way.
 */
export class GatewayEventHub {
  private readonly handlers = new Map<string, Set<(event: GatewayEvent) => void>>()

  on<K extends GatewayEventName>(type: K, handler: (event: GatewayEvent<K>) => void): () => void {
    let set = this.handlers.get(type)

    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }

    set.add(handler as (event: GatewayEvent) => void)

    return () => set?.delete(handler as (event: GatewayEvent) => void)
  }

  onAny(handler: (event: GatewayEvent) => void): () => void {
    // ANY is a client-side wildcard, not a wire name; it never reaches the typed map.
    return this.on(ANY as GatewayEventName, handler as (event: GatewayEvent<GatewayEventName>) => void)
  }

  dispatch(event: GatewayEvent): void {
    for (const handler of this.handlers.get(event.type) ?? []) {
      handler(event)
    }

    for (const handler of this.handlers.get(ANY) ?? []) {
      handler(event)
    }
  }
}

/**
 * Bring a `JsonRpcRequestChannel` to a raw text sink — a WebSocket here, a
 * child's stdin in the TUI. Kept separate from the socket so the channel never
 * holds a reference to a specific socket generation.
 */
const socketTransport = (socket: WebSocketLike): JsonRpcTransport => ({ send: text => socket.send(text) })

export class JsonRpcGatewayClient {
  private socket: WebSocketLike | null = null
  private state: ConnectionState = 'idle'
  private readonly channel: JsonRpcRequestChannel
  private readonly events = new GatewayEventHub()
  /** Last observed event seq per session_id — drives lossless reconnect replay. */
  private lastSeenSeq = new Map<string, number>()
  /** Set while a post-reconnect replay fetch is in flight (dedup guard). */
  private replayInFlight = false
  /** Invalidates an interrupted replay so its async cleanup cannot own a replacement socket. */
  private replayGeneration = 0
  /**
   * While a replay fetch is in flight, live seq'd frames for the sessions
   * being replayed are parked here instead of dispatching immediately.
   * Without this hold, a live frame racing the replay response is dispatched
   * twice (once live, once when the replay returns the same seq) or, worse,
   * advances the watermark so the gap events the replay carries get skipped.
   */
  private replayHold: Map<string, GatewayEvent[]> | null = null
  /**
   * Server process identity for the replay contract (from gateway.ready /
   * session.events.since). Seq counters are in-process on the backend, so a
   * restart resets them while we still hold high watermarks — without this
   * check events_since(sid, 97) returns [] + truncated=false forever and we
   * silently believe nothing was missed.
   */
  private replayEpoch: string | null = null
  private attempt: ConnectAttempt | null = null
  private readonly stateHandlers = new Set<(state: ConnectionState) => void>()
  private readonly options: Required<
    Omit<GatewayClientOptions, 'onRequestHandlerError' | 'onUnhandledRequest' | 'socketFactory'>
  > &
    Pick<GatewayClientOptions, 'onRequestHandlerError' | 'onUnhandledRequest' | 'socketFactory'>

  constructor(options: GatewayClientOptions = {}) {
    const connectErrorMessage = options.connectErrorMessage ?? 'WebSocket connection failed'

    this.options = {
      authRejectedErrorMessage: options.authRejectedErrorMessage ?? connectErrorMessage,
      closedErrorMessage: options.closedErrorMessage ?? 'WebSocket closed',
      connectErrorMessage,
      connectTimeoutMs: options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS,
      handshake: options.handshake ?? 'gateway.ready',
      createRequestId: options.createRequestId ?? ((nextId: number) => `${options.requestIdPrefix ?? 'r'}${nextId}`),
      heartbeatDeadlineMs: options.heartbeatDeadlineMs ?? DEFAULT_HEARTBEAT_DEADLINE_MS,
      heartbeatIntervalMs: options.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS,
      notConnectedErrorMessage: options.notConnectedErrorMessage ?? 'gateway not connected',
      onSocketClose: options.onSocketClose ?? (() => false),
      replay: options.replay ?? true,
      requestIdPrefix: options.requestIdPrefix ?? 'r',
      onRequestHandlerError: options.onRequestHandlerError,
      onUnhandledRequest: options.onUnhandledRequest,
      requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      socketFactory: options.socketFactory
    }
    this.channel = new JsonRpcRequestChannel({
      createRequestId: this.options.createRequestId,
      heartbeatDeadlineMs: this.options.heartbeatDeadlineMs,
      heartbeatIntervalMs: this.options.heartbeatIntervalMs,
      // Desktop/web have always counted any inbound frame as liveness; the
      // TUI (stdio/attach owner) keeps its stricter pong-based contract.
      heartbeatLiveness: 'any-inbound',
      onEvent: event => this.handleEvent(event),
      onHeartbeatFailure: error => this.invalidate(error.message),
      onRequestHandlerError: this.options.onRequestHandlerError,
      onUnhandledRequest: this.options.onUnhandledRequest,
      requestTimeoutMs: this.options.requestTimeoutMs
    })
  }

  get connectionState(): ConnectionState {
    return this.state
  }

  connect(wsUrl: string): Promise<void> {
    // Refuse garbage; WebSocket coerces non-strings into
    // `ws://<origin>/[object%20Object]` (#68250 stale-emit boot loop).
    const invalidUrl = () => {
      const got = typeof wsUrl === 'string' ? JSON.stringify(wsUrl) : `type "${typeof wsUrl}"`

      return new Error(`gateway connect() requires a ws:// or wss:// URL string, got ${got}`)
    }

    if (!isGatewayWebSocketUrl(wsUrl)) {
      return Promise.reject(invalidUrl())
    }

    if (this.state === 'open' && this.socket?.readyState === WebSocket.OPEN) {
      return Promise.resolve()
    }

    if (this.attempt && !this.attempt.settled) {
      if (this.attempt.url === wsUrl) {
        return this.attempt.promise
      }

      return Promise.reject(new Error('gateway connect() already in progress'))
    }

    this.setState('connecting')

    let socket: WebSocketLike

    try {
      socket = this.options.socketFactory?.(wsUrl) ?? new WebSocket(wsUrl)
    } catch {
      this.setState('error')

      return Promise.reject(this.connectFailure('WebSocket error before open'))
    }

    const transport = socketTransport(socket)
    this.socket = socket
    this.channel.stopHeartbeat()

    let resolveAttempt!: () => void
    let rejectAttempt!: (error: Error) => void

    const promise = new Promise<void>((resolve, reject) => {
      resolveAttempt = resolve
      rejectAttempt = reject
    })

    const attempt: ConnectAttempt = {
      socket,
      url: wsUrl,
      promise,
      resolve: resolveAttempt,
      reject: rejectAttempt,
      settled: false
    }

    this.attempt = attempt

    const onOpen = () => {
      if (this.socket !== socket || this.attempt !== attempt || attempt.settled) {
        return
      }

      // A raw WebSocket open is only transport readiness. The connection stays
      // in 'connecting' until the gateway identifies itself with gateway.ready.
      if (this.options.handshake !== 'open') {
        return
      }

      if (!this.settleConnectAttempt(attempt)) {
        return
      }

      this.channel.attach(transport)
      this.setState('open')
      attempt.resolve()

      // Lossless resume: drain events emitted while we were disconnected.
      // Fire-and-forget so connect() latency is unaffected; only runs when
      // we actually observed seq'd events before the drop.
      void this.fetchReplay()
    }

    const onError = () => {
      if (this.socket !== socket || this.attempt !== attempt || attempt.settled) {
        return
      }

      if (!this.settleConnectAttempt(attempt)) {
        return
      }

      this.setState('error')
      attempt.reject(this.connectFailure('WebSocket error before open'))
    }

    socket.addEventListener('message', message => {
      if (this.socket !== socket) {
        return
      }

      const parsed = this.parseMessage(message.data)

      if (this.attempt === attempt && !attempt.settled && this.options.handshake === 'gateway.ready') {
        const frame = parsed?.frame

        if (parsed && frame?.method === 'event' && frame.params?.type === 'gateway.ready') {
          if (!this.settleConnectAttempt(attempt)) {
            return
          }

          this.channel.attach(transport)
          this.setState('open')
          this.channel.handleFrame(parsed.text)
          attempt.resolve()

          // Lossless resume: drain events emitted while we were disconnected.
          // Fire-and-forget so connect() latency is unaffected; only runs when
          // we actually observed seq'd events before the drop.
          void this.fetchReplay()

          return
        }

        if (!this.settleConnectAttempt(attempt)) {
          return
        }

        this.setState('error')

        try {
          socket.close()
        } catch {
          // ignore
        } finally {
          if (this.socket === socket) {
            this.socket = null
          }
        }

        attempt.reject(new GatewayConnectError(this.options.connectErrorMessage))

        return
      }

      if (parsed) {
        this.channel.handleFrame(parsed.text)
      }
    })

    socket.addEventListener('close', event => {
      if (this.socket !== socket) {
        return
      }

      if (this.attempt === attempt && !attempt.settled) {
        if (!this.settleConnectAttempt(attempt)) {
          return
        }

        this.socket = null
        this.setState('closed')

        const needsOauthLogin = event.code === 4401
        attempt.reject(
          this.connectFailure(
            `WebSocket closed during handshake: code ${event.code}${event.reason ? ` ${event.reason}` : ''}`,
            {
              wsCloseCode: event.code,
              needsOauthLogin: needsOauthLogin || undefined
            },
            needsOauthLogin ? this.options.authRejectedErrorMessage : this.options.connectErrorMessage
          )
        )

        return
      }

      // onSocketClose is an established-connection interception hook. Handshake
      // closes are classified above and never flow through it.
      if (this.state === 'open') {
        if (this.options.onSocketClose(event)) {
          return
        }

        this.dropSocket(new Error(this.options.closedErrorMessage))

        return
      }

      // A failed handshake may close after its error/protocol-failure path has
      // already settled. Release that socket without overwriting 'error'.
      this.socket = null
      this.channel.stopHeartbeat()
    })

    socket.addEventListener('open', onOpen, { once: true })
    socket.addEventListener('error', onError, { once: true })

    if (this.options.connectTimeoutMs > 0) {
      attempt.timer = setTimeout(() => {
        if (this.socket !== socket || this.attempt !== attempt || attempt.settled) {
          return
        }

        if (!this.settleConnectAttempt(attempt)) {
          return
        }

        // Drop the half-open socket so the next connect() starts clean
        // instead of short-circuiting on a zombie 'connecting' state.
        try {
          socket.close()
        } catch {
          // ignore
        } finally {
          if (this.socket === socket) {
            this.socket = null
            this.setState('error')
          }
        }

        this.setState('error')
        attempt.reject(this.connectFailure(`no WebSocket open within ${this.options.connectTimeoutMs} ms`))
      }, this.options.connectTimeoutMs)
    }

    return promise
  }

  private connectFailure(
    detail: string,
    options?: { wsCloseCode?: number; needsOauthLogin?: boolean },
    message = this.options.connectErrorMessage
  ): GatewayConnectError {
    return new GatewayConnectError(`${message} (${detail})`, options)
  }

  close(): void {
    const attempt = this.attempt

    // Settle a pending attempt eagerly before invalidate() drops the socket.
    // A raw-open socket was a real transport waiting on gateway.ready; a
    // CONNECTING socket never established one.
    if (attempt && !attempt.settled && this.settleConnectAttempt(attempt)) {
      this.setState('closed')
      attempt.reject(
        new GatewayConnectError(
          this.socket?.readyState === WebSocket.OPEN
            ? this.options.closedErrorMessage
            : this.options.connectErrorMessage
        )
      )
    }

    this.invalidate()
  }

  /**
   * Invalidate the current socket generation after an ambiguous transport
   * outcome. The outer connection owner decides whether/when to reconnect.
   */
  invalidate(message = this.options.closedErrorMessage): void {
    const socket = this.socket

    if (!socket) {
      return
    }

    // Drop the generation BEFORE closing: a synchronous `close` event from
    // the socket must hit the identity guard and not run the default
    // closed-path a second time on top of whatever the owner redialed.
    this.dropSocket(new Error(message))

    try {
      socket.close()
    } catch {
      // The generation was already invalidated; the reconnect owner can redial.
    }
  }

  on<K extends GatewayEventName>(type: K, handler: (event: GatewayEvent<K>) => void): () => void {
    return this.events.on(type, handler)
  }

  onAny(handler: (event: GatewayEvent) => void): () => void {
    return this.events.onAny(handler)
  }

  onEvent(handler: (event: GatewayEvent) => void): () => void {
    return this.onAny(handler)
  }

  /**
   * Server→client requests (clarify, approval, sudo, …). Live frames and
   * `open_requests` re-delivered after a reconnect both arrive here; the
   * latter carry `replayed: true`.
   */
  onRequest(handler: ServerRequestHandler): () => void {
    return this.channel.onRequest(handler)
  }

  onState(handler: (state: ConnectionState) => void): () => void {
    this.stateHandlers.add(handler)
    handler(this.state)

    return () => this.stateHandlers.delete(handler)
  }

  request<T>(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs = this.options.requestTimeoutMs,
    signal?: AbortSignal
  ): Promise<T> {
    const socket = this.socket

    if (!socket || this.state !== 'open' || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error(this.options.notConnectedErrorMessage))
    }

    return this.channel.request<T>(method, params, timeoutMs, signal, () => new Error(this.options.notConnectedErrorMessage))
  }

  private parseMessage(raw: unknown): {
    frame: { method?: unknown; params?: GatewayEvent }
    text: string
  } | null {
    const text = wireFrameText(raw)

    if (text === null) {
      return null
    }

    let parsed: unknown

    try {
      parsed = JSON.parse(text)
    } catch {
      return null
    }

    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return null
    }

    return {
      frame: parsed as { method?: unknown; params?: GatewayEvent },
      text
    }
  }

  private handleEvent(event: GatewayEvent): void {
    if (isGatewayReady(event)) {
      if (event.payload?.heartbeat === true) {
        this.channel.startHeartbeat()
      }

      const epoch = event.payload?.replay_epoch

      if (typeof epoch === 'string' && epoch) {
        this.adoptReplayEpoch(epoch)
      }
    }

    const sid = event.session_id
    const seqValue = event.seq

    if (this.replayHold && sid && typeof seqValue === 'number' && this.replayHold.has(sid)) {
      // Replay in flight for this session: park the frame; flushReplayHold
      // dispatches it after the replayed gap, gated on seq.
      this.replayHold.get(sid)?.push(event)

      return
    }

    this.recordSeq(event)
    this.dispatchEvent(event)
  }

  /**
   * Track each session's last observed event seq. Events without a seq
   * (legacy backend, session-less globals) leave the map untouched.
   */
  private recordSeq(event: GatewayEvent): void {
    const sid = event.session_id
    const seq = event.seq

    if (!sid || typeof seq !== 'number' || !Number.isFinite(seq)) {
      return
    }

    const prev = this.lastSeenSeq.get(sid) ?? 0

    if (seq > prev) {
      this.lastSeenSeq.set(sid, seq)
    }
  }

  /** Test/telemetry hook: current last-seen seq map snapshot. */
  getSeqWatermarks(): Record<string, number> {
    return Object.fromEntries(this.lastSeenSeq)
  }

  /**
   * After a reconnect, ask the gateway to replay every event newer than our
   * per-session watermarks. Replayed frames go through the SAME dispatchEvent
   * path as live frames — dedupe happens naturally because recordSeq ignores
   * non-increasing seqs and downstream stores key on event identity.
   * Best-effort: failures are swallowed (the next reconnect retries).
   */
  private async fetchReplay(): Promise<void> {
    if (!this.options.replay || this.replayInFlight || this.lastSeenSeq.size === 0) {
      return
    }

    this.replayInFlight = true
    const replayGeneration = ++this.replayGeneration
    // Park live frames for the sessions we're about to replay so a frame
    // racing the replay response can't dispatch ahead of (or duplicate) the
    // gap events. Sessions without watermarks are unaffected.
    const hold = new Map<string, GatewayEvent[]>()

    for (const sid of this.lastSeenSeq.keys()) {
      hold.set(sid, [])
    }

    this.replayHold = hold

    try {
      const entries = Object.entries(this.getSeqWatermarks())

      // One RPC per known session keeps params flat; sessions are few (<20).
      const results = await Promise.allSettled(
        entries.map(([sid, lastSeen]) =>
          // `open_requests` on the answer are re-delivered by the channel itself.
          this.request<{ events?: Array<{ type: string; session_id?: string; seq?: number; payload?: unknown }> }>(
            'session.events.since',
            { session_id: sid, last_seen: lastSeen },
            REPLAY_REQUEST_TIMEOUT_MS
          )
        )
      )

      // The socket that owned this replay was dropped while its requests were
      // settling. Its results and cleanup must not consume the replacement
      // socket's replay window.
      if (this.replayGeneration !== replayGeneration) {
        return
      }

      for (const result of results) {
        if (result.status !== 'fulfilled' || !Array.isArray(result.value?.events)) {
          continue
        }

        const epoch = (result.value as { epoch?: unknown }).epoch

        if (typeof epoch === 'string' && epoch && this.replayEpoch && epoch !== this.replayEpoch) {
          // Backend restarted: its seq numbering reset, so our watermarks —
          // and this replay window — are meaningless. Drop them and start
          // fresh under the new epoch.
          this.adoptReplayEpoch(epoch)

          continue
        }

        if (typeof epoch === 'string' && epoch && !this.replayEpoch) {
          this.replayEpoch = epoch
        }

        for (const event of result.value.events) {
          if (!event?.type) {
            continue
          }

          this.dispatchIfNewer(event as GatewayEvent)
        }
      }
    } catch {
      // Replay is an optimization over lossy-reconnect; never surface errors.
    } finally {
      if (this.replayGeneration === replayGeneration) {
        this.flushReplayHold()
        this.replayInFlight = false
      }
    }
  }

  /**
   * Dispatch an event only when its seq advances the session watermark.
   * Seq-less events always dispatch (no ordering contract to violate).
   */
  private dispatchIfNewer(event: GatewayEvent): void {
    const sid = event.session_id
    const seq = event.seq

    if (sid && typeof seq === 'number' && Number.isFinite(seq)) {
      const prev = this.lastSeenSeq.get(sid) ?? 0

      if (seq <= prev) {
        return
      }

      this.lastSeenSeq.set(sid, seq)
    }

    this.dispatchEvent(event)
  }

  /**
   * Record the server's replay epoch; on change (backend restart) the old
   * seq watermarks describe a numbering that no longer exists — clear them
   * so the next reconnect doesn't silently believe it missed nothing.
   */
  private adoptReplayEpoch(epoch: string): void {
    if (this.replayEpoch === epoch) {
      return
    }

    if (this.replayEpoch !== null) {
      this.lastSeenSeq.clear()
    }

    this.replayEpoch = epoch
  }

  /** Release frames parked during a replay fetch, seq-gated against dupes. */
  private flushReplayHold(): void {
    const hold = this.replayHold
    this.replayHold = null

    if (!hold) {
      return
    }

    for (const parked of hold.values()) {
      for (const event of parked) {
        this.dispatchIfNewer(event)
      }
    }
  }

  private settleConnectAttempt(attempt: ConnectAttempt): boolean {
    if (attempt.settled || this.attempt !== attempt) {
      return false
    }

    attempt.settled = true

    if (attempt.timer !== undefined) {
      clearTimeout(attempt.timer)
      attempt.timer = undefined
    }

    this.attempt = null

    return true
  }

  /** Forget the current socket generation, fail its calls, and go 'closed'. */
  private dropSocket(error: Error): void {
    // A replay belongs to the socket that started it. Detaching that socket
    // rejects its requests asynchronously, so clear its ownership now; the
    // next open can immediately schedule a replay of its own.
    this.replayGeneration += 1
    this.replayInFlight = false
    this.replayHold = null
    this.socket = null
    this.channel.detach(error)
    this.setState('closed')
  }

  private dispatchEvent(event: GatewayEvent): void {
    this.events.dispatch(event)
  }

  private setState(state: ConnectionState): void {
    if (this.state === state) {
      return
    }

    this.state = state

    for (const handler of this.stateHandlers) {
      handler(state)
    }
  }
}
