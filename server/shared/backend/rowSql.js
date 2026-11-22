// shared/backend/rowSql.js — 行级 API 共用的 SQL 构造与标识符护栏
// 浏览器与 Node server 共用；不含任何平台 API。

export function assertIdent(id, what = '标识符') {
  if (typeof id !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(id)) {
    throw new Error('非法 SQL 标识符（' + what + '）：' + JSON.stringify(id))
  }
  return id
}

export function q(id) {
  return '"' + assertIdent(id).replace(/"/g, '""') + '"'
}

function encodeBlob(v) {
  if (v instanceof Uint8Array || v instanceof ArrayBuffer) {
    const u8 = v instanceof Uint8Array ? v : new Uint8Array(v)
    let bin = ''
    for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i])
    const b64 = (typeof btoa === 'function') ? btoa(bin) : Buffer.from(u8).toString('base64')
    return { __blob_b64: b64 }
  }
  return v
}

export function normalizeIn(v) {
  if (v && typeof v === 'object' && typeof v.__blob_b64 === 'string') {
    const b64 = v.__blob_b64
    if (typeof Buffer !== 'undefined') return Buffer.from(b64, 'base64')
    const bin = atob(b64)
    const u8 = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i)
    return u8
  }
  return v
}

export function normalizeValues(values) {
  const out = {}
  for (const [k, v] of Object.entries(values || {})) {
    if (v === undefined) continue
    out[assertIdent(k, '列名')] = encodeBlob(v)
  }
  return out
}

export function buildWhere(where) {
  const parts = []
  const params = []
  for (const [k, v] of Object.entries(where || {})) {
    if (v === undefined) continue
    const col = q(k)
    if (Array.isArray(v)) {
      if (!v.length) { parts.push('0 = 1'); continue }
      parts.push(col + ' IN (' + v.map(() => '?').join(',') + ')')
      params.push(...v.map(normalizeIn))
    } else if (v === null) {
      parts.push(col + ' IS NULL')
    } else {
      parts.push(col + ' = ?')
      params.push(normalizeIn(v))
    }
  }
  return { sql: parts.length ? ' WHERE ' + parts.join(' AND ') : '', params }
}

export function buildInsert(table, values) {
  assertIdent(table, '表名')
  const vals = normalizeValues(values)
  const cols = Object.keys(vals)
  if (!cols.length) throw new Error('INSERT 缺少字段')
  return {
    sql: 'INSERT INTO ' + q(table) + ' (' + cols.map(q).join(',') + ') VALUES (' + cols.map(() => '?').join(',') + ')',
    params: cols.map((c) => normalizeIn(vals[c]))
  }
}

export function buildReplace(table, values) {
  assertIdent(table, '表名')
  const vals = normalizeValues(values)
  const cols = Object.keys(vals)
  if (!cols.length) throw new Error('REPLACE 缺少字段')
  return {
    sql: 'INSERT OR REPLACE INTO ' + q(table) + ' (' + cols.map(q).join(',') + ') VALUES (' + cols.map(() => '?').join(',') + ')',
    params: cols.map((c) => normalizeIn(vals[c]))
  }
}

export function buildUpdate(table, values, where) {
  assertIdent(table, '表名')
  const vals = normalizeValues(values)
  const cols = Object.keys(vals)
  if (!cols.length) throw new Error('UPDATE 缺少字段')
  const w = buildWhere(where)
  if (!w.sql) throw new Error('UPDATE 拒绝无 WHERE 的写操作')
  return {
    sql: 'UPDATE ' + q(table) + ' SET ' + cols.map((c) => q(c) + ' = ?').join(',') + w.sql,
    params: cols.map((c) => normalizeIn(vals[c])).concat(w.params)
  }
}

export function buildDelete(table, where) {
  assertIdent(table, '表名')
  const w = buildWhere(where)
  if (!w.sql) throw new Error('DELETE 拒绝无 WHERE 的写操作')
  return { sql: 'DELETE FROM ' + q(table) + w.sql, params: w.params }
}
