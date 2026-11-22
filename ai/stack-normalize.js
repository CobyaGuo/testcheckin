/*!
 * shared/ai/stack-normalize.js — 异常堆栈归一化（纯字符串处理，零依赖、零外联）
 * ----------------------------------------------------------------------------
 * 本文件供「错误知识库」建库 / 推荐 / 归簇共用，把任意异常堆栈归一化为可比较的规范形态：
 *   · 解析 Caused by / Suppressed 异常链，每段取首行异常类型；
 *   · 业务帧链：保留完整类名+方法名，剔除行号、文件偏移、0x 地址；
 *   · 框架噪声帧（java.util.concurrent.*、sun.reflect.* 等）排到业务帧之后；
 *   · 连续重复帧折叠为 1 次 + [xN]；只保留栈顶 15 帧；
 *   · 变量归一：数字 / UUID / 时间戳 / hex 统一替换为 <num> <uuid> <hex> 占位符；
 *   · stackHash：归一化结果的 FNV-1a 64 位 hash（无依赖实现）。
 *
 * 双口径（批次 v3.8 返工，务必分清）：
 *   · 展示 / 向量检索口径 top15 + normalized：含框架噪声帧、含 [xN] 折叠标记、栈顶 15 帧；
 *   · 身份口径 businessFrames + identity：只含业务帧（过滤噪声帧）、去 [xN]、栈顶 15 帧，
 *     拼成「异常类型 + 首帧 + 业务帧链 + 错误信息」，专供 docId / 跨轮对齐 / 失败归簇。
 *     同根因但线程包装不同（ThreadPoolExecutor.runWorker vs Thread.run）或重复帧次数不同
 *     （submit [x2] vs submit）的两条栈，必须得到同一身份；差异全在噪声帧与折叠标记里。
 *
 * 红线：本文件不得包含任何网络调用（无公网地址、无任何请求类/长连接类 API），
 * 不得 import 任何模块（零依赖，可被 Node 单测直接 import）。
 */

// 框架噪声帧前缀：对「根因归类」价值低，统一排到业务帧之后。
export const NOISE_PREFIXES = [
  'java.util.concurrent.', 'sun.reflect.', 'sun.', 'jdk.internal.',
  'java.lang.reflect.', 'com.sun.', 'dalvik.system.', 'kotlinx.coroutines.',
  'android.', 'org.junit.', 'org.testng.', 'javax.', 'java.lang.Thread.',
  'org.mockito.', 'net.bytebuddy.'
];

// 变量归一：先 UUID，再 0x 十六进制地址，最后剩余数字（含小数 / 负号）。
export function normVars(s) {
  s = String(s == null ? '' : s);
  s = s.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '<uuid>');
  s = s.replace(/0x[0-9a-f]+/gi, '<hex>');
  s = s.replace(/-?\d+(?:\.\d+)?/g, '<num>');
  return s;
}

// 从一行提取异常类型（XxxException / XxxError / XxxFault / XxxThrowable）。
function extractException(line) {
  let s = String(line || '').replace(/^\s*Caused by:\s*/i, '').trim();
  const m = s.match(/([\w$.]+(?:Exception|Error|Fault|Throwable|TimeoutException))/);
  if (m) return m[1];
  const t = s.split(/[:\s]/)[0];
  return t && /[\w$.]/.test(t) ? t : '';
}

// 从首行提取归一化错误信息（冒号后的正文，变量占位）。
function extractMessage(line) {
  let s = String(line || '').replace(/^\s*Caused by:\s*/i, '').trim();
  const idx = s.indexOf(':');
  const msg = idx >= 0 ? s.slice(idx + 1).trim() : s;
  return normVars(msg);
}

