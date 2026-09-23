import { JSDOM } from 'jsdom'
import { afterEach, expect, test, vi } from 'vitest'
import updateUi from '../tests/install/e2e-assets/update-ui.cjs'

const windows = []
afterEach(() => {
  windows.splice(0).forEach(window => window.close())
  vi.useRealTimers()
})

function fixture({ details = true, available = true } = {}) {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(0)
  const { window } = new JSDOM('<body></body>', { runScripts: 'outside-only' })
  windows.push(window)
  const { document } = window
  const clicks = []
  const addButton = (text, onClick) => {
    const button = document.createElement('button')
    button.textContent = text
    button.onclick = () => { clicks.push(text); onClick?.() }
    document.body.append(button)
    return button
  }
  const revealUpdate = () => addButton('Update now')
  const check = addButton('Check now', () => {
    check.disabled = true
    if (!available) return
    if (details) {
      const more = addButton("See what's new", () => { more.remove(); revealUpdate() })
    } else {
      revealUpdate()
    }
  })
  const status = { supported: true, behind: available ? 1 : 0 }
  window.hermesDesktop = { updates: { check: async () => status } }
  const page = {
    getByRole(role, { name }) {
      expect(role).toBe('button')
      const selected = () => [...document.querySelectorAll('button')].find(button => name.test(button.textContent))
      const locator = {
        first: () => locator,
        isVisible: async () => Boolean(selected()),
        click: async () => {
          const button = selected()
          if (!button || button.disabled) throw new Error('button not actionable')
          button.click()
        },
      }
      return locator
    },
    waitForTimeout: async ms => vi.setSystemTime(Date.now() + ms),
    evaluate: async fn => window.eval(`(${fn.toString()})()`),
  }
  return { page, clicks, log: vi.fn(), shot: vi.fn() }
}

test.each([true, false])('reveals the actual update button without applying (details=%s)', async details => {
  const f = fixture({ details })
  const update = await updateUi.waitForUpdate(f.page, f)
  expect(await update.isVisible()).toBe(true)
  expect(f.clicks).toEqual(details ? ['Check now', "See what's new"] : ['Check now'])
  expect(f.shot).toHaveBeenCalledWith(f.page, '04-update-available')
})

test('does not turn a completed check without an update into success', async () => {
  const f = fixture({ available: false })
  await expect(updateUi.waitForUpdate(f.page, f)).rejects.toThrow(/"Update now" never appeared/)
  expect(f.clicks).toEqual(['Check now'])
  expect(f.log).toHaveBeenCalledWith('[update-status] {"supported":true,"behind":0}')
  expect(f.shot).toHaveBeenCalledWith(f.page, 'ERROR-no-update-now')
})
