/* global window, document, getComputedStyle */
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
// Run from any directory: node scripts/session-model-logo-smoke.mjs [output-directory]
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const out = resolve(process.argv[2] ?? resolve(root, 'e2e-artifacts/session-model-logos'))
await mkdir(out, { recursive: true })
const require = createRequire(root + '/package.json')
const { createServer } = await import(require.resolve('vite'))
const { chromium } = require('@playwright/test')
const entry = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import '/src/styles.css';
import { SidebarSessionRow } from '/src/app/chat/sidebar/session-row';
import { createClientSessionState } from '/src/lib/chat-runtime';
import { publishSessionState, clearAllSessionStates } from '/src/store/session-states';
import { $sessionListDensity } from '/src/store/session-list-density';
const models = ['gpt-6-astra','claude-sonnet-4.5','glm-5.2','gemini-2.5-pro','DeepSeek-R1','Qwen3-30B','grok-4','mistral-small','llama-3.3','Hermes-4','custom-model'];
const names = ['OpenAI','Anthropic','z.ai','Google','DeepSeek','Qwen','xAI','Mistral','Meta','Nous','Unknown'];
const noop = () => {};
for (const kind of ['compact','card']) {
  publishSessionState(kind+'-running', {...createClientSessionState(kind+'-1'), model:models[1], busy:true});
  publishSessionState(kind+'-waiting', {...createClientSessionState(kind+'-2'), model:models[2], busy:true, needsInput:true});
}
window.__switchLogo = () => publishSessionState('rt-first', {...createClientSessionState('compact-0'), model: 'glm-5.2', busy:true});
window.__density = value => $sessionListDensity.set(value);
window.__cleanup = clearAllSessionStates;
createRoot(document.getElementById('root')).render(React.createElement('div', {style:{display:'flex',gap:24}},
  [false,true].map(card=>React.createElement('section',{key:String(card),style:{width:320},'data-probe-section':card?'card':'compact'},
    React.createElement('h2',{style:{margin:'0 0 12px',fontSize:16}},card?'Card':'Compact'),
    ...models.map((model,i)=>React.createElement(SidebarSessionRow, {
      key:model, card, branchStem:(i===3||i===4)?'└─ ':undefined, reorderable:i===4, session:{id:(card?'card-':'compact-')+i,title:names[i]+' — '+(i===1?'working':i===2?'awaiting input':'topic title'),model,
      profile:'default', source:'desktop',last_active:Date.now()/1000,started_at:Date.now()/1000,ended_at:null,
      message_count:2,tool_call_count:0,input_tokens:10,output_tokens:10,is_active:false,
      preview:'Existing preview stays unchanged',cwd:'/example/project'},
      isPinned:false,isSelected:false,unread:false,onResume:()=>{window.__clicked=(window.__clicked||0)+1},
      onArchive:noop,onDelete:noop,onPin:noop,onToggleUnread:noop
    }))
  ))
));
`
const plugin = {
  name: 'isolated-logo-smoke',
  resolveId(id) {
    if (id === 'virtual:logo-smoke') return '\0logo-smoke'
  },
  load(id) {
    if (id === '\0logo-smoke') return entry
  },
  configureServer(server) {
    server.middlewares.use('/__logo_probe__', async (_req, res, next) => {
      try {
        const html = await server.transformIndexHtml(
          '/__logo_probe__',
          `<!doctype html><html class="dark"><head><meta charset="utf-8"></head><body style="padding:24px"><div id="root"></div><script type="module" src="/@id/virtual:logo-smoke"></script></body></html>`
        )
        res.setHeader('Content-Type', 'text/html')
        res.end(html)
      } catch (error) {
        next(error)
      }
    })
  }
}
let server, browser
try {
  server = await createServer({
    root,
    configFile: root + '/vite.config.ts',
    plugins: [plugin],
    server: { host: '127.0.0.1', port: 0, strictPort: true, open: false }
  })
  await server.listen()
  const origin = new URL(server.resolvedUrls.local[0])
  browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 740, height: 1180 }, deviceScaleFactor: 2 })
  const errors = [],
    blocked = []
  page.on('pageerror', e => errors.push(e.message))
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin.origin) return route.continue()
    blocked.push(route.request().url())
    return route.abort()
  })
  await page.goto(new URL('/__logo_probe__', origin).href)
  await page.waitForSelector('[data-model-brand]', { timeout: 30000 })
  const rows = await page.locator('[data-model-brand]').evaluateAll(els =>
    els.map(el => {
      const box = el.getBoundingClientRect(),
        css = getComputedStyle(el)
      const section = el.closest('section')
      const row = el.closest('[data-slot="row-button"]')
      const title = row.querySelector('.hover-marquee')?.getBoundingClientRect()
      const clipped = []
      for (let parent = el.parentElement; parent && parent !== row; parent = parent.parentElement) {
        const bounds = parent.getBoundingClientRect()
        if (
          ['hidden', 'clip'].includes(getComputedStyle(parent).overflowX) &&
          (box.left < bounds.left - 0.5 || box.right > bounds.right + 0.5)
        )
          clipped.push(parent.className)
      }
      return {
        clipped,
        brand: el.dataset.modelBrand,
        kind: section.dataset.probeSection,
        width: box.width,
        height: box.height,
        beforeTitle:
          !!title && (section.dataset.probeSection === 'card' ? box.bottom <= title.top : box.right <= title.left),
        gap: title ? title.left - box.right : null,
        status: el.closest('[data-session-state]')?.dataset.sessionState,
        mask: css.maskImage !== 'none',
        color: css.color,
        pointerEvents: css.pointerEvents,
        tooltip: !!el.closest('[data-slot="tooltip-trigger"]')
      }
    })
  )
  assert.equal(rows.length, 22)
  for (const row of rows) {
    assert.equal(row.width, 14)
    assert.equal(row.height, 14)
    assert(row.status)
    assert.deepEqual(row.clipped, [], JSON.stringify(row))
    assert(row.beforeTitle, JSON.stringify(row))
    assert.equal(row.pointerEvents, 'none')
    assert(!row.tooltip)
    if (row.brand !== 'unknown') assert(row.mask)
  }
  const logo = page.locator('[data-probe-section="compact"] [data-model-brand]').first()
  const box = await logo.boundingBox()
  await page.mouse.move(box.x + 7, box.y + 7)
  await page.waitForTimeout(600)
  assert.equal(await page.locator('[role="tooltip"]').count(), 0)
  await page.mouse.click(box.x + 7, box.y + 7)
  assert.equal(await page.evaluate(() => window.__clicked), 1)
  await page.mouse.move(720, 0)
  await page.screenshot({ path: out + '/logos-status.png', fullPage: true, animations: 'disabled' })
  await page.evaluate(() => window.__switchLogo())
  await page.waitForFunction(
    () => document.querySelector('[data-probe-section="compact"] [data-model-brand]').dataset.modelBrand === 'zai'
  )
  assert.equal(
    await page.locator('[data-probe-section="card"] [data-model-brand]').first().getAttribute('data-model-brand'),
    'openai'
  )
  for (const density of ['comfortable', 'detailed', 'compact']) {
    await page.evaluate(value => window.__density(value), density)
    assert.equal(await page.locator('[data-model-brand]').count(), 22)
  }
  assert.deepEqual(errors, [])
  assert.deepEqual(blocked, [])
  assert.equal(await page.locator('[data-session-state=working]').count(), 3)
  assert.equal(await page.locator('[data-session-state=needs-input]').count(), 2)
  await page.evaluate(() => window.__cleanup())
  const result = {
    passed: true,
    rows,
    liveSwitch: 'only matching row changed OpenAI → z.ai',
    hover: 'no added tooltip',
    click: 'existing resume handler',
    errors,
    blocked
  }
  await writeFile(out + '/browser-smoke.json', JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  await browser?.close()
  await server?.close()
}
