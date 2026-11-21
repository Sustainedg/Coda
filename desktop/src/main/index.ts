/**
 * 主进程入口：应用生命周期 + 窗口/托盘 + 后端编排。
 *
 * 流程（方案 C）：
 *   ready → 选端口 → 初始化 Python 运行时 → spawn 后端 → 健康检查 → 加载页面
 *
 * 窗口策略（桌面版价值点）：关窗 = 隐藏到托盘，后端继续跑
 * → IM 在线、工单协作、生成中的回复都不中断。
 */
import { app, BrowserWindow, Tray, Menu, dialog, ipcMain, shell } from 'electron';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { findFreePort, isPortInUse } from './ports';
import { waitForHealth } from './health';
import { spawnBackend, watchBackendRestart, type BackendHandle } from './backend';
import { ensurePythonRuntime } from './pythonRuntime';
import { bootstrapBundledBackend } from './bootstrap';
import { ensureLayout, dataDir, installLogPath, pythonRuntimeDir } from './paths';
import {
  checkForUpdatesManually,
  disposeAutoUpdate,
  registerUpdateIpc,
  setupAutoUpdate,
} from './updater';

/**
 * CI 冒烟模式（M3）：只跑「运行时 → 后端 → 健康检查」这条链路，不建窗口、不建托盘，
 * 就绪后打印一行标记并按退出码收工。npm 侧（Windows/macOS runner）用它验证安装包，
 * 因此这里刻意不碰任何 GUI 设施，避免无头环境里卡在窗口初始化上。
 */
const smokeMode = process.env.CODA_DESKTOP_SMOKE === '1';

/** 冒烟模式的总看门狗：卡住也要以非零退出，绝不让 CI 任务挂到超时。 */
const SMOKE_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * 自绘标题栏高度（Windows 的 titleBarOverlay 与注入的 CSS 必须一致）。
 * 默认原生标题栏在深色界面顶上会留一条纯白的系统色条 —— 那正是「不像原生软件」的来源。
 */
const TITLEBAR_HEIGHT = 36;

/** macOS 系统红绿灯的单枚直径（HIG 尺寸），用于把它们垂直居中进标题栏。 */
const TRAFFIC_LIGHT_SIZE = 12;

/** Windows / macOS 使用自绘标题栏；Linux 没有 overlay 支持，保留系统边框。 */
const customTitlebar = process.platform === 'win32' || process.platform === 'darwin';

/** 桌面壳的图标目录（随 files 打进 app.asar）。 */
function iconsDir(): string {
  return path.join(__dirname, '..', 'resources', 'icons');
}

/**
 * 托盘图标：
 *   macOS 用模板图（系统按菜单栏明暗自动着色）；
 *   Windows 用全彩品牌图（系统不做模板着色，单色剪影缩放后辨识度差）；
 *   Linux 保持品牌色剪影。
 *
 * 三种图都由 ``desktop/scripts/gen-icons.py`` 生成并入库，随 ``files: resources/**``
 * 打进 app.asar；``__dirname`` 在 asar 内为 ``<app.asar>/dist-main``，故
 * ``../resources/icons`` 指向包内资源，``fs.existsSync`` 与 ``Tray`` 都能直接读。
 */
function trayIconPath(): string {
  const name =
    process.platform === 'darwin'
      ? 'trayTemplate.png'
      : process.platform === 'win32'
        ? 'tray-win.png'
        : 'tray.png';
  return path.join(iconsDir(), name);
}

/**
 * 给页面注入「桌面外壳」样式与拖拽条（只在 Electron 里执行，浏览器访问完全不受影响）。
 *
 * 为什么给 `.sidebar` / `.main` 分别加内边距、而不是给 `.app-shell` 加 margin：
 * 加 margin 会在顶部留下一条背景色带（侧栏渐变到那里断掉），看起来就是「一条横边」；
 * 改成各自内边距后，两块背景都铺到窗口最上沿，控制按钮浮在应用自己的背景上。
 * 拖拽条本身透明，露出的是页面背景渐变，明暗主题都自动对得上。
 */
