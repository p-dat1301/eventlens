import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react"

import type { Conversation } from "../api/chat"
import type { ApiFailure } from "../api/http"
import { useServices } from "../app/services"
import { FailureCallout, LoadingLine } from "../app/ui"
import { formatDateTime } from "../lib/dates"
import { useMediaQuery, usePrefersReducedMotion } from "../lib/hooks"
import { describeFailure } from "../lib/messages"
import { Link, navigate, useDocumentTitle, useLocation } from "../lib/router"
import { useCitationViewer } from "./citations"
import { type DraftScope, draftHref, parseDraftScope } from "./draft"
import { RunView } from "./RunView"
import { DraftScopeChip, parseScope, ScopeHelp, ScopeSummary, scopeKind } from "./ScopeSummary"
import { SourcePane } from "./SourcePane"
import { useFirstQuestion } from "./start"
import { activeRun, isUncertain } from "./thread"
import { useThread } from "./useThread"

const MAX_QUESTION = 2_000

export const ChatPage = ({ conversationId }: { readonly conversationId: string | null }) => {
  const { chat, session } = useServices()
  const [list, setList] = useState<ListState>({ status: "loading", items: [], next: null })
  const [listToken, refreshList] = useState(0)
  const [sessionNotice, setSessionNotice] = useState<"replaced" | null>(null)

  useEffect(
    () =>
      session.subscribe((event) => {
        if (event === "replaced") setSessionNotice("replaced")
      }),
    [session],
  )

  // Conversation list (also establishes the session: a 401 creates one once).
  // biome-ignore lint/correctness/useExhaustiveDependencies: refresh after create/delete (listToken) and on switching conversation
  useEffect(() => {
    const controller = new AbortController()
    setList((current) => ({ ...current, status: "loading" }))
    void chat.listConversations(null, controller.signal).then((result) => {
      if (controller.signal.aborted) return
      setList(
        result.ok
          ? { status: "ready", items: result.data.items, next: result.data.next_cursor }
          : result.failure.kind === "cancelled"
            ? { status: "ready", items: [], next: null }
            : { status: "failed", items: [], next: null, failure: result.failure },
      )
    })
    return () => controller.abort()
    // listToken / conversationId: refresh after create, delete or switching.
  }, [chat, listToken, conversationId])

  const loadMore = async () => {
    if (list.next === null) return
    const result = await chat.listConversations(list.next)
    if (result.ok) {
      setList((current) => ({
        status: "ready",
        items: [
          ...current.items,
          ...result.data.items.filter(
            (item) => !current.items.some((c) => c.conversation_id === item.conversation_id),
          ),
        ],
        next: result.data.next_cursor,
      }))
    }
  }

  const remove = async (id: string) => {
    const result = await chat.deleteConversation(id)
    if (result.ok || (result.failure.kind === "api" && result.failure.status === 404)) {
      setList((current) => ({
        ...current,
        items: current.items.filter((item) => item.conversation_id !== id),
      }))
      if (id === conversationId) navigate("/chat", { replace: true })
      return null
    }
    return result.failure
  }

  useDocumentTitle(conversationId === null ? "Hỏi đáp" : "Hội thoại")
  const refresh = useCallback(() => refreshList((n) => n + 1), [])

  return (
    <div
      className={`shell-width chat-page${conversationId === null ? " chat-home" : " chat-open"}`}
    >
      <aside className="chat-sidebar" aria-labelledby="conversations-title">
        <div className="chat-sidebar-head">
          <h2 id="conversations-title">Hội thoại của bạn</h2>
          <Link className="btn btn-small" to="/chat">
            Mới
          </Link>
        </div>
        <ConversationList
          list={list}
          currentId={conversationId}
          onRetry={refresh}
          onLoadMore={loadMore}
          onDelete={remove}
        />
        <SessionNote replaced={sessionNotice === "replaced"} />
      </aside>

      <section className="chat-main" aria-label="Hội thoại">
        {conversationId === null ? (
          <ChatHome onSettled={refresh} />
        ) : (
          <Thread key={conversationId} conversationId={conversationId} />
        )}
      </section>
    </div>
  )
}

