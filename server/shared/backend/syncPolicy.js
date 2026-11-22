// shared/backend/syncPolicy.js — auto/perf 行级同步的表、键与 KV 白名单
// 浏览器与 Node server 共用；纯函数，零平台 API。
// 真源：docs/auto-perf-realtime-sync-design.md §5.2 / §7.1。新增键必须先改设计。

export const SYNC_SCHEMA_VER = 1
export const MAX_BATCH_OPS = 500
export const KEEP_DAYS = 7
export const KEEP_CHANGES = 100000
export const SYNC_APPS = Object.freeze(['auto', 'perf'])

const UPSERT = 'upsert'
const APPEND = 'append-only'

export const SYNC_TABLES = Object.freeze({
  auto: Object.freeze({
    auto_round: { keys: ['id'], mode: UPSERT },
    auto_case: { keys: ['key'], mode: UPSERT },
    auto_result: { keys: ['round_id', 'case_key'], mode: UPSERT },
    auto_annotation: { keys: ['round_id', 'case_key'], mode: UPSERT },
    auto_attempt: { keys: ['round_id', 'case_key', 'seq'], mode: APPEND },
    auto_job_url: { keys: ['round_id', 'job_name'], mode: UPSERT },
    auto_import_fp: { keys: ['round_id', 'fp'], mode: UPSERT },
    auto_assign: { keys: ['round_id', 'case_key'], mode: UPSERT },
    // 错误知识库（rev7.3）：元数据/向量/簇全部同步，谁都能读；向量 TEXT(f32 base64) 不是
    // BLOB 列（__blob_b64 拦截不触发），且只在新条目 insert 时写一次，无更新放大。
    ke_doc: { keys: ['hash'], mode: UPSERT },
    ke_vec: { keys: ['hash'], mode: UPSERT },
    ke_cluster: { keys: ['id'], mode: UPSERT },
    auto_kv: { keys: ['k'], mode: UPSERT, kv: true }
  }),
  perf: Object.freeze({
    perf_version: { keys: ['id'], mode: UPSERT },
    perf_case: { keys: ['id'], mode: UPSERT },
    perf_country: { keys: ['id'], mode: UPSERT },
    perf_scenario: { keys: ['id'], mode: UPSERT },
    perf_record: { keys: ['uid'], mode: UPSERT },
    perf_point: { keys: ['uid', 'api', 'metric'], mode: UPSERT },
    perf_kv: { keys: ['k'], mode: UPSERT, kv: true }
  })
})

const SHARED_KV = Object.freeze({
  auto: Object.freeze(new Set([
    'settings', 'jobMap', 'dimMap', 'jobUnmatched', 'assignees', 'failureTypes'
  ])),
  perf: Object.freeze(new Set([
    'settings', 'caseColumns', 'caseIdKey', 'countryColumns', 'countryIdKey',
    'scenarioColumns', 'scenarioIdKey'
  ]))
})

const PERF_SETTINGS_SHARED = Object.freeze(['metrics', 'format', 'resMountFinal'])

function has(obj, key) {
  return !!obj && Object.prototype.hasOwnProperty.call(obj, key) && obj[key] !== undefined
}

function parseJsonObject(text) {
  try {
    const v = JSON.parse(text || '{}')
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {}
  } catch (e) { return {} }
}

// jsonSafe 把 Uint8Array 编码成 {__blob_b64}；同步白名单表当前无 BLOB 列，
// 一旦将来出现，base64 文本会被当普通 TEXT 入库造成类型损坏。这里 fail fast。
function containsBlobMarker(v) {
  if (!v || typeof v !== 'object') return false
  if (Array.isArray(v)) return v.some(containsBlobMarker)
  if (Object.prototype.hasOwnProperty.call(v, '__blob_b64')) return true
  return Object.values(v).some(containsBlobMarker)
}

export function isSyncApp(app) {
  return app === 'auto' || app === 'perf'
}

export function tableSpec(app, table) {
  const map = SYNC_TABLES[app]
  return map && map[table] ? map[table] : null
}

export function isSyncTable(app, table) {
  return !!tableSpec(app, table)
}

/** 从完整 after-image（insert/upsert/update）或 where（delete）提取业务主键。 */
export function keysFromOp(app, op) {
  const spec = tableSpec(app, op && op.table)
  if (!spec) return null
  const source = op.op === 'delete' ? op.where : op.values
  if (!source || typeof source !== 'object' || Array.isArray(source)) return null
  const keys = {}
  for (const col of spec.keys) {
    if (!has(source, col) || source[col] === null || source[col] === '') return null
    keys[col] = source[col]
  }
  return keys
}

export function stableRowKey(app, table, keys) {
  const spec = tableSpec(app, table)
  const parts = spec ? spec.keys : Object.keys(keys || {}).sort()
  return app + ':' + table + ':' + parts.map((k) => k + '=' + JSON.stringify(keys && keys[k])).join('&')
}

export function isSharedKvKey(app, k) {
  const set = SHARED_KV[app]
  return !!(set && set.has(String(k)))
}

