// server/syncStore.js — Node server 行级写链路：sync_changes / sync_applied / appRev
// 只依赖 node:sqlite 传入的 DatabaseSync 实例与共享纯函数，便于单测直接跑内存库。

import { buildDelete, buildInsert, buildReplace, buildUpdate, buildWhere, q } from './shared/backend/rowSql.js'
import {
  KEEP_CHANGES, KEEP_DAYS, MAX_BATCH_OPS, SYNC_SCHEMA_VER,
  isPrivilegedOp, sanitizeKvValues, validateSyncOp
} from './shared/backend/syncPolicy.js'
// 错误知识库自动归类（rev7.3）：纯函数来自 shared/ai/keAssign（零浏览器依赖，
// decodeVec 用全局 atob，Node 18+ 内置）。server 是 cluster_id 的单一裁决者：
// 客户端写入的预判值在事务内重算覆写，修正行作为 server 事件广播给包括发起者在内的一切端。
import { bestClusterMatch, decodeVec, AUTO_ASSIGN_THRESHOLD } from './shared/ai/keAssign.js'
// rev7.5：server 旧库（rev7.3 之前恢复出来的备份）缺 ke_* 业务表 → ke op 写穿全部
// 400 'table-not-allowed' 毒丸，知识库永不跨端同步。openDb 时按白名单幂等补建。
import { ensureBusinessTables } from './shared/backend/businessSchema.js'

export class SyncRequestError extends Error {
  constructor(status, code, message, extra = {}) {
    super(message)
    this.name = 'SyncRequestError'
    this.status = status
    this.code = code
    this.extra = extra
  }
}

const SYNC_SCHEMA = `
CREATE TABLE IF NOT EXISTS sync_changes (
  app TEXT NOT NULL,
  appRev INTEGER NOT NULL,
  opId TEXT NOT NULL,
  table_name TEXT NOT NULL,
  op TEXT NOT NULL,
  keys_json TEXT NOT NULL,
  values_json TEXT,
  where_json TEXT,
  clientId TEXT,
  at TEXT NOT NULL,
  PRIMARY KEY (app, appRev),
  UNIQUE (app, opId)
);
CREATE INDEX IF NOT EXISTS idx_sync_changes_app_rev ON sync_changes(app, appRev);
CREATE TABLE IF NOT EXISTS sync_applied (
  app TEXT NOT NULL, opId TEXT NOT NULL, appRev INTEGER NOT NULL,
  PRIMARY KEY (app, opId)
);
CREATE TABLE IF NOT EXISTS sync_state (
  app TEXT PRIMARY KEY, appRev INTEGER NOT NULL DEFAULT 0, minAppRev INTEGER NOT NULL DEFAULT 1
);
`

function nowIso() { return new Date().toISOString() }

/** SQLite 主键/唯一约束冲突 → 409，让客户端毒丸隔离为 failed，而不是当 5xx 无限退避重试。 */
function isConstraintConflict(e) {
  const code = String((e && e.code) || '')
  const msg = String((e && e.message) || e || '')
  return /SQLITE_CONSTRAINT/.test(code) || /UNIQUE constraint failed|PRIMARY KEY constraint failed/.test(msg)
}

export function initSyncSchema(db, app) {
  db.exec(SYNC_SCHEMA)
  // rev7.5：业务表补建（ke_* 等）必须早于任何 batch 校验——旧库缺表时
  // businessColumns 会 400，客户端 op 被隔离成毒丸。已存在的表绝不动结构。
  ensureBusinessTables(db, app)
  db.prepare('INSERT OR IGNORE INTO sync_state(app, appRev, minAppRev) VALUES(?, 0, 1)').run(app)
  return readSyncState(db, app)
}

export function readSyncState(db, app) {
  const row = db.prepare('SELECT appRev, minAppRev FROM sync_state WHERE app=?').get(app)
  return { appRev: Number(row && row.appRev) || 0, minAppRev: Number(row && row.minAppRev) || 1 }
}

export function writeSyncState(db, app, state) {
  db.prepare('UPDATE sync_state SET appRev=?, minAppRev=? WHERE app=?')
    .run(Number(state.appRev) || 0, Number(state.minAppRev) || 1, app)
}

function businessColumns(db, table) {
  const rows = db.prepare('PRAGMA table_info(' + q(table) + ')').all()
  if (!rows.length) throw new SyncRequestError(400, 'table-not-allowed', '业务表不存在：' + table)
  return rows.filter((r) => !r.hidden).map((r) => r.name)
}

function existsByKeys(db, table, keys) {
  if (!keys || !Object.keys(keys).length) return false
  const w = buildWhere(keys)
  const row = db.prepare('SELECT 1 AS hit FROM ' + q(table) + w.sql + ' LIMIT 1').get(...w.params)
  return !!row
}

