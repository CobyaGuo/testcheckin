// dist/server/start.mjs — 数据层 server 的可配置启动入口
//
// 用法：
//   node start.mjs [选项]
//   npm start -- --port 9000 --root D:/myproj
//
// 选项（均可省略，省略时读 server.config.json，再缺省用内置默认）：
//   --root <目录>       项目根目录（默认本仓库根）
//   --data-dir <目录>   SQLite 数据目录（默认 <root>/server/data）
//   --dist-dir <目录>   静态页面目录（默认 <root>/dist）
//   --host <地址>       仅允许 loopback：127.0.0.1 / localhost / ::1 / [::1]
//   --port <端口>       监听端口 1-65535（默认 8100）
//   --config <文件>     指定配置文件路径（默认 <本目录>/server.config.json）
//   --print             只打印生效配置，不启动
//   --init              按当前选项生成配置文件后退出（不启动）
//   --no-auth           仅供测试回退：关闭账号鉴权（所有请求视为 L3 全权账号）
//   --help              显示帮助
//
// 说明：CLI 选项优先级高于配置文件；server 内部仍会再解析一次同一份 env，
// 因此本脚本把 CLI 选项映射为环境变量后交给 server/index.mjs，配置口径唯一。

import { loadServerConfig, writeServerConfig, publicConfig, CODE_ROOT } from './config.mjs'

const HELP = `
数据层 server 启动脚本
用法: node start.mjs [选项]

  --root <目录>       项目根目录（默认 ${CODE_ROOT}）
  --data-dir <目录>   SQLite 数据目录（默认 <root>/server/data）
  --dist-dir <目录>   静态页面目录（默认 <root>/dist）
  --host <地址>       仅 loopback: 127.0.0.1 / localhost / ::1 / [::1]
  --port <端口>       监听端口 1-65535（默认 8100）
  --config <文件>     配置文件路径（默认 <本目录>/server.config.json）
  --print             只打印生效配置
  --init              写入配置文件后退出（不启动）
  --no-auth           仅供测试回退：关闭账号鉴权（禁止生产使用）
  --help              本帮助

示例:
  node start.mjs --port 9000
  node start.mjs --root D:/work/myproj --data-dir D:/data/checkin
  npm start -- --print
`

function parseArgv(argv) {
  const out = { flags: {} }
  const envMap = {
    '--root': 'CHECKIN_SERVER_ROOT',
    '--data-dir': 'CHECKIN_DATA_DIR',
    '--dist-dir': 'CHECKIN_DIST_DIR',
    '--host': 'HOST',
    '--port': 'PORT',
    '--config': 'CHECKIN_CONFIG_FILE'
  }
  for (let i = 0; i < argv.length; i++) {
    const raw = argv[i]
    const eq = raw.indexOf('=')
    const key = eq >= 0 ? raw.slice(0, eq) : raw
    let val = eq >= 0 ? raw.slice(eq + 1) : undefined
    if (key === '--help' || key === '-h') { out.flags.help = true; continue }
    if (key === '--print') { out.flags.print = true; continue }
    if (key === '--init') { out.flags.init = true; continue }
    if (key === '--no-auth') { out.flags.noAuth = true; continue }
    if (envMap[key]) {
      if (val === undefined) val = argv[++i]
      if (val === undefined || val === '') throw new Error('选项 ' + key + ' 缺少值')
      out.env = out.env || {}
      out.env[envMap[key]] = val
      continue
    }
    throw new Error('未知选项：' + raw + '\n' + HELP)
  }
  return out
}

let parsed
try { parsed = parseArgv(process.argv.slice(2)) } catch (e) {
  console.error('[start-server] ' + e.message)
  process.exit(1)
}
if (parsed.flags.help) { console.log(HELP); process.exit(0) }

for (const [k, v] of Object.entries(parsed.env || {})) process.env[k] = v

let cfg
try { cfg = await loadServerConfig() } catch (e) {
  console.error('[start-server] ' + (e && e.message || e))
  process.exit(1)
}

const view = publicConfig(cfg, cfg.configFile)
console.log('[start-server] 生效配置：')
console.log('  项目根 root      : ' + view.root)
console.log('  数据目录 dataDir : ' + view.dataDir)
console.log('  页面目录 distDir : ' + view.distDir)
console.log('  监听 host:port   : ' + view.host + ':' + view.port)
console.log('  配置文件         : ' + view.configFile)
if (parsed.flags.noAuth) {
  process.env.CHECKIN_NO_AUTH = '1'
  console.log('  账号鉴权         : 已关闭（--no-auth 仅供测试回退，禁止生产使用）')
}

if (parsed.flags.init) {
  try {
    await writeServerConfig(view, cfg.configFile)
    console.log('[start-server] 已写入配置文件：' + cfg.configFile)
  } catch (e) {
    console.error('[start-server] 写配置失败：' + (e && e.message || e))
    process.exit(1)
  }
  process.exit(0)
}
if (parsed.flags.print) process.exit(0)

console.log('[start-server] 启动 server ...')
await import('./index.mjs')