function isPerfSettings(values) {
  return values && values.k === 'settings' && has(values, 'v')
}

/** 本地写捕获用：perf_kv.settings 只保留共享嵌套字段，本机目录字段绝不入队。 */
export function sanitizeKvValues(app, table, values) {
  if (!values || app !== 'perf' || table !== 'perf_kv' || !isPerfSettings(values)) return values
  const obj = parseJsonObject(values.v)
  const shared = {}
  for (const key of PERF_SETTINGS_SHARED) if (has(obj, key)) shared[key] = obj[key]
  return { ...values, v: JSON.stringify(shared) }
}

/** 判断一条 KV 本地写是否含有可同步载荷；纯本机目录写返回 false。 */
export function hasSyncableKvPayload(app, table, values) {
  if (!tableSpec(app, table) || !tableSpec(app, table).kv) return true
  const k = values && values.k
  if (!isSharedKvKey(app, k)) return false
  if (app === 'perf' && table === 'perf_kv' && isPerfSettings(values)) {
    const obj = parseJsonObject(values.v)
    return PERF_SETTINGS_SHARED.some((key) => has(obj, key))
  }
  return true
}

/** 远端回写用：覆盖共享字段，保留本机目录字段与未知字段。 */
export function mergeRemoteKvValues(app, table, localValues, remoteValues) {
  if (app !== 'perf' || table !== 'perf_kv' || !isPerfSettings(remoteValues)) return remoteValues
  const local = parseJsonObject(localValues && localValues.v)
  const remote = parseJsonObject(remoteValues.v)
  const merged = { ...local }
  for (const key of PERF_SETTINGS_SHARED) if (has(remote, key)) merged[key] = remote[key]
  return { ...(remoteValues || {}), v: JSON.stringify(merged) }
}

/**
 * 导入 / replace 清轮重导类 op 的判定（鉴权设计 §4.1-②）。
 * 这类 op 与普通 edit 同走 batch 入口，但单独要求 L3；落地语义仍是整行 upsert。
 */
export function isPrivilegedOp(op) {
  return !!(op && (op.op === 'replace' || op.op === 'import'))
}

/** server 在事务内校验 op；columns 为业务表当前列（已过滤 hidden）。 */
export function validateSyncOp(app, rawOp, columns = null) {
  const op = rawOp && typeof rawOp === 'object' && !Array.isArray(rawOp) ? rawOp : null
  if (!op) return { ok: false, code: 'bad-op', message: '非法 op' }
  if (!isSyncApp(app)) return { ok: false, code: 'bad-app', message: '未知 app：' + app }
  if (op.sql !== undefined) return { ok: false, code: 'sql-op-forbidden', message: 'sql op 不允许写穿' }
  const kind = op.op
  if (kind !== 'insert' && kind !== 'upsert' && kind !== 'update' && kind !== 'delete' && !isPrivilegedOp(op)) {
    return { ok: false, code: 'bad-op', message: '未知 op：' + String(kind) }
  }
  const spec = tableSpec(app, op.table)
  if (!spec) return { ok: false, code: 'table-not-allowed', message: '表不在同步白名单：' + String(op.table) }
  const opSource = kind === 'delete' ? op.where : op.values
  if (containsBlobMarker(opSource)) {
    return { ok: false, code: 'blob-not-supported', message: '同步值含 __blob_b64（BLOB 列暂不支持同步）' }
  }
  if (spec.mode === APPEND && kind !== 'insert') {
    return { ok: false, code: 'append-only', message: '追加表只接受 insert：' + op.table }
  }
  if (op.schemaVer !== SYNC_SCHEMA_VER) {
    return { ok: false, code: 'schema-mismatch', message: 'schemaVer 不一致：' + String(op.schemaVer) }
  }
  const keys = keysFromOp(app, op)
  if (!keys) return { ok: false, code: 'missing-keys', message: '无法提取业务主键：' + op.table }
  if (spec.kv) {
    const source = kind === 'delete' ? op.where : op.values
    const k = source && source.k
    if (!isSharedKvKey(app, k) || !hasSyncableKvPayload(app, op.table, kind === 'delete' ? { k } : source)) {
      return { ok: false, code: 'kv-not-shared', message: 'KV 键不同步：' + String(k) }
    }
  }
  if (kind !== 'delete' && (!op.values || typeof op.values !== 'object' || Array.isArray(op.values))) {
    return { ok: false, code: 'missing-values', message: 'update/insert/upsert 必须携带完整 values' }
  }
  if (kind === 'delete' && (!op.where || typeof op.where !== 'object' || Array.isArray(op.where))) {
    return { ok: false, code: 'missing-where', message: 'delete 必须携带 where' }
  }
  if (columns && kind !== 'delete') {
    const values = sanitizeKvValues(app, op.table, op.values)
    for (const col of columns) {
      if (!has(values, col)) {
        return { ok: false, code: 'update-miss', message: 'values 不是完整行，缺少字段：' + col }
      }
    }
  }
  return { ok: true, keys, spec }
}
