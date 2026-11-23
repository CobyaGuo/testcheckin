// server/config.mjs — Node 数据层可配置启动参数（单一解析真源）
//
// 配置优先级（高 → 低）：环境变量 > 配置文件(dist/server/server.config.json) > 默认值。
// 字段：root（项目根）、dataDir（数据目录）、distDir（静态资源目录）、host、port。
// 红线：鉴权落地前只允许 loopback 绑定；非 loopback 一律拒绝（不设 ALLOW_REMOTE 例外）。
// 该文件只做解析/校验/持久化，不启动 server；源码态供 server/index.mjs 与 scripts/start-server.mjs 用，
// 打包态原样复制到 dist/server/config.mjs，供 dist/server/index.mjs 与 dist/server/start.mjs 用。

import fs from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const CONFIG_IMPL_DIR = path.dirname(fileURLToPath(import.meta.url))
/** 打包态判定：config.mjs 位于 <部署根>/dist/server/ 下；源码态位于 <仓库>/server/ 下。 */
export const PACKAGED_LAYOUT = path.basename(CONFIG_IMPL_DIR) === 'server' && path.basename(path.dirname(CONFIG_IMPL_DIR)) === 'dist'
export const CODE_ROOT = PACKAGED_LAYOUT ? path.resolve(CONFIG_IMPL_DIR, '..', '..') : path.resolve(CONFIG_IMPL_DIR, '..')
export const DIST_SERVER_DIR = path.join(CODE_ROOT, 'dist', 'server')
export const DEFAULT_CONFIG_FILE = path.join(DIST_SERVER_DIR, 'server.config.json')
export const LOOPBACK_HOSTS = Object.freeze(['127.0.0.1', 'localhost', '::1', '[::1]'])
export const WRITABLE_KEYS = Object.freeze(['root', 'dataDir', 'distDir', 'host', 'port'])

function abs(base, value) {
  if (value === undefined || value === null || value === '') return base
  const s = String(value)
  return path.isAbsolute(s) ? path.resolve(s) : path.resolve(base, s)
}

export function defaultConfig(root = CODE_ROOT) {
  const base = abs(CODE_ROOT, root)
  return {
    root: base,
    // 打包态随 dist 一起部署，数据库落到 dist/server/data；源码态沿用 server/data。
    dataDir: PACKAGED_LAYOUT ? path.join(base, 'dist', 'server', 'data') : path.join(base, 'server', 'data'),
    distDir: path.join(base, 'dist'),
    host: '127.0.0.1',
    port: 8100
  }
}

/** 校验并归一化配置；非 loopback / 非法端口直接抛错。 */
export function normalizeConfig(input = {}, baseRoot = CODE_ROOT) {
  const root = abs(baseRoot, input.root || baseRoot)
  const def = defaultConfig(root)
  const host = String(input.host || def.host).trim()
  if (!LOOPBACK_HOSTS.includes(host)) {
    throw new Error('拒绝启动：鉴权落地前只允许 loopback 绑定，收到 HOST=' + host)
  }
  const portRaw = input.port === undefined || input.port === '' ? def.port : Number(input.port)
  if (!Number.isInteger(portRaw) || portRaw < 1 || portRaw > 65535) {
    throw new Error('端口非法（须为 1-65535 整数）：' + String(input.port))
  }
  return {
    root,
    dataDir: abs(root, input.dataDir || def.dataDir),
    distDir: abs(root, input.distDir || def.distDir),
    host,
    port: portRaw
  }
}

export function readConfigFile(file = DEFAULT_CONFIG_FILE) {
  try {
    const obj = JSON.parse(fs.readFileSync(file, 'utf8'))
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {}
  } catch (e) { return {} }
}

/** 对外展示的配置（无敏感项，显式列出字段，避免误带内部状态）。 */
export function publicConfig(cfg, configFile = DEFAULT_CONFIG_FILE) {
  return {
    root: cfg.root,
    dataDir: cfg.dataDir,
    distDir: cfg.distDir,
    host: cfg.host,
    port: cfg.port,
    configFile: path.resolve(configFile),
    hotReloadable: ['distDir'],
    restartRequired: ['root', 'dataDir', 'host', 'port']
  }
}