type ListState =
  | {
      readonly status: "loading" | "ready"
      readonly items: readonly Conversation[]
      readonly next: string | null
    }
  | {
      readonly status: "failed"
      readonly items: readonly Conversation[]
      readonly next: string | null
      readonly failure: ApiFailure
    }

// One line by default; the details stay one click (or Enter/Space) away.
// Expiry ends *access*: nothing here promises physical deletion of the data.
const SessionNote = ({ replaced }: { readonly replaced: boolean }) => (
  <div className="session-note">
    {replaced ? (
      <p className="callout callout-warning">
        <strong>Phiên cũ đã hết hạn.</strong> EventLens đã tạo phiên ẩn danh mới; hội thoại của
        phiên trước không còn truy cập được.
      </p>
    ) : null}
    <details className="session-details">
      <summary>Phiên ẩn danh · truy cập tối đa 7 ngày</summary>
      <p className="field-note">
        Đây không phải tài khoản. Hội thoại chỉ mở được từ trình duyệt này, và không truy cập được
        nữa khi phiên hết hạn (7 ngày) hoặc khi bạn xóa cookie. Hết hạn hoặc xóa hội thoại là ngừng
        truy cập; EventLens chưa có lịch xóa vật lý dữ liệu nên không cam kết dữ liệu bị xóa sau 7
        ngày.
      </p>
    </details>
  </div>
)

const ConversationList = ({
  list,
  currentId,
  onRetry,
  onLoadMore,
  onDelete,
}: {
  readonly list: ListState
  readonly currentId: string | null
  readonly onRetry: () => void
  readonly onLoadMore: () => void
  readonly onDelete: (id: string) => Promise<ApiFailure | null>
}) => {
  const [confirming, setConfirming] = useState<string | null>(null)
  const [deleteFailure, setDeleteFailure] = useState<ApiFailure | null>(null)
  if (list.status === "failed") {
    return <FailureCallout failure={list.failure} onRetry={onRetry} heading="h3" />
  }
  if (list.status === "loading" && list.items.length === 0)
    return <LoadingLine label="Đang tải hội thoại…" />
  if (list.items.length === 0) return <p className="field-note">Chưa có hội thoại nào.</p>
  return (
    <>
      {deleteFailure === null ? null : <FailureCallout failure={deleteFailure} heading="h3" />}
      <ul className="conversation-list">
        {list.items.map((item) => {
          const scope = parseScope(item.scope)
          return (
            <li
              key={item.conversation_id}
              className={item.conversation_id === currentId ? "is-current" : undefined}
            >
              <Link
                className="conversation-link"
                to={`/chat/${item.conversation_id}`}
                aria-current={item.conversation_id === currentId ? "page" : undefined}
              >
                <span className="conversation-title">{item.title ?? "Hội thoại không tên"}</span>
                <span className="conversation-meta">
                  {scopeKind(scope)} · {formatDateTime(item.updated_at)}
                </span>
              </Link>
              {confirming === item.conversation_id ? (
                <span className="confirm-row">
                  <button
                    className="btn btn-small btn-danger"
                    type="button"
                    onClick={async () => {
                      const failure = await onDelete(item.conversation_id)
                      setDeleteFailure(failure)
                      setConfirming(null)
                    }}
                  >
                    Xóa hẳn
                  </button>
                  <button
                    className="btn btn-small btn-quiet"
                    type="button"
                    onClick={() => setConfirming(null)}
                  >
                    Giữ lại
                  </button>
                </span>
              ) : (
                <button
                  className="btn btn-small btn-quiet delete-button"
                  type="button"
                  aria-label={`Xóa hội thoại ${item.title ?? ""}`.trim()}
                  onClick={() => setConfirming(item.conversation_id)}
                >
                  Xóa
                </button>
              )}
            </li>
          )
        })}
      </ul>
      {list.next === null ? null : (
        <button className="btn btn-small" type="button" onClick={onLoadMore}>
          Xem thêm hội thoại
        </button>
      )}
    </>
  )
}

