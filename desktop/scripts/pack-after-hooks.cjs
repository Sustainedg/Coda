/**
 * afterPack 组合钩子入口 —— electron-builder 的 afterPack 只接受单个函数（见
 * packager.js 的 resolveFunction：数组会被当成「路径」解析而失败），所以多个
 * 打包后处理钩子在这里按顺序串行执行。
 *
 * 顺序是硬约束，不能调换：
 *   1. trim-runtimes：剥离二进制调试段（改文件内容）—— 必须发生在任何签名之前，
 *      先签后改会让签名失效；
 *   2. sign-runtimes：macOS 上逐个给内嵌运行时里的 Mach-O 签名（或无证书时整包
 *      ad-hoc 签名）—— 依赖「文件内容已定型」。
 *
 * 任一钩子抛错都会中断打包（铁律 11：错误显式暴露，不静默吞掉）。
 */
'use strict';

const trimRuntimes = require('./trim-runtimes.cjs');
const signRuntimes = require('./sign-runtimes.cjs');

module.exports = async function packAfterHooks(context) {
  await trimRuntimes(context);
  await signRuntimes(context);
};
