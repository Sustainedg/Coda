/**
 * 输入区（**两端共享**）：对话页与协作「找 CEO 聊」都用这一份，样式与交互完全一致。
 *
 * 框里从左到右是：附件（可选，加号）、输入框、语音、设置（可选，齿轮）、发送（运行中变成停止）。
 * 输入框**没有 placeholder**：框里一长串提示文案在手机上既挤又容易跟真实输入混淆，
 * 光秃秃的输入框配图标反而更清楚。
 *
 * 附件与语音都收在这里，页面只提供数据与回调：
 * - `value` / `onChange`：草稿文字（页面持有，才能按场景清空与预填）；
 * - `attachments` / `onAttachmentsChange`：待发附件（页面持有，发送成功后由页面清空）；
 * - `onUpload`：把读好的 base64 交给页面调接口（不传就不显示加号，也不接管粘贴）；
 *   **粘贴截图走同一条路径**：输入框里粘贴图片直接变成附件（见下方 `onPaste`）；
 * - `onSend`：正文与附件都空时不触发（除非页面开了 `allowEmptySend`），页面从自己的状态里取附件。
 *
 * 齿轮菜单收的是**这条输入区的显示偏好**：目前一项「显示工具调用」——关掉后对话只亮
 * 最终回复（`MessageView` 的 `finalOnly`）。开关值由页面持有（本机偏好，存 localStorage），
 * 组件只收 `showTools` / `onShowToolsChange` 这对受控 props —— 输入区不自己长状态。
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type MutableRefObject,
  type ReactNode,
  type Ref,
} from 'react'
import type { AttachmentRef } from '../lib/types'
import { isImagePath, useMediaUrl } from '../lib/media'
import { describeSpeechError, finalTranscript, speechRecognitionCtor, type SpeechRecognizer } from '../lib/speech'
import { CheckIcon, MicIcon, PlusIcon, SendIcon, SettingsIcon, StopIcon } from './Icons'
import { ZoomableImage } from './ZoomableImage'

/** 单个附件的上限，与服务端 `MAX_ATTACHMENT_BYTES` 保持一致（前端先拦一道，省一次往返）。 */
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

/**
 * 输入框长高的上限（px）：样式表基础 `max-height`（160px）乘 `--composer-vscale`
 * （0.85 = 整体压低 15%）。改样式表里的系数时这个数要一起改。
 */
const MAX_INPUT_HEIGHT = 136

interface Props {
  /** 草稿文字 */
  value: string
  onChange: (value: string) => void
  /** 发送（正文与附件都空时不会触发） */
  onSend: () => void
  /** 待发送的附件 */
  attachments: AttachmentRef[]
  onAttachmentsChange: (next: AttachmentRef[]) => void
  /** 上传一个附件（回形针）；不传就不显示回形针 */
  onUpload?: (name: string, contentBase64: string) => Promise<AttachmentRef>
  /** 运行中：右侧按钮变成停止 */
  onStop?: () => void
  busy?: boolean
  /** 整块禁用（例如还没选对话、另一台设备正在跑） */
  disabled?: boolean
  /** 回车直接发送（默认回车换行；桌面端用键盘时开这个） */
  submitOnEnter?: boolean
  /** 空内容也能发送：协作的「推进任务」模式下，不补充内容就是让领导者继续推进 */
  allowEmptySend?: boolean
  /** 附件与语音的提示 / 报错回给页面显示 */
  onNotice?: (text: string) => void
  /** 输入框的无障碍名称 */
  inputLabel?: string
  /** 输入框引用：页面需要主动聚焦时传入（例如点「自由答复」跳到输入框） */
  inputRef?: Ref<HTMLTextAreaElement>
  /** 框上方的一条说明（token 用量、协作状态…）：贴着输入框，但不进框里 */
  header?: ReactNode
  /** 框下方的一条说明（用量统计之类）：贴着输入框下沿，也不进框里 */
  footer?: ReactNode
  /** 额外的类名：页面按自己的版式微调输入区（例如移动端对话页把文字放到图标上面） */
  className?: string
  /**
   * 运行中也允许继续输入 / 发送：发送交给页面排队，本轮回复结束后由页面自动发出。
   * 不开时沿用旧行为（运行中输入框置灰，右侧只有停止）。
   */
  queueWhenBusy?: boolean
  /** 齿轮菜单：「显示工具调用」当前值（页面持有；不传就不显示齿轮）。关掉 = 对话只亮最终回复。 */
  showTools?: boolean
  /** 齿轮菜单：切换「显示工具调用」。 */
  onShowToolsChange?: (show: boolean) => void
  /**
   * 草稿里 @ 提及的变化（@ 召唤）：值为 null = 当前没有正在输入的提及，
   * 非 null = 光标前刚打了 @（可带过滤词，如「@小」）。页面据此弹出智能体选择浮层。
   */
  onMentionChange?: (query: string | null) => void
}