function desktopChromeCss(): string {
  return `
body.coda-desktop { overflow: hidden; }
/* 内容**直接铺到窗口边缘**：顶部 36px 靠侧栏 / 主区各自的内边距让出来，
   背景（侧栏渐变、主区径向渐变）一路延伸到顶，不会出现一条横向色带。
   控制按钮浮在这块背景上，看起来就是应用自带的顶栏。 */
body.coda-desktop .sidebar {
  padding-top: calc(18px * var(--sbs) + ${TITLEBAR_HEIGHT}px);
}
body.coda-desktop .main { padding-top: ${TITLEBAR_HEIGHT}px; }
#coda-titlebar {
  position: fixed; top: 0; left: 0; right: 0; height: ${TITLEBAR_HEIGHT}px;
  -webkit-app-region: drag; z-index: 20; background: transparent;
  display: flex; align-items: stretch; justify-content: flex-end;
}
/* Windows 自绘窗口按钮：颜色跟随页面主题变量，不再有系统浅色条 */
.coda-win-controls { display: flex; height: 100%; -webkit-app-region: no-drag; }
/* 可交互元素显式 no-drag（防御性声明）：品牌标 / 折叠按钮在 36px 拖拽带之下正常碰不到
   拖拽层，但万一布局变化把它们顶进带里，也不能被拖拽区域吞掉点击。 */
body.coda-desktop .brand-mark,
body.coda-desktop .sidebar-toggle { -webkit-app-region: no-drag; }
.coda-win-controls button {
  width: 46px; height: 100%; border: 0; padding: 0; background: transparent;
  color: var(--text-dim, #9aa0b0); display: grid; place-items: center; cursor: default;
}
.coda-win-controls button:hover { background: rgba(255, 255, 255, 0.08); color: var(--text, #e8eaf0); }
.coda-win-controls button.coda-close:hover { background: #e81123; color: #fff; }
.coda-win-controls svg { pointer-events: none; }
#coda-titlebar.is-maximized .coda-max-icon { display: none; }
#coda-titlebar:not(.is-maximized) .coda-restore-icon { display: none; }

/* macOS 折叠态：红绿灯由系统画在窗口左上角（x=14 起，三个按钮约 52px 宽），
   而折叠栏只有 68px —— 按钮正好顶到右边缘，看起来像溢出了侧栏。
   这里只给 macOS 壳的窄栏补 16px（68 → 84），让红绿灯完整落在侧栏里、右侧还留点余量。
   网页端和 Windows 壳没有系统红绿灯，不受影响。 */
body.coda-mac .app-shell.collapsed { grid-template-columns: 84px 1fr; }
/* 同上，兜住 index.css 里 < 980px 的 CSS 断点：JS 还没把 collapsed 类挂上时，
   窄窗依旧按 68px 渲染，这里同步放宽，避免首屏闪一下红绿灯被压住。 */
@media (max-width: 980px) {
  body.coda-mac .app-shell { grid-template-columns: 84px 1fr; }
}
`;
}

function desktopChromeScript(): string {
  const controls = process.platform === 'win32';
  // 平台标记：macOS 的系统红绿灯嵌在窗口左上角，样式要据此给折叠栏留出宽度。
  const platformClass = process.platform === 'darwin' ? 'coda-mac' : '';
  return `(() => {
  // classList.add 只接受单个 token：把 'coda-desktop coda-mac' 拼成一个字符串传入会在
  // macOS 上抛 InvalidCharacterError，整段脚本当场中断 —— 之前顶部拖不动、logo 压住
  // 红绿灯的根因就是这次抛错把后面 #coda-titlebar 的创建一并吞掉了。两个类必须分开加。
  document.body.classList.add('coda-desktop');
  ${platformClass ? `document.body.classList.add('${platformClass}');` : ''}
  if (document.getElementById('coda-titlebar')) return;
  const bar = document.createElement('div');
  bar.id = 'coda-titlebar';
  bar.setAttribute('aria-hidden', 'true');
  ${controls ? `
  const icons = {
    minimize: '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M1 5h8" stroke="currentColor" stroke-width="1"/></svg>',
    maximize: '<svg class="coda-max-icon" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><rect x="1.5" y="1.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1"/></svg><svg class="coda-restore-icon" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><rect x="1" y="3.5" width="5.5" height="5.5" fill="none" stroke="currentColor" stroke-width="1"/><path d="M3 3.5V1.5h5.5V7" fill="none" stroke="currentColor" stroke-width="1"/></svg>',
    close: '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M1 1l8 8M9 1l-8 8" stroke="currentColor" stroke-width="1"/></svg>',
  };
  const box = document.createElement('div');
  box.className = 'coda-win-controls';
  for (const action of ['minimize', 'toggle-maximize', 'close']) {
    const button = document.createElement('button');
    button.type = 'button';
    if (action === 'close') button.className = 'coda-close';
    button.innerHTML = icons[action === 'toggle-maximize' ? 'maximize' : action];
    button.addEventListener('click', () => window.codaDesktop?.window?.run?.(action));
    box.appendChild(button);
  }
  bar.appendChild(box);
  window.codaDesktop?.window?.onMaximized?.((maximized) => {
    bar.classList.toggle('is-maximized', Boolean(maximized));
  });
  ` : ''}
  document.body.appendChild(bar);
})();`;
}

