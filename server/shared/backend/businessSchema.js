// shared/backend/businessSchema.js — 同步白名单业务表的「server 旧库补建」DDL
// ----------------------------------------------------------------------------
// 背景（2026-11-22 实测）：server 的业务表从来只靠客户端整库备份恢复带入；rev7.3
// 新增的 ke_doc/ke_vec/ke_cluster 三张表，在 rev7.3 之前恢复出来的旧 server 库里
// 不存在 → 客户端 ke op 写穿全部 400 'table-not-allowed'，被隔离成 failed 毒丸，
// 知识库永不跨端同步（A 本地可见、B 永远收不到，刷新/重登录无效）。
// 修复：server openDb → initSyncSchema 对白名单里缺失且这里有 DDL 的表幂等补建。
//
// 单一真源约束：本文件 DDL 必须与客户端建表 schema（src/utils/autoDb.js 的 SCHEMA）
// 逐字一致——autoDb.js 的 ke 三表段直接引用 AUTO_KE_TABLES_DDL，禁止两边各写一份。
// 将来新增同步白名单表时：在这里补一条 DDL（键 = 表名），旧 server 库即自动升级。

const KE_DOC_DDL = `CREATE TABLE IF NOT EXISTS ke_doc (
  hash TEXT PRIMARY KEY,
  stack TEXT DEFAULT '', exception TEXT DEFAULT '', first_frame TEXT DEFAULT '',
  message TEXT DEFAULT '', case_name TEXT DEFAULT '', suite TEXT DEFAULT '', platform TEXT DEFAULT 'auto',
  count INTEGER DEFAULT 0, confirmed INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0,
  first_seen_at TEXT DEFAULT '', last_seen_at TEXT DEFAULT '',
  new_flag INTEGER DEFAULT 0, ref_round_id TEXT DEFAULT '', ref_case_key TEXT DEFAULT '',
  cluster_id TEXT DEFAULT '', emb_ver TEXT DEFAULT '', by_user TEXT DEFAULT '', at TEXT
)`

const KE_VEC_DDL = `CREATE TABLE IF NOT EXISTS ke_vec (
  hash TEXT PRIMARY KEY, vec TEXT DEFAULT '', emb_ver TEXT DEFAULT ''
)`

const KE_CLUSTER_DDL = `CREATE TABLE IF NOT EXISTS ke_cluster (
  id TEXT PRIMARY KEY, seed_hash TEXT NOT NULL, label TEXT DEFAULT '',
  created_at TEXT DEFAULT '', by_user TEXT DEFAULT ''
)`

/** 客户端（src/utils/autoDb.js SCHEMA）与 server 补建共用的 ke 三表 DDL。 */
export const AUTO_KE_TABLES_DDL = [KE_DOC_DDL, KE_VEC_DDL, KE_CLUSTER_DDL].join(';\n') + ';'

/** 按 app 组织的补建 DDL；键 = 表名，仅列「旧 server 库可能缺失」的表。 */
export const ENSURE_TABLE_DDL = Object.freeze({
  auto: Object.freeze({
    ke_doc: KE_DOC_DDL,
    ke_vec: KE_VEC_DDL,
    ke_cluster: KE_CLUSTER_DDL
  }),
  perf: Object.freeze({})
})

/**
 * 幂等补建：只建 sqlite_master 里不存在的表（已存在的旧表绝不动结构）。
 * 返回本次实际新建的表数（0 = 库已是最新结构）。
 */
export function ensureBusinessTables(db, app) {
  const ddl = ENSURE_TABLE_DDL[app]
  if (!ddl) return 0
  let created = 0
  for (const table of Object.keys(ddl)) {
    const row = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(table)
    if (row) continue
    db.exec(ddl[table])
    created += 1
  }
  return created
}
