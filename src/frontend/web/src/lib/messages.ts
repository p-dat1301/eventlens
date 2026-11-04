import type { ApiFailure } from "../api/http"

// User-facing Vietnamese copy for API failures. Only stable codes are mapped;
// nothing from a response body other than the code is ever shown.

export type FailureCopy = {
  readonly title: string
  readonly message: string
  readonly retryable: boolean
}

const API_COPY: Readonly<Record<string, FailureCopy>> = {
  catalog_unavailable: {
    title: "Kho tin tạm thời không phản hồi",
    message: "Dịch vụ danh mục bài viết chưa sẵn sàng. Thử lại sau ít giây.",
    retryable: true,
  },
  upstream_unavailable: {
    title: "Máy chủ đang khởi động lại",
    message: "Không kết nối được tới dịch vụ phía sau. Thử lại sau ít giây.",
    retryable: true,
  },
  catalog_contract_mismatch: {
    title: "Dữ liệu trả về không hợp lệ",
    message: "Máy chủ trả dữ liệu không đúng định dạng nên giao diện không hiển thị.",
    retryable: true,
  },
  cursor_mismatch: {
    title: "Trang tiếp theo không còn khớp bộ lọc",
    message: "Bộ lọc đã thay đổi. Danh sách sẽ được tải lại từ đầu.",
    retryable: true,
  },
  invalid_cursor: {
    title: "Không tải được trang tiếp theo",
    message:
      "Vị trí phân trang đã hết hạn (ví dụ sau khi máy chủ khởi động lại). Tải lại danh sách.",
    retryable: true,
  },
  timezone_required: {
    title: "Khoảng thời gian không hợp lệ",
    message: "Chọn lại ngày bắt đầu và kết thúc.",
    retryable: false,
  },
  invalid_publication_range: {
    title: "Khoảng thời gian không hợp lệ",
    message: "Ngày bắt đầu phải trước ngày kết thúc.",
    retryable: false,
  },
  query_too_long: {
    title: "Từ khóa quá dài",
    message: "Rút gọn từ khóa tìm kiếm (tối đa 200 ký tự).",
    retryable: false,
  },
  unknown_topic_id: {
    title: "Chủ đề không còn tồn tại",
    message: "Một chủ đề trong bộ lọc không còn trong danh mục. Bỏ chọn rồi thử lại.",
    retryable: false,
  },
  unknown_location_id: {
    title: "Địa phương không còn tồn tại",
    message: "Một địa phương trong bộ lọc không còn trong danh mục. Bỏ chọn rồi thử lại.",
    retryable: false,
  },
  relevance_requires_query: {
    title: "Cần từ khóa để sắp theo mức liên quan",
    message: "Nhập từ khóa hoặc chọn cách sắp xếp khác.",
    retryable: false,
  },
  article_not_found: {
    title: "Không tìm thấy bài viết",
    message: "Bài không tồn tại trong kho tin hoặc đường dẫn bị sai.",
    retryable: false,
  },
  article_gone: {
    title: "Bài viết đã bị gỡ",
    message: "Bài này đã bị xóa khỏi kho tin nên không còn hiển thị.",
    retryable: false,
  },
  article_version_stale: {
    title: "Bài đã có phiên bản mới",
    message: "Phiên bản bạn mở đã được thay bằng bản cập nhật.",
    retryable: false,
  },
  unauthenticated: {
    title: "Phiên ẩn danh không còn hiệu lực",
    message: "Phiên trên trình duyệt này đã hết hạn hoặc bị thu hồi.",
    retryable: true,
  },
  conversation_not_found: {
    title: "Không mở được hội thoại",
    message:
      "Hội thoại không tồn tại, đã bị xóa, hoặc thuộc một phiên ẩn danh khác (ví dụ phiên cũ đã hết hạn).",
    retryable: false,
  },
  run_not_found: {
    title: "Không tìm thấy lượt hỏi",
    message: "Lượt hỏi không còn truy cập được từ phiên hiện tại.",
    retryable: false,
  },
  citation_not_found: {
    title: "Không tìm thấy trích dẫn",
    message: "Trích dẫn này không còn trong hệ thống.",
    retryable: false,
  },
  source_not_found: {
    title: "Không tìm thấy nguồn",
    message: "Liên kết nguồn đã hết hạn. Mở lại trích dẫn để cấp liên kết mới.",
    retryable: true,
  },
  source_stale: {
    title: "Nguồn đã thay đổi",
    message:
      "Bài được trích dẫn đã được sửa hoặc gỡ sau khi trả lời. Đoạn trích đã lưu bên dưới là bản lúc trả lời, không phải nội dung hiện tại.",
    retryable: false,
  },
  access_denied: {
    title: "Không có quyền xem nguồn",
    message: "Phiên hiện tại không được phép xem nguồn này.",
    retryable: false,
  },
  idempotency_key_reused: {
    title: "Yêu cầu bị trùng",
    message: "Câu hỏi đã gửi trước đó với nội dung khác. Gửi lại như một câu hỏi mới.",
    retryable: false,
  },
  run_in_progress: {
    title: "Đang có câu hỏi chưa xong",
    message: "Chờ câu trả lời hiện tại hoặc hủy nó trước khi hỏi tiếp.",
    retryable: false,
  },
  invalid_question: {
    title: "Câu hỏi không hợp lệ",
    message: "Nhập câu hỏi (tối đa 2.000 ký tự).",
    retryable: false,
  },
  invalid_scope: {
    title: "Không tạo được phạm vi hỏi đáp",
    message: "Bộ lọc hoặc bài đã chọn không hợp lệ cho hội thoại.",
    retryable: false,
  },
  conversation_storage_unavailable: {
    title: "Hỏi đáp chưa sẵn sàng",
    message: "Kho lưu hội thoại chưa được cấu hình hoặc không phản hồi.",
    retryable: true,
  },
  retrieval_unavailable: {
    title: "Tìm kiếm nguồn tạm lỗi",
    message: "Không truy xuất được nguồn để trả lời. Thử lại sau.",
    retryable: true,
  },
  model_unavailable: {
    title: "AI chưa khả dụng",
    message: "Máy chủ chưa được nối với mô hình trả lời nên chưa thể trả lời câu hỏi.",
    retryable: true,
  },
  invalid_request: {
    title: "Yêu cầu không hợp lệ",
    message: "Máy chủ từ chối yêu cầu. Kiểm tra lại dữ liệu nhập.",
    retryable: false,
  },
  not_found: {
    title: "Không tìm thấy",
    message: "Đường dẫn không tồn tại.",
    retryable: false,
  },
}

