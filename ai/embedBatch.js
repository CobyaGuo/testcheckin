// shared/ai/embedBatch.js — 推理后端 / 分块常量（三台共用，源码级 import）
// ----------------------------------------------------------------------------
// 唯一真源：auto 的 knownErrors / similarCases、三台的 AI 桥接层与设置卡都从这里取常量。
// classic script 的 ai-client.js 因不进打包器，另有一份同值镜像（见其中注释），
// 改动本文件必须同步 ai-client.js 顶部常量，check:ai 会静态比对两侧数值。
//
// 红线：这些数字只影响「单次前向批量上限」，与 dim / pooling / hash 无关；
// 后端切换不触发向量库重建（ensureModelFingerprint 的指纹不含 backend）。

/** 单次前向批量上限，防 WASM 堆耗尽（WASM/q8 路径） */
export const EMBED_BATCH = 24;
/** WebGPU（fp16）内存压力小，批量可放宽 */
export const EMBED_BATCH_WEBGPU = 48;
/** 文本硬截断：超长错误堆栈/正文不进单次前向，防张量过大 */
export const MAX_EMBED_TEXT_CHARS = 4000;
/** 推理后端开关的 localStorage 键（三台同域共享一个口径） */
export const BACKEND_KEY = 'ai-infer-backend';
/** WebGPU 初始化超时（毫秒） */
export const WEBGPU_TIMEOUT_MS = 15000;

export function normalizeBackendMode(mode) {
  return mode === 'wasm' ? 'wasm' : 'auto';
}

export function readBackendMode() {
  try {
    if (typeof localStorage === 'undefined') return 'auto';
    return normalizeBackendMode(localStorage.getItem(BACKEND_KEY));
  } catch (e) {
    return 'auto';
  }
}

export function writeBackendMode(mode) {
  const v = normalizeBackendMode(mode);
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(BACKEND_KEY, v);
  } catch (e) {}
  return v;
}