/** Windows 自绘窗口按钮的 IPC 入口（监听一次即可）。 */
function registerWindowControls(): void {
  if (process.platform !== 'win32') return;
  ipcMain.on('desktop-window', (event, action: unknown) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed()) return;
    if (action === 'minimize') {
      win.minimize();
    } else if (action === 'toggle-maximize') {
      if (win.isMaximized()) win.unmaximize();
      else win.maximize();
    } else if (action === 'close') {
      win.close(); // 主进程的 close 处理 = 隐藏到托盘
    }
  });
}

/**
 * 每次页面加载（含后端自更新后的重载）都把桌面外壳装回去。
 *
 * 用 dom-ready 而不是 did-finish-load：后者要等 React 与静态资源全部加载完，
 * 页面会先按 100vh 渲染、再往下跳 36px；dom-ready 在挂载前就位，不会看到跳动。
 */
function installDesktopChrome(win: BrowserWindow): void {
  if (!customTitlebar) return;
  win.webContents.on('dom-ready', () => {
    if (win.isDestroyed()) return;
    void win.webContents.insertCSS(desktopChromeCss()).catch(() => undefined);
    // 脚本注入失败要留日志线索（此前 macOS 的 InvalidCharacterError 就是被这里静默吞掉的）
    void win.webContents
      .executeJavaScript(desktopChromeScript())
      .catch((err: Error) => {
        process.stdout.write(`[desktop] 标题栏脚本注入失败：${err?.message ?? err}\n`);
      });
  });
}

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let backend: BackendHandle | null = null;
let backendPort: number | null = null;
/** 启动失败时显示错误，不再自动重启循环（避免用户面对无限弹窗）。 */
let startupError: string | null = null;
/** execv 自更新看门狗（退出时清掉，防止句柄挂住进程）。 */
let restartWatcher: NodeJS.Timeout | null = null;

// ---- 单实例锁：两个后端抢同一个 SQLite 文件是不可接受的 ----
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 有人想开第二个实例 → 把现有窗口唤到前台
    showMainWindow();
  });
  void main();
}