/** 加载生效配置：env > 配置文件 > 默认。env 覆盖键：CHECKIN_SERVER_ROOT / CHECKIN_DATA_DIR / CHECKIN_DIST_DIR / HOST / PORT / NODE_SERVER_PORT。 */
export async function loadServerConfig(env = process.env, fileOverride = '') {
  const configFile = path.resolve(fileOverride || env.CHECKIN_CONFIG_FILE || DEFAULT_CONFIG_FILE)
  const fileCfg = readConfigFile(configFile)
  // 配置文件里的相对 root 以配置文件所在目录（dist/server）为基准；env root 仍以代码根为基准。
  const rootBase = env.CHECKIN_SERVER_ROOT ? CODE_ROOT : path.dirname(configFile)
  const root = abs(rootBase, env.CHECKIN_SERVER_ROOT || fileCfg.root || CODE_ROOT)
  const def = defaultConfig(root)
  const cfg = normalizeConfig({
    root,
    dataDir: env.CHECKIN_DATA_DIR || fileCfg.dataDir || def.dataDir,
    distDir: env.CHECKIN_DIST_DIR || fileCfg.distDir || def.distDir,
    host: env.HOST || fileCfg.host || def.host,
    port: env.PORT || env.NODE_SERVER_PORT || fileCfg.port || def.port
  }, CODE_ROOT)
  return { ...cfg, configFile }
}

/**
 * 绝对路径转相对落盘值。统一用 '/' 分隔：dist 会被整体复制到其他机器（可能是 Linux），
 * Windows 的 '\\' 分隔符在那边不是路径分隔符，会导致配置失效。
 */
function relativeForConfig(base, target) {
  if (!base || !target || !path.isAbsolute(target)) return target
  const r = path.relative(base, target)
  if (!r) return '.'
  return r.split(path.sep).join('/')
}

/** 写回配置文件；root 变化且未显式给 dataDir/distDir 时按新 root 重算默认。返回归一化配置。 */
export async function writeServerConfig(patch = {}, file = DEFAULT_CONFIG_FILE) {
  const target = path.resolve(file)
  const configDir = path.dirname(target)
  const current = readConfigFile(target)
  // 与 loadServerConfig 同口径：配置文件里的相对 root 按配置文件所在目录解析。
  const baseRoot = abs(configDir, patch.root || current.root || CODE_ROOT)
  const def = defaultConfig(baseRoot)
  const next = {
    root: baseRoot,
    dataDir: patch.dataDir || (patch.root ? def.dataDir : (current.dataDir || def.dataDir)),
    distDir: patch.distDir || (patch.root ? def.distDir : (current.distDir || def.distDir)),
    host: patch.host || current.host || def.host,
    port: patch.port === undefined || patch.port === '' ? (current.port || def.port) : patch.port
  }
  const cfg = normalizeConfig(next, CODE_ROOT)
  // 落盘一律相对路径：root 相对配置文件所在目录，dataDir/distDir 相对 root。
  // 这样 dist 整体复制到任意机器/任意盘符后，配置仍指向自身目录结构。
  const persisted = {
    root: relativeForConfig(configDir, cfg.root),
    dataDir: relativeForConfig(cfg.root, cfg.dataDir),
    distDir: relativeForConfig(cfg.root, cfg.distDir),
    host: cfg.host,
    port: cfg.port
  }
  await fsp.mkdir(configDir, { recursive: true })
  await fsp.writeFile(target, JSON.stringify(persisted, null, 2) + '\n', 'utf8')
  return cfg
}

/** 计算本次 patch 相对当前生效配置中哪些字段变化、哪些需重启。 */
export function diffRestart(current, patch) {
  const changed = WRITABLE_KEYS.filter((k) => patch[k] !== undefined && String(patch[k]) !== String(current[k]))
  const restart = changed.filter((k) => ['root', 'dataDir', 'host', 'port'].includes(k))
  return { changed, restartRequired: restart }
}