/** 读成 base64（去掉 data URL 前缀），交给上传接口。 */
function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result ?? '')
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => reject(new Error(`读取附件「${file.name}」失败`))
    reader.readAsDataURL(file)
  })
}

/**
 * 剪贴板里的图片常常**没有文件名**（截图）或只叫「image.png」。按 MIME 补一个带时间戳的名字，
 * 落进工作目录后一眼能认出是哪次粘贴的。生成 `粘贴图片-20261114-153012.png` 这种。
 */
function pastedImageName(file: File): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  // `image/jpeg` → jpg，`image/svg+xml` → svg；认不出的类型退回 png。
  const ext = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg').replace('+xml', '')
  return `粘贴图片-${stamp}.${ext}`
}

/** 粘贴图片的落库名：真实文件名（从文件管理器复制的）保留，截图那类通用名换成时间戳名。 */
function nameForPastedImage(file: File): string {
  const name = (file.name || '').trim()
  if (!name || /^image\.(png|jpe?g|gif|webp|bmp|avif)$/i.test(name)) return pastedImageName(file)
  return name
}

export function Composer({
  value,
  onChange,
  onSend,
  attachments,
  onAttachmentsChange,
  onUpload,
  onStop,
  busy = false,
  disabled = false,
  submitOnEnter = false,
  allowEmptySend = false,
  onNotice,
  inputLabel = '输入消息',
  inputRef,
  header,
  footer,
  className,
  queueWhenBusy = false,
  showTools,
  onShowToolsChange,
  onMentionChange,
}: Props) {
  const [uploading, setUploading] = useState(false)
  const [listening, setListening] = useState(false)
  /** 齿轮菜单开合（只管浮层，开关值本身在页面手里）。 */
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsRef = useRef<HTMLDivElement | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const areaRef = useRef<HTMLTextAreaElement | null>(null)
  const voiceRef = useRef<SpeechRecognizer | null>(null)
  /**
   * 语音识别是否可用。**按钮一直显示**：独立应用（加到主屏）里 iOS 常常没有
   * `webkitSpeechRecognition`，藏起来用户只会以为功能没了；点一下给中文说明更好懂。
   */
  const voiceSupported = useMemo(() => Boolean(speechRecognitionCtor()), [])
  const locked = disabled || (busy && !queueWhenBusy)
  // 待发附件的取图出口（页面在顶层提供）：图片附件才能画缩略图并点开看。
  const mediaUrl = useMediaUrl()

  // 卸载（离开页面 / 关掉弹层）时收起麦克风，别让识别在后台空转。
  useEffect(() => () => voiceRef.current?.abort(), [])

  // 齿轮菜单：点外面或按 Esc 收起（与侧栏账号菜单同一套收法）。
  // settingsRef **只**挂在「齿轮按钮 + 气泡」那个小容器上（`.composer-settings`）：
  // 气泡在容器内，点气泡里的勾选框不会被当成「点外面」；点输入框、加号、发送、
  // 页面别处都会收起。注意别再把这个 ref 挂到 `.composer-box` 上 —— 同一个 ref
  // 挂两处时 React 以最后挂上的为准，整只输入框都会变成「内部」，点哪都关不掉。
  useEffect(() => {
    if (!settingsOpen) return
    const onPointerDown = (event: MouseEvent) => {
      if (!settingsRef.current?.contains(event.target as Node)) setSettingsOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSettingsOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [settingsOpen])

  /**
   * 输入框随内容长高，最多 `MAX_INPUT_HEIGHT`。
   *
   * **必须盯着 `value` 量，而不是只在键盘 `onChange` 里量**：草稿由页面持有，发送 / 换对话 /
   * 预填都是页面把它清掉或改掉，不会经过输入框的 `onChange` —— 只在 `onChange` 里量的话，
   * 写满一屏再发出去，框还是原来那么高，收不回来。
   *
   * 用 `useLayoutEffect` 在绘制前量：先归零再读 `scrollHeight`（`height: auto` 那一步），
   * 否则读到的永远是当前高度、只会越量越高。
   */
  useLayoutEffect(() => {
    const node = areaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, MAX_INPUT_HEIGHT)}px`
  }, [value])

  /**
   * 输入框同时挂两个 ref：页面传进来的（要主动聚焦 / 取节点）与本组件内部量高度用的。
   * 一个元素只接受一个 `ref`，所以在这里合并转发。
   */
  const setAreaRef = useCallback(
    (node: HTMLTextAreaElement | null) => {
      areaRef.current = node
      if (!inputRef) return
      if (typeof inputRef === 'function') inputRef(node)
      else (inputRef as MutableRefObject<HTMLTextAreaElement | null>).current = node
    },
    [inputRef],
  )

  /**
   * 检测光标处是否正在输入 @ 提及（@ 召唤）：
   * 从光标往前找最近的 `@`，它与光标之间只允许「中文 / 字母 / 数字 / 下划线 / 连字符」
   * （不含空格与换行 —— 遇到即视作普通文本），且 `@` 前面必须是行首或空白
   * （否则「a@b.com」这类邮箱也会误触发）。没有命中就回报 null。
   */
  const detectMention = useCallback(
    (text: string, caret: number) => {
      if (!onMentionChange) return
      const before = text.slice(0, caret)
      const at = before.lastIndexOf('@')
      if (at < 0) {
        onMentionChange(null)
        return
      }
      const word = before.slice(at + 1)
      if (!/^[\w一-鿿-]*$/.test(word)) {
        onMentionChange(null)
        return
      }
      const prev = at === 0 ? '' : before[at - 1]
      if (prev && !/\s/.test(prev)) {
        onMentionChange(null)
        return
      }
      onMentionChange(word)
    },
    [onMentionChange],
  )

  /** 草稿被页面清空 / 替换时（发送、切对话），提及浮层也要收起。 */
  useEffect(() => {
    if (value === '' && onMentionChange) onMentionChange(null)
  }, [value, onMentionChange])

  const notify = useCallback(
    (text: string) => {
      onNotice?.(text)
    },
    [onNotice],
  )

  /**
   * 把一批文件读成 base64 交给页面。回形针选文件与粘贴图片共用这一条路径，
   * 上限校验、上传中的转圈、错误提示也就只有一份。
   * `nameOf` 决定落库用的文件名：回形针用文件本名，粘贴的截图补一个时间戳名。
   */
  const uploadFiles = async (files: File[], nameOf: (file: File) => string = (file) => file.name) => {
    if (files.length === 0 || !onUpload) return
    if (locked) {
      notify('当前还不能添加附件')
      return
    }
    setUploading(true)
    try {
      const uploaded: AttachmentRef[] = []
      for (const file of files) {
        if (file.size > MAX_ATTACHMENT_BYTES) {
          notify(`「${nameOf(file)}」超过 ${MAX_ATTACHMENT_BYTES / 1024 / 1024} MB，换个小一点的文件`)
          continue
        }
        const encoded = await readBase64(file)
        uploaded.push(await onUpload(nameOf(file), encoded))
      }
      if (uploaded.length > 0) onAttachmentsChange([...attachments, ...uploaded])
    } catch (e) {
      notify((e as Error)?.message || '附件上传失败')
    } finally {
      setUploading(false)
    }
  }

  const pick = (files: FileList | null) => {
    if (!files || files.length === 0) return
    void uploadFiles(Array.from(files))
  }

  /**
   * 粘贴：剪贴板里有图片就直接当附件传（截图最常见）。命中图片才 `preventDefault`，
   * 免得浏览器顺手把图片的其他表示（URL / 文本）塞进输入框；没有图片时原样走默认的文本粘贴。
   * 不支持上传（没给 `onUpload`）或当前禁用时，不接管，交回浏览器默认行为。
   */
  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    if (!onUpload) return
    const images = Array.from(event.clipboardData?.items ?? [])
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((file): file is File => Boolean(file))
    if (images.length === 0) return
    event.preventDefault()
    void uploadFiles(images, nameForPastedImage)
  }

  const toggleVoice = () => {
    if (listening) {
      voiceRef.current?.stop()
      setListening(false)
      return
    }
    const Recognizer = speechRecognitionCtor()
    if (!Recognizer) {
      notify('当前环境不支持语音输入（独立应用到主屏时浏览器常常禁用），可以用键盘上的听写')
      return
    }
    const recognizer = new Recognizer()
    recognizer.lang = 'zh-CN'
    recognizer.continuous = true
    recognizer.interimResults = false
    recognizer.onresult = (event) => {
      const text = finalTranscript(event)
      if (text) onChange(value ? `${value}${text}` : text)
    }
    recognizer.onerror = (event) => {
      notify(describeSpeechError(event.error))
      setListening(false)
    }
    recognizer.onend = () => setListening(false)
    voiceRef.current = recognizer
    try {
      recognizer.start()
      setListening(true)
    } catch {
      notify('语音输入启动失败，请重试')
      setListening(false)
    }
  }

  const canSend = allowEmptySend || Boolean(value.trim() || attachments.length > 0)
  const submit = () => {
    if (locked || !canSend) return
    onSend()
  }

  return (
    <div className={className ? `composer ${className}` : 'composer'}>
      {header && <div className="composer-head">{header}</div>}

      {attachments.length > 0 && (
        <div className="composer-attach-row">
          {attachments.map((item) => {
            // 图片附件画缩略图并可点开看原图 —— 只显示文件名的话，粘的是哪张图根本认不出来。
            const thumb = mediaUrl && isImagePath(item.name) ? mediaUrl(item.rel_path) : null
            return (
              <span className="composer-attach-chip" key={item.rel_path}>
                {thumb && (
                  <ZoomableImage className="composer-attach-thumb" src={thumb} alt={item.name} />
                )}
                <span className="composer-attach-name">{item.name}</span>
                <button
                  onClick={() =>
                    onAttachmentsChange(attachments.filter((row) => row.rel_path !== item.rel_path))
                  }
                  aria-label={`移除附件 ${item.name}`}
                >
                  ✕
                </button>
              </span>
            )
          })}
        </div>
      )}

      <div className="composer-box">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            void pick(event.target.files)
            // 清空 value：同一个文件连选两次也要能触发 change
            event.target.value = ''
          }}
        />
        {onUpload && (
          <button
            className="composer-tool"
            onClick={() => fileRef.current?.click()}
            disabled={locked || uploading}
            aria-label="添加附件"
            title="添加附件（图片 / 文档都行，会上传到工作目录）"
          >
            {uploading ? <span className="composer-tool-spin">◌</span> : <PlusIcon size={17} />}
          </button>
        )}
        {/* 设置齿轮贴着输入框左侧（与加号一道在左）：显示偏好的入口。
            气泡是白卡片 + 底部朝下的尖角，尖角正对齿轮 ——「点外面收起」的
            判定范围也正好是按钮 + 气泡这一团。 */}
        {typeof showTools === 'boolean' && onShowToolsChange && (
          <div className="composer-settings" ref={settingsRef}>
            <button
              className={`composer-tool${settingsOpen ? ' open' : ''}`}
              onClick={() => setSettingsOpen((open) => !open)}
              aria-label="显示设置"
              aria-expanded={settingsOpen}
              aria-haspopup="menu"
              title="显示设置（工具调用显隐等）"
            >
              <SettingsIcon size={17} />
            </button>
            {settingsOpen && (
              <div className="composer-settings-menu" role="menu" aria-label="显示设置">
                {/* 底部正中的朝下尖角：指向下面的齿轮按钮 */}
                <span className="composer-settings-caret" aria-hidden />
                <button
                  type="button"
                  className="composer-settings-item"
                  role="menuitemcheckbox"
                  aria-checked={showTools}
                  onClick={() => onShowToolsChange(!showTools)}
                >
                  {/* 勾选标记：选中显示细线 ✓，未选中只留占位（文字不跳动） */}
                  <span className={`composer-settings-check${showTools ? ' on' : ''}`}>
                    {showTools && <CheckIcon size={16} />}
                  </span>
                  <span className="composer-settings-label">显示工具调用</span>
                </button>
              </div>
            )}
          </div>
        )}
        <textarea
          ref={setAreaRef}
          rows={1}
          value={value}
          aria-label={inputLabel}
          disabled={locked}
          // 高度不在这里量：草稿可能由页面清空（发送 / 换对话），只有盯着 `value` 的 effect 才收得回来。
          onChange={(event) => {
            onChange(event.target.value)
            detectMention(event.target.value, event.target.selectionStart ?? 0)
          }}
          // 粘贴截图 → 直接变成待发附件（走与回形针同一条上传路径）。
          onPaste={handlePaste}
          // 光标移动（点击 / 方向键）后重算 @ 提及：浮层要跟着光标走。
          onSelect={(event) => {
            const node = event.currentTarget
            detectMention(node.value, node.selectionStart ?? 0)
          }}
          onKeyDown={(event) => {
            if (!submitOnEnter) return
            if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
            event.preventDefault()
            submit()
          }}
        />
        {/* 运行中只留一个按钮：框里写了东西就是「排队发送」，空着就是「停止」。
            这样手指永远落在同一个位置，也不会出现「想停止却点到发送」。 */}
        {/* 语音紧贴发送：说完就发，这两个动作本来就是一对。
            voice 类是对话页布局的推右锚点（右侧一组的起点），见 index.css / mobile.css。 */}
        <button
          className={`composer-tool voice${listening ? ' on' : ''}${voiceSupported ? '' : ' muted'}`}
          onClick={toggleVoice}
          disabled={locked}
          aria-label={listening ? '结束语音输入' : '语音输入'}
          title={
            voiceSupported
              ? listening
                ? '正在听…点一下结束'
                : '语音输入（按浏览器识别转成文字）'
              : '当前环境不支持语音输入，点一下看说明'
          }
        >
          <MicIcon size={17} />
        </button>
        {busy && (!queueWhenBusy || !canSend) && (
          <button className="composer-tool stop" onClick={() => onStop?.()} aria-label="停止" title="停止这次运行">
            <StopIcon size={15} />
          </button>
        )}
        {(!busy || (queueWhenBusy && canSend)) && (
          <button
            className="composer-tool send"
            onClick={submit}
            disabled={disabled || !canSend}
            aria-label={busy ? '排队发送' : '发送'}
            title={busy ? '排队发送：本轮回复结束后自动发出' : '发送'}
          >
            <SendIcon size={18} />
          </button>
        )}
      </div>

      {footer && <div className="composer-foot">{footer}</div>}
    </div>
  )
}