async function main(): Promise<void> {
  await app.whenReady();
  registerWindowControls();
  // 「设置 → 关于」的更新入口：与托盘菜单共用 electron-updater（下载完整安装包）
  registerUpdateIpc();

  if (smokeMode) {
    // 不建窗口；只给整条链路一个总时限（依赖安装 + 首启可能花几分钟）
    setTimeout(() => {
      process.stderr.write('[desktop] smoke 超时：链路没有在时限内走完\n');
      app.exit(2);
    }, SMOKE_TIMEOUT_MS).unref?.();
  } else {
    createWindow();
  }

  try {
    // 目录布局：数据目录 / Python 运行时 / 版本目录都要在起后端之前建好。
    // 放进 try 里：创建失败要弹「启动失败」并带上具体路径与原因，不能静默卡在启动页。
    ensureLayout();
    // ⓪ 首启物化：安装包内置代码 → releases/<版本>/（托管模式布局，自更新的地基）
    const { ranDir } = bootstrapBundledBackend();
    // ① 端口：首选 8010（和其他宿主一致），被占则退到随机空闲端口
    backendPort = await pickPort();
    // ② Python 运行时：打包版优先用内置解释器（依赖已预装，零网络、零拷贝）；
    // 只有指纹对不上 / 开发模式才回退到 venv + 离线 wheelhouse / 在线 pip。
    // 依赖清单从实际运行目录取：packaged 下就是 releases/<版本>，不依赖 current 指针。
    const runtime = await ensurePythonRuntime(ranDir, (p) => sendProgress(p.message));
    process.stdout.write(`[desktop] Python 运行时：${runtime.mode}\n`);
    // ③ 起后端（cwd 留在 ranDir，保持与 entrypoint.sh 的 exec 行为一致）
    sendProgress('启动后端服务…');
    backend = spawnBackend({
      port: backendPort,
      backendRoot: path.join(ranDir, 'backend'),
      python: runtime.python,
    });
    // ④ 健康检查门控
    sendProgress('等待服务就绪…');
    const { version } = await waitForHealth(backendPort, { timeoutMs: 60_000 });
    sendProgress('就绪');
    if (smokeMode) {
      // 链路全部走通：打印机器可读的一行，CI 用退出码判定成败
      process.stdout.write(
        `[desktop] smoke ok version=${version} port=${backendPort} pid=${process.pid}\n`,
      );
      await backend?.stop();
      app.exit(0);
      return;
    }
    // ⑤ 托盘（关窗=隐藏到托盘的前提；图标缺失时降级跳过）
    createTray();
    // ⑥ 加载主界面
    if (mainWindow && !mainWindow.isDestroyed()) {
      await mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
    }
    // ⑦ execv 看门狗：界面里点「安装更新」后，后端同 PID 换血，这里负责刷新页面
    restartWatcher = watchBackendRestart(backendPort, version, () => {
      process.stdout.write(`[desktop] 后端已自更新 → 刷新窗口\n`);
      if (mainWindow && !mainWindow.isDestroyed()) {
        void mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
      }
    });
    // ⑧ 壳自身更新（electron-updater）：启动 20 秒后自动检查一次，之后每 6 小时一次
    setupAutoUpdate();
  } catch (err) {
    startupError = `${(err as Error).message ?? err}`;
    if (smokeMode) {
      process.stderr.write(`[desktop] smoke failed: ${startupError}\n`);
      await backend?.stop();
      app.exit(1);
      return;
    }
    showFatal(`启动失败：${startupError}`);
  }
}

/** 选端口：8010 被占（很可能是残留进程）则随机；两者都失败则报错。 */
async function pickPort(): Promise<number> {
  const defaultPort = 8010;
  if (!(await isPortInUse(defaultPort))) return defaultPort;
  process.stdout.write('[desktop] 8010 被占用（残留进程或并行实例），改用随机端口\n');
  return findFreePort();
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: '#0f1117',
    title: 'Coda',
    icon: process.platform === 'darwin' ? undefined : path.join(iconsDir(), 'icon.png'),
    autoHideMenuBar: true,
    // 深色自绘标题栏：Windows 用 titleBarOverlay 保留系统的最小化/最大化/关闭，
    // macOS 用 hiddenInset 把红绿灯嵌进内容；两者都不再出现系统浅色条。
    // macOS 红绿灯垂直居中在 TITLEBAR_HEIGHT 内（y = (36 - 12) / 2 = 12），
    // 随标题栏高度联动而不是写死，改高度不会把按钮挤到带外。
    ...(process.platform === 'win32'
      ? { titleBarStyle: 'hidden' as const }
      : process.platform === 'darwin'
        ? {
            titleBarStyle: 'hiddenInset' as const,
            trafficLightPosition: {
              x: 14,
              y: Math.round((TITLEBAR_HEIGHT - TRAFFIC_LIGHT_SIZE) / 2),
            },
          }
        : {}),
    webPreferences: {
      // 壳内页面就是普通 Web 页面：不开 nodeIntegration、不关 webSecurity
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, '..', 'dist-preload', 'index.js'),
    },
  });
  installDesktopChrome(mainWindow);
  // 最大化状态同步给自绘标题栏（Windows 的还原图标）
  const syncMaximized = (): void => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('desktop-window-state', mainWindow.isMaximized());
    }
  };
  mainWindow.on('maximize', syncMaximized);
  mainWindow.on('unmaximize', syncMaximized);
  // 就绪前先显示启动页（内联 HTML，无网络依赖）
  void loadSplash();
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
  });
  // 关窗 = 隐藏到托盘（后端继续跑）
  mainWindow.on('close', (event) => {
    if (isQuitting) return; // 真退出走 will-quit
    event.preventDefault();
    mainWindow?.hide();
  });
}