function normalizeOp(raw, app, clientId, bodySchemaVer, at) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new SyncRequestError(400, 'bad-op', '非法 op')
  }
  const op = { ...raw }
  if (op.app !== undefined && op.app !== app) throw new SyncRequestError(400, 'bad-app', 'op.app 不匹配')
  op.app = app
  if (typeof op.opId !== 'string' || !op.opId.trim()) {
    throw new SyncRequestError(400, 'bad-op', 'op 缺少 opId')
  }
  if (op.schemaVer === undefined) op.schemaVer = bodySchemaVer
  if (bodySchemaVer !== undefined && op.schemaVer !== bodySchemaVer) {
    throw new SyncRequestError(400, 'schema-mismatch', 'op.schemaVer 与 batch 不一致')
  }
  if (!op.clientId) op.clientId = clientId || ''
  if (!op.at) op.at = at
  return op
}

function executeBusinessOp(db, app, op, keys, spec) {
  const table = op.table
  const values = sanitizeKvValues(app, table, op.values)
  if (spec.mode === 'append-only') {
    if (existsByKeys(db, table, keys)) return { status: 'duplicate', effectiveOp: op.op, revived: false }
  }
  // replace/import 的落地语义是整行覆盖（upsert）；权限由 batch 入口逐 op 校验，这里不重复判角色。
  if (isPrivilegedOp(op)) {
    const built = buildReplace(table, values)
    db.prepare(built.sql).run(...built.params)
    return { status: 'applied', effectiveOp: 'upsert', revived: false }
  }
  if (op.op === 'delete') {
    if (!existsByKeys(db, table, keys)) return { status: 'duplicate', effectiveOp: 'delete', revived: false }
    const built = buildDelete(table, op.where)
    db.prepare(built.sql).run(...built.params)
    return { status: 'applied', effectiveOp: 'delete', revived: false }
  }
  if (op.op === 'insert') {
    const built = buildInsert(table, values)
    try {
      db.prepare(built.sql).run(...built.params)
    } catch (e) {
      // 两端各自 insert 同一业务主键：这是可预期的业务冲突，不是 server 故障。
      // 转 409 让客户端按 4xx 隔离毒丸；否则 5xx 会触发无限退避重试。
      if (isConstraintConflict(e)) {
        throw new SyncRequestError(409, 'pk-conflict', '主键冲突：' + (e && e.message || e), { table, keys })
      }
      throw e
    }
    return { status: 'applied', effectiveOp: 'insert', revived: false }
  }
  if (op.op === 'upsert') {
    const built = buildReplace(table, values)
    db.prepare(built.sql).run(...built.params)
    return { status: 'applied', effectiveOp: 'upsert', revived: false }
  }
  const built = buildUpdate(table, values, op.where)
  const res = db.prepare(built.sql).run(...built.params)
  if (Number(res.changes) > 0) return { status: 'applied', effectiveOp: 'update', revived: false }
  if (spec.mode === 'append-only') {
    throw new SyncRequestError(400, 'update-miss', '目标行已被远端删除，追加表不允许复活')
  }
  const revive = buildReplace(table, values)
  db.prepare(revive.sql).run(...revive.params)
  return { status: 'revived', effectiveOp: 'upsert', revived: true }
}

/* ---------------- 错误知识库自动归类（rev7.3） ---------------- */
function keTablesReady(db) {
  const rows = db.prepare(`SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ke_doc','ke_vec','ke_cluster')`).all()
  return rows.length === 3
}

/**
 * 对本批触及的 ke_doc/ke_vec 行重算 cluster_id 并在事务内覆写。
 * 判定 = bestClusterMatch(条目向量, 各簇 seed 向量)：≥ 阈值归入，否则未归类（''）。
 * 无簇 / 表不存在（旧备份启动的 server）→ 直接跳过，客户端预判值保持原样。
 * 修正行以 server 事件（opId 前缀 ke-assign-）写入 sync_changes 并广播。
 */
function autoAssignClusters(db, app, hashes, state, events, at) {
  if (!hashes || !hashes.size || !keTablesReady(db)) return
  let seedRows
  try {
    seedRows = db.prepare(`SELECT kc.id AS id, kd.exception AS exception, kv.vec AS vec
      FROM ke_cluster kc
      LEFT JOIN ke_doc kd ON kd.hash = kc.seed_hash
      LEFT JOIN ke_vec kv ON kv.hash = kc.seed_hash`).all()
  } catch (e) { return }
  const seeds = seedRows.map((s) => {
    let vec = null
    try { vec = s.vec ? decodeVec(s.vec) : null } catch (e) { vec = null }
    return { id: s.id, vec, exception: s.exception || '' }
  }).filter((s) => s.vec)
  for (const hash of hashes) {
    if (!hash) continue
    const doc = db.prepare('SELECT * FROM ke_doc WHERE hash=?').get(hash)
    if (!doc) continue
    const vrow = db.prepare('SELECT vec FROM ke_vec WHERE hash=?').get(hash)
    let vec = null
    try { vec = vrow && vrow.vec ? decodeVec(vrow.vec) : null } catch (e) { vec = null }
    if (!vec) continue
    const best = bestClusterMatch(vec, doc.exception, seeds, AUTO_ASSIGN_THRESHOLD)
    const target = best ? best.id : ''
    if ((doc.cluster_id || '') === target) continue
    db.prepare('UPDATE ke_doc SET cluster_id=? WHERE hash=?').run(target, hash)
    state.appRev += 1
    const appRev = state.appRev
    const fresh = db.prepare('SELECT * FROM ke_doc WHERE hash=?').get(hash)
    const opId = 'ke-assign-' + appRev
    db.prepare(`INSERT INTO sync_changes
      (app, appRev, opId, table_name, op, keys_json, values_json, where_json, clientId, at)
      VALUES(?,?,?,?,?,?,?,?,?,?)`)
      .run(app, appRev, opId, 'ke_doc', 'update', JSON.stringify({ hash }), JSON.stringify(fresh), JSON.stringify({ hash }), '', at)
    db.prepare('INSERT INTO sync_applied(app, opId, appRev) VALUES(?,?,?)').run(app, opId, appRev)
    events.push({
      type: 'row-change', app, appRev, opId, clientId: '', at,
      op: 'update', table: 'ke_doc', keys: { hash }, values: fresh, where: { hash }, serverAssigned: true
    })
  }
}

function prune(db, app, state) {  const cutoff = new Date(Date.now() - KEEP_DAYS * 86400000).toISOString()
  const row = db.prepare('SELECT MIN(appRev) AS oldest, COUNT(*) AS n FROM sync_changes WHERE app=? AND at>=?')
    .get(app, cutoff)
  const oldestByTime = Number(row && row.oldest) || (state.appRev + 1)
  const oldestByCount = Math.max(1, state.appRev - KEEP_CHANGES + 1)
  state.minAppRev = Math.max(oldestByTime, oldestByCount)
  db.prepare('DELETE FROM sync_changes WHERE app=? AND appRev<?').run(app, state.minAppRev)
  db.prepare('DELETE FROM sync_applied WHERE app=? AND appRev<?').run(app, state.minAppRev)
  writeSyncState(db, app, state)
}

/**
 * 单事务提交一批行级 op。返回结果含 results/events/audit，调用方负责 commit 后广播与写人工审计。
 */
export function applyBatch(db, app, ops, clientId = '', bodySchemaVer = undefined, opts = {}) {
  if (!Array.isArray(ops) || !ops.length) throw new SyncRequestError(400, 'bad-batch', 'ops 必须是非空数组')
  if (ops.length > MAX_BATCH_OPS) throw new SyncRequestError(413, 'batch-too-large', '单批 op 超过 ' + MAX_BATCH_OPS)
  const at = nowIso()
  const results = []
  const events = []
  const audit = []
  let state = null
  // rev7.3：本批触及的知识库行（auto 的 ke_doc/ke_vec），op 循环结束后统一重算归类
  let keHashes = null

  db.exec('BEGIN IMMEDIATE')
  try {
    state = readSyncState(db, app)
    for (const raw of ops) {
      const op = normalizeOp(raw, app, clientId, bodySchemaVer === undefined ? SYNC_SCHEMA_VER : bodySchemaVer, at)
      const columns = businessColumns(db, op.table)
      const check = validateSyncOp(app, op, columns)
      if (!check.ok) throw new SyncRequestError(400, check.code, check.message)
      if (app === 'auto' && (op.table === 'ke_doc' || op.table === 'ke_vec')) {
        const h = (check.keys && check.keys.hash) || (op.values && op.values.hash) || null
        if (h) { (keHashes ||= new Set()).add(h) }
      }
      // op 级二次校验（§4.1-②）：replace/导入类 op 即使 batch 入口是 L2 也必须 L3。
      // 抛错发生在事务内 → 整批 ROLLBACK、appRev 不推进，不部分成功。
      if (isPrivilegedOp(op) && !(typeof opts.canOp === 'function' && opts.canOp(op))) {
        throw new SyncRequestError(403, 'forbidden', '导入/清轮重导需要 L3 权限')
      }
      const dup = db.prepare('SELECT appRev FROM sync_applied WHERE app=? AND opId=?').get(app, op.opId)
      if (dup) {
        const dupRev = Number(dup.appRev) || 0
        results.push({ opId: op.opId, appRev: dupRev, status: 'duplicate' })
        audit.push({ type: 'duplicate', app, appRev: dupRev, opId: op.opId, clientId: op.clientId, table: op.table, at })
        continue
      }
      const outcome = executeBusinessOp(db, app, op, check.keys, check.spec)
      if (outcome.status === 'duplicate') {
        results.push({ opId: op.opId, appRev: state.appRev, status: 'duplicate' })
        audit.push({ type: 'duplicate', app, appRev: state.appRev, opId: op.opId, clientId: op.clientId, table: op.table, at, reason: 'business-key-exists' })
        continue
      }
      state.appRev += 1
      const appRev = state.appRev
      const values = sanitizeKvValues(app, op.table, op.values)
      db.prepare(`INSERT INTO sync_changes
        (app, appRev, opId, table_name, op, keys_json, values_json, where_json, clientId, at)
        VALUES(?,?,?,?,?,?,?,?,?,?)`)
        .run(app, appRev, op.opId, op.table, outcome.effectiveOp,
          JSON.stringify(check.keys), values ? JSON.stringify(values) : null,
          op.where ? JSON.stringify(op.where) : null, op.clientId || '', at)
      db.prepare('INSERT INTO sync_applied(app, opId, appRev) VALUES(?,?,?)').run(app, op.opId, appRev)
      const ev = {
        type: 'row-change', app, appRev, opId: op.opId, clientId: op.clientId || '', at,
        op: outcome.effectiveOp, table: op.table, keys: check.keys,
        values: values || null, where: op.where || null
      }
      if (outcome.revived) ev.revived = true
      events.push(ev)
      results.push({ opId: op.opId, appRev, status: outcome.status })
      audit.push({ ...ev, status: outcome.status })
    }
    // 知识库自动归类：与本批同一事务（原子），修正行随事件一起广播
    if (keHashes && keHashes.size) autoAssignClusters(db, app, keHashes, state, events, at)
    prune(db, app, state)
    db.exec('COMMIT')
  } catch (e) {
    try { db.exec('ROLLBACK') } catch (e2) {}
    // 兜底：upsert/revive 等其他 DML 若也撞上约束，同样按 409 处理，避免泄漏成 500。
    if (!(e instanceof SyncRequestError) && isConstraintConflict(e)) {
      throw new SyncRequestError(409, 'pk-conflict', '主键冲突：' + (e && e.message || e))
    }
    throw e
  }
  return { ok: true, app, appRev: state.appRev, minAppRev: state.minAppRev, applied: events.length, results, events, audit }
}

export function queryChanges(db, app, since, limit = MAX_BATCH_OPS) {
  const from = Number.isFinite(Number(since)) && Number(since) > 0 ? Math.floor(Number(since)) : 0
  const state = readSyncState(db, app)
  if (from < state.minAppRev - 1) {
    throw new SyncRequestError(410, 'sync-required', '补包窗口已过期', {
      reason: 'changes-expired', minAppRev: state.minAppRev, appRev: state.appRev
    })
  }
  const rows = db.prepare('SELECT * FROM sync_changes WHERE app=? AND appRev>? ORDER BY appRev LIMIT ?')
    .all(app, from, Math.max(1, Math.floor(limit)))
  const changes = rows.map((r) => ({
    type: 'row-change', app: r.app, appRev: Number(r.appRev), opId: r.opId,
    clientId: r.clientId || '', at: r.at, op: r.op, table: r.table_name,
    keys: JSON.parse(r.keys_json || '{}'),
    values: r.values_json ? JSON.parse(r.values_json) : null,
    where: r.where_json ? JSON.parse(r.where_json) : null
  }))
  return { ok: true, app, since: from, changes, appRev: state.appRev, minAppRev: state.minAppRev }
}

export function markBackupRestored(db, app, clientId = '') {
  const state = readSyncState(db, app)
  state.appRev += 1
  const at = nowIso()
  db.prepare(`INSERT INTO sync_changes
    (app, appRev, opId, table_name, op, keys_json, values_json, where_json, clientId, at)
    VALUES(?,?,?,?,?,?,?,?,?,?)`)
    .run(app, state.appRev, 'backup-restored-' + state.appRev, '', 'sync-required', '{}', null, null, clientId, at)
  state.minAppRev = Math.min(state.minAppRev, state.appRev)
  writeSyncState(db, app, state)
  return { appRev: state.appRev, minAppRev: state.minAppRev, at, clientId }
}