const ChatHome = ({ onSettled }: { readonly onSettled: () => void }) => {
  const location = useLocation()
  const draft = useMemo<DraftScope>(() => parseDraftScope(location.search), [location.search])
  const first = useFirstQuestion(onSettled)
  const titleId = useId()
  return (
    <div className="chat-home-body">
      <h1 className="chat-home-title" id={titleId}>
        Bạn muốn tìm hiểu tin gì?
      </h1>
      <div className="scope-line">
        <DraftScopeChip scope={draft} />
        <ScopeHelp />
      </div>
      {first.failure === null ? null : (
        <FailureCallout failure={first.failure} heading="h2">
          {isUncertain(first.failure) ? (
            <p>
              Có thể hội thoại đã được tạo dù chưa nhận được phản hồi; nếu có, nó sẽ hiện trong danh
              sách “Hội thoại của bạn”. Gửi lại sẽ tạo một hội thoại mới.
            </p>
          ) : null}
        </FailureCallout>
      )}
      <Composer
        inline
        labelledBy={titleId}
        disabled={first.pending}
        busyReason={first.pending ? "Đang tạo hội thoại…" : null}
        onSubmit={(question) => first.send(draft, question)}
        seed={null}
      />
    </div>
  )
}

const Thread = ({ conversationId }: { readonly conversationId: string }) => {
  const { chat } = useServices()
  const thread = useThread(chat, conversationId)
  const viewer = useCitationViewer(chat)
  const wide = useMediaQuery("(min-width: 72rem)")
  const { state } = thread
  const active = activeRun(state)
  const turnsRef = useRef<HTMLOListElement>(null)
  const [unseen, setUnseen] = useState(false)
  const seenTerminal = useRef<Set<string> | null>(null)
  // Set when the reader sends a question: its answer is followed into view.
  const followNext = useRef(false)
  const reduced = usePrefersReducedMotion()

  useDocumentTitle(state.conversation?.title ?? "Hội thoại")

  // New answers: follow them only if the reader is already at the end;
  // otherwise offer a button instead of yanking the scroll position.
  const terminalIds = state.runs
    .filter(
      (entry) =>
        entry.run !== null && entry.run.status !== "queued" && entry.run.status !== "running",
    )
    .map((entry) => entry.runId)
    .join(",")
  useEffect(() => {
    if (state.status !== "ready") return
    const ids = new Set(terminalIds === "" ? [] : terminalIds.split(","))
    if (seenTerminal.current === null) {
      seenTerminal.current = ids
      return
    }
    const fresh = [...ids].filter((id) => !seenTerminal.current?.has(id))
    seenTerminal.current = ids
    if (fresh.length === 0) return
    const nearBottom =
      window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 160
    const answer = document.querySelector(`[data-run-id="${fresh[0]}"] .turn-answer`)
    if (followNext.current || nearBottom) {
      // The reader just asked (or is already at the end): follow the answer.
      followNext.current = false
      answer?.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" })
    } else {
      setUnseen(true)
    }
  }, [terminalIds, state.status, reduced])

  useEffect(() => {
    if (!unseen) return
    const onScroll = () => {
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 160)
        setUnseen(false)
    }
    window.addEventListener("scroll", onScroll, { passive: true })
    return () => window.removeEventListener("scroll", onScroll)
  }, [unseen])

  const submit = useCallback(
    async (question: string) => {
      followNext.current = true
      await thread.submit(question)
      window.requestAnimationFrame(() => {
        turnsRef.current?.lastElementChild?.scrollIntoView({ block: "nearest" })
      })
    },
    [thread],
  )

  if (state.status === "loading" && state.conversation === null) {
    return <LoadingLine label="Đang mở hội thoại…" />
  }
  if (state.status === "failed" && state.failure !== null) {
    const gone = state.failure.kind === "api" && state.failure.status === 404
    return (
      <div className="thread-failed">
        <h1 className="page-title">{describeFailure(state.failure).title}</h1>
        <FailureCallout failure={state.failure} onRetry={gone ? undefined : thread.reload} />
        <p>
          <Link className="link" to="/chat">
            Bắt đầu hội thoại mới
          </Link>
        </p>
      </div>
    )
  }

  const conversation = state.conversation
  const scope = conversation === null ? null : parseScope(conversation.scope)
  // scope_stale: a new conversation about the current version of the same
  // article (draft only; it is created when its first question is sent).
  const pinned = scope?.type === "articles" ? (scope.articles ?? []) : []
  const onlyPin = pinned.length === 1 ? pinned[0] : undefined
  const newVersionConversation =
    onlyPin === undefined
      ? null
      : () =>
          navigate(draftHref({ type: "article", articleId: onlyPin.article_id, versionId: null }))

  return (
    <div
      className={`thread-layout${viewer.state.status !== "closed" && wide ? " has-source" : ""}`}
    >
      <div className="thread">
        <header className="thread-header">
          <p className="back-row chat-back">
            <Link className="link" to="/chat">
              ← Danh sách hội thoại
            </Link>
          </p>
          <h1 className="thread-title">{conversation?.title ?? "Hội thoại"}</h1>
          {conversation === null ? null : <ScopeSummary raw={conversation.scope} />}
        </header>

        {state.runs.length === 0 && state.submission?.phase === "sending" ? (
          <ol className="turns">
            <li className="turn">
              <p className="turn-question">{state.submission.question}</p>
              <LoadingLine label="Đang gửi câu hỏi…" />
            </li>
          </ol>
        ) : state.runs.length === 0 ? (
          <p className="field-note thread-empty">Chưa có câu hỏi nào.</p>
        ) : (
          <ol className="turns" ref={turnsRef}>
            {state.runs.map((entry) => (
              <RunView
                key={entry.runId}
                entry={entry}
                activeCitationId={
                  viewer.state.status === "closed" ? null : viewer.state.citation.citation_id
                }
                onCite={(runId, citation, trigger) => void viewer.open(runId, citation, trigger)}
                onCancel={(runId) => void thread.cancel(runId)}
                onAskAgain={(question) => void submit(question)}
                onReload={thread.retryRun}
                onNewConversation={newVersionConversation}
                canAskAgain={active === null && state.submission === null}
              />
            ))}
          </ol>
        )}

        {state.notice === null ? null : (
          <FailureCallout failure={state.notice} heading="h2">
            <button
              className="btn btn-small btn-quiet"
              type="button"
              onClick={thread.dismissNotice}
            >
              Đóng thông báo
            </button>
          </FailureCallout>
        )}
        <SubmissionNotice
          submission={state.submission}
          onRetry={thread.retrySubmission}
          onDiscard={thread.discardSubmission}
        />

        {unseen ? (
          <button
            className="btn btn-primary new-content"
            type="button"
            onClick={() => {
              setUnseen(false)
              turnsRef.current?.lastElementChild?.scrollIntoView({ block: "start" })
            }}
          >
            Có câu trả lời mới ↓
          </button>
        ) : null}

        <Composer
          disabled={
            active !== null || state.submission?.phase === "sending" || conversation === null
          }
          busyReason={
            active !== null
              ? "Đang chờ câu trả lời trước."
              : state.submission?.phase === "sending"
                ? "Đang gửi…"
                : null
          }
          onSubmit={submit}
          seed={state.submission?.phase === "rejected" ? state.submission.question : null}
        />
      </div>

      {viewer.state.status === "closed" ? null : (
        <SourcePane state={viewer.state} onClose={viewer.close} modal={!wide} />
      )}
    </div>
  )
}

