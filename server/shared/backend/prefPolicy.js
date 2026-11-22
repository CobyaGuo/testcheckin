// shared/backend/prefPolicy.js — 账号个人偏好 key 白名单与校验（rev7 §4，浏览器 / server 共用纯函数）
//
// 红线：prefs 不是业务写。这里登记的 key 只能进 server 的 prefs.db / user_prefs，
// 绝不进 outbox / batch / sync_changes，不触发 appRev，不写业务审计。
// 值与 src/utils/theme.js 的 MODES / ACCENTS 及 i18n 的语言口径保持一致。

export const PREF_KEYS = Object.freeze(['theme.mode', 'theme.accent', 'lang', 'density'])

const ENUMS = Object.freeze({
  // rev7.7：theme.mode 增补 console（终端控制台）——漏登记会让 PATCH /api/prefs 400，
  // 账号级主题偏好永远存不进 prefs.db（表现为每次登录主题回退默认）。
  'theme.mode': Object.freeze(['light', 'matte', 'console']),
  'theme.accent': Object.freeze(['brand', 'green', 'teal']),
  lang: Object.freeze(['zh', 'en']),
  density: Object.freeze(['normal', 'compact'])
})

/** 单个偏好值：只允许短标量，避免把对象 / 超长文本塞进偏好表。 */
export function validPrefValue(key, value) {
  const allowed = ENUMS[key]
  if (allowed) return typeof value === 'string' && allowed.includes(value)
  if (typeof value === 'string') return value.length <= 256
  return typeof value === 'number' || typeof value === 'boolean'
}

/**
 * 校验 PATCH /api/prefs/:app 的 changes。
 * @returns {{ok:true, changes:Object}|{ok:false,status:number,code:string,message:string}}
 */
export function validatePrefChanges(changes) {
  if (!changes || typeof changes !== 'object' || Array.isArray(changes)) {
    return { ok: false, status: 400, code: 'bad-prefs', message: 'changes 必须是对象' }
  }
  const out = {}
  for (const [key, value] of Object.entries(changes)) {
    if (!PREF_KEYS.includes(key)) {
      return { ok: false, status: 400, code: 'pref-key-not-allowed', message: '不支持的偏好项：' + key }
    }
    if (!validPrefValue(key, value)) {
      return { ok: false, status: 400, code: 'bad-pref-value', message: '偏好值不合法：' + key }
    }
    out[key] = value
  }
  if (!Object.keys(out).length) {
    return { ok: false, status: 400, code: 'bad-prefs', message: 'changes 不能为空' }
  }
  return { ok: true, changes: out }
}

/** 从服务端 prefs 对象里挑出已登记的白名单项，避免任何未来新增字段被本地误用。 */
export function pickPrefs(raw) {
  const out = {}
  if (!raw || typeof raw !== 'object') return out
  for (const key of PREF_KEYS) if (Object.prototype.hasOwnProperty.call(raw, key)) out[key] = raw[key]
  return out
}
