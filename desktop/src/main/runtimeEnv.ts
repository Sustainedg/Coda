/**
 * 桌面端运行时环境：「Node 依赖问题」的解法（M4）。
 *
 * 为什么要自己带一份 Node：
 *   Claude Code / Codex / Gemini CLI 三个内核官方都走 npm 分发，"设置 → 内核管理"
 *   代用户装 / 更新时跑的是 `npm install -g`；装完的垫片（claude / codex / gemini）
 *   本身也是 Node 程序。桌面用户的机器上**可能根本没有 Node**——这正是 M4 要解决的
 *   依赖问题：壳里已经内嵌 Python，同样再内嵌一份 Node，内核链路就自洽了。
 *
 * 两个必须钉死的细节：
 *   1. **PATH 前置**：内置 Node 的目录与「平台托管的 npm 全局 bin」都要排在系统 PATH
 *      前面，否则用户机器上旧版本的内核 / 另一个 Node 会先被找到，版本与技能配置全乱。
 *      后端进程（uvicorn）继承这份环境，它再 spawn 内核、执行安装命令时用的就是这份。
 *   2. **npm 前缀 / 缓存钉在 userData**：见 paths.ts `nodeGlobalDir()` 的注释（只读卷、
 *      签名后的 .app、系统 Node 目录冲突）。
 *
 * 开发模式不注入：直接用开发者自己的 node / npm（与 README 的「开发模式要求本机
 * python3」同一口径），避免把 `resources/` 里的东西跟源码模式混起来。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { app } from 'electron';
import { nodeCacheDir, nodeGlobalBinDir, nodeGlobalDir } from './paths';

/** 安装包内置的 Node 运行时根目录（scripts/fetch-node.mjs 铺进 resources/node-runtime）。 */
export function bundledNodeDir(): string | null {
  if (!app.isPackaged) return null;
  const dir = path.join(process.resourcesPath, 'node-runtime');
  const exe =
    process.platform === 'win32' ? path.join(dir, 'node.exe') : path.join(dir, 'bin', 'node');
  return fs.existsSync(exe) ? dir : null;
}

/** 运行时根目录 → 可执行文件目录（Windows 是根目录本身，POSIX 是 `<root>/bin`）。 */
function nodeBinDirOf(root: string): string {
  return process.platform === 'win32' ? root : path.join(root, 'bin');
}

/**
 * 内置 Node 的**可执行文件目录**（要放进 PATH 的那个）。
 *
 * 两个发行包的布局不一样，混用会得到「未找到命令 node」：
 *   - Windows：`node.exe` / `npm.cmd` 直接躺在运行时根目录；
 *   - POSIX（macOS / Linux）：官方发行包是 `<root>/bin/node`、`<root>/bin/npm`，
 *     根目录里只有 `bin/` `lib/` `VERSION` 等。把根目录塞进 PATH 找不到任何命令 ——
 *     后端 `provisioning` 起的 `node -e …` 包装命令（npm 安装 / 清扫）因此必然失败。
 */
export function bundledNodeBinDir(): string | null {
  const dir = bundledNodeDir();
  return dir ? nodeBinDirOf(dir) : null;
}

/** 该目录是否是内置运行时（供诊断 / 启动页展示）。 */
export function bundledNodeVersion(): string | null {
  const dir = bundledNodeDir();
  if (!dir) return null;
  try {
    // 版本号写在运行时根目录的 README？不必：fetch 脚本会留一个 VERSION 文件
    return fs.readFileSync(path.join(dir, 'VERSION'), 'utf8').trim() || null;
  } catch {
    return null;
  }
}

function prependPath(dirs: string[]): string {
  const existing = process.env.PATH ?? '';
  const merged = dirs.filter((dir) => dir && !existing.split(path.delimiter).includes(dir));
  return [ ...merged, existing ].filter(Boolean).join(path.delimiter);
}

/**
 * 计算后端子进程与安装命令共用的环境变量覆盖项。
 *
 * 只返回**新增 / 覆盖**的键（调用方自己与 `process.env` 合并），不在这里改进程环境：
 * 开发模式下没有任何覆盖，`{}` 返回时调用方行为与以前完全一致。
 */
export function desktopRuntimeEnv(): Record<string, string> {
  const nodeDir = bundledNodeDir();
  const prefix = nodeGlobalDir();
  fs.mkdirSync(nodeGlobalBinDir(), { recursive: true });
  fs.mkdirSync(nodeCacheDir(), { recursive: true });

  const env: Record<string, string> = {
    // npm 全局目录与缓存：内核 CLI 全装在这两份目录里
    npm_config_prefix: prefix,
    npm_config_cache: nodeCacheDir(),
    // 安装时少打点广告式输出，日志里只看得到真正的安装进度
    npm_config_update_notifier: 'false',
    npm_config_fund: 'false',
    npm_config_audit: 'false',
  };

  // 托管 bin 永远要在 PATH 上：即使将来改用原生二进制通道、没有内置 Node，
  // 「内核装在哪」也是这个目录，后端与内核都靠它找命令。
  const pathDirs = [nodeGlobalBinDir()];
  if (nodeDir) {
    // 前置的是**可执行文件所在目录**（Windows 是运行时根，POSIX 是 <root>/bin），
    // 不是运行时根 —— 见 bundledNodeBinDir() 的注释。
    pathDirs.unshift(nodeBinDirOf(nodeDir));
    // CODA_NODE_HOME 仍指运行时根：它是「内置 Node 装在哪」的标识，诊断与
    // 后端定位 lib/ 时用，与 PATH 要找可执行文件是两回事。
    env.CODA_NODE_HOME = nodeDir;
  }
  env.PATH = prependPath(pathDirs);
  return env;
}