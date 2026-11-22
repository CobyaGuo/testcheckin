// server/index.mjs — Node.js 统一数据层（行级 API + node:sqlite + SSE）
//
// 口径（重做版）：
//   · server 真开 SQLite（node:sqlite DatabaseSync），四库落 server/data/*.db；
//   · 主数据通道是行级 CRUD / batch 事务 API，前端为「行级写穿 + 本地读缓存」；
//   · 原整库 download/upload 降级为「备份 / 恢复」，仅首次引导与显式备份使用；
//   · SSE 推送 row-change / backup-restored，客户端可随时连接 / 切断。
//
// 只用内置模块 + 仓库内共享纯函数，无新依赖。

import http from 'node:http'
import fs from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { assertIdent, buildWhere, q } from './shared/backend/rowSql.js'
import { SYNC_SCHEMA_VER, SYNC_TABLES, isSyncApp } from './shared/backend/syncPolicy.js'
import { createAuth, can, canAccess } from './auth.mjs'
import { getPrefsStore } from './prefsStore.mjs'
import {
  SyncRequestError, applyBatch, initSyncSchema, markBackupRestored, queryChanges, readSyncState
} from './syncStore.js'
import { loadServerConfig, publicConfig, writeServerConfig, diffRestart } from './config.mjs'

// 配置统一由 server/config.mjs 解析：env > dist/server/server.config.json > 默认；非 loopback 直接抛错退出。
let CFG
try {
  CFG = await loadServerConfig()
} catch (e) {
  console.error('[checkin-node-data] ' + (e && e.message || e))
  process.exit(1)
}
const DATA_DIR = CFG.dataDir // 重启生效
let DIST_DIR = CFG.distDir   // 可热更新（PATCH /api/config 的 distDir）
const AUDIT_FILE = path.join(DATA_DIR, 'audit.jsonl')
const HOST = CFG.host
const PORT = CFG.port
// body 上限分级（评审修复）：batch/rows JSON 16MB；auth/prefs/users/config 2MB；
// restore 整库 256MB 且流式落临时文件（全量读进内存会让并发几个请求就 OOM）。
const MAX_BODY = 16 * 1024 * 1024
const MAX_BODY_SMALL = 2 * 1024 * 1024
const MAX_RESTORE_BODY = 256 * 1024 * 1024
const SQLITE_MAGIC = 'SQLite format 3\0'
// 测试可缩短心跳以快速验证「存活 SSE 的账号过期回收」；生产固定 15s。
const HEARTBEAT_MS = Math.max(1000, Number(process.env.CHECKIN_SSE_HEARTBEAT_MS) || 15000)

const APPS = Object.freeze({ auto: 'auto.db', perf: 'perf.db' })
const NO_AUTH = process.env.CHECKIN_NO_AUTH === '1'
let AUTH
try {
  AUTH = await createAuth({ dataDir: DATA_DIR, noAuth: NO_AUTH })
} catch (e) {
  console.error('[checkin-node-data] ' + (e && e.message || e))
  process.exit(1)
}
const MIME = Object.freeze({
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.ico': 'image/x-icon', '.wasm': 'application/wasm',
  '.gz': 'application/gzip', '.md': 'text/markdown; charset=utf-8'
})

const states = new Map()
const chains = new Map()
const sseClients = new Set()
let sseSeq = 0
let auditSeq = 0
let auditChain = Promise.resolve()

function nowIso() { return new Date().toISOString() }
function dbPath(app) { return Object.prototype.hasOwnProperty.call(APPS, app) ? path.join(DATA_DIR, APPS[app]) : null }
function state(app) { return states.get(app) || null }

// CORS 收敛（评审修复）：默认同源部署不回 ACAO（浏览器同源请求不受影响）。
// 需要跨源回退时用 CHECKIN_ALLOWED_ORIGINS=a,b 配置白名单；不再使用 * ——
// 任意网页都能带着泄露的 Bearer token 直调 API 的口子必须关掉。
const ALLOWED_ORIGINS = new Set(
  String(process.env.CHECKIN_ALLOWED_ORIGINS || '').split(',').map((s) => s.trim()).filter(Boolean)
)

function applyCors(req, res) {
  const origin = String(req.headers.origin || '')
  if (ALLOWED_ORIGINS.has(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin)
    res.setHeader('Vary', 'Origin')
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PATCH,DELETE,PUT,OPTIONS')
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Client-Id, Authorization, X-Sse-Ticket')
    res.setHeader('Access-Control-Expose-Headers', 'X-DB-Rev, X-Checkin-Auth')
  }
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return true }
  return false
}

/** Cookie 属性：TLS 直连（socket.encrypted）或显式 CHECKIN_COOKIE_SECURE=1（反代终止 TLS）时加 Secure。 */
function cookieAttrs(req) {
  const secure = (req && req.socket && req.socket.encrypted === true) || process.env.CHECKIN_COOKIE_SECURE === '1'
  return '; HttpOnly; SameSite=Strict; Path=/' + (secure ? '; Secure' : '')
}

function sendJson(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8')
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length, 'Cache-Control': 'no-store' })
  res.end(body)
}

function sendText(res, code, text, type = 'text/plain; charset=utf-8') {
  const body = Buffer.from(text, 'utf8')
  res.writeHead(code, { 'Content-Type': type, 'Content-Length': body.length })
  res.end(body)
}

function isSqliteBytes(buf) {
  return !!buf && buf.length >= 100 && SQLITE_MAGIC.split('').every((ch, i) => buf[i] === ch.charCodeAt(0))
}

async function readBody(req, json = false, maxBytes = MAX_BODY) {
  const chunks = []
  let total = 0
  let tooLarge = false
  // 事件式消费（评审修复）：超限立即 pause 停读，不再把整个 body 排空——
  // 排空会让慢速大 body 无总量上限地占住连接。剩余字节留在内核缓冲，
  // 由 rejectTooLarge 在 413 响应冲刷完后销毁 socket（不再复用该连接）。
  await new Promise((resolve, reject) => {
    const onData = (chunk) => {
      total += chunk.length
      if (total > maxBytes) {
        tooLarge = true
        chunks.length = 0
        cleanup()
        req.pause()
        resolve()
        return
      }
      chunks.push(chunk)
    }
    const onDone = () => { cleanup(); resolve() }
    const onError = (e) => { cleanup(); reject(e) }
    function cleanup() {
      req.off('data', onData); req.off('end', onDone); req.off('error', onError); req.off('close', onDone)
    }
    req.on('data', onData)
    req.on('end', onDone)
    req.on('error', onError)
    req.on('close', onDone)
  })
  if (tooLarge) { const e = new Error('body too large'); e.code = 'TOO_LARGE'; throw e }
  const buf = Buffer.concat(chunks)
  return json ? JSON.parse(buf.toString('utf8') || '{}') : buf
}