// 从一行提取业务帧（完整类名+方法名）。支持 Java 风格 `at pkg.Class.method(...)`，
// 以及非 Java 风格（Kotlin/Python/JS 等）的兜底：取去前缀后到 ( 或 : 前的 token。
// 异常头行（XxxException: ... / Caused by: ... / Suppressed: ...）是链头不是帧，一律跳过。
function extractFrame(line) {
  const raw = String(line || '');
  const m = raw.match(/at\s+([^\s(]+)\s*\(/);
  if (m) return normVars(m[1]);
  const trimmed = raw.trim();
  if (/^(Caused by:|Suppressed:)/i.test(trimmed)) return null;
  let s = trimmed;
  const head = s.split(/[:\s]/)[0] || '';
  if (/(?:Exception|Error|Throwable|Fault)$/.test(head)) return null;
  if (!s) return null;
  s = s.replace(/[:(].*$/, '').trim();
  return s ? normVars(s) : null;
}

function isNoise(frame) {
  return NOISE_PREFIXES.some((p) => frame.indexOf(p) === 0);
}

// 连续重复帧折叠为 1 次 + 计数 n。展示链与身份链各自折叠，互不污染。
function collapseFrames(frames) {
  const out = [];
  for (const f of frames) {
    const last = out[out.length - 1];
    if (last && last.frame === f) last.n++;
    else out.push({ frame: f, n: 1 });
  }
  return out;
}

/**
 * 归一化异常堆栈。
 * @param {string} rawStack 原始堆栈（可能含 Caused by / Suppressed 链）。
 * @returns {{exceptionType:string, firstFrame:string, message:string, top15:string[],
 *            frames:string[], normalized:string, businessFrames:string[], identity:string}}
 */
export function normalizeStack(rawStack) {
  const text = String(rawStack == null ? '' : rawStack).replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const headerLine = lines.find((l) => l.trim()) || '';
  const exceptionType = extractException(headerLine);
  const message = extractMessage(headerLine);

  const framesAll = [];
  for (const l of lines) {
    const fr = extractFrame(l);
    if (fr) framesAll.push(fr);
  }
  const business = framesAll.filter((f) => !isNoise(f));
  const noise = framesAll.filter((f) => isNoise(f));
  const ordered = business.concat(noise);

  // 展示 / 向量检索口径：业务帧在前、噪声帧在后，重复帧带 [xN]，栈顶 15 帧
  const top15 = collapseFrames(ordered)
    .map((c) => (c.n > 1 ? c.frame + ' [x' + c.n + ']' : c.frame))
    .slice(0, 15);

  // 身份口径：只保留业务帧；丢掉 [xN]（折叠后仅取 frame，重复次数不参与身份）；
  // 同样取栈顶 15 帧保持口径稳定。线程池 / 反射 / 测试框架等包装帧一律不参与身份 hash。
  const businessFrames = collapseFrames(business).map((c) => c.frame).slice(0, 15);

  const firstFrame = business[0] || ordered[0] || '';
  const normalized = [exceptionType, firstFrame].concat(top15).concat([message]).join('\n');
  const identity = [exceptionType, firstFrame].concat(businessFrames).concat([message]).join('\n');
  return { exceptionType, firstFrame, message, top15, frames: top15, normalized, businessFrames, identity };
}

// FNV-1a 64 位（BigInt 实现，无依赖）。
// 入参为字符串时直接 hash；入参为 normalizeStack() 结果时优先取 identity（身份口径），
// 老结构若无 identity 则回退 normalized，保证兼容。返回 base36 字符串，稳定且紧凑。
export function stackHash(normalized) {
  const FNV_OFFSET = 0xcbf29ce484222325n;
  const FNV_PRIME = 0x100000001b3n;
  const s = typeof normalized === 'string'
    ? normalized
    : (normalized && (normalized.identity || normalized.normalized)) || '';
  let h = FNV_OFFSET;
  for (let i = 0; i < s.length; i++) {
    h ^= BigInt(s.charCodeAt(i));
    h = (h * FNV_PRIME) & 0xffffffffffffffffn;
  }
  return h.toString(36);
}

// 兼容浏览器非 ESM 场景（挂到全局；打包方按需具名导入即可）。
if (typeof globalThis !== 'undefined') {
  globalThis.MN_STACK_NORM = { normalizeStack, stackHash, normVars, NOISE_PREFIXES };
}
