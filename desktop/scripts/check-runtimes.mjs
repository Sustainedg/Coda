/**
 * 打包前自检：本次要打进安装包的内嵌运行时（Python / Node）是否齐全。
 *
 * 为什么需要它：`electron-builder.yml` 的 `extraResources` 用的是
 * `build/<runtime>/${arch}`，源目录不存在时 electron-builder 只打一条
 * `file source doesn't exist` 的 **警告** 就继续打包 —— 产出一个能装、能开、
 * 但一装内核就报「未找到命令 node」的包。CI 里会先跑 fetch 脚本，所以问题
 * 只在「本地手动打包」时出现，而这时最容易被当成代码 bug 排查半天。
 *
 * 用法：node scripts/check-runtimes.mjs            # 按当前平台（mac 默认双架构）
 *       node scripts/check-runtimes.mjs mac-arm64  # 只查指定目标（与 fetch 脚本同一套名字）
 *
 * 目标名与 `fetch-node.mjs` / `fetch-python.mjs` 的 PLATFORMS 保持一致；只读不写，
 * 缺失就列出补跑哪条命令并退出 1（铁律 11：错误显式暴露）。
 */
import fs from 'node:fs';
import path from 'node:path';

const BUILD = path.resolve(import.meta.dirname, '..', 'build');

/** 目标名（与 fetch 脚本一致）→ 产物目录名（electron-builder 的 `${arch}`）。 */
const ARCH = {
  'win-x64': 'x64',
  'mac-arm64': 'arm64',
  'mac-x64': 'x64',
  'linux-x64': 'x64',
};

/**
 * 各平台运行时里「可执行文件」的相对路径（与两个 fetch 脚本校验的 marker 同源）。
 * Python 用 `bin/python3` 而不是 `python3.11`：升级解释器小版本时这里不必跟着改。
 */
function layoutOf(target) {
  return target.startsWith('win-')
    ? { python: ['python', 'python.exe'], node: ['node.exe'] }
    : { python: ['python', 'bin', 'python3'], node: ['bin', 'node'] };
}

/**
 * 默认目标：mac 的 electron-builder 配置是 `arch: [arm64, x64]`，一次打包出两个架构，
 * 所以必须两份运行时都在；其余平台只有当前架构。
 */
function defaultTargets() {
  if (process.platform === 'win32') return ['win-x64'];
  if (process.platform === 'darwin') return ['mac-arm64', 'mac-x64'];
  return process.arch === 'x64' ? ['linux-x64'] : [];
}

const targets = process.argv.slice(2);
const requested = targets.length ? targets : defaultTargets();
let failed = false;

if (requested.length === 0) {
  console.error(`✗ 无法推断当前平台的打包目标（${process.platform}/${process.arch}）`);
  process.exit(1);
}

for (const target of requested) {
  const arch = ARCH[target];
  if (!arch) {
    console.error(`✗ 不认识的目标平台：${target}`);
    failed = true;
    continue;
  }
  const layout = layoutOf(target);
  const checks = [
    {
      label: 'Python',
      file: path.join(BUILD, 'python-runtime', arch, ...layout.python),
      fetch: `node scripts/fetch-python.mjs ${target}`,
      prep: 'node scripts/prepare-python-deps.mjs',
    },
    {
      label: 'Node',
      file: path.join(BUILD, 'node-runtime', arch, ...layout.node),
      fetch: `node scripts/fetch-node.mjs ${target}`,
      prep: null,
    },
  ];
  for (const check of checks) {
    if (fs.existsSync(check.file)) {
      console.log(`✓ ${target} / 内置 ${check.label}：${path.relative(BUILD, check.file)}`);
      continue;
    }
    console.error(
      `✗ ${target} / 内置 ${check.label} 缺失：${path.relative(BUILD, check.file)}\n` +
        `  打包前先跑：${check.fetch}` +
        (check.prep ? `\n  后端依赖还没预装时再跑：${check.prep}` : ''),
    );
    failed = true;
  }
}

if (failed) {
  console.error(
    '\n内嵌运行时缺失时打包不会失败，只会产出一个「装内核必报未找到命令」的坏包，故在此拦下。',
  );
  process.exit(1);
}
