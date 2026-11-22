// server/prefsStore.mjs — 账号个人偏好持久化（rev7 §4，独立于业务库）
//
// 红线：prefs.db 与 auto.db / perf.db 分离；user_prefs 不进 sync_changes / outbox，
// 不推进 appRev，不写审计。只登记 shared/backend/prefPolicy.js 白名单内的 key。

import fs from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { pickPrefs, validatePrefChanges } from './shared/backend/prefPolicy.js'

const stores = new Map()

function nowIso() { return new Date().toISOString() }

/**
 * 按 dataDir 复用单实例（e2e 会用临时 dataDir 起多个 server，互不串库）。
 */
export function getPrefsStore(dataDir) {
  if (stores.has(dataDir)) return stores.get(dataDir)
  fs.mkdirSync(dataDir, { recursive: true })
  const file = path.join(dataDir, 'prefs.db')
  const db = new DatabaseSync(file)
  db.exec(`
    CREATE TABLE IF NOT EXISTS user_prefs (
      user_name TEXT NOT NULL,
      app       TEXT NOT NULL CHECK (app IN ('auto','perf')),
      key       TEXT NOT NULL,
      value     TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_name, app, key)
    )
  `)

  function readAll(userName, app) {
    const rows = db.prepare('SELECT key, value, updated_at FROM user_prefs WHERE user_name = ? AND app = ?')
      .all(String(userName), String(app))
    const prefs = {}
    let updatedAt = ''
    for (const row of rows) {
      try { prefs[row.key] = JSON.parse(row.value) } catch (e) { /* 坏值当不存在 */ }
      if (String(row.updated_at) > updatedAt) updatedAt = String(row.updated_at)
    }
    return { prefs: pickPrefs(prefs), updatedAt }
  }

  function get(userName, app) {
    return readAll(userName, app)
  }

  function patch(userName, app, changes, baseUpdatedAt) {
    const check = validatePrefChanges(changes)
    if (!check.ok) return { ok: false, status: check.status, code: check.code, message: check.message }
    const current = readAll(userName, app)
    if (baseUpdatedAt && String(baseUpdatedAt) !== current.updatedAt) {
      return { ok: false, status: 409, code: 'prefs-conflict', message: '偏好已在别处更新，请刷新' }
    }
    const at = nowIso()
    const stmt = db.prepare(`INSERT INTO user_prefs (user_name, app, key, value, updated_at)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(user_name, app, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`)
    db.exec('BEGIN')
    try {
      for (const [key, value] of Object.entries(check.changes)) {
        stmt.run(String(userName), String(app), key, JSON.stringify(value), at)
      }
      db.exec('COMMIT')
    } catch (e) {
      try { db.exec('ROLLBACK') } catch (_) {}
      throw e
    }
    return { ok: true, ...readAll(userName, app) }
  }

  const store = { file, get, patch, close: () => { try { db.close() } catch (e) {} } }
  stores.set(dataDir, store)
  return store
}

/** 仅供测试隔离：清空实例缓存（不删磁盘文件）。 */
export function resetPrefsStores() {
  for (const store of stores.values()) store.close()
  stores.clear()
}