/** restore 专用：流式写临时文件，任何时刻内存里只有当前 chunk，不整库进内存。 */
async function readBodyToFile(req, filePath, maxBytes = MAX_RESTORE_BODY) {
  const ws = fs.createWriteStream(filePath, { flags: 'w' })
  let total = 0
  let tooLarge = false
  let done = false
  try {
    // 同 readBody：超限立即 pause 停读，不排空（rejectTooLarge 负责收尾销毁）。
    await new Promise((resolve, reject) => {
      let waitingDrain = false
      let drainHandler = null
      const onData = (chunk) => {
        total += chunk.length
        if (total > maxBytes) {
          tooLarge = true
          cleanup()
          req.pause()
          resolve()
          return
        }
        if (!ws.write(chunk) && !waitingDrain) {
          waitingDrain = true
          req.pause()
          drainHandler = () => { waitingDrain = false; req.resume() }
          ws.once('drain', drainHandler)
        }
      }
      const onDone = () => { cleanup(); resolve() }
      const onError = (e) => { cleanup(); reject(e) }
      function cleanup() {
        req.off('data', onData); req.off('end', onDone); req.off('error', onError); req.off('close', onDone)
        if (drainHandler) ws.off('drain', drainHandler)
      }
      req.on('data', onData)
      req.on('end', onDone)
      req.on('error', onError)
      req.on('close', onDone)
    })
    if (tooLarge) { const e = new Error('body too large'); e.code = 'TOO_LARGE'; throw e }
    await new Promise((resolve, reject) => { ws.end((err) => (err ? reject(err) : resolve())) })
    done = true
    return total
  } finally {
    if (!done) ws.destroy()
    if (!done) { try { fs.unlinkSync(filePath) } catch (e) {} }
  }
}

/**
 * 413 统一出口（评审修复）：声明 Connection: close，响应冲刷完后销毁 socket。
 * 不再排空超限 body——慢速大 body 无法长期占住连接；客户端故意不读响应时
 * 由下方 10s 定时兜底断开，极端情况仍有 Node requestTimeout 兜底。
 */
function rejectTooLarge(req, res, extra = null) {
  if (!res.headersSent) {
    res.setHeader('Connection', 'close')
    sendJson(res, 413, { ok: false, error: 'body-too-large', ...(extra || {}) })
  } else {
    try { res.end() } catch (e) {}
  }
  const kill = () => { try { req.destroy() } catch (e) {} }
  res.on('finish', kill)
  res.on('close', kill)
  const t = setTimeout(kill, 10000)
  if (t.unref) t.unref()
}

function openDb(app) {
  const file = dbPath(app)
  if (!file) throw new Error('unknown app: ' + app)
  const db = new DatabaseSync(file)
  db.exec('PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;')
  const sync = initSyncSchema(db, app)
  states.set(app, { db, file, appRev: sync.appRev, minAppRev: sync.minAppRev })
  return db
}

function closeDb(app) {
  const s = state(app)
  if (!s || !s.db) return
  try { s.db.close() } catch (e) {}
  s.db = null
}

/** 每库串行队列：批次事务、表结构读取、备份恢复都排队，避免并发交错。 */
function withDb(app, fn) {
  const prev = chains.get(app) || Promise.resolve()
  const next = prev.catch(() => {}).then(() => fn(state(app) || { db: null, appRev: 0, minAppRev: 1, file: dbPath(app) }))
  chains.set(app, next.catch(() => {}))
  return next
}

function ensureDb(s) {
  if (!s || !s.db) throw new Error('数据库未打开')
  return s.db
}

function tableColumns(db, table) {
  assertIdent(table, '表名')
  const cols = db.prepare('PRAGMA table_info(' + q(table) + ')').all()
  if (!cols.length) throw new Error('表不存在：' + table)
  return cols
}

function appendAudit(entry) {
  const rec = { seq: ++auditSeq, at: nowIso(), ...entry }
  auditChain = auditChain.then(() => fsp.appendFile(AUDIT_FILE, JSON.stringify(rec) + '\n', 'utf8'))
  return auditChain.then(() => rec)
}

/** audit 写失败只告警不阻断业务（设计 §4.2）。 */
function logAudit(entry) {
  try { Promise.resolve(appendAudit(entry)).catch((e) => console.error('[server] audit 写入失败', e && e.message)) } catch (e) {}
}

function parseCookies(req) {
  const out = {}
  for (const part of String(req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=')
    if (i > 0) {
      try { out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim()) } catch (e) {}
    }
  }
  return out
}

/** httpOnly Cookie 主通道 + Bearer 跨源回退，等价校验（设计 §3）。 */
function tokenFrom(req) {
  const cookie = parseCookies(req).ckbk_session
  if (cookie) return cookie
  const auth = String(req.headers.authorization || '')
  const m = /^Bearer\s+(.+)$/i.exec(auth)
  return m ? m[1].trim() : ''
}

function deny(res, user, app, cap) {
  // 必须返回 truthy：sendJson 本身无返回值，若直接 `return sendJson(...)`，
  // 所有 `if (denied) return denied` 都会穿透——响应已是 403，但业务写仍照常执行。
  if (app && !canAccess(user, app)) {
    logAudit({ type: 'denied', reason: 'app-denied', app, user: user && user.name })
    sendJson(res, 403, { ok: false, error: 'app-denied', code: 'app-denied', message: '无权访问该应用' })
    return true
  }
  if (cap && !can(user, cap)) {
    logAudit({ type: 'denied', reason: 'forbidden', cap, app, user: user && user.name })
    sendJson(res, 403, { ok: false, error: 'forbidden', code: 'forbidden', message: '权限不足（需要 ' + cap + '）' })
    return true
  }
  return false
}

/**
 * rev7.4「perf 权限收紧 L3」的服务端落点（评审 2026-11-22 补缺口）：
 * perf 台全部写动作在 UI 层走 data-cap=config + ensureConfigCap()，但 batch/rows 写入口
 * 此前统一要求 'edit'（L2 即过）——L2 用合法会话直调 API 仍可写 perf_ 全部表。
 * 服务端必须同口径按 app 区分：perf 写 = config（仅 L3），auto 写维持 edit（L2）。
 */
