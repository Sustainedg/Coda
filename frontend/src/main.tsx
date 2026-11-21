import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { AuthGate } from './components/AuthGate'
import { warmKernelLogoCache } from './lib/kernelIcons'
import { initTheme } from './lib/theme'
import './index.css'

// 先把外观（深色 / 浅色 / 跟随系统）落到 <html> 上，再渲染，避免首屏闪一下。
initTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthGate>
        <App />
      </AuthGate>
    </BrowserRouter>
  </React.StrictMode>,
)

// 内核图标提前解好码：用户点进「设置 → 内核管理」时直接命中缓存，图标即点即现。
warmKernelLogoCache()