const SubmissionNotice = ({
  submission,
  onRetry,
  onDiscard,
}: {
  readonly submission: ReturnType<typeof useThread>["state"]["submission"]
  readonly onRetry: () => void
  readonly onDiscard: () => void
}) => {
  if (submission === null || submission.phase === "sending") return null
  if (submission.phase === "uncertain") {
    return (
      <div className="callout callout-warning" role="alert">
        <h2>Chưa xác nhận được câu hỏi đã gửi</h2>
        <p>
          “{submission.question}” — máy chủ chưa phản hồi rõ. Gửi lại an toàn: nếu máy chủ đã nhận,
          câu hỏi sẽ không bị tạo trùng.
        </p>
        <div className="callout-actions">
          <button className="btn btn-small" type="button" onClick={onRetry}>
            Gửi lại
          </button>
          <button className="btn btn-small btn-quiet" type="button" onClick={onDiscard}>
            Bỏ qua
          </button>
        </div>
      </div>
    )
  }
  return submission.failure === null ? null : (
    <FailureCallout failure={submission.failure} heading="h2">
      <button className="btn btn-small btn-quiet" type="button" onClick={onDiscard}>
        Đóng thông báo
      </button>
    </FailureCallout>
  )
}

const Composer = ({
  disabled,
  busyReason,
  onSubmit,
  seed,
  inline = false,
  labelledBy,
}: {
  readonly disabled: boolean
  readonly busyReason: string | null
  /** Inline: the text stays in the box whatever the outcome (the page is left on success). */
  readonly onSubmit: (question: string) => Promise<unknown>
  readonly seed: string | null
  /** New-conversation composer: in the page flow (not sticky), text kept until accepted. */
  readonly inline?: boolean
  readonly labelledBy?: string
}) => {
  const [text, setText] = useState("")
  const [issue, setIssue] = useState<string | null>(null)
  const id = useId()
  const hintId = useId()
  const errorId = useId()

  // A rejected question comes back into the box so it is not lost.
  useEffect(() => {
    if (seed !== null) setText((current) => (current === "" ? seed : current))
  }, [seed])

  const send = async () => {
    const question = text.trim()
    if (question === "") {
      setIssue("Nhập câu hỏi trước khi gửi.")
      return
    }
    if (Array.from(question).length > MAX_QUESTION) {
      setIssue("Câu hỏi tối đa 2.000 ký tự.")
      return
    }
    setIssue(null)
    if (inline) {
      // Keep the text until the conversation exists; a failure leaves it as typed.
      await onSubmit(question)
      return
    }
    setText("")
    await onSubmit(question)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter breaks the line; never while an IME is composing (Vietnamese input).
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (!disabled) void send()
    }
  }

  return (
    <form
      className={inline ? "composer composer-inline" : "composer"}
      onSubmit={(event: FormEvent<HTMLFormElement>) => {
        event.preventDefault()
        if (!disabled) void send()
      }}
    >
      {labelledBy === undefined ? (
        <label className="visually-hidden" htmlFor={id}>
          Câu hỏi
        </label>
      ) : null}
      <textarea
        id={id}
        rows={inline ? 3 : 2}
        value={text}
        maxLength={MAX_QUESTION * 2}
        placeholder={
          inline ? "Ví dụ: Các bài nói gì về giao thông?" : "Đặt câu hỏi tiếp trong phạm vi này…"
        }
        aria-labelledby={labelledBy}
        aria-invalid={issue !== null}
        aria-describedby={issue === null ? hintId : `${hintId} ${errorId}`}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer-row">
        <p className="field-note" id={hintId}>
          {busyReason ?? "Enter để gửi, Shift+Enter để xuống dòng."}
        </p>
        <button className="btn btn-primary" type="submit" disabled={disabled}>
          Gửi
        </button>
      </div>
      {issue === null ? null : (
        <p className="field-error" id={errorId} role="alert">
          {issue}
        </p>
      )}
    </form>
  )
}