const GENERIC: FailureCopy = {
  title: "Đã có lỗi",
  message: "Máy chủ trả lỗi chưa xác định. Thử lại sau.",
  retryable: true,
}

export const describeFailure = (failure: ApiFailure): FailureCopy => {
  switch (failure.kind) {
    case "network":
      return {
        title: "Mất kết nối",
        message: "Không gửi được yêu cầu tới máy chủ. Kiểm tra mạng rồi thử lại.",
        retryable: true,
      }
    case "unavailable":
      return {
        title: "Dịch vụ tạm thời không khả dụng",
        message: "Máy chủ chưa phản hồi (có thể đang khởi động lại). Thử lại sau ít giây.",
        retryable: true,
      }
    case "timeout":
      return {
        title: "Máy chủ phản hồi quá lâu",
        message: "Chưa nhận được phản hồi. Thử lại — yêu cầu sẽ không bị gửi trùng.",
        retryable: true,
      }
    case "malformed":
      return {
        title: "Dữ liệu trả về không hợp lệ",
        message: "Phản hồi không đúng hợp đồng API nên không được hiển thị.",
        retryable: true,
      }
    case "cancelled":
      return { title: "Đã hủy", message: "Yêu cầu đã bị hủy.", retryable: true }
    case "api":
      return API_COPY[failure.code] ?? GENERIC
  }
}

/** Short technical reference shown under an error (code + HTTP + request id). */
export const failureReference = (failure: ApiFailure): string | null => {
  if (failure.kind === "api") {
    const parts = [`Mã ${failure.code}`, `HTTP ${failure.status}`]
    if (failure.reason !== null) parts.push(failure.reason)
    if (failure.requestId !== null) parts.push(`yêu cầu ${failure.requestId.slice(0, 12)}`)
    return parts.join(" · ")
  }
  if (failure.kind === "malformed" || failure.kind === "unavailable") {
    return failure.status > 0 ? `HTTP ${failure.status}` : null
  }
  return null
}

/** Copy for a failed run (`query_run.v1.error.code`). */
export const RUN_FAILURE_COPY: Readonly<Record<string, string>> = {
  model_unavailable: "AI chưa khả dụng: máy chủ chưa được nối với mô hình trả lời.",
  retrieval_unavailable: "Không truy xuất được nguồn để trả lời.",
  deadline_exceeded: "Quá thời gian xử lý; câu hỏi chưa được trả lời.",
  evidence_stale: "Nguồn đã thay đổi trong lúc trả lời; hỏi lại để dùng bản mới.",
  invalid_grounded_answer: "Câu trả lời không vượt qua kiểm tra trích dẫn nên không được hiển thị.",
  budget_exceeded: "Câu hỏi vượt giới hạn xử lý.",
  capacity_exceeded: "Hệ thống đang quá tải; thử lại sau.",
  access_denied: "Phiên hiện tại không được phép truy cập nguồn trong phạm vi này.",
  scope_stale: "Bài trong phạm vi đã có phiên bản mới. Tạo hội thoại mới để hỏi về bản hiện tại.",
  scope_article_unavailable:
    "Bài trong phạm vi không còn khả dụng (đã gỡ hoặc không truy cập được).",
  scope_too_large: "Bộ lọc khớp quá nhiều bài (hơn 5.000). Thu hẹp bộ lọc rồi tạo hội thoại mới.",
  invalid_scope: "Phạm vi hội thoại không hợp lệ.",
  configuration_invalid: "Cấu hình máy chủ không hợp lệ.",
  orphaned_run: "Lượt xử lý bị gián đoạn phía máy chủ.",
  internal_error: "Lỗi nội bộ khi xử lý câu hỏi.",
}

export const runFailureMessage = (code: string): string =>
  RUN_FAILURE_COPY[code] ?? "Câu hỏi chưa được trả lời do lỗi phía máy chủ."
