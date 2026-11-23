/**
 * 打包期给内嵌运行时（Python / Node）瘦身 —— electron-builder 的 `afterPack` 钩子。
 *
 * 与 fetch-python / fetch-node 的分工：
 *   - fetch 脚本删的是「目录级的确定无用物」（头文件、tcl/tk、文档），构建期一次到位；
 *   - 本脚本做的是「文件级的体积优化」：剥掉 ELF 里携带的符号表与调试段。
 *     放在打包期（afterPack）是因为 strip 必须发生在签名之前（见下），而签名
 *     只存在于打包流程里。
 *     注意：electron-builder 会把 build/ 原型**硬链接**进产物目录（实测同 inode），
 *     strip 产物会连带 strip 原型 —— 好在这正是我们想要的效果（下次构建直接
 *     从已瘦身原型出发），但别指望 build/ 里留有「未 strip 的原件」。
 *
 * 为什么能安全地 strip：
 *   - 调试段（.debug_* / DWARF）只服务于崩溃符号化，运行时不加载；
 *   - 符号表（.symtab / .strtab）同理，动态链接只依赖 .dynsym（strip 保留它）；
 *   - 崩溃排查需要符号时，重跑 `fetch-node.mjs` / `fetch-python.mjs` 下载原始
 *     发行包（sha256 校验过的官方件），配合 `addr2line` 符号化即可 ——
 *     符号在官方发行包里永远拿得到，不必随安装包分发。
 *
 * 平台差异（这是本脚本存在的另一半理由）：
 *   - Linux：官方 Node 发行包默认带完整符号表（实测 v24 二进制 ~120MB 里约 14MB
 *     是 .symtab/.strtab/.debug_*）；python-build-standalone 的 libpython 同样带
 *     DWARF 调试段（~56MB 里约 15MB）。系统 `strip` 即可剥掉。
 *   - macOS：apple 的发行包默认已 strip，Mach-O 的 strip 交由打包流程里的
 *     sign-runtimes.cjs 处理（签名前 strip、签名后不动），本脚本直接跳过；
 *   - Windows：PE 格式无 DWARF，官方二进制不含调试信息，无需处理。
 *
 * 为什么是 afterPack 而不是 afterSign：必须在签名**之前**改二进制 ——
 * 改完再签名会导致签名失效；签完再改会让公证/校验直接失败。
 * 本钩子由 electron-builder.yml 的 afterPack 串在 sign-runtimes.cjs 前面执行。
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

/** 内嵌运行时在打包产物里的落点（相对 resources 目录）。 */
const RUNTIME_DIRS = ['python-runtime', 'node-runtime'];

/** 剥离模式：--strip-debug 只删调试段（比全 strip 保守，动态符号不受影响）。 */
const STRIP_ARGS = ['--strip-debug'];

/**
 * 判断文件是否为 ELF（Linux 可执行 / 共享库）。
 * 读前 4 字节比 `file` 命令快且无子进程开销，逐文件判定只需要这一种格式。
 */
function isElf(file) {
  try {
    const fd = fs.openSync(file, 'r');
    try {
      const buf = Buffer.alloc(4);
      const n = fs.readSync(fd, buf, 0, 4, 0);
      return n === 4 && buf[0] === 0x7f && buf[1] === 0x45 && buf[2] === 0x4c && buf[3] === 0x46;
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return false;
  }
}

/** 递归收集候选二进制（与 sign-runtimes 的 shouldCheck 同思路：只看「像二进制」的名字）。 */
function collectElfCandidates(root) {
  if (!fs.existsSync(root)) return [];
  const found = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!entry.isFile() || entry.isSymbolicLink()) continue;
      // .so 后缀或无扩展名的普通文件（bin/node、bin/python3.11 这类）才探测
      if (/\.(so|so\.\d[\w.]*)$/i.test(entry.name) || !entry.name.includes('.')) {
        found.push(full);
      } else if (/^(python|node)[\d.]*$/i.test(entry.name)) {
        // 带扩展名的解释器本体（bin/python3.11）：对齐 sign-runtimes.cjs 的 shouldCheck
        found.push(full);
      }
    }
  };
  walk(root);
  return found;
}

/** 对单个 ELF 执行 strip；文件系统只读（如 AppImage 挂载）等异常静默跳过并计数。 */
function stripOne(file, stats) {
  const before = fs.statSync(file).size;
  execFileSync('strip', [...STRIP_ARGS, file], { stdio: 'pipe' });
  const after = fs.statSync(file).size;
  stats.files += 1;
  stats.saved += Math.max(0, before - after);
}

module.exports = async function trimRuntimes(context) {
  // 只在 Linux 上需要（mac 由 sign-runtimes 管，win 无调试信息）
  if (context.electronPlatformName !== 'linux') {
    console.log('[trim-runtimes] 非 Linux 平台，跳过 strip（mac 签名链路不含此步）');
    return;
  }
  // strip 是系统工具，CI 的 ubuntu runner 与常见构建环境都有；缺失时跳过而不是失败
  // —— 体积优化是「尽力而为」，不能因为环境里少了它就发不出包。
  let hasStrip = true;
  try {
    execFileSync('strip', ['--version'], { stdio: 'pipe' });
  } catch {
    hasStrip = false;
  }
  if (!hasStrip) {
    console.log('[trim-runtimes] 环境无 strip，跳过（包体会略大，不影响功能）');
    return;
  }

  const resourcesDir = path.join(context.appOutDir, 'resources');
  const stats = { files: 0, saved: 0 };
  for (const dirName of RUNTIME_DIRS) {
    const dir = path.join(resourcesDir, dirName);
    if (!fs.existsSync(dir)) continue;
    for (const file of collectElfCandidates(dir)) {
      if (!isElf(file)) continue;
      try {
        stripOne(file, stats);
      } catch (err) {
        console.warn(`[trim-runtimes] strip 失败（跳过）：${path.relative(resourcesDir, file)}：${err.message}`);
      }
    }
  }
  if (stats.files > 0) {
    console.log(
      `[trim-runtimes] 已剥离 ${stats.files} 个二进制的调试段，共省 ${(stats.saved / 1024 / 1024).toFixed(1)} MB（解压态）`,
    );
  } else {
    console.log('[trim-runtimes] 无可处理的 ELF（可能已剥离过）');
  }
};
