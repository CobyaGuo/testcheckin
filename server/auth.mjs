// server/auth.mjs — auto/perf server 账号壳：scrypt 用户、实时身份会话、用户管理、SSE ticket
//
// 真源：docs/auto-perf-auth-design.md rev5 §4 / §4.1 / §4.2
// 红线：
//   · 会话只存 userName，不存 role/apps 快照；每次 resolve 回查内存用户表，
//     删除 / 降级 / 改密后的已签发会话下一次请求即失效。
//   · 只依赖 node:crypto / node:fs，零新依赖；users.json 只在本模块内读写。
//   · users.json / audit.jsonl 禁止入库（gitignore）。

import crypto from 'node:crypto'
import fsp from 'node:fs/promises'
import path from 'node:path'

export const ROLES = Object.freeze(['L1', 'L2', 'L3'])
export const APPS = Object.freeze(['auto', 'perf'])

// 与 shared/backend/capabilities.js 的能力口径一致（server 独立一份，避免浏览器模块反向依赖）。
const ROLE_CAPS = Object.freeze({
  L1: Object.freeze({ view: 1, search: 1, export: 1, localw: 1, passwd: 1, mode: 1, sync: 1, prefs: 1 }),
  L2: Object.freeze({ view: 1, search: 1, export: 1, edit: 1, localw: 1, mode: 1, passwd: 1, sync: 1, prefs: 1 }),
  L3: Object.freeze({ view: 1, search: 1, export: 1, edit: 1, import: 1, backup: 1, config: 1, audit: 1, users: 1, localw: 1, mode: 1, passwd: 1, sync: 1, prefs: 1 })
})

export function can(user, cap) {
  return !!(user && ROLE_CAPS[user.role] && ROLE_CAPS[user.role][cap])
}

export function canAccess(user, app) {
  return !!(user && Array.isArray(user.apps) && user.apps.includes(app))
}

const NAME_RE = /^[a-z0-9_-]{3,32}$/
const SESSION_MS = 7 * 24 * 3600 * 1000
const SESSION_MS_TEMP = 12 * 3600 * 1000
// 会话滑动续期的绝对上限（评审修复）：活跃会话最多 30 天必须重登录一次。
const SESSION_ABS_MS = 30 * 24 * 3600 * 1000
const TICKET_MS = 30 * 1000
const LOGIN_WINDOW_MS = 5 * 60 * 1000
const MAX_LOGIN_FAILS = 5
// loginFails 兜底容量：异常洪峰下按最旧条目淘汰（近似 LRU），防内存无界增长。
const MAX_FAIL_KEYS = 10000

/**
 * 容量超限按最旧条目淘汰（评审修复）：绝对禁用整体 clear()——洪峰攻击者
 * 可以借自刷失败把别人的合法锁定（甚至自己被锁的条目）一并清掉。
 * Map 迭代序 = 插入序，删最早的 key 即近似 LRU；纯函数导出便于单测。
 */
export function evictOldest(map, max) {
  if (map.size <= max) return 0
  let n = 0
  for (const key of map.keys()) {
    if (map.size <= max) break // map.size 已随删除递减，直接比较即可
    map.delete(key)
    n++
  }
  return n
}
const PASSWORD_MAX = 128

function now() { return Date.now() }
function nowIso() { return new Date().toISOString() }

export function validateUserName(name) {
  return typeof name === 'string' && NAME_RE.test(name)
}

export function validateRole(role) {
  return ROLES.includes(role)
}

export function validateApps(apps) {
  return Array.isArray(apps) && apps.length > 0 && apps.every((a) => APPS.includes(a)) && new Set(apps).size === apps.length
}

export function validatePassword(passwd) {
  return typeof passwd === 'string' && passwd.length >= 8 && passwd.length <= PASSWORD_MAX
}

export function hashPassword(passwd, salt = crypto.randomBytes(16).toString('hex')) {
  // 显式 scrypt 参数（与 Node 默认一致，仅固化口径，老哈希校验不受影响）。
  return { salt, hash: crypto.scryptSync(String(passwd), salt, 64, { N: 16384, r: 8, p: 1 }).toString('hex') }
}

export function verifyPassword(passwd, salt, hash) {
  try {
    const actual = crypto.scryptSync(String(passwd || ''), String(salt || ''), 64, { N: 16384, r: 8, p: 1 })
    const expect = Buffer.from(String(hash || ''), 'hex')
    return actual.length === expect.length && crypto.timingSafeEqual(actual, expect)
  } catch (e) { return false }
}

/** 时序对齐用的哑元凭证：账号不存在时也跑一次等价 scrypt，防用户名枚举（响应时间侧信道）。 */
const DUMMY_CREDENTIALS = hashPassword('!timing-equalizer-dummy')

/** 纯函数：在用户集合的副本上模拟变更，统一执行 rev4-⑯ 的保护规则。 */
export function checkUserMutation(users, actor, action, targetName, patch = {}) {
  const list = Array.isArray(users) ? users : []
  // actor 允许传用户名字符串（API 口径）或 user 对象（CLI / 单测口径）。
  const actorName = actor && typeof actor === 'object' ? actor.name : actor
  const target = list.find((u) => u.name === targetName)
  if (action !== 'add' && !target) return { ok: false, status: 404, code: 'user-not-found', reason: '用户不存在' }

  if (action === 'add') {
    if (!validateUserName(targetName)) return { ok: false, status: 400, code: 'bad-user', reason: '账号需匹配 ^[a-z0-9_-]{3,32}$' }
    if (list.some((u) => u.name === targetName)) return { ok: false, status: 409, code: 'user-exists', reason: '账号已存在' }
    if (!validateRole(patch.role)) return { ok: false, status: 400, code: 'bad-role', reason: '角色必须为 L1/L2/L3' }
    if (!validateApps(patch.apps)) return { ok: false, status: 400, code: 'bad-apps', reason: 'apps 必须为非空的子集（auto/perf）' }
    if (!validatePassword(patch.password)) return { ok: false, status: 400, code: 'bad-password', reason: '密码需 8–128 位' }
    const next = list.concat([{ name: targetName, role: patch.role, apps: [...patch.apps] }])
    if (!next.some((u) => u.role === 'L3')) return { ok: false, status: 400, code: 'last-admin', reason: '系统内必须至少保留一个 L3 账号' }
    return { ok: true }
  }

  if (action === 'del') {
    if (actorName && actorName === targetName) return { ok: false, status: 400, code: 'self-denied', reason: '不能删除自己' }
    const next = list.filter((u) => u.name !== targetName)
    if (!next.some((u) => u.role === 'L3')) return { ok: false, status: 400, code: 'last-admin', reason: '系统内必须至少保留一个 L3 账号' }
    return { ok: true }
  }

  if (action === 'edit') {
    const nextRole = patch.role === undefined ? target.role : patch.role
    const nextApps = patch.apps === undefined ? [...target.apps] : patch.apps
    if (!validateRole(nextRole)) return { ok: false, status: 400, code: 'bad-role', reason: '角色必须为 L1/L2/L3' }
    if (!validateApps(nextApps)) return { ok: false, status: 400, code: 'bad-apps', reason: 'apps 必须为非空的子集（auto/perf）' }
    if (patch.password !== undefined && !validatePassword(patch.password)) {
      return { ok: false, status: 400, code: 'bad-password', reason: '密码需 8–128 位' }
    }
    if (actorName && actorName === targetName) {
      if (nextRole !== 'L3') return { ok: false, status: 400, code: 'self-denied', reason: '不能降级自己' }
      if (!nextApps.includes('auto') || !nextApps.includes('perf')) {
        return { ok: false, status: 400, code: 'self-denied', reason: '不能缩减自己的应用访问权' }
      }
    }
    const next = list.map((u) => u.name === targetName ? { ...u, role: nextRole, apps: [...nextApps] } : u)
    if (!next.some((u) => u.role === 'L3')) return { ok: false, status: 400, code: 'last-admin', reason: '系统内必须至少保留一个 L3 账号' }
    return { ok: true }
  }

  return { ok: false, status: 400, code: 'bad-action', reason: '未知操作' }
}

/**
 * 创建鉴权实例。noAuth=true 仅供测试回退（--no-auth）：不校验凭证，返回一个内存 L3 全权账号。
 */
export async function createAuth({ dataDir, config = null, noAuth = false } = {}) {
  if (!dataDir) throw new Error('auth 缺少 dataDir')
  const userFile = path.join(dataDir, 'users.json')
  const sessions = new Map()
  const tickets = new Map()
  const loginFails = new Map()

  let users = []
  let noAuthUser = null
  if (noAuth) {
    noAuthUser = { name: 'noauth', role: 'L3', apps: [...APPS], createdAt: nowIso(), salt: '', hash: '' }
    users = [noAuthUser]
  } else {
    try {
      const parsed = JSON.parse(await fsp.readFile(userFile, 'utf8'))
      users = Array.isArray(parsed && parsed.users) ? parsed.users : []
    } catch (e) {
      if (e && e.code !== 'ENOENT') throw e
      users = []
    }
    if (!users.length) {
      throw new Error('users.json 不存在或没有任何用户：请先用 `node scripts/server-user.mjs --add admin01 --role L3 --apps auto,perf --passwd <密码>` 创建首个 L3 管理员（不允许 API 自助注册）')
    }
  }

  async function saveUsers() {
    if (noAuth) return
    const tmp = userFile + '.tmp-' + process.pid
    await fsp.writeFile(tmp, JSON.stringify({ users }, null, 2) + '\n', { mode: 0o600 })
    await fsp.rename(tmp, userFile)
  }

  function publicUser(u) {
    return u && { name: u.name, role: u.role, apps: [...(u.apps || [])], createdAt: u.createdAt }
  }

  function findUser(name) { return users.find((u) => u.name === name) || null }

  function reap() {
    const t = now()
    for (const [token, s] of sessions) if (s.expiresAt <= t) sessions.delete(token)
    for (const [id, s] of tickets) if (s.expiresAt <= t) tickets.delete(id)
    // loginFails 清理（评审修复）：锁定过期的 + 窗口滑走仍未达阈值的（until=0 陈旧条目）
    // 都回收，防止长期运行下 Map 无界增长。
    for (const [key, rec] of loginFails) {
      const expired = rec.until > 0 ? rec.until <= t : (t - rec.firstAt >= LOGIN_WINDOW_MS)
      if (expired) loginFails.delete(key)
    }
  }

  /** 每次请求都回查内存用户表；用户被删 / 降级 / 改密后旧 token 立即失效。 */
  function resolve(token, opts = {}) {
    if (!token) return null
    const s = sessions.get(token)
    if (!s) return null
    const t = now()
    if (s.expiresAt <= t) { sessions.delete(token); return null }
    // 滑动续期的绝对上限：无论多活跃，超 30 天必须重新登录。
    if (s.absExpiresAt && s.absExpiresAt <= t) { sessions.delete(token); return null }
    const u = findUser(s.userName)
    if (!u) { sessions.delete(token); return null }
    if (!noAuth && u.passwordChangedAt && s.issuedAt < u.passwordChangedAt) { sessions.delete(token); return null }
    if (s.sliding && opts.touch !== false) s.expiresAt = t + SESSION_MS
    return publicUser(u)
  }

  function dropSessions(name) {
    for (const [token, s] of sessions) if (s.userName === name) sessions.delete(token)
  }

  // 登录限流（评审修复）改为双维度：
  //   · `ip|账号` 组合键——NAT/办公网下不同账号互不影响，整网段不再被一人锁死；
  //   · 纯账号键——防分布式换 IP 撞同一个账号。
  // 任一维度达到阈值即锁 5 分钟。条目由 reap() 定期清理，容量超限按最旧条目淘汰（evictOldest）。
  const ACCT_FAIL_PREFIX = 'acct:'
  function acctKey(name) { return ACCT_FAIL_PREFIX + String(name || '').toLowerCase().slice(0, 64) }
  function pairKey(ip, name) { return String(ip || 'unknown') + '|' + String(name || '').toLowerCase().slice(0, 64) }

  function bumpFail(ip, name) {
    for (const key of [pairKey(ip, name), acctKey(name)]) {
      const rec = loginFails.get(key)
      const fresh = !rec || now() - rec.firstAt >= LOGIN_WINDOW_MS
      const fails = (fresh ? 0 : rec.fails) + 1
      loginFails.set(key, {
        fails, firstAt: fresh ? now() : rec.firstAt,
        until: fails >= MAX_LOGIN_FAILS ? now() + LOGIN_WINDOW_MS : 0
      })
    }
    // 容量超限按最旧条目淘汰（评审修复），不再整体清空——否则洪峰可把合法锁定一并清掉。
    evictOldest(loginFails, MAX_FAIL_KEYS)
  }

  function isLocked(ip, name) {
    const t = now()
    for (const key of [pairKey(ip, name), acctKey(name)]) {
      const rec = loginFails.get(key)
      if (rec && rec.until > t) return true
    }
    return false
  }

  function login(name, password, opts = {}) {
    if (noAuth) return { ok: true, token: 'noauth', user: publicUser(noAuthUser) }
    const ip = String(opts.ip || 'unknown')
    if (isLocked(ip, name)) return { ok: false, status: 429, code: 'too-many-attempts', message: '登录失败次数过多，请 5 分钟后再试' }
    const u = findUser(String(name || ''))
    let passOk = false
    if (u) {
      passOk = verifyPassword(password, u.salt, u.hash)
    } else {
      // 时序对齐：账号不存在也跑一次等价 scrypt，防响应时间差枚举有效账号名。
      verifyPassword(password, DUMMY_CREDENTIALS.salt, DUMMY_CREDENTIALS.hash)
    }
    if (!passOk) {
      bumpFail(ip, name)
      return { ok: false, status: 401, code: 'invalid-credentials', message: '账号或密码错误' }
    }
    loginFails.delete(pairKey(ip, u.name))
    loginFails.delete(acctKey(u.name))
    const token = crypto.randomBytes(32).toString('hex')
    const remember = !!opts.remember
    const expiresAt = now() + (remember ? SESSION_MS : SESSION_MS_TEMP)
    sessions.set(token, {
      userName: u.name, issuedAt: now(),
      expiresAt, absExpiresAt: now() + SESSION_ABS_MS, sliding: remember
    })
    return { ok: true, token, user: publicUser(u), expiresAt }
  }

  function logout(token) {
    if (!token) return false
    return sessions.delete(token)
  }

  function changePassword(token, oldPassword, newPassword) {
    const s = token && sessions.get(token)
    if (!s) return { ok: false, status: 401, code: 'unauthorized', message: '请先登录' }
    const u = findUser(s.userName)
    if (!u) return { ok: false, status: 401, code: 'unauthorized', message: '请先登录' }
    if (!verifyPassword(oldPassword, u.salt, u.hash)) return { ok: false, status: 400, code: 'bad-password', message: '原密码错误' }
    if (!validatePassword(newPassword)) return { ok: false, status: 400, code: 'bad-password', message: '新密码需 8–128 位' }
    const { salt, hash } = hashPassword(newPassword)
    u.salt = salt
    u.hash = hash
    u.passwordChangedAt = now()
    return saveUsers().then(() => {
      dropSessions(u.name) // 改密后包括自己在内的旧会话全部失效，重新登录
      return { ok: true }
    })
  }

  function issueTicket(user, app) {
    if (!user || !canAccess(user, app)) return null
    const id = crypto.randomBytes(24).toString('hex')
    tickets.set(id, { userName: user.name, app, expiresAt: now() + TICKET_MS })
    return { ticket: id, expiresIn: TICKET_MS / 1000 }
  }

  function consumeTicket(id, app, token, res) {
    const t = tickets.get(String(id || ''))
    if (!t || t.expiresAt <= now() || t.app !== app) return null
    tickets.delete(t.id || String(id))
    const user = token ? resolve(token) : findUser(t.userName)
    if (!user || !canAccess(user, app)) return null
    return publicUser(user)
  }

  function listUsers() { return users.map(publicUser) }

  function addUser(actor, name, patch) {
    const check = checkUserMutation(users, actor, 'add', String(name || ''), patch)
    if (!check.ok) return check
    const { salt, hash } = hashPassword(patch.password)
    users.push({ name: String(name), role: patch.role, apps: [...patch.apps], salt, hash, createdAt: nowIso() })
    return saveUsers().then(() => ({ ok: true, user: publicUser(findUser(String(name))) }))
  }

  function updateUser(actor, name, patch) {
    const check = checkUserMutation(users, actor, 'edit', String(name || ''), patch)
    if (!check.ok) return check
    const u = findUser(String(name))
    if (patch.role !== undefined) u.role = patch.role
    if (patch.apps !== undefined) u.apps = [...patch.apps]
    if (patch.password !== undefined) {
      const { salt, hash } = hashPassword(patch.password)
      u.salt = salt
      u.hash = hash
      u.passwordChangedAt = now()
      dropSessions(u.name)
    }
    return saveUsers().then(() => ({ ok: true, user: publicUser(u) }))
  }

  function deleteUser(actor, name) {
    const check = checkUserMutation(users, actor, 'del', String(name || ''), {})
    if (!check.ok) return check
    users = users.filter((u) => u.name !== String(name))
    dropSessions(String(name))
    return saveUsers().then(() => ({ ok: true }))
  }

  return {
    noAuth, userFile, isNoAuth: () => noAuth,
    resolve, activeUser: (name) => publicUser(findUser(String(name || ''))),
    login, logout, changePassword, issueTicket, consumeTicket,
    listUsers, addUser, updateUser, deleteUser, dropSessions, reap,
    // 供 CLI / e2e 离线使用，不开放 HTTP
    _rawUsers: () => users
  }
}
