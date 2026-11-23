// dist/server/bootstrap-admin.mjs — 首个 L3 管理员离线引导（随 dist 打包为 dist/server/bootstrap-admin.mjs）
//
// 用途：dist 里 server/data 按部署规则不下发，全新部署的 users.json 为空、无人能登录。
//       本脚本只在「空库」时创建第一个 L3 管理员，是 user.mjs 的收窄特化版：
//       固定 L3 + auto,perf（避免自建者日后被 self-denied 规则锁死），账号可用 --name 指定，
//       密码交互输入不回显、不写进 shell 历史。已有任何用户时直接拒绝，防止覆盖既有账号体系。
//
// 双写者约束（同 user.mjs）：server 运行中拒绝写（探测 /api/health），仅离线 bootstrap 可用。
// 用法：node bootstrap-admin.mjs [--name admin01] [--data-dir <目录>]
//       （密码在交互提示后输入；非 TTY 环境下从 stdin 读一行）

import fsp from 'node:fs/promises'
import path from 'node:path'
import readline from 'node:readline'
import { fileURLToPath } from 'node:url'
import { loadServerConfig } from './config.mjs'
import { checkUserMutation, hashPassword } from './auth.mjs'

export const FIRST_ADMIN_ROLE = 'L3'
export const FIRST_ADMIN_APPS = ['auto', 'perf']

/**
 * 纯函数：首个管理员引导的准入校验。
 * 仅在 users 为空时放行；角色/apps 固定为 L3 + 全应用，复用 checkUserMutation 的统一口径。
 * @param {{name?:string, password?:string, users?:Array}} input
 * @returns {{ok:boolean, reason?:string}}
 */
export function validateBootstrap({ name, password, users } = {}) {
  const list = Array.isArray(users) ? users : []
  if (list.length > 0) {
    return { ok: false, reason: 'users.json 已有用户，首个管理员引导仅允许在空库执行；如需新增请用 node user.mjs --add 或门户用户管理' }
  }
  const check = checkUserMutation(list, null, 'add', name, {
    role: FIRST_ADMIN_ROLE, apps: [...FIRST_ADMIN_APPS], password
  })
  return check.ok ? { ok: true } : { ok: false, reason: check.reason }
}

const USAGE = `用法：node bootstrap-admin.mjs [--name <账号>] [--data-dir <目录>]

首个 L3 管理员离线引导（仅当 users.json 为空时可用）：
  --name <账号>       账号名，匹配 ^[a-z0-9_-]{3,32}$（缺省时交互输入，默认 admin）
  --data-dir <目录>   覆盖 users.json 所在数据目录
  --help              显示本帮助

角色固定 L3、应用固定 auto+perf；密码在交互提示后输入，不回显、不进 shell 历史。
server 运行中禁止写操作，请先停止 server 再执行。`

function arg(name) {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 ? (process.argv[i + 1] || '') : ''
}
function has(name) { return process.argv.includes('--' + name) }

async function serverReachable(cfg) {
  const host = cfg.host === '::1' ? '[::1]' : cfg.host
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 1000)
  try {
    const r = await fetch('http://' + host + ':' + cfg.port + '/api/health', { signal: ctrl.signal })
    return r.ok
  } catch (e) { return false } finally { clearTimeout(timer) }
}

/** 非 TTY 下一次性把 stdin 按行读入队列，避免多次 question 各自关闭 stdin 丢行。 */
let _stdinLines = null
async function nextStdinLine() {
  if (!_stdinLines) {
    const chunks = []
    for await (const chunk of process.stdin) chunks.push(chunk)
    _stdinLines = Buffer.concat(chunks).toString('utf8').split(/\r?\n/)
  }
  return (_stdinLines.shift() ?? '').replace(/\r$/, '')
}

/** TTY 下关闭回显读密码；非 TTY 从 stdin 行队列取（供 CI/管道验证）。 */
async function readPassword(prompt) {
  if (process.stdin.isTTY) {
    process.stdout.write(prompt)
    return await new Promise((resolve, reject) => {
      let value = ''
      const stdin = process.stdin
      const prevRaw = stdin.isRaw
      stdin.setRawMode(true)
      stdin.resume()
      stdin.setEncoding('utf8')
      const cleanup = () => {
        stdin.removeListener('data', onData)
        stdin.setRawMode(!!prevRaw)
        stdin.pause()
      }
      const onData = (ch) => {
        if (ch === '\n' || ch === '\r' || ch === '\u0004') {
          cleanup(); process.stdout.write('\n'); resolve(value)
        } else if (ch === '\u0003') {
          cleanup(); process.stdout.write('\n'); reject(new Error('已取消'))
        } else if (ch === '\u007f' || ch === '\b') {
          value = value.slice(0, -1)
        } else { value += ch }
      }
      stdin.on('data', onData)
    })
  }
  return nextStdinLine()
}

async function readName(prompt) {
  if (!process.stdin.isTTY) return 'admin'
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout })
  try {
    const answer = await rl.question(prompt)
    return answer.trim() || 'admin'
  } finally { rl.close() }
}

async function main() {
  if (has('help') || process.argv.length <= 2) { console.log(USAGE); return }

  const cfg = await loadServerConfig()
  const dataDir = arg('data-dir') ? path.resolve(arg('data-dir')) : cfg.dataDir
  const userFile = path.join(dataDir, 'users.json')

  if (await serverReachable(cfg)) {
    console.error('失败：server 运行中（' + cfg.host + ':' + cfg.port + '），请在停止 server 后执行本引导；运行期请用门户用户管理。')
    process.exitCode = 1
    return
  }

  let users = []
  try {
    const parsed = JSON.parse(await fsp.readFile(userFile, 'utf8'))
    if (Array.isArray(parsed && parsed.users)) users = parsed.users
  } catch (e) { if (e && e.code !== 'ENOENT') throw e }

  const name = arg('name') || await readName('管理员账号 [admin]: ')
  const password = await readPassword('密码（至少 8 位，不回显）: ')
  const check = validateBootstrap({ name, password, users })
  if (!check.ok) { console.error('失败：' + check.reason); process.exitCode = 1; return }
  if (password !== (await readPassword('再输入一次密码: '))) {
    console.error('失败：两次密码不一致'); process.exitCode = 1; return
  }

  const { salt, hash } = hashPassword(password)
  users.push({ name, role: FIRST_ADMIN_ROLE, apps: [...FIRST_ADMIN_APPS], salt, hash, createdAt: new Date().toISOString() })
  await fsp.mkdir(dataDir, { recursive: true })
  const tmp = userFile + '.tmp-' + process.pid
  await fsp.writeFile(tmp, JSON.stringify({ users }, null, 2) + '\n', { mode: 0o600 })
  await fsp.rename(tmp, userFile)
  console.log('已创建首个管理员 ' + name + '（L3，auto+perf）。请妥善保管密码，服务启动后可在门户用户管理中修改。')
}

const invoked = process.argv[1] ? path.resolve(process.argv[1]) : ''
if (invoked === fileURLToPath(import.meta.url)) {
  try { await main() } catch (e) {
    console.error('[bootstrap-admin] 引导失败：' + (e && e.message || e))
    process.exitCode = 1
  }
}
