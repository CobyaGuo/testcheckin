// shared/ai/keAssign.js — 错误知识库「自动归类」纯函数核心（rev7.3）
// client（kbCore 转发）与 Node server（syncStore applyBatch 钩子）共用的唯一实现。
// 纯函数、零平台依赖：decodeVec 用全局 atob/btoa（Node 18+ / 浏览器均内置），可单测。

// 余弦相似度（向量已 L2 归一化时即点积；与原 kbCore 实现逐位一致）。
export function cosine(a, b) {
  if (!a || !b || a.length !== b.length) return 0;
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    const x = a[i], y = b[i];
    dot += x * y; na += x * x; nb += y * y;
  }
  if (na === 0 || nb === 0) return 0;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

// 类型门控相似度：异常类型不同视为不同根因候选（NPE ≠ 超时），在余弦基础上乘惩罚系数。
// 背景：bge 类句向量对「共享 Java 词汇」的短文本区分度有限（实测跨根因裸余弦可达 0.8+）。
export const TYPE_MISMATCH_PENALTY = 0.65;
export function gatedCosine(a, b, typeA, typeB) {
  const s = cosine(a, b);
  if (!s) return 0;
  const ta = String(typeA || '');
  const tb = String(typeB || '');
  if (ta && tb && ta !== tb) return s * TYPE_MISMATCH_PENALTY;
  return s;
}

// 自动归类阈值（server 与本地同一口径）：与贪心聚类的 seed 阈值一致；
// 低于阈值 → 未归类（cluster_id ''），绝不硬塞。
export const AUTO_ASSIGN_THRESHOLD = 0.8;

/**
 * 新条目向量与各簇 seed 向量做「类型门控余弦」，取最高分且 ≥ 阈值的簇；
 * 全部低于阈值 → 返回 null（未归类桶）。seeds = [{ id, vec, exception }]。
 * server 端 applyBatch 用它覆写 ke_doc.cluster_id（单一裁决者），sandbox 客户端
 * 用同一实现本地预判；双方分歧时以 server 为准（远端事件回写覆盖）。
 */
export function bestClusterMatch(vec, exceptionType, seeds, threshold = AUTO_ASSIGN_THRESHOLD) {
  if (!vec || !Array.isArray(seeds) || !seeds.length) return null;
  let best = null;
  for (const s of seeds) {
    if (!s || !s.vec) continue;
    const score = gatedCosine(vec, s.vec, exceptionType, s.exception);
    if (score >= threshold && (!best || score > best.score)) best = { id: s.id, score };
  }
  return best;
}

/* ---------------- 向量 <-> base64（Float32 无损；Node 18+ / 浏览器内置 atob/btoa） ---------------- */
function bytesToB64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}
function b64ToBytes(b64) {
  const s = atob(b64);
  const u = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
  return u;
}
export function encodeVec(arr) {
  const f = Float32Array.from(arr);
  return bytesToB64(new Uint8Array(f.buffer));
}
export function decodeVec(b64) {
  const u = b64ToBytes(b64);
  return Array.from(new Float32Array(u.buffer));
}