function writeCap(app) {
  return app === 'perf' ? 'config' : 'edit'
}

// 审计尾部读（评审修复）：audit.jsonl 无界增长，全量读入会越来越慢。
// 只读文件尾部（默认 1MB，约 5000 条），limit 很大时按需扩大。
const AUDIT_TAIL_BYTES = 1024 * 1024

async function readAuditEntries(limit = 200) {
  const want = Math.max(1, Math.min(10000, Math.floor(limit)))
  let fh
  try {
    fh = await fsp.open(AUDIT_FILE, 'r')
  } catch (e) {
    if (e && e.code === 'ENOENT') return []
    throw e
  }
  try {
    const st = await fh.stat()
    const readLen = Math.min(st.size, Math.max(AUDIT_TAIL_BYTES, want * 512))
    const start = Math.max(0, st.size - readLen)
    const buf = Buffer.alloc(st.size - start)
    await fh.read(buf, 0, buf.length, start)
    const lines = buf.toString('utf8').split('\n')
    if (start > 0) lines.shift() // 尾部块第一条大概率是被截断的半行，掐掉
    const out = []
    for (let i = lines.length - 1; i >= 0 && out.length < want; i--) {
      const line = lines[i]
      if (!line.trim()) continue
      try { out.push(JSON.parse(line)) } catch (e) {}
    }
    return out.reverse()
  } finally {
    await fh.close()
  }
}

function writeSse(res, event, data, id) {
  const payload = 'id: ' + (id || ++sseSeq) + '\nevent: ' + event + '\ndata: ' + JSON.stringify(data) + '\n\n'
  try { res.write(payload) } catch (e) { return false }
  return true
}

function broadcast(event, data) {
  const id = data && data.appRev ? data.appRev : ++sseSeq
  for (const c of sseClients) {
    if (data && data.app && c.app !== data.app) continue
    // 流级授权（§4.1-③）：按实时 apps 过滤，用户在用户管理里被缩权后下一帧即生效。
    if (c.user && !canAccess(c.user, data && data.app ? data.app : c.app)) continue
    if (!writeSse(c.res, event, data, id)) sseClients.delete(c)
  }
}

function handleSse(req, res, url, user) {
  const app = url.searchParams.get('app') || ''
  if (!isSyncApp(app)) return sendJson(res, 400, { ok: false, error: 'invalid app', app })
  if (!canAccess(user, app)) return deny(res, user, app, null)
  const s = state(app)
  const sync = s && s.db ? readSyncState(s.db, app) : { appRev: 0, minAppRev: 1 }
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive', 'X-Accel-Buffering': 'no'
  })
  res.write('retry: 3000\n\n')
  const client = { res, app, userName: user.name, user }
  sseClients.add(client)
  const lastEventId = Number(url.searchParams.get('lastEventId') || req.headers['last-event-id'] || 0) || 0
  writeSse(res, 'hello', {
    type: 'hello', at: nowIso(), app, appRev: sync.appRev, minAppRev: sync.minAppRev,
    schemaVer: SYNC_SCHEMA_VER, clients: sseClients.size, lastEventId
  })
  if (lastEventId > 0 && s && s.db && canAccess(user, app)) {
    try {
      const backfill = queryChanges(s.db, app, lastEventId)
      for (const ev of backfill.changes) writeSse(res, 'row-change', ev, ev.appRev)
    } catch (e) {
      if (e instanceof SyncRequestError && e.status === 410) {
        writeSse(res, 'sync-required', {
          type: 'sync-required', app, reason: e.extra.reason || 'changes-expired',
          appRev: e.extra.appRev, minAppRev: e.extra.minAppRev, at: nowIso()
        })
      } else throw e
    }
  }
  // rev5-⑰：心跳顺带回查实时身份；被动只听不发的 SSE 连接也不能在用户删除后继续收 7 天事件。
  const timer = setInterval(() => {
    try {
      const current = AUTH.activeUser(client.userName)
      if (!current) {
        writeSse(res, 'auth-expired', { type: 'auth-expired', at: nowIso(), reason: 'account-removed' })
        clearInterval(timer)
        sseClients.delete(client)
        try { res.end() } catch (e2) {}
        return
      }
      client.user = current // apps 被改后按实时授权重建过滤集，无需断连
      res.write(': heartbeat ' + nowIso() + '\n\n')
    } catch (e) { clearInterval(timer) }
  }, HEARTBEAT_MS)
  const cleanup = () => { clearInterval(timer); sseClients.delete(client) }
  req.on('close', cleanup)
  req.on('error', cleanup)
}

function healthLite() {
  return { ok: true, service: 'checkin-node-data-layer', schemaVer: SYNC_SCHEMA_VER }
}

async function healthInfo(scope = 'full') {
  const apps = {}
  for (const app of Object.keys(APPS)) {
    const s = state(app)
    let tables = 0
    let businessTables = 0
    let tableNames = []
    if (s && s.db) {
      try { tables = s.db.prepare("SELECT count(*) n FROM sqlite_master WHERE type='table'").get().n } catch (e) {}
      try {
        // rev7.5：客户端「是否需要首次引导」改看核心业务表是否存在（tableNames）——
        // server 现在启动时会补建 ke_* 等新增表，businessTables 计数恒 > 0，
        // 再拿它当「库已初始化」会让全新 server 跳过引导、业务表永远建不出来。
        tableNames = s.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sync_%' AND name NOT LIKE 'sqlite_%'").all().map((r) => r.name)
        businessTables = tableNames.length
      } catch (e) {}
    }
    let size = 0
    if (s && s.db) { try { size = fs.statSync(s.file).size } catch (e) {} }
    const sync = s && s.db ? readSyncState(s.db, app) : { appRev: 0, minAppRev: 1 }
    apps[app] = {
      exists: !!(s && s.db), file: APPS[app], tables, businessTables, tableNames, size,
      appRev: sync.appRev, minAppRev: sync.minAppRev, schemaVer: SYNC_SCHEMA_VER
    }
  }
  const info = {
    ok: true, service: 'checkin-node-data-layer', mode: 'nodejs', engine: 'node:sqlite',
    sseClients: sseClients.size, auditSeq,
    schemaVer: SYNC_SCHEMA_VER, apps: apps, at: nowIso()
  }
  if (scope === 'full') {
    info.host = HOST
    info.port = PORT
    info.dataDir = DATA_DIR
    info.distDir = DIST_DIR
  }
  return info
}

