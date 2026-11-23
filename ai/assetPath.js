// shared/ai/assetPath.js — 本地 AI 资产的「站点根相对」路径（三台共用）
// ----------------------------------------------------------------------------
// dist/ai 永远位于站点根：/ai/ai-client.js、/ai/model-manifest.json、/ai/model-v1/...
// 但四台页面并不都在站点根：
//   · workbench → /index.html
//   · auto / perf / marknote / login → /<app>/index.html
// 若写死 './ai/'，在子目录页面会解析成 /auto/ai/... → 404，
// 表现为「本地 AI 资产不可用」且用户看不到明确原因（脚本 onerror 只落到内部状态）。
//
// 这里按当前页面位置算出到站点根的相对前缀。GitHub Pages 的 /<repo>/ 前缀
// 不影响相对路径计算（/repo/auto/index.html 同样用 ../ai/）。
// 红线：只产出同域相对路径，绝不产生绝对 URL / 公网地址 / 版本查询参数。

const APP_DIR = /\/(?:auto|perf|marknote|workbench|login)\/(?:[^/]*)$/i;

/** 当前页面到站点根的相对前缀：站点根 './'，应用子目录 '../'。 */
export function aiAssetPrefix() {
  const pathname = typeof location !== 'undefined' ? String(location.pathname || '/') : '/';
  return APP_DIR.test(pathname) ? '../' : './';
}

/** 运行时注入的 AI 客户端脚本地址（同域相对路径）。 */
export function aiClientSrc() {
  return aiAssetPrefix() + 'ai/ai-client.js';
}

/** 模型 / manifest / wasm 等资产的基址（同域相对路径）。 */
export function aiBase() {
  return aiAssetPrefix() + 'ai/';
}
