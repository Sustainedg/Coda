/**
 * 路径与目录布局约定（桌面版唯一权威来源，其他模块只准 import 这里）。
 *
 * 目录布局（userData 即系统标准应用数据目录）：
 *
 *   userData/
 *   ├── data/          ← CODA_DATA_DIR：SQLite、工作区、secret.key、内核 HOME
 *   │                     （对话数据全在这，卸载应用不清空，升级原样保留）
 *   ├── python/        ← embedded 解释器 + venv（首启联网装依赖，可随时删掉重建）
 *   ├── node/          ← npm 全局安装前缀 + 缓存（内核 CLI 装在这里，不碰系统 Node）
 *   └── releases/      ← 后端版本目录 + current 指针（复用主项目自更新机制）
 */
import { app } from 'electron';
import * as os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

/** userData 根目录。 */
export function userDataRoot(): string {
  return app.getPath('userData');
}

/**
 * 默认数据目录不可用时由 :func:`ensureLayout` 设定的回退目录。
 *
 * 只在内存里记：下次启动仍先尝试标准位置，能建回来就继续用标准位置。
 */
let fallbackDataDir: string | null = null;

/** 展开开头的 `~`：Node 不像 shell 那样自动展开，用户手写 CODA_DATA_DIR 时常带。 */
function expandHome(input: string): string {
  if (input === '~') return os.homedir();
  if (input.startsWith('~/') || input.startsWith('~\\')) {
    return path.join(os.homedir(), input.slice(2));
  }
  return input;
}

/**
 * 数据目录（CODA_DATA_DIR）：对话、密钥、工作区都在这。
 *
 * 默认由 Electron 的 ``app.getPath('userData')`` 按当前系统用户动态推导
 * （Windows 即 ``%APPDATA%\\Coda``），**不写死用户名**；也允许用 ``CODA_DATA_DIR``
 * 指定自定义位置（支持 ``~``）。回退目录由 ``ensureLayout`` 在默认位置不可写时设定。
 */
export function dataDir(): string {
  if (fallbackDataDir) return fallbackDataDir;
  const override = (process.env.CODA_DATA_DIR ?? '').trim();
  return override
    ? path.resolve(expandHome(override))
    : path.join(userDataRoot(), 'data');
}

/** Python 运行时目录（embedded 解释器 + venv）。 */
export function pythonRuntimeDir(): string {
  return path.join(userDataRoot(), 'python');
}

/** venv 的 python 可执行文件路径（Windows 是 python.exe，unix 是 bin/python）。 */
export function venvPython(): string {
  const dir = path.join(pythonRuntimeDir(), 'venv');
  return process.platform === 'win32'
    ? path.join(dir, 'Scripts', 'python.exe')
    : path.join(dir, 'bin', 'python');
}

/** 后端 releases 目录（自更新机制用，M1 阶段先放源码形态）。 */
export function releasesDir(): string {
  return path.join(userDataRoot(), 'releases');
}

/**
 * 安装期日志（venv / pip 的完整输出）。
 *
 * 放在 userData 根目录而不是 `python/` 里：弹窗会引导用户删运行时目录重建，
 * 日志放里面会跟着一起没掉，用户想反馈时反而找不到。
 */
export function installLogPath(): string {
  return path.join(userDataRoot(), 'install.log');
}

/**
 * npm 的全局安装前缀（桌面版自己的一份，绝不写进系统 Node 的目录）。
 *
 * 为什么必须隔离：打包版的 Node 运行时在 `resources/` 里（macOS 上位于 .app 内部，
 * 签名后只读），`npm install -g` 想写全局目录会直接 EACCES；Windows 上默认写到
 * `%APPDATA%\npm`，又会和用户自己的 Node 抢同名垫片。把前缀钉在 userData，
 * 既解决了权限问题，也让「内核装在哪」可预期、可随数据目录一起备份 / 迁移。
 */
export function nodeGlobalDir(): string {
  return path.join(userDataRoot(), 'node', 'global');
}

/** npm 缓存目录（同样放 userData，卸载 / 重建不影响系统缓存）。 */
export function nodeCacheDir(): string {
  return path.join(userDataRoot(), 'node', 'cache');
}

/** 已安装内核命令所在目录：POSIX 是 `<prefix>/bin`，Windows 是 `<prefix>` 本身。 */
export function nodeGlobalBinDir(): string {
  const prefix = nodeGlobalDir();
  return process.platform === 'win32' ? prefix : path.join(prefix, 'bin');
}

/** 统一的目录失败信息：报出路径、系统错误码与可照做的处理方式。 */
function dirError(dir: string, label: string, err: unknown): Error {
  const code = (err as NodeJS.ErrnoException).code;
  const message = (err as Error).message ?? String(err);
  return new Error(
    `无法创建或写入${label} ${dir}：${message}${code ? `（${code}）` : ''}。` +
      '请检查该路径是否被同名文件占用、当前用户是否有写入权限（或安全软件拦截），' +
      '也可设置环境变量 CODA_DATA_DIR 指向可写目录。',
  );
}

/** 确保目录存在（递归创建，幂等）。 */
export function ensureDir(dir: string, label = '目录'): void {
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    throw dirError(dir, label, err);
  }
}

/**
 * 确保目录存在且**可写**（递归创建 + 写一个探测文件）。
 *
 * 为什么数据目录不能只 mkdir：Windows 上「目录建得出来、文件写不进去」很常见
 * （受控文件夹访问 / 杀毒软件 / 漫游配置被重定向）。只建目录会把问题推迟到写
 * SQLite 时才炸，那时错误信息已经和「数据目录」对不上了。这里当场验证并清掉探测文件。
 */
function ensureWritableDir(dir: string, label: string): void {
  const probe = path.join(dir, `.write-probe-${process.pid}`);
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(probe, '');
  } catch (err) {
    throw dirError(dir, label, err);
  } finally {
    try {
      fs.unlinkSync(probe);
    } catch {
      /* 探测文件没建出来 / 已被清理：无需处理 */
    }
  }
}

/**
 * 初始化全部目录布局（app.ready 之后、起后端之前调用一次）。
 *
 * 数据目录先建：默认位置不可用时回退到用户家目录下 ``~/.coda/data``（持久、可备份），
 * 并打印实际落点；显式配置了 ``CODA_DATA_DIR`` 就不回退，直接把原因抛给用户。
 */
export function ensureLayout(): void {
  ensureDataDir();
  // 工作区根目录跟数据目录一起建好（后端 settings.ensure_dirs 也会建，这里先建让
  // 「删掉 data 目录再启动」时布局完整，且不用等后端起得来）。
  ensureWritableDir(path.join(dataDir(), 'workspaces'), '工作目录根');
  ensureDir(pythonRuntimeDir(), 'Python 运行时目录');
  ensureDir(releasesDir(), '后端版本目录');
  ensureDir(nodeGlobalBinDir(), '内核安装目录');
  ensureDir(nodeCacheDir(), 'npm 缓存目录');
}

/** 建数据目录；默认位置不可写时回退到家目录（见 :func:`ensureLayout`）。 */
function ensureDataDir(): void {
  const primary = dataDir();
  try {
    ensureWritableDir(primary, '应用数据目录');
    return;
  } catch (err) {
    if ((process.env.CODA_DATA_DIR ?? '').trim()) throw err;
    const fallback = path.join(os.homedir(), '.coda', 'data');
    process.stdout.write(
      `[paths] 默认数据目录不可用，回退到 ${fallback}：${(err as Error).message}\n`,
    );
    ensureWritableDir(fallback, '回退数据目录');
    fallbackDataDir = fallback;
  }
}
