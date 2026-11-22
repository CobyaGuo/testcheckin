// dist/server/user.mjs — users.json 离线 bootstrap / 运维兜底 CLI
//
// 双写者约束（设计 rev4-⑮）：server 运行期用户管理一律走 UI/API；本 CLI 仅供离线 bootstrap。
// 启动先探测 GET /api/health 精简版：可达则拒绝写操作（--list 只读放行，但标注文件态）。

import fsp from 'node:fs/promises'
import path from 'node:path'
import { loadServerConfig } from './config.mjs'
import {
  hashPassword, validateUserName, validateRole, validateApps, validatePassword,
  checkUserMutation
} from './auth.mjs'

const USAGE = `用法：node user.mjs <命令> [参数]

命令：
  --list                     列出用户（文件态；server 运行中以 API 为准）
  --add <name>               新增用户，需配 --role / --apps / --passwd
  --del <name>               删除用户
  --role <L1|L2|L3>          角色（--add 必填；配合 --name 可改角色）
  --apps <auto,perf>         应用访问（--add 必填）
  --passwd <password>        密码（--add 必填，至少 8 位）
  --data-dir <path>          覆盖 users.json 所在数据目录

示例：
  node user.mjs --add admin01 --role L3 --apps auto,perf --passwd 'StrongPass1'
`

function arg(name) {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 ? (process.argv[i + 1] || '') : ''
}

function has(name) { return process.argv.includes('--' + name) }

async function serverReachable(cfg) {
  const url = 'http://' + (cfg.host === '::1' ? '[::1]' : cfg.host) + ':' + cfg.port + '/api/health'
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 1000)
  try {
    const r = await fetch(url, { signal: ctrl.signal })
    return r.ok
  } catch (e) { return false } finally { clearTimeout(timer) }
}

if (has('help') || process.argv.length <= 2) {
  console.log(USAGE)
  process.exit(0)
}

const cfg = await loadServerConfig()
const dataDir = arg('data-dir') ? path.resolve(arg('data-dir')) : cfg.dataDir
const userFile = path.join(dataDir, 'users.json')
const reachable = await serverReachable(cfg)

async function loadUsers() {
  try {
    const parsed = JSON.parse(await fsp.readFile(userFile, 'utf8'))
    return Array.isArray(parsed && parsed.users) ? parsed.users : []
  } catch (e) {
    if (e && e.code !== 'ENOENT') throw e
    return []
  }
}

async function saveUsers(users) {
  await fsp.mkdir(dataDir, { recursive: true })
  const tmp = userFile + '.tmp-' + process.pid
  await fsp.writeFile(tmp, JSON.stringify({ users }, null, 2) + '\n', { mode: 0o600 })
  await fsp.rename(tmp, userFile)
}

function fail(reason) { console.error('失败：' + reason); process.exit(1) }

if (has('list')) {
  const users = await loadUsers()
  console.log(JSON.stringify(users.map((u) => ({ name: u.name, role: u.role, apps: u.apps, createdAt: u.createdAt })), null, 2))
  console.log('以下为文件态，运行时状态以 server 为准')
  process.exit(0)
}

if (reachable) fail('server 运行中（' + cfg.host + ':' + cfg.port + '），请通过门户的用户管理界面操作；CLI 写操作仅在 server 停止时可用。')

let users = await loadUsers()
const actor = null // CLI 离线 bootstrap，不做「不能删/降级自己」判定（没有当前登录者）

if (has('add')) {
  const name = arg('add')
  const role = arg('role')
  const apps = arg('apps') ? arg('apps').split(',').map((s) => s.trim()).filter(Boolean) : []
  const passwd = arg('passwd')
  if (!validateUserName(name)) fail('账号需匹配 ^[a-z0-9_-]{3,32}$')
  if (!validateRole(role)) fail('角色必须为 L1/L2/L3')
  if (!validateApps(apps)) fail('apps 必须为非空子集（auto,perf）')
  if (!validatePassword(passwd)) fail('密码至少 8 位')
  const check = checkUserMutation(users, actor, 'add', name, { role, apps, password: passwd })
  if (!check.ok) fail(check.reason)
  const { salt, hash } = hashPassword(passwd)
  users.push({ name, role, apps, salt, hash, createdAt: new Date().toISOString() })
  await saveUsers(users)
  console.log('已创建用户 ' + name + '（' + role + '，' + apps.join('+') + '）')
  process.exit(0)
}

if (has('del')) {
  const name = arg('del')
  const check = checkUserMutation(users, actor, 'del', name, {})
  if (!check.ok) fail(check.reason)
  users = users.filter((u) => u.name !== name)
  await saveUsers(users)
  console.log('已删除用户 ' + name)
  process.exit(0)
}

const name = arg('name')
if (name && (has('role') || has('apps') || has('passwd'))) {
  const patch = {}
  if (has('role')) patch.role = arg('role')
  if (has('apps')) patch.apps = arg('apps').split(',').map((s) => s.trim()).filter(Boolean)
  if (has('passwd')) patch.password = arg('passwd')
  const check = checkUserMutation(users, actor, 'edit', name, patch)
  if (!check.ok) fail(check.reason)
  const u = users.find((x) => x.name === name)
  if (patch.role !== undefined) u.role = patch.role
  if (patch.apps !== undefined) u.apps = patch.apps
  if (patch.password !== undefined) {
    const { salt, hash } = hashPassword(patch.password)
    u.salt = salt
    u.hash = hash
    u.passwordChangedAt = new Date().toISOString()
  }
  await saveUsers(users)
  console.log('已更新用户 ' + name)
  process.exit(0)
}

console.log(USAGE)