async function handleConfigApi(req, res) {
  if (req.method === 'GET') {
    return sendJson(res, 200, { ok: true, ...publicConfig(CFG, CFG.configFile) })
  }
  if (req.method === 'PATCH' || req.method === 'PUT') {
    const body = await readBody(req, true, MAX_BODY_SMALL)
    // 只允许写白名单字段；其余忽略，避免内部状态被覆盖。
    const patch = {}
    for (const k of ['root', 'dataDir', 'distDir', 'host', 'port']) {
      if (body[k] !== undefined) patch[k] = body[k]
    }
    try {
      const diff = diffRestart(CFG, patch)
      const cfg = await writeServerConfig(patch, CFG.configFile)
      // distDir 热更新：立即作用于静态服务；其余字段需重启，写盘后本次进程继续用旧值。
      if (patch.distDir !== undefined) DIST_DIR = cfg.distDir
      const restarted = diff.restartRequired
      CFG = cfg
      return sendJson(res, 200, {
        ok: true, ...publicConfig(cfg, cfg.configFile),
        applied: diff.changed, restartRequired: restarted,
        message: restarted.length
          ? '配置已保存；' + restarted.join('/') + ' 需重启 server 后生效'
          : '配置已保存并即时生效'
      })
    } catch (e) {
      return sendJson(res, 400, { ok: false, error: 'invalid-config', message: String(e && e.message || e) })
    }
  }
  return sendJson(res, 405, { ok: false, error: 'method not allowed' })
}

async function runBatch(app, ops, clientId, schemaVer = SYNC_SCHEMA_VER, user = null) {
  return withDb(app, async (s) => {
    const db = ensureDb(s)
    const result = applyBatch(db, app, ops, clientId, schemaVer, {
      canOp: () => can(user, 'import')
    })
    s.appRev = result.appRev
    s.minAppRev = result.minAppRev
    for (const entry of result.audit) {
      try { await appendAudit(entry) } catch (e) { console.error('[server] audit 写入失败', e && e.message) }
    }
    for (const ev of result.events) broadcast('row-change', ev)
    return result
  })
}

async function handleDataApi(req, res, url, parts, user = null) {
  const app = parts[2]
  if (!dbPath(app)) return sendJson(res, 404, { ok: false, error: 'unknown app', app })
  const sub = parts[3] || 'meta'

  if (sub === 'meta' && req.method === 'GET') {
    const s = state(app)
    const sync = s && s.db ? readSyncState(s.db, app) : { appRev: 0, minAppRev: 1 }
    return sendJson(res, 200, {
      ok: true, app, engine: 'node:sqlite', appRev: sync.appRev, minAppRev: sync.minAppRev,
      schemaVer: SYNC_SCHEMA_VER, whitelist: SYNC_TABLES[app],
      tables: s && s.db ? s.db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all().map((r) => r.name) : [],
      auditSeq, at: nowIso()
    })
  }

  if (sub === 'changes' && req.method === 'GET') {
    const since = Number(url.searchParams.get('since') || 0) || 0
    try {
      return sendJson(res, 200, queryChanges(ensureDb(state(app)), app, since))
    } catch (e) {
      if (e instanceof SyncRequestError) {
        return sendJson(res, e.status, {
          ok: false, error: e.code, message: e.message, app,
          ...e.extra
        })
      }
      throw e
    }
  }

  if (sub === 'batch' && req.method === 'POST') {
    const body = await readBody(req, true)
    const schemaVer = body.schemaVer === undefined ? SYNC_SCHEMA_VER : body.schemaVer
    const r = await runBatch(app, body.ops, body.clientId || req.headers['x-client-id'] || '', schemaVer, user)
    return sendJson(res, 200, r)
  }

  if (sub === 'rows' && parts.length >= 5) {
    const table = parts[4]
    if (!SYNC_TABLES[app] || !SYNC_TABLES[app][table]) {
      return sendJson(res, 400, { ok: false, error: 'table-not-allowed', table })
    }
    if (req.method === 'GET') {
      return withDb(app, (s) => {
        const db = ensureDb(s)
        tableColumns(db, table)
        const where = JSON.parse(url.searchParams.get('where') || '{}')
        const w = buildWhere(where)
        let sql = 'SELECT * FROM ' + q(table) + w.sql
        const orderBy = JSON.parse(url.searchParams.get('orderBy') || 'null')
        if (orderBy && orderBy.col) sql += ' ORDER BY ' + q(orderBy.col) + (orderBy.asc === false ? ' DESC' : ' ASC')
        const limit = Number(url.searchParams.get('limit') || 0)
        if (limit > 0) sql += ' LIMIT ' + Math.floor(limit)
        const rows = db.prepare(sql).all(...w.params)
        return sendJson(res, 200, { ok: true, app, table, rows, appRev: s.appRev, rev: s.appRev })
      })
    }
    const body = await readBody(req, true)
    const clientId = body.clientId || req.headers['x-client-id'] || ''
    const schemaVer = body.schemaVer === undefined ? SYNC_SCHEMA_VER : body.schemaVer
    const opId = body.opId || 'row-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8)
    let op
    if (req.method === 'POST') op = { opId, schemaVer, op: 'insert', table, values: body.values || body }
    else if (req.method === 'PATCH') op = { opId, schemaVer, op: 'update', table, values: body.values, where: body.where }
    else if (req.method === 'DELETE') op = { opId, schemaVer, op: 'delete', table, where: body.where }
    else return sendJson(res, 405, { ok: false, error: 'method not allowed' })
    const r = await runBatch(app, [op], clientId, schemaVer, user)
    return sendJson(res, 200, { ...r, row: r.events[0] || null })
  }

  return sendJson(res, 404, { ok: false, error: 'not found', path: url.pathname })
}

/**
 * 生成可被浏览器 wa-sqlite 直接打开的备份字节：
 * server 自身跑 WAL，直接发主库文件会让浏览器 VFS 因缺少 WAL/SHM 报 unable to open
 * database file。先 checkpoint，再复制到临时文件转为 DELETE journal 后发出。
 */
function portableBackupBytes(file) {
  const tmp = file + '.export-' + process.pid + '-' + Date.now()
  try {
    fs.copyFileSync(file, tmp)
    const tmpDb = new DatabaseSync(tmp)
    try { tmpDb.exec('PRAGMA journal_mode = DELETE; VACUUM;') } finally { tmpDb.close() }
    return fs.readFileSync(tmp)
  } finally {
    try { fs.unlinkSync(tmp) } catch (e) {}
  }
}