/** 把应用图标读成 data URI（启动页是 data: 页面，不能直接引用本地文件）。 */
function splashIconDataUri(): string {
  try {
    const png = fs.readFileSync(path.join(iconsDir(), 'icon.png'));
    return `data:image/png;base64,${png.toString('base64')}`;
  } catch {
    return '';
  }
}

/** 启动页（等待后端就绪时显示）：品牌图标 + 呼吸光晕 + 环形进度。 */
async function loadSplash(): Promise<void> {
  const icon = splashIconDataUri();
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    html, body { height: 100%; margin: 0; }
    body {
      background:
        radial-gradient(620px 420px at 50% 16%, rgba(224, 83, 214, 0.16), transparent 70%),
        radial-gradient(520px 380px at 82% 88%, rgba(107, 76, 240, 0.12), transparent 72%),
        #0b0a12;
      color: #e8eaf0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                   "Microsoft YaHei", sans-serif;
      display: flex; align-items: center; justify-content: center;
      -webkit-font-smoothing: antialiased; user-select: none; overflow: hidden;
    }
    .wrap {
      display: flex; flex-direction: column; align-items: center; gap: 14px;
      padding-bottom: 36px; animation: rise 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
    }
    .logo-ring { position: relative; display: grid; place-items: center; }
    .logo-ring::before {
      content: ""; position: absolute; inset: -14px; border-radius: 34px;
      background: radial-gradient(closest-side, rgba(217, 79, 208, 0.28), transparent 78%);
      filter: blur(6px); animation: breathe 3.2s ease-in-out infinite;
    }
    .logo { width: 84px; height: 84px; border-radius: 22px; display: block; position: relative;
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255, 255, 255, 0.08) inset; }
    .name { margin-top: 6px; font-size: 17px; font-weight: 600; letter-spacing: 0.06em; }
    .sub { font-size: 10.5px; letter-spacing: 0.22em; color: #7d7899; text-transform: uppercase; }
    .spinner {
      width: 22px; height: 22px; margin-top: 10px; border-radius: 50%;
      border: 2px solid rgba(255, 255, 255, 0.09); border-top-color: #e053d6;
      animation: spin 0.85s linear infinite;
    }
    #msg { font-size: 12.5px; color: #9aa0b0; min-height: 18px; letter-spacing: 0.02em; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes breathe { 0%, 100% { opacity: 0.55; transform: scale(0.96); } 50% { opacity: 1; transform: scale(1.04); } }
    @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  </style></head><body>
    <div class="wrap">
      <div class="logo-ring"><img class="logo" src="${icon}" alt="" /></div>
      <div class="name">Coda</div>
      <div class="sub">多智能体控制台</div>
      <div class="spinner"></div>
      <div id="msg">正在初始化…</div>
    </div>
  </body></html>`;
  await mainWindow?.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
}

/** 启动进度文案推给启动页。 */
function sendProgress(message: string): void {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents) {
    void mainWindow.webContents
      .executeJavaScript(
        `document.getElementById('msg')?.textContent = ${JSON.stringify(message)}; undefined`,
      )
      .catch(() => undefined);
  }
}

function showMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (process.platform === 'win32') {
    // Windows 托盘左键单击唤回：最小化先还原，隐藏再显示，最后聚焦置前。
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow.show();
  mainWindow.focus();
}

/** 致命错误：弹窗告知并保留窗口可看日志（铁律 11：显式暴露）。 */
function showFatal(message: string): void {
  void dialog.showMessageBox({
    type: 'error',
    title: 'Coda 启动失败',
    message,
    detail: [
      `数据目录：${dataDir()}`,
      `Python 运行时：${pythonRuntimeDir()}`,
      `安装日志：${installLogPath()}`,
      '删除 Python 运行时目录后重启即可重建（对话数据不受影响）。',
    ].join('\n'),
  });
}

// ---- 托盘 ----
/**
 * 真正退出（单一入口）：置 isQuitting → 容错销毁托盘 → app.quit()。
 * 「退出」菜单项单击一次就走到这里：托盘图标当场摘掉（进程收尾前的几百毫秒里
 * 用户不会再看到/点到它），窗口 close 处理器据此放行关闭，will-quit 再优雅停后端。
 */
function quitApp(): void {
  isQuitting = true; // 让窗口 close 放行（不再隐藏到托盘），再走正常退出流程
  try {
    tray?.destroy();
  } catch {
    /* 托盘可能已销毁（重复触发退出）：忽略，不能挡住退出 */
  }
  tray = null;
  app.quit();
}

function createTray(): void {
  // 与网页同源的品牌图：macOS 模板图 / Windows 全彩 / Linux 品牌色剪影（见 trayIconPath）
  const iconPath = trayIconPath();
  if (!fs.existsSync(iconPath)) {
    // 图标缺失时宁可不出托盘也不能崩（后面还有 Dock/任务栏兜底）；但必须留下日志线索
    process.stdout.write('[desktop] 托盘图标缺失，跳过托盘创建\n');
    return;
  }
  const trayIcon = new Tray(iconPath);
  tray = trayIcon;
  trayIcon.setToolTip(`Coda 正在运行（桌面端 ${app.getVersion()}）`);
  const menu = Menu.buildFromTemplate([
    { label: `Coda ${app.getVersion()}`, enabled: false },
    { label: '打开主界面', click: () => showMainWindow() },
    { type: 'separator' },
    { label: '检查桌面端更新…', click: () => void checkForUpdatesManually() },
    { type: 'separator' },
    { label: '退出', click: () => quitApp() },
  ]);

  if (process.platform === 'win32') {
    // Windows：左键单击 = 唤回/聚焦主窗口（最小化还原、隐藏显示、聚焦置前），绝不退出；
    // 右键 = 弹出同一份菜单，菜单项「退出」单击一次即彻底退出（quitApp）。
    //
    // 「退出需点两次」的根因在这里：Windows 对一次托盘右键会先后投递 WM_RBUTTONDOWN 与
    // WM_CONTEXTMENU 两条回调消息，Electron 把两者都按"右键"派发（notify_icon_host.cc
    // 的 case 列表共用）→ 'right-click' 事件触发两次；而 popUpContextMenu 的第一步就是
    // CloseContextMenu() 取消正在显示的菜单，Chromium 的 RunMenuAt 发现已有活动的
    // MenuController 时直接 Cancel 并返回、不再弹新菜单（menu_runner_impl.cc）→ 用户
    // 第一次点「退出」时菜单正在被拆掉，点击只把菜单关掉。这里对同一手势内的重复派发
    // 做去重（时间窗 250ms，自愈式，不依赖菜单关闭事件）保证一次右键只弹一次菜单。
    let menuShownAt = 0;
    trayIcon.on('right-click', () => {
      const now = Date.now();
      if (now - menuShownAt < 250) return;
      menuShownAt = now;
      trayIcon.popUpContextMenu(menu);
    });
    trayIcon.on('click', () => showMainWindow());
  } else {
    trayIcon.setContextMenu(menu);
    trayIcon.on('click', () => showMainWindow());
  }
}

let isQuitting = false;
app.on('before-quit', () => {
  isQuitting = true;
});

// ---- 退出清理：优雅停后端（只拦一次，stop 完成后放行） ----
let quitPending = false;
app.on('will-quit', (event) => {
  disposeAutoUpdate();
  if (restartWatcher) {
    clearInterval(restartWatcher);
    restartWatcher = null;
  }
  if (!backend || quitPending) return;
  quitPending = true;
  event.preventDefault();
  const handle = backend;
  backend = null; // 防重入：第二次 will-quit 直接放行
  void handle.stop().finally(() => app.quit());
});

// ---- macOS 惯例 ----
app.on('activate', () => {
  showMainWindow();
});

// ---- 开发快捷键 ----
app.on('web-contents-created', (_e, contents) => {
  contents.on('before-input-event', (event, input) => {
    // 打包版禁用 DevTools 快捷键；开发模式保留（isPackaged 判断）
    if (app.isPackaged && input.key === 'F12') event.preventDefault();
  });
});

// 外部链接交给系统浏览器（不在壳内导航走）
app.on('web-contents-created', (_e, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://127.0.0.1')) return { action: 'allow' };
    void shell.openExternal(url);
    return { action: 'deny' };
  });
});