/** 备份/恢复：沿用原 download/upload 语义，但不再作为主数据通道。 */
async function handleBackupApi(req, res, url, parts) {
  const app = parts[2]
  const file = dbPath(app)
  if (!file) return sendJson(res, 404, { ok: false, error: 'unknown app', app })
  const op = parts[3]

  if (op === 'download' && (req.method === 'GET' || req.method === 'HEAD')) {
    return withDb(app, (s) => {
      const db = ensureDb(s)
      try { db.exec('PRAGMA wal_checkpoint(TRUNCATE)') } catch (e) {}
      if (!fs.existsSync(file)) return sendJson(res, 404, { ok: false, error: 'db not found', app })
      const portable = portableBackupBytes(file)
      const body = req.method === 'HEAD' ? Buffer.alloc(0) : portable
      res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': portable.length, 'X-DB-Rev': String(s.appRev), 'Cache-Control': 'no-store' })
      res.end(body)
    })
  }

  if (op === 'restore' && req.method === 'PUT') {
    // 流式落临时文件：不再把最多数百 MB 的 body 一次性读进内存（OOM 面）。
    const tmp = file + '.restore-' + process.pid + '-' + Date.now()
    let size = 0
    try {
      size = await readBodyToFile(req, tmp)
    } catch (e) {
      // 评审修复：不再排空超限 body；413 冲刷后销毁 socket（Connection: close）。
      if (e && e.code === 'TOO_LARGE') return rejectTooLarge(req, res, { app, max: MAX_RESTORE_BODY })
      throw e
    }
    // 魔数校验只读头 100 字节。
    let head = null
    try {
      head = Buffer.alloc(100)
      const fd = fs.openSync(tmp, 'r')
      try { fs.readSync(fd, head, 0, 100, 0) } finally { fs.closeSync(fd) }
    } catch (e) {}
    if (!isSqliteBytes(head)) {
      try { fs.unlinkSync(tmp) } catch (e) {}
      return sendJson(res, 400, { ok: false, error: 'not a sqlite database', app, size })
    }
    let marker = null
    await withDb(app, async () => {
      closeDb(app)
      await fsp.rename(tmp, file)
      openDb(app)
      const cur = state(app)
      marker = markBackupRestored(cur.db, app, req.headers['x-client-id'] || '')
      cur.appRev = marker.appRev
      cur.minAppRev = marker.minAppRev
      try { await appendAudit({ type: 'backup-restored', app, appRev: marker.appRev, size, clientId: marker.clientId }) } catch (e) {}
    })
    broadcast('backup-restored', {
      type: 'backup-restored', app, appRev: marker ? marker.appRev : 0,
      at: marker ? marker.at : nowIso(), clientId: req.headers['x-client-id'] || ''
    })
    return sendJson(res, 200, { ok: true, app, action: 'restore', size, appRev: marker && marker.appRev })
  }

  return sendJson(res, 404, { ok: false, error: 'not found', path: url.pathname })
}

/**
 * 未登录访问 auto/perf 页面的跳转垫片：200 返回一段 meta-refresh HTML，
 * 由浏览器（含 SW 控制的导航）自己再发起对 /login/ 的导航。
 * 不能直接 302：navigate 请求的 Request.redirect 是 manual，SW respondWith
 * 拿到重定向响应会报 "a redirected response was used for a request whose
 * redirect mode is not 'follow'"（网络错误白屏）；200 垫片对任何 SW 版本都安全。
 * 非 document 请求（预缓存 fetch 等）保留 302，行为不变。
 */
/** 属性级 HTML 转义：rel/target 拼进 meta refresh 与 href 前必须过这里（注入面）。 */
function escAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function loginShimHtml(next) {
  const target = escAttr('/login/index.html?next=' + next)
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<meta http-equiv="refresh" content="0;url=' + target + '">' +
    '<title>跳转登录…</title></head>' +
    '<body style="font-family:system-ui,sans-serif;padding:2rem;color:#1f2937">' +
    '请先登录，<a href="' + target + '">前往登录页</a>…</body></html>'
}

/**
 * 无尾斜杠目录（/auto、/marknote）的加斜杠垫片：与登录垫片同理，
 * document 导航不能 302——SW fetch(redirect:'follow') 跟随后返回的响应带
 * redirected=true，respondWith 对 redirect:manual 的导航请求会报
 * "a redirected response was used for a request whose redirect mode is
 * not 'follow'"（ERR_FAILED）。200 垫片让浏览器自己再导航到带斜杠 URL。
 */
function slashShimHtml(target) {
  const safe = escAttr(target)
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta http-equiv="refresh" content="0;url=' + safe + '/">' +
    '<title>跳转中…</title></head>' +
    '<body style="font-family:system-ui,sans-serif;padding:2rem;color:#1f2937">' +
    '<a href="' + safe + '/">继续</a>…</body></html>'
}

/** 页面级鉴权：auto/perf 的 HTML 由 server 直接拦，未登录先跳登录，不靠应用 JS 事后跳转。 */
function allowAppPage(req, res, pathname, url) {
  if (NO_AUTH) return true
  const clean = String(pathname || '').split(/[?#]/)[0]
  if (!/^\/(auto|perf)\//.test(clean)) return true
  if (AUTH.resolve(tokenFrom(req))) return true
  const next = encodeURIComponent(clean + (url.search || ''))
  const dest = String(req.headers['sec-fetch-dest'] || '')
  if (dest === 'document') {
    const html = loginShimHtml(next)
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' })
    res.end(html)
    return false
  }
  res.writeHead(302, { Location: '/login/index.html?next=' + next, 'Cache-Control': 'no-cache' })
  res.end()
  return false
}

function serveStatic(req, res, url) {
  // 畸形编码（GET /%zz）会抛 URIError：不接住就是未认证 DoS（进程同步崩溃）。
  let rel = ''
  try {
    rel = decodeURIComponent(url.pathname || '/')
  } catch (e) {
    return sendText(res, 400, 'bad request: malformed url encoding')
  }
  if (rel === '/' || rel === '') rel = '/index.html'
  const resolved = path.resolve(DIST_DIR, '.' + rel)
  if (resolved !== DIST_DIR && !resolved.startsWith(DIST_DIR + path.sep)) return sendText(res, 403, 'forbidden')
  fs.stat(resolved, (err, st) => {
    if (err || (!st.isFile() && !st.isDirectory())) {
      if (!fs.existsSync(DIST_DIR)) return sendText(res, 404, 'dist/ 不存在：请先执行 npm run build')
      return sendText(res, 404, 'not found: ' + rel)
    }
    // 目录 URL（/login/、/perf/、/auto/、/marknote/、/workbench/）：
    // · 带尾斜杠 → 直接 200 返回目录内 index.html。不能再 302：navigate 请求的
    //   Request.redirect 是 manual，SW fetch 拿到 opaqueredirect（ok=false）后进兜底，
    //   /auto/ 不被识别就错退成 workbench 壳（点 auto 出工作台）。
    // · 无尾斜杠 → 302 加斜杠规范化，保证页面内相对资源路径基准正确。
    // 页面级鉴权按 /<app>/index.html 判定，让登录 ?next= 白名单拿到规范路径。
    if (st.isDirectory()) {
      const indexFile = path.join(resolved, 'index.html')
      if (!fs.existsSync(indexFile)) return sendText(res, 404, 'not found: ' + rel)
      if (!rel.endsWith('/')) {
        // document 导航走 200 垫片（SW respondWith 不兼容重定向响应，见 slashShimHtml）；
        // 非 document（预缓存 fetch 等）保留 302，行为不变。
        if (String(req.headers['sec-fetch-dest'] || '') === 'document') {
          res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' })
          return res.end(slashShimHtml(rel))
        }
        res.writeHead(302, { Location: rel + '/', 'Cache-Control': 'no-cache' })
        return res.end()
      }
      const pageRel = rel.replace(/\/+$/, '') + '/index.html'
      if (!allowAppPage(req, res, pageRel, url)) return
      const indexStat = fs.statSync(indexFile)
      res.writeHead(200, {
        'Content-Type': MIME['.html'] || 'text/html; charset=utf-8',
        'Content-Length': indexStat.size, 'Cache-Control': 'no-cache'
      })
      if (req.method === 'HEAD') return res.end()
      const s = fs.createReadStream(indexFile)
      s.on('error', () => { try { res.destroy() } catch (e) {} })
      return s.pipe(res)
    }
    const ext = path.extname(resolved).toLowerCase()
    if (ext === '.html' && !allowAppPage(req, res, rel, url)) return
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream', 'Content-Length': st.size, 'Cache-Control': ext === '.html' ? 'no-cache' : 'public, max-age=0' })
    if (req.method === 'HEAD') return res.end()
    const stream = fs.createReadStream(resolved)
    stream.on('error', () => { try { res.destroy() } catch (e) {} })
    stream.pipe(res)
  })
}

async function handleAuthApi(req, res, url, parts, user, token) {
  const action = parts[2] || ''
  if (action === 'login' && req.method === 'POST') {
    const body = await readBody(req, true, MAX_BODY_SMALL)
    const name = body.name || body.user || body.username || ''
    const r = AUTH.login(name, body.password || body.passwd, {
      remember: !!body.remember, ip: req.socket.remoteAddress || 'unknown'
    })
    if (!r.ok) {
      logAudit({ type: 'login-failed', user: name, reason: r.code })
      return sendJson(res, r.status, { ok: false, error: r.code, message: r.message })
    }
    const maxAge = body.remember ? 604800 : 43200
    res.setHeader('Set-Cookie', 'ckbk_session=' + r.token + cookieAttrs(req) + '; Max-Age=' + maxAge)
    logAudit({ type: 'login', user: r.user.name, role: r.user.role, apps: r.user.apps })
    return sendJson(res, 200, { ok: true, token: r.token, user: r.user, expiresAt: r.expiresAt })
  }
  if (action === 'whoami' && req.method === 'GET') {
    return sendJson(res, 200, { ok: true, user })
  }
  if (action === 'logout' && req.method === 'POST') {
    AUTH.logout(token)
    res.setHeader('Set-Cookie', 'ckbk_session=' + cookieAttrs(req) + '; Max-Age=0')
    logAudit({ type: 'logout', user: user && user.name })
    return sendJson(res, 200, { ok: true })
  }
  if (action === 'passwd' && req.method === 'POST') {
    const body = await readBody(req, true, MAX_BODY_SMALL)
    const r = await AUTH.changePassword(token, body.oldPassword || body.old, body.newPassword || body.password)
    if (!r.ok) return sendJson(res, r.status, { ok: false, error: r.code, message: r.message })
    res.setHeader('Set-Cookie', 'ckbk_session=' + cookieAttrs(req) + '; Max-Age=0')
    logAudit({ type: 'passwd', user: user && user.name })
    return sendJson(res, 200, { ok: true, message: '密码已修改，请重新登录' })
  }
  if (action === 'sse-ticket' && req.method === 'GET') {
    const app = url.searchParams.get('app') || ''
    if (!isSyncApp(app)) return sendJson(res, 400, { ok: false, error: 'invalid app', app })
    const denied = deny(res, user, app, null) || deny(res, user, null, 'view')
    if (denied) return denied
    const t = AUTH.issueTicket(user, app)
    if (!t) return sendJson(res, 403, { ok: false, error: 'app-denied', code: 'app-denied' })
    logAudit({ type: 'sse-ticket', user: user.name, app })
    return sendJson(res, 200, { ok: true, ...t })
  }
  return sendJson(res, 404, { ok: false, error: 'not found', path: url.pathname })
}

/** rev7 §4：账号个人偏好，按 (user, app) 隔离；不进业务库、不进审计、不推 appRev。 */
async function handlePrefsApi(req, res, parts, user) {
  const app = parts[2] || ''
  const store = getPrefsStore(DATA_DIR)
  if (req.method === 'GET') {
    return sendJson(res, 200, { ok: true, ...store.get(user.name, app) })
  }
  if (req.method === 'PATCH') {
    const body = await readBody(req, true, MAX_BODY_SMALL)
    const r = store.patch(user.name, app, body && body.changes, body && body.baseUpdatedAt)
    if (!r.ok) return sendJson(res, r.status, { ok: false, error: r.code, message: r.message })
    return sendJson(res, 200, { ok: true, prefs: r.prefs, updatedAt: r.updatedAt })
  }
  return sendJson(res, 405, { ok: false, error: 'method not allowed' })
}

async function handleUsersApi(req, res, parts, user) {
  const name = parts[2] || ''
  const actor = user && user.name
  if (!name && req.method === 'GET') return sendJson(res, 200, { ok: true, users: AUTH.listUsers() })
  if (!name && req.method === 'POST') {
    const body = await readBody(req, true, MAX_BODY_SMALL)
    const r = await AUTH.addUser(actor, body.name, { role: body.role, apps: body.apps, password: body.password })
    if (!r.ok) return sendJson(res, r.status, { ok: false, error: r.code, reason: r.reason, message: r.reason })
    logAudit({ type: 'user_add', actor, target: r.user.name, role: r.user.role, apps: r.user.apps })
    return sendJson(res, 201, { ok: true, user: r.user })
  }
  if (name && req.method === 'PATCH') {
    const body = await readBody(req, true, MAX_BODY_SMALL)
    const patch = {}
    for (const k of ['role', 'apps', 'password']) if (body[k] !== undefined) patch[k] = body[k]
    const r = await AUTH.updateUser(actor, name, patch)
    if (!r.ok) return sendJson(res, r.status, { ok: false, error: r.code, reason: r.reason, message: r.reason })
    logAudit({ type: 'user_edit', actor, target: name, fields: Object.keys(patch) })
    return sendJson(res, 200, { ok: true, user: r.user })
  }
  if (name && req.method === 'DELETE') {
    const r = await AUTH.deleteUser(actor, name)
    if (!r.ok) return sendJson(res, r.status, { ok: false, error: r.code, reason: r.reason, message: r.reason })
    logAudit({ type: 'user_del', actor, target: name })
    return sendJson(res, 200, { ok: true })
  }
  return sendJson(res, 405, { ok: false, error: 'method not allowed' })
}

/** /api 总鉴权入口：所有非公开端点先过这里，/api/db 旧别名同样被覆盖（§4.1-①）。 */
async function handleApi(req, res, url) {
  const parts = url.pathname.split('/').filter(Boolean)
  const group = parts[1] || ''
  const token = tokenFrom(req)
  const isLogin = group === 'auth' && parts[2] === 'login' && req.method === 'POST'
  const isHealthLite = group === 'health' && parts.length === 2 && req.method === 'GET'
  let user = NO_AUTH ? AUTH.activeUser('noauth') : (token ? AUTH.resolve(token) : null)

  if (isLogin) return handleAuthApi(req, res, url, parts, user, token)
  // 公开精简版：无凭证只回 {ok,service,schemaVer}；带凭证时给完整信息，兼容现有 client 的 health 解析。
  // 是否需要登录用响应头告知（不进 body 白名单），让未登录的 Node 模式页面直接跳登录页。
  if (isHealthLite) {
    res.setHeader('X-Checkin-Auth', NO_AUTH ? 'disabled' : 'required')
    // 带凭证也分级：dataDir/distDir/host/port 只给 config 能力（L3），普通账号给运行概况。
    return sendJson(res, 200, user ? await healthInfo(can(user, 'config') ? 'full' : 'safe') : healthLite()) // eslint-disable-line no-await-in-loop
  }
  if (!user) {
    // 跨源回退：一次性 ticket 连 SSE，长效 token 不进 URL。
    // ticket 走 X-Sse-Ticket 头优先（不进代理/访问日志）；query 保留作
    // EventSource 无法设头时的回退，loopback 场景可接受。
    const ticketParam = req.headers['x-sse-ticket'] || url.searchParams.get('ticket')
    if (group === 'events' && req.method === 'GET' && ticketParam) {
      const ticketUser = AUTH.consumeTicket(ticketParam, url.searchParams.get('app'), token)
      if (ticketUser) return handleSse(req, res, url, ticketUser)
    }
    logAudit({ type: 'denied', reason: 'unauthorized', path: url.pathname })
    return sendJson(res, 401, { ok: false, error: 'unauthorized', code: 'login-required', message: '请先登录' })
  }

  if (group === 'auth') return handleAuthApi(req, res, url, parts, user, token)
  if (group === 'health' && parts[2] === 'full' && req.method === 'GET') {
    // health/full 含 dataDir/distDir 绝对路径，收敛到 config 能力（L3）。
    const denied = deny(res, user, null, 'config'); if (denied) return denied
    return sendJson(res, 200, await healthInfo())  // eslint-disable-line no-await-in-loop -- 完整信息仅 L3
  }
  if (group === 'prefs') {
    const app = parts[2] || ''
    if (!isSyncApp(app)) return sendJson(res, 400, { ok: false, error: 'invalid app', app })
    const denied = deny(res, user, app, 'prefs'); if (denied) return denied
    return handlePrefsApi(req, res, parts, user)
  }
  if (group === 'users') {
    // 注册用户名只读（rev7.10）：auto 人员名单默认并入全部注册用户，任何已登录账号都要能取；
    // 只回 name，不暴露角色 / 应用 / 创建时间；users CRUD 仍锁 L3（下方 deny 原样）。
    if (req.method === 'GET' && parts[2] === 'names') {
      return sendJson(res, 200, { ok: true, names: AUTH.listUsers().map(u => u.name) })
    }
    const denied = deny(res, user, null, 'users'); if (denied) return denied
    return handleUsersApi(req, res, parts, user)
  }
  if (group === 'audit' && req.method === 'GET') {
    const denied = deny(res, user, null, 'audit'); if (denied) return denied
    return sendJson(res, 200, { ok: true, entries: await readAuditEntries(Number(url.searchParams.get('limit')) || 200) })
  }
  if (group === 'config') {
    const denied = deny(res, user, null, req.method === 'GET' ? 'view' : 'config')
    if (denied) return denied
    return handleConfigApi(req, res)
  }
  if (group === 'events' && req.method === 'GET') {
    const app = url.searchParams.get('app') || ''
    if (!isSyncApp(app)) return sendJson(res, 400, { ok: false, error: 'invalid app', app })
    const denied = deny(res, user, app, 'view'); if (denied) return denied
    return handleSse(req, res, url, user)
  }
  if (group === 'changes' && req.method === 'GET') {
    const app = url.searchParams.get('app') || ''
    if (!isSyncApp(app)) return sendJson(res, 400, { ok: false, error: 'invalid app', app })
    const denied = deny(res, user, app, 'view'); if (denied) return denied
    return handleDataApi(req, res, url, ['api', 'data', app, 'changes'], user)
  }
  if (group === 'op' && parts[2] === 'batch' && req.method === 'POST') {
    const app = url.searchParams.get('app') || ''
    if (!isSyncApp(app)) return sendJson(res, 400, { ok: false, error: 'invalid app', app })
    const denied = deny(res, user, app, writeCap(app)); if (denied) return denied
    return handleDataApi(req, res, url, ['api', 'data', app, 'batch'], user)
  }
  if (group === 'data') {
    const app = parts[2]
    if (!dbPath(app)) return sendJson(res, 404, { ok: false, error: 'unknown app', app })
    const write = (parts[3] === 'batch') || (parts[3] === 'rows' && req.method !== 'GET')
    const denied = deny(res, user, app, write ? writeCap(app) : 'view')
    if (denied) return denied
    return handleDataApi(req, res, url, parts, user)
  }
  if (group === 'backup') {
    const app = parts[2]
    if (!dbPath(app)) return sendJson(res, 404, { ok: false, error: 'unknown app', app })
    const write = !(parts[3] === 'download' && (req.method === 'GET' || req.method === 'HEAD'))
    const denied = deny(res, user, app, write ? 'backup' : 'view')
    if (denied) return denied
    return handleBackupApi(req, res, url, parts)
  }
  // 旧路径别名：语义固定为备份/恢复，与 /api/backup 同走一条鉴权链。
  if (group === 'db') {
    const app = parts[2]
    if (!dbPath(app)) return sendJson(res, 404, { ok: false, error: 'unknown app', app })
    const write = !(parts[3] === 'download' && (req.method === 'GET' || req.method === 'HEAD'))
    const denied = deny(res, user, app, write ? 'backup' : 'view')
    if (denied) return denied
    return handleBackupApi(req, res, url, ['api', 'backup', parts[2], parts[3]])
  }
  return sendJson(res, 404, { ok: false, error: 'not found', path: url.pathname })
}

const server = http.createServer((req, res) => {
  try {
    dispatch(req, res)
  } catch (e) {
    // 同步异常兜底（畸形 URL / URL 构造失败等）：回 400/500，绝不让进程死。
    console.error('[server] sync error', req.method, req.url, String(e && e.message || e))
    try { if (!res.headersSent) sendJson(res, 400, { ok: false, error: 'bad request' }); else res.end() } catch (e2) { try { res.destroy() } catch (e3) {} }
  }
})

// 慢速攻击兜底（评审修复）：显式声明超时口径（与 Node 默认一致或收紧）。
//   · headersTimeout 30s：请求头必须 30s 内发完（默认 60s，收紧）；
//   · requestTimeout 300s：整个请求（含 body）最长 5 分钟（Node 默认），
//     未完成的慢速 body 最终会被这里掐断——413 快断之后的最后一道防线。
server.headersTimeout = 30 * 1000
server.requestTimeout = 300 * 1000

function dispatch(req, res) {
  if (applyCors(req, res)) return
  const url = new URL(req.url || '/', 'http://' + (req.headers.host || 'localhost'))
  if (url.pathname.startsWith('/api/')) {
    handleApi(req, res, url).catch((e) => {
      const msg = String(e && e.message || e)
      if (e instanceof SyncRequestError) {
        if (!res.headersSent) sendJson(res, e.status, { ok: false, error: e.code, message: msg, ...e.extra })
        else res.end()
        return
      }
      const bad = /拒绝无 WHERE|非法 SQL|表不存在|未知 op|ops 必须|只允许 DML|缺少字段|缺少 table|body too large/.test(msg)
      if (e && e.code === 'TOO_LARGE') {
        // 评审修复：不再排空剩余 body；改走 rejectTooLarge（Connection: close + 冲刷后销毁）。
        return rejectTooLarge(req, res, { max: MAX_BODY })
      }
      if (!bad) console.error('[server] api error', req.method, url.pathname, msg)
      if (!res.headersSent) sendJson(res, bad ? 400 : 500, { ok: false, error: bad ? 'bad request' : 'internal error', message: msg })
      else res.end()
    })
    return
  }
  serveStatic(req, res, url)
}

// 最后兜底：任何漏网的异步异常只记日志 + 断开当前连接，进程必须活着（未认证 DoS 红线）。
process.on('uncaughtException', (e) => { console.error('[server] uncaughtException', (e && e.stack) || e) })
process.on('unhandledRejection', (e) => { console.error('[server] unhandledRejection', (e && (e.stack || e.message)) || e) })

await fsp.mkdir(DATA_DIR, { recursive: true })

// 启动清理（评审修复）：restore/export 的临时文件带 `<pid>-<时间戳>` 后缀，
// 进程在 closeDb → rename → openDb 窗口被 kill 时会残留（重启后状态自愈，
// 但文件留下）。这里统一回收；正则只匹配临时后缀，绝不触碰真实库文件。
{
  const TEMP_RE = /\.(restore|export)-\d+-\d+$/
  let cleaned = 0
  try {
    for (const name of await fsp.readdir(DATA_DIR)) {
      if (!TEMP_RE.test(name)) continue
      try { await fsp.unlink(path.join(DATA_DIR, name)); cleaned++ } catch (e) {}
    }
  } catch (e) {}
  if (cleaned) console.log('[server] cleaned ' + cleaned + ' leftover temp file(s) from a previous run')
}

for (const app of Object.keys(APPS)) {
  try { openDb(app) } catch (e) { console.error('[server] open failed', app, e && e.message) }
}
try {
  const text = await fsp.readFile(AUDIT_FILE, 'utf8')
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    try { const r = JSON.parse(line); if (r.seq > auditSeq) auditSeq = r.seq } catch (e) {}
  }
} catch (e) {}

server.listen(PORT, HOST, () => {
  console.log('[checkin-node-data] listening http://' + HOST + ':' + PORT)
  console.log('[checkin-node-data] engine node:sqlite, data dir ' + DATA_DIR)
  console.log('[checkin-node-data] apps: ' + Object.keys(APPS).join(', '))
  console.log('[checkin-node-data] auth: ' + (NO_AUTH ? 'DISABLED (--no-auth 仅供测试)' : 'required'))
  if (NO_AUTH) console.warn('[checkin-node-data] 警告：--no-auth 下所有请求被视为 L3 全权账号，禁止用于生产')
})
setInterval(() => { try { AUTH.reap() } catch (e) {} }, 60000).unref()

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    server.close(() => { for (const app of states.keys()) closeDb(app); process.exit(0) })
    setTimeout(() => process.exit(0), 3000).unref()
  })
}
