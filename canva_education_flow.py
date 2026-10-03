from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    expect,
    sync_playwright,
)


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


CANVA_URL = "https://www.canva.com/"
CANVA_EDUCATION_PRICING_URL = (
    "https://www.canva.com/vi_vn/bang-gia/?tab=education"
)
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_TIMEOUT_MS = 30_000
UPLOAD_TIMEOUT_MS = 60_000
POST_LOGIN_DELAY_MS = 5_000
FINAL_FORM_FIELD_DELAY_MS = 1_000
DEFAULT_SCHOOL_ADDRESS = "National Road 5 Phnom Penh, Campuchia"
DEFAULT_SCHOOL_WEBSITE = "https://www.beltei.edu.kh/"
DEFAULT_MAIL_API_URL = "https://tools.dongvanfb.net/api/graph_messages"
DEFAULT_MAIL_REQUEST_TIMEOUT = 30
DEFAULT_OTP_INITIAL_DELAY = 10.0
DEFAULT_OTP_INTERVAL = 5.0
DEFAULT_OTP_TIMEOUT = 120.0
MAX_OTP_RESENDS = 3
OTP_RESEND_WAIT_MS = 90_000
OTP_RESULT_TIMEOUT_MS = 60_000
EDUCATION_ONBOARDING_FORM_NAME = "Bạn sẽ sử dụng Canva cho công việc gì?"
EDUCATION_ENTRY_PATTERN = re.compile(r"^Giáo dục(?:\s|$)", re.IGNORECASE)
TEACHER_LOCATION_HEADING_PATTERN = re.compile(
    r"Bạn đang giảng dạy ở đâu\?",
    re.IGNORECASE,
)
DOCUMENT_STEP_HEADING_PATTERN = re.compile(
    r"Xác minh thông tin",
    re.IGNORECASE,
)
IDENTITY_STEP_HEADING_PATTERN = re.compile(
    r"Xác nhận thông tin chi tiết.*khớp với tài liệu",
    re.IGNORECASE,
)
CREATE_ACCOUNT_NAME_PATTERN = re.compile(
    r"^(?:Họ\s+và\s+tên|Tên)(?:\s*\*)?$",
    re.IGNORECASE,
)
SCHOOL_NO_OPTIONS_PATTERN = re.compile(
    r"^(?:Không\s+có\s+tùy\s+chọn\s+nào|No\s+options?)$",
    re.IGNORECASE,
)
PASSKEY_SKIP_PATTERN = re.compile(
    r"^(?:Tạm\s+thời\s+bỏ\s+qua|Skip\s+for\s+now|Not\s+now)$",
    re.IGNORECASE,
)

OTP_PATTERNS = (
    re.compile(
        r"mã\s+canva\s+của\s+bạn\s+là\D{0,30}(\d{6})",
        re.IGNORECASE,
    ),
    re.compile(
        r"mã\s+(?:xác\s+minh|đăng\s+nhập)\D{0,30}(\d{6})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:your\s+canva\s+code|canva\s+code)\D{0,30}(\d{6})",
        re.IGNORECASE,
    ),
    re.compile(r"verification\s+code\D{0,30}(\d{6})", re.IGNORECASE),
    re.compile(
        r"one[- ]time\s+(?:code|password)\D{0,30}(\d{6})",
        re.IGNORECASE,
    ),
)

OTP_REJECTED_PATTERN = re.compile(
    r"(?:mã[^.]{0,80}(?:hết\s*hạn|không\s*đúng)|"
    r"(?:code|verification code)[^.]{0,80}(?:expired|incorrect|invalid))",
    re.IGNORECASE,
)
OTP_INPUT_NAME_PATTERN = re.compile(
    r"^(?:Mã|Mã\s+(?:xác\s+minh|đăng\s+nhập)|Code|Verification\s+code)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FlowData:
    email: str
    otp: str | None
    refresh_token: str | None
    client_id: str | None
    mail_api_url: str
    mail_request_timeout: int
    otp_initial_delay: float
    otp_interval: float
    otp_timeout: float
    pdf_path: Path
    full_name: str
    school: str
    school_address: str
    school_website: str
    country: str
    school_level: str
    teacher_role: str
    grade: str
    subject: str
    document_type: str
    cdp_url: str
    timeout_ms: int
    submit: bool
    screenshot_dir: Path


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def first_visible(locator: Locator) -> Locator | None:
    """Trả về phần tử đang hiển thị đầu tiên trong một nhóm locator."""
    try:
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
    except Exception:
        return None
    return None


def last_visible(locator: Locator) -> Locator | None:
    """Trả về phần tử hiển thị cuối cùng, thường là bản render mới nhất."""
    try:
        for index in range(locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
    except Exception:
        return None
    return None


def validate_pdf(pdf_path: Path) -> Path:
    resolved = pdf_path.expanduser().resolve()

    if not resolved.is_file():
        raise FileNotFoundError(f"Không tìm thấy PDF: {resolved}")

    if resolved.suffix.lower() != ".pdf":
        raise ValueError(f"File không có đuôi .pdf: {resolved}")

    if resolved.stat().st_size == 0:
        raise ValueError(f"PDF rỗng: {resolved}")

    with resolved.open("rb") as pdf_file:
        if pdf_file.read(5) != b"%PDF-":
            raise ValueError(f"File không có header PDF hợp lệ: {resolved}")

    return resolved


def read_otp(configured_otp: str | None) -> str:
    otp = configured_otp or input("Nhập OTP Canva gồm 6 chữ số: ").strip()

    if not re.fullmatch(r"\d{6}", otp):
        raise ValueError("OTP phải gồm đúng 6 chữ số")

    return otp


def mail_api_is_configured(data: FlowData) -> bool:
    return bool(data.refresh_token and data.client_id)


def fetch_mail_messages(data: FlowData) -> list[dict[str, Any]]:
    if not data.refresh_token or not data.client_id:
        raise RuntimeError("Thiếu refresh_token hoặc client_id")

    payload = json.dumps(
        {
            "email": data.email,
            "refresh_token": data.refresh_token,
            "client_id": data.client_id,
            "list_mail": "all",
        }
    ).encode("utf-8")

    request = Request(
        data.mail_api_url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "canva-education-flow/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=data.mail_request_timeout) as response:
            raw_response = response.read()
    except HTTPError as error:
        raise RuntimeError(f"Mail API trả HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Không kết nối được Mail API: {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError("Mail API quá thời gian phản hồi") from error

    try:
        response_data = json.loads(raw_response.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Mail API không trả về JSON hợp lệ") from error

    if not isinstance(response_data, dict):
        raise RuntimeError("Mail API trả về kiểu dữ liệu không hợp lệ")

    if response_data.get("status") is False:
        message = str(response_data.get("message") or "Không rõ nguyên nhân")
        raise RuntimeError(f"Mail API báo lỗi: {message}")

    messages = response_data.get("messages")
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise RuntimeError("Trường messages của Mail API không phải danh sách")

    return [message for message in messages if isinstance(message, dict)]


def get_message_uid(message: dict[str, Any]) -> str:
    value = message.get("uid") or message.get("id") or ""
    return str(value).strip()


def get_message_sender(message: dict[str, Any]) -> str:
    sender = message.get("from")

    if isinstance(sender, list):
        parts: list[str] = []
        for item in sender:
            if isinstance(item, dict):
                parts.extend(
                    str(item.get(key) or "") for key in ("name", "address")
                )
            elif item:
                parts.append(str(item))
        return " ".join(part for part in parts if part).strip()

    if isinstance(sender, dict):
        return " ".join(
            str(sender.get(key) or "") for key in ("name", "address")
        ).strip()

    return str(sender or "").strip()


def parse_message_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for date_format in ("%H:%M - %d/%m/%Y", "%H:%M %d/%m/%Y"):
        try:
            return datetime.strptime(text, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def normalize_mail_content(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_canva_otp(message: dict[str, Any]) -> str | None:
    sender = get_message_sender(message)
    subject = normalize_mail_content(message.get("subject"))
    body = normalize_mail_content(
        message.get("message") or message.get("content") or ""
    )
    searchable = f"{sender} {subject} {body}"

    # Chỉ phân tích mã sau khi thư đã được nhận diện là liên quan đến Canva.
    if "canva" not in searchable.casefold():
        return None

    api_code = str(message.get("code") or "").strip()
    if re.fullmatch(r"\d{6}", api_code):
        return api_code

    for pattern in OTP_PATTERNS:
        match = pattern.search(searchable)
        if match:
            return match.group(1)

    candidates = set(re.findall(r"\b\d{6}\b", searchable))
    if len(candidates) == 1:
        return candidates.pop()

    return None


def find_new_canva_otp(
    messages: list[dict[str, Any]],
    baseline_uids: set[str],
    requested_at: datetime,
) -> str | None:
    dated_messages = sorted(
        messages,
        key=lambda message: parse_message_datetime(message.get("date"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    earliest_allowed = requested_at - timedelta(seconds=10)

    for message in dated_messages:
        uid = get_message_uid(message)
        message_date = parse_message_datetime(message.get("date"))

        if uid and uid in baseline_uids:
            continue
        if message_date and message_date < earliest_allowed:
            continue
        if not uid and message_date is None:
            continue

        code = extract_canva_otp(message)
        if code:
            return code

    return None


def wait_for_canva_otp(
    data: FlowData,
    baseline_uids: set[str],
    requested_at: datetime,
) -> str:
    deadline = time.monotonic() + data.otp_timeout

    if data.otp_initial_delay > 0:
        log(f"Chờ {data.otp_initial_delay:g} giây trước khi đọc mailbox")
        time.sleep(min(data.otp_initial_delay, data.otp_timeout))

    attempt = 0
    consecutive_errors = 0

    while time.monotonic() < deadline:
        attempt += 1
        log(f"Kiểm tra OTP qua Mail API, lần {attempt}")

        try:
            messages = fetch_mail_messages(data)
            consecutive_errors = 0
        except RuntimeError as error:
            consecutive_errors += 1
            log(f"Mail API tạm lỗi ({consecutive_errors}/3): {error}")
            if consecutive_errors >= 3:
                raise
        else:
            code = find_new_canva_otp(messages, baseline_uids, requested_at)
            if code:
                log("Đã nhận OTP Canva mới từ Mail API")
                return code

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(data.otp_interval, remaining))

    raise TimeoutError(f"Không tìm thấy OTP Canva mới sau {data.otp_timeout:g} giây")


def open_tool_page(context: BrowserContext, timeout_ms: int) -> Page:
    # Luôn tạo một tab riêng và giữ đúng Page này trong toàn bộ quy trình.
    # Các tab Chrome khác trong profile sẽ không bị chọn nhầm.
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    page.set_default_navigation_timeout(max(timeout_ms, 60_000))
    page.goto(CANVA_URL, wait_until="domcontentloaded")
    page.bring_to_front()
    return page


def click_button(
    page: Page,
    name: str | re.Pattern[str],
    *,
    exact: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> None:
    button_candidates = page.get_by_role("button", name=name, exact=exact)
    deadline = time.monotonic() + timeout_ms / 1000
    button: Locator | None = None
    while time.monotonic() < deadline:
        button = first_visible(button_candidates)
        if button is not None:
            break
        page.wait_for_timeout(200)

    if button is None:
        raise TimeoutError(f"Không tìm thấy nút đang hiển thị: {name}")

    expect(button).to_be_enabled(timeout=timeout_ms)
    button.click()


def find_create_account_heading(page: Page) -> Locator | None:
    heading = first_visible(page.get_by_role(
        "heading",
        name="Tạo tài khoản",
        exact=True,
    ))
    if heading is not None:
        return heading

    # Dự phòng khi Canva hiển thị cùng nội dung bằng div thay vì heading.
    return first_visible(page.get_by_text("Tạo tài khoản", exact=True))


def find_create_account_name_input(page: Page) -> Locator | None:
    named_input = first_visible(page.get_by_role(
        "textbox",
        name=CREATE_ACCOUNT_NAME_PATTERN,
    ))
    if named_input is not None:
        return named_input

    # Một số biến thể giao diện không gắn label vào input. Khi tiêu đề tạo
    # tài khoản đang hiện, ô textbox đang hiển thị chính là ô họ và tên.
    if find_create_account_heading(page) is not None:
        return first_visible(page.get_by_role("textbox"))
    return None


def find_education_entry(page: Page) -> Locator | None:
    """Tìm đúng thẻ Giáo dục trong hộp onboarding, tránh menu trang nền."""
    onboarding_form = first_visible(page.get_by_role(
        "form",
        name=EDUCATION_ONBOARDING_FORM_NAME,
        exact=True,
    ))
    if onboarding_form is not None:
        education_entry = first_visible(onboarding_form.get_by_role(
            "button",
            name=EDUCATION_ENTRY_PATTERN,
        ))
        if education_entry is not None:
            return education_entry

    onboarding_heading = first_visible(page.get_by_role(
        "heading",
        name="Hãy xây dựng Canva dành riêng cho bạn",
        exact=True,
    ))
    if onboarding_heading is not None:
        return first_visible(page.get_by_role(
            "button",
            name=EDUCATION_ENTRY_PATTERN,
        ))
    return None


def find_authenticated_indicator(page: Page) -> Locator | None:
    education_entry = find_education_entry(page)
    if education_entry is not None:
        return education_entry

    # Tài khoản đã hoàn tất onboarding sẽ đi thẳng vào trang chính. Nút menu
    # tài khoản này chỉ xuất hiện khi phiên Canva đã đăng nhập.
    return first_visible(page.get_by_role(
        "button",
        name=re.compile(
            r"^Các tùy chọn khác về tài khoản(?: và đội)?$",
            re.IGNORECASE,
        ),
    ))


def find_passkey_skip_action(page: Page) -> Locator | None:
    """Tìm nút bỏ qua lời mời tạo mã khóa sau khi đăng nhập."""
    action = first_visible(page.get_by_role(
        "button",
        name=PASSKEY_SKIP_PATTERN,
    ))
    if action is not None:
        return action

    # Dự phòng khi Canva render CTA bằng phần tử không có role button.
    return first_visible(page.get_by_text(PASSKEY_SKIP_PATTERN))


def dismiss_passkey_prompt_if_present(
    page: Page,
    timeout_ms: int = 5_000,
) -> bool:
    """Bỏ qua passkey nếu đang hiện; trả False nếu không có form này."""
    skip_action = find_passkey_skip_action(page)
    if skip_action is None:
        return False

    log("Canva mời thiết lập mã khóa; nhấn Tạm thời bỏ qua")
    try:
        skip_action.click(timeout=5_000, no_wait_after=True)
    except Exception as error:
        raise RuntimeError(
            "Không thể nhấn Tạm thời bỏ qua trên form mã khóa"
        ) from error

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if find_passkey_skip_action(page) is None:
            log("Đã bỏ qua bước thiết lập mã khóa")
            return True
        page.wait_for_timeout(200)

    raise TimeoutError(
        "Đã nhấn Tạm thời bỏ qua nhưng form mã khóa chưa đóng"
    )


def wait_for_authenticated(page: Page, timeout_ms: int) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        indicator = find_authenticated_indicator(page)
        if indicator is not None:
            return indicator
        page.wait_for_timeout(200)

    raise TimeoutError(
        "Không xác nhận được trạng thái đăng nhập Canva "
        f"sau {timeout_ms / 1000:g} giây"
    )


def find_teacher_verification_action(page: Page) -> Locator | None:
    for role in ("button", "link"):
        action = first_visible(page.get_by_role(
            role,
            name="Yêu cầu xác minh",
            exact=True,
        ))
        if action is not None:
            return action

    # Dự phòng nếu CTA không được Canva gắn role button/link.
    return first_visible(page.get_by_text("Yêu cầu xác minh", exact=True))


def wait_for_teacher_verification_action(
    page: Page,
    timeout_ms: int,
) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        action = find_teacher_verification_action(page)
        if action is not None:
            return action
        page.wait_for_timeout(200)

    raise TimeoutError(
        "Không tìm thấy nút Yêu cầu xác minh của gói Giáo viên "
        f"sau {timeout_ms / 1000:g} giây"
    )


def find_otp_input(page: Page) -> Locator | None:
    # Canva có thể dùng nhãn tiếng Việt "Mã" hoặc tiếng Anh "Code" ngay
    # cả khi toàn trang đang ở tiếng Việt. Thuộc tính one-time-code là dấu
    # hiệu ổn định nhất và không phụ thuộc ngôn ngữ giao diện.
    candidates = (
        page.locator('input[autocomplete="one-time-code"]'),
        page.get_by_role("textbox", name=OTP_INPUT_NAME_PATTERN),
        page.locator('input[inputmode="numeric"][maxlength="6"]'),
    )
    for candidate_group in candidates:
        candidate = last_visible(candidate_group)
        if candidate is not None:
            return candidate
    return None


def otp_rejected_is_visible(page: Page) -> bool:
    return first_visible(page.get_by_text(OTP_REJECTED_PATTERN)) is not None


def find_resend_otp_action(page: Page) -> Locator | None:
    for role in ("link", "button"):
        action = last_visible(page.get_by_role(
            role,
            name="Gửi lại mã",
            exact=True,
        ))
        if action is not None:
            try:
                if action.is_enabled():
                    return action
            except Exception:
                continue

    return last_visible(page.get_by_text("Gửi lại mã", exact=True))


def wait_for_resend_otp_action(page: Page, timeout_ms: int) -> Locator:
    log("Chờ hết thời gian đếm ngược để gửi lại OTP")
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        action = find_resend_otp_action(page)
        if action is not None:
            return action
        page.wait_for_timeout(500)

    raise TimeoutError(
        "Không thấy liên kết Gửi lại mã sau khi hết thời gian đếm ngược"
    )


def wait_for_otp_form_reset(page: Page, timeout_ms: int = 5_000) -> bool:
    """Chờ cảnh báo của mã cũ biến mất sau khi nhấn Gửi lại mã."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        otp_input = find_otp_input(page)
        if otp_input is not None and not otp_rejected_is_visible(page):
            return True
        page.wait_for_timeout(200)
    return False


def submit_otp(page: Page, otp: str, timeout_ms: int) -> bool:
    """Nhập OTP và trả về True nếu cảnh báo cũ đã được xóa trước khi gửi."""
    deadline = time.monotonic() + timeout_ms / 1000
    otp_input: Locator | None = None
    while time.monotonic() < deadline:
        otp_input = find_otp_input(page)
        if otp_input is not None:
            break
        page.wait_for_timeout(200)

    if otp_input is None:
        raise TimeoutError("Không tìm thấy ô nhập OTP")

    had_old_rejection = otp_rejected_is_visible(page)
    otp_input.fill("")

    if had_old_rejection:
        clear_deadline = time.monotonic() + 2
        while time.monotonic() < clear_deadline:
            if not otp_rejected_is_visible(page):
                break
            page.wait_for_timeout(100)

    old_rejection_cleared = not otp_rejected_is_visible(page)
    otp_input.fill(otp)

    # Canva có thể tự gửi khi đủ 6 số. Chỉ click nếu nút vẫn còn hiển thị
    # và khả dụng; không click nút có nhãn "Đang tải".
    otp_continue = first_visible(page.get_by_role(
        "button",
        name=re.compile(r"^(Tiếp tục|Đăng nhập)$", re.IGNORECASE),
    ))
    if otp_continue is not None and otp_continue.is_enabled():
        otp_continue.click()

    return old_rejection_cleared


def wait_for_otp_outcome(
    page: Page,
    timeout_ms: int,
    *,
    old_rejection_cleared: bool,
) -> str:
    """Trả về authenticated hoặc rejected sau khi gửi một OTP."""
    deadline = time.monotonic() + timeout_ms / 1000
    # Nếu cảnh báo cũ vẫn nằm trên DOM, không được coi nó ngay lập tức là
    # kết quả của mã mới. Cho lần gửi mới đủ thời gian phản hồi trước.
    reject_grace_seconds = 1.5 if old_rejection_cleared else 8
    reject_grace_until = time.monotonic() + reject_grace_seconds

    while time.monotonic() < deadline:
        # Sau OTP, một số tài khoản được Canva mời tạo passkey trước khi
        # trang chính xuất hiện. Đây là trạng thái đăng nhập hợp lệ, không
        # phải OTP sai; bỏ qua rồi tiếp tục chờ chỉ báo authenticated.
        if dismiss_passkey_prompt_if_present(page):
            reject_grace_until = time.monotonic() + 1.5
            continue
        if find_authenticated_indicator(page) is not None:
            return "authenticated"
        if (
            time.monotonic() >= reject_grace_until
            and otp_rejected_is_visible(page)
        ):
            return "rejected"
        page.wait_for_timeout(200)

    raise TimeoutError(
        "Canva không phản hồi kết quả đăng nhập sau khi nhập OTP"
    )


def wait_for_login_state(page: Page, timeout_ms: int) -> str:
    """Chờ Canva hiện OTP, bước xác nhận tên, hoặc đã đăng nhập."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if dismiss_passkey_prompt_if_present(page):
            continue
        if find_otp_input(page) is not None:
            return "otp"
        if (
            find_create_account_heading(page) is not None
            and find_create_account_name_input(page) is not None
        ):
            return "create_account"
        if find_authenticated_indicator(page) is not None:
            return "authenticated"
        page.wait_for_timeout(200)

    raise TimeoutError(
        "Canva không hiện ô OTP, form xác nhận tên hoặc màn hình sau đăng nhập "
        f"sau {timeout_ms / 1000:g} giây"
    )


def login_with_email(page: Page, data: FlowData) -> None:
    log("Mở màn hình đăng nhập Canva")

    login_link = page.get_by_role("link", name="Đăng nhập").first
    expect(login_link).to_be_visible(timeout=data.timeout_ms)
    login_link.click()

    click_button(
        page,
        "Tiếp tục với email",
        exact=True,
        timeout_ms=data.timeout_ms,
    )

    email_input = page.get_by_role(
        "textbox",
        name="Email (cá nhân hoặc công việc)",
    )
    expect(email_input).to_be_visible(timeout=data.timeout_ms)
    email_input.fill(data.email)

    baseline_uids: set[str] = set()
    mail_api_available = mail_api_is_configured(data)
    use_mail_api = data.otp is None and mail_api_available

    if use_mail_api:
        log("Đọc danh sách thư hiện tại để loại OTP cũ")
        baseline_messages = fetch_mail_messages(data)
        baseline_uids = {
            uid
            for message in baseline_messages
            if (uid := get_message_uid(message))
        }
        log(f"Đã ghi nhận {len(baseline_uids)} UID thư hiện có")

    # Ghi thời điểm ngay trước thao tác làm Canva gửi OTP.
    otp_requested_at = datetime.now(timezone.utc)
    click_button(
        page,
        "Tiếp tục",
        exact=True,
        timeout_ms=data.timeout_ms,
    )

    login_state = wait_for_login_state(page, data.timeout_ms)

    if login_state == "create_account":
        log("Canva yêu cầu xác nhận tên cho tài khoản mới")
        create_account_heading = find_create_account_heading(page)
        generated_name_input = find_create_account_name_input(page)
        if create_account_heading is None:
            raise RuntimeError("Không còn thấy màn hình Tạo tài khoản")
        if generated_name_input is None:
            raise RuntimeError("Không tìm thấy ô Họ và tên trên form tạo tài khoản")

        # Canva đã tự tạo tên. Không sửa nội dung ô này, chỉ xác nhận tiếp tục.
        generated_name = generated_name_input.input_value().strip()
        if not generated_name:
            raise RuntimeError(
                "Canva hiện bước tạo tài khoản nhưng chưa tự điền tên"
            )

        create_account_continue = first_visible(
            page.get_by_role(
                "button",
                name="Tiếp tục",
                exact=True,
            )
        )
        if create_account_continue is None:
            raise RuntimeError("Không tìm thấy nút Tiếp tục trên form tạo tài khoản")
        expect(create_account_continue).to_be_enabled(timeout=data.timeout_ms)

        # Với tài khoản mới, chính nút này mới là thao tác có thể gửi OTP.
        otp_requested_at = datetime.now(timezone.utc)
        log("Giữ nguyên tên ngẫu nhiên do Canva tạo và nhấn Tiếp tục")
        create_account_continue.click()
        expect(create_account_heading).not_to_be_visible(timeout=data.timeout_ms)
        login_state = wait_for_login_state(page, data.timeout_ms)

    if login_state == "authenticated":
        log("Đăng nhập thành công; Canva không yêu cầu OTP")
        return

    if data.otp is not None:
        otp = read_otp(data.otp)
    elif use_mail_api:
        otp = wait_for_canva_otp(data, baseline_uids, otp_requested_at)
    else:
        log("Chưa cấu hình Mail API; chuyển sang nhập OTP thủ công")
        otp = read_otp(None)

    for otp_attempt in range(MAX_OTP_RESENDS + 1):
        log(f"Nhập OTP, lần {otp_attempt + 1}")
        old_rejection_cleared = submit_otp(page, otp, data.timeout_ms)
        outcome = wait_for_otp_outcome(
            page,
            OTP_RESULT_TIMEOUT_MS,
            old_rejection_cleared=old_rejection_cleared,
        )

        if outcome == "authenticated":
            log("Đăng nhập thành công")
            return

        if otp_attempt >= MAX_OTP_RESENDS:
            raise RuntimeError(
                f"OTP vẫn hết hạn hoặc không đúng sau {MAX_OTP_RESENDS} lần gửi lại"
            )

        log(
            "Canva báo OTP hết hạn hoặc không đúng; "
            f"chuẩn bị gửi lại lần {otp_attempt + 1}/{MAX_OTP_RESENDS}"
        )
        resend_action = wait_for_resend_otp_action(
            page,
            OTP_RESEND_WAIT_MS,
        )

        resend_baseline_uids: set[str] = set()
        if mail_api_available:
            log("Ghi nhận mailbox hiện tại để không lấy lại OTP cũ")
            current_messages = fetch_mail_messages(data)
            resend_baseline_uids = {
                uid
                for message in current_messages
                if (uid := get_message_uid(message))
            }

        # Tìm lại action sau lúc gọi Mail API phòng trường hợp DOM vừa render.
        latest_resend_action = find_resend_otp_action(page)
        if latest_resend_action is not None:
            resend_action = latest_resend_action
        otp_requested_at = datetime.now(timezone.utc)
        resend_action.click()
        log("Đã nhấn Gửi lại mã")

        if wait_for_otp_form_reset(page):
            log("Form OTP mới đã sẵn sàng")
        else:
            log(
                "Cảnh báo OTP cũ chưa biến mất; sẽ bỏ qua cảnh báo cũ "
                "trong lúc chờ kết quả mã mới"
            )

        if mail_api_available:
            otp = wait_for_canva_otp(
                data,
                resend_baseline_uids,
                otp_requested_at,
            )
        else:
            log("OTP mới đã được gửi; nhập mã mới trong CMD")
            otp = read_otp(None)

    raise RuntimeError("Luồng OTP kết thúc ngoài dự kiến")


def find_teacher_location_dialog(page: Page) -> Locator | None:
    """Lấy bản dialog hỏi quốc gia được render mới nhất và đang hiển thị."""
    dialogs = page.get_by_role("dialog")
    try:
        for index in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            if not dialog.is_visible():
                continue
            heading = dialog.get_by_role(
                "heading",
                name=TEACHER_LOCATION_HEADING_PATTERN,
            )
            if first_visible(heading) is not None:
                return dialog
    except Exception:
        return None
    return None


def document_step_is_visible(page: Page) -> bool:
    heading = page.get_by_role(
        "heading",
        name=DOCUMENT_STEP_HEADING_PATTERN,
    )
    if first_visible(heading) is not None:
        return True

    document_combobox = page.get_by_role(
        "combobox",
        name=re.compile(
            r"^(?:Chọn tài liệu|Chưa chọn tùy chọn nào)$",
            re.IGNORECASE,
        ),
    )
    return first_visible(document_combobox) is not None


def checkbox_is_checked(checkbox: Locator) -> bool:
    try:
        return bool(checkbox.evaluate(
            """
            element => {
                if ('checked' in element) return Boolean(element.checked);
                return element.getAttribute('aria-checked') === 'true';
            }
            """
        ))
    except Exception:
        return False


def find_no_school_email_checkbox(dialog: Locator) -> Locator | None:
    candidates = dialog.get_by_role(
        "checkbox",
        name="Tôi không có email trường học",
    )
    try:
        return candidates.last if candidates.count() > 0 else None
    except Exception:
        return None


def check_checkbox_via_dom(page: Page, checkbox: Locator) -> bool:
    """Tích checkbox bằng DOM, không phụ thuộc tọa độ đang bị che."""
    try:
        # Luôn cuộn vào giữa. scroll_into_view_if_needed không cuộn nếu input
        # chỉ lộ một phần phía sau footer cố định.
        checkbox.evaluate(
            "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
        )
        page.wait_for_timeout(300)

        if checkbox_is_checked(checkbox):
            return True

        # Click label/input bằng DOM, không sử dụng tọa độ màn hình nên footer
        # hoặc cửa sổ CMD phủ lên Chrome cũng không thể chặn thao tác này.
        checkbox.evaluate(
            """
            element => {
                const wrappingLabel = element.closest('label');
                const externalLabel = element.id
                    ? Array.from(document.querySelectorAll('label')).find(
                        label => label.htmlFor === element.id
                    )
                    : null;
                (wrappingLabel || externalLabel || element).click();
            }
            """
        )
        page.wait_for_timeout(300)
        if checkbox_is_checked(checkbox):
            return True

        # Nếu đây là input thật nhưng Canva dùng controlled component, gọi
        # native setter rồi phát input/change để React cập nhật state.
        checkbox.evaluate(
            """
            element => {
                if (!('checked' in element)) return;
                const prototype = Object.getPrototypeOf(element);
                const descriptor = Object.getOwnPropertyDescriptor(
                    prototype,
                    'checked'
                );
                if (descriptor && descriptor.set) {
                    descriptor.set.call(element, true);
                } else {
                    element.checked = true;
                }
                element.dispatchEvent(new Event('input', {bubbles: true}));
                element.dispatchEvent(new Event('change', {bubbles: true}));
            }
            """
        )
        page.wait_for_timeout(300)
        if checkbox_is_checked(checkbox):
            return True

        # Giữ check() làm phương án cuối nếu cấu trúc checkbox lại thay đổi.
        try:
            checkbox.check(timeout=3_000)
        except PlaywrightTimeoutError:
            return False
        page.wait_for_timeout(200)
        return checkbox_is_checked(checkbox)
    except Exception as error:
        # Canva có thể thay DOM đúng lúc đang thao tác. Vòng ngoài sẽ tìm lại
        # checkbox thuộc bản render mới nhất rồi thử lại.
        log(
            "Thao tác checkbox chưa thành công: "
            f"{type(error).__name__}: {error}"
        )
        return False


def confirm_no_school_email_and_continue(page: Page, timeout_ms: int) -> None:
    """Tích checkbox và qua bước tài liệu, tự xử lý khi Canva render lại."""
    deadline = time.monotonic() + timeout_ms / 1000
    attempt = 0

    while time.monotonic() < deadline:
        if document_step_is_visible(page):
            log("Đã chuyển sang bước tài liệu")
            return

        dialog = find_teacher_location_dialog(page)
        if dialog is None:
            page.wait_for_timeout(200)
            continue

        checkbox = find_no_school_email_checkbox(dialog)
        continue_button = last_visible(dialog.get_by_role(
            "button",
            name="Tiếp tục",
            exact=True,
        ))
        if checkbox is None or continue_button is None:
            page.wait_for_timeout(200)
            continue

        attempt += 1
        log(f"Cuộn tới và tích checkbox, lần {attempt}")
        if not check_checkbox_via_dom(page, checkbox):
            page.wait_for_timeout(200)
            continue

        # Chờ ngắn để phát hiện Canva render lại form và xóa dấu tích.
        stable_until = min(deadline, time.monotonic() + 1.2)
        stable = True
        while time.monotonic() < stable_until:
            current_dialog = find_teacher_location_dialog(page)
            if current_dialog is None:
                stable = False
                break
            current_checkbox = find_no_school_email_checkbox(current_dialog)
            if (
                current_checkbox is None
                or not checkbox_is_checked(current_checkbox)
            ):
                stable = False
                break
            page.wait_for_timeout(150)

        if not stable:
            log("Canva vừa cập nhật form; tích lại checkbox")
            continue

        dialog = find_teacher_location_dialog(page)
        if dialog is None:
            continue
        continue_button = last_visible(dialog.get_by_role(
            "button",
            name="Tiếp tục",
            exact=True,
        ))
        if continue_button is None:
            continue

        log("Đã tích Tôi không có email trường học")
        expect(continue_button).to_be_enabled(timeout=timeout_ms)
        log("Nhấn Tiếp tục sang bước tài liệu")
        continue_button.click()

        transition_deadline = min(deadline, time.monotonic() + 6)
        while time.monotonic() < transition_deadline:
            if document_step_is_visible(page):
                log("Đã chuyển sang bước tài liệu")
                return

            current_dialog = find_teacher_location_dialog(page)
            if current_dialog is not None:
                current_checkbox = find_no_school_email_checkbox(current_dialog)
                if (
                    current_checkbox is not None
                    and not checkbox_is_checked(current_checkbox)
                ):
                    break
            page.wait_for_timeout(200)

        log(f"Chưa sang bước tài liệu sau lần {attempt}; thử lại")

    raise TimeoutError(
        "Không thể tích checkbox và chuyển sang bước tài liệu"
    )


def choose_country(page: Page, country: str, timeout_ms: int) -> None:
    change_country_candidates = page.get_by_role(
        "button",
        name="Thay đổi quốc gia",
        exact=True,
    )
    country_combobox_candidates = page.get_by_role(
        "combobox",
        name="Quốc gia",
    )

    # Sau khi nhấn "Yêu cầu xác minh", modal xuất hiện bất đồng bộ. Phải
    # chờ nút bút chì thay vì chỉ kiểm tra một lần ngay lập tức.
    deadline = time.monotonic() + timeout_ms / 1000
    country_combobox: Locator | None = None
    while time.monotonic() < deadline:
        change_country = first_visible(change_country_candidates)
        if change_country is not None:
            log("Mở danh sách thay đổi quốc gia")
            change_country.click()
            break

        country_combobox = first_visible(country_combobox_candidates)
        if country_combobox is not None:
            break

        page.wait_for_timeout(200)
    else:
        raise TimeoutError(
            "Không tìm thấy nút Thay đổi quốc gia trong form xác minh"
        )

    if country_combobox is None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            country_combobox = first_visible(country_combobox_candidates)
            if country_combobox is not None:
                break
            page.wait_for_timeout(200)

    if country_combobox is None:
        raise TimeoutError("Không thấy danh sách Quốc gia sau khi nhấn nút sửa")

    country_combobox.click()

    searchbox = page.get_by_role("searchbox", name="Tùy chọn tìm kiếm")
    expect(searchbox).to_be_visible(timeout=timeout_ms)
    searchbox.fill(country)

    country_option = page.get_by_role(
        "option",
        name=country,
        exact=True,
    )
    expect(country_option).to_be_visible(timeout=timeout_ms)
    country_option.click()
    log(f"Đã chọn quốc gia: {country}")

    # Canva thay toàn bộ form khoảng một giây sau khi đổi quốc gia. Chờ lần
    # render này kết thúc rồi mới tích checkbox để trạng thái không bị mất.
    log("Chờ form ổn định sau khi đổi quốc gia")
    page.wait_for_timeout(2_000)


def complete_eligibility_steps(page: Page, data: FlowData) -> None:
    log("Chờ 5 giây sau khi đăng nhập thành công")
    page.wait_for_timeout(POST_LOGIN_DELAY_MS)

    # Kiểm tra thêm lần nữa vì đôi khi Canva hiển thị lời mời passkey trễ,
    # sau khi chỉ báo đăng nhập đã xuất hiện trên trang nền.
    dismiss_passkey_prompt_if_present(page)

    log("Mở thẳng trang bảng giá Canva Giáo dục")
    page.goto(
        CANVA_EDUCATION_PRICING_URL,
        wait_until="domcontentloaded",
    )

    verification_action = wait_for_teacher_verification_action(
        page,
        data.timeout_ms,
    )
    expect(verification_action).to_be_enabled(timeout=data.timeout_ms)
    log("Nhấn Yêu cầu xác minh của gói Giáo viên")
    verification_action.click()

    choose_country(page, data.country, data.timeout_ms)
    confirm_no_school_email_and_continue(page, data.timeout_ms)


def choose_document_type(page: Page, document_type: str, timeout_ms: int) -> None:
    document_name_pattern = re.compile(
        rf"^{re.escape(document_type)}(?:\s|$)",
        re.IGNORECASE,
    )
    unselected_pattern = re.compile(
        r"^(?:Chọn tài liệu|Chưa chọn tùy chọn nào)$",
        re.IGNORECASE,
    )
    unselected_candidates = page.get_by_role(
        "combobox",
        name=unselected_pattern,
    )
    selected_candidates = page.get_by_role(
        "combobox",
        name=document_name_pattern,
    )

    # Bước tài liệu tải bất đồng bộ sau nút Tiếp tục của form quốc gia.
    deadline = time.monotonic() + timeout_ms / 1000
    document_combobox: Locator | None = None
    already_selected = False
    while time.monotonic() < deadline:
        selected_combobox = first_visible(selected_candidates)
        if selected_combobox is not None:
            document_combobox = selected_combobox
            already_selected = True
            break

        document_combobox = first_visible(unselected_candidates)
        if document_combobox is not None:
            break

        # Dự phòng cho biến thể UI dùng label khác: chỉ nhận combobox khi
        # tiêu đề bước xác minh tài liệu đã xuất hiện.
        document_heading = first_visible(page.get_by_role(
            "heading",
            name=re.compile(r"Xác minh thông tin", re.IGNORECASE),
        ))
        if document_heading is not None:
            document_combobox = first_visible(page.get_by_role("combobox"))
            if document_combobox is not None:
                break

        page.wait_for_timeout(200)

    if document_combobox is None:
        raise TimeoutError("Không tìm thấy ô Chọn tài liệu")

    if not already_selected:
        log("Mở danh sách loại tài liệu")
        document_combobox.click()

        option_candidates = page.get_by_role(
            "option",
            name=document_name_pattern,
        )
        deadline = time.monotonic() + timeout_ms / 1000
        document_option: Locator | None = None
        while time.monotonic() < deadline:
            document_option = first_visible(option_candidates)
            if document_option is None:
                # Badge "Đề xuất" có thể làm thay đổi accessible name của
                # option; text con "Bảng lương" vẫn ổn định.
                document_option = first_visible(
                    page.get_by_text(document_type, exact=True)
                )
            if document_option is not None:
                break
            page.wait_for_timeout(200)

        if document_option is None:
            raise TimeoutError(
                f"Không tìm thấy loại tài liệu: {document_type}"
            )
        document_option.click()

    page.wait_for_timeout(300)
    log(f"Đã chọn loại tài liệu: {document_type}")


def find_document_upload_dialog(page: Page) -> Locator | None:
    dialogs = page.get_by_role("dialog")
    try:
        for index in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            if not dialog.is_visible():
                continue
            if dialog.locator('input[type="file"]').count() > 0:
                return dialog
            heading = dialog.get_by_role(
                "heading",
                name=DOCUMENT_STEP_HEADING_PATTERN,
            )
            if first_visible(heading) is not None:
                return dialog
            if (
                first_visible(dialog.get_by_text("Tài liệu 1", exact=True))
                is not None
            ):
                return dialog
    except Exception:
        return None
    return None


def find_identity_dialog(page: Page) -> Locator | None:
    dialogs = page.get_by_role("dialog")
    try:
        for index in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            if not dialog.is_visible():
                continue
            heading = dialog.get_by_role(
                "heading",
                name=IDENTITY_STEP_HEADING_PATTERN,
            )
            if first_visible(heading) is not None:
                return dialog
            if dialog.get_by_role("textbox", name="Tên đầy đủ").count() > 0:
                return dialog
    except Exception:
        return None
    return None


def wait_for_identity_dialog(page: Page, timeout_ms: int) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        dialog = find_identity_dialog(page)
        if dialog is not None:
            return dialog
        page.wait_for_timeout(200)
    raise TimeoutError("Không thấy form xác nhận họ tên và trường")


def identity_step_has_advanced(page: Page) -> bool:
    """Return True only after the identity form has really changed step."""
    done_button = first_visible(page.get_by_role(
        "button",
        name="Xong",
        exact=True,
    ))
    if done_button is not None:
        return True

    if find_identity_dialog(page) is not None:
        return False

    # React đôi khi tháo rồi gắn lại dialog trong một nhịp render. Đợi ngắn
    # và kiểm tra lần hai để không coi nhầm một lần re-render là đã submit.
    page.wait_for_timeout(400)
    return find_identity_dialog(page) is None


def click_final_identity_continue(page: Page, timeout_ms: int) -> None:
    """Click the final Continue even when its sticky footer is outside dialog."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_error: Exception | None = None
    final_continue: Locator | None = None

    while time.monotonic() < deadline:
        if identity_step_has_advanced(page):
            return

        # Nút nằm trong sticky footer ở một số bản Canva và footer đó có thể
        # là sibling của phần form. Vì vậy phải tìm trên toàn trang, không chỉ
        # giới hạn trong locator dialog chứa ô Tên đầy đủ/Tên trường.
        candidate = last_visible(page.get_by_role(
            "button",
            name="Tiếp tục",
            exact=True,
        ))
        if candidate is None:
            page.wait_for_timeout(200)
            continue

        try:
            if not candidate.is_enabled():
                page.wait_for_timeout(200)
                continue
        except Exception as exc:
            last_error = exc
            page.wait_for_timeout(200)
            continue

        final_continue = candidate
        break

    if final_continue is None:
        raise TimeoutError("Không tìm thấy nút Tiếp tục cuối đang hiển thị")

    # Chỉ chuyển sang phương án dự phòng khi phương án trước ném lỗi, tức là
    # chưa xác nhận phát được click. Khi một click đã phát thành công, tuyệt
    # đối không click lại; chỉ chờ trạng thái để tránh gửi hồ sơ trùng.
    click_dispatched = False
    for attempt in range(1, 4):
        try:
            try:
                final_continue.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                # Nút fixed/sticky vốn đã nằm trong viewport nên lỗi cuộn
                # không được phép ngăn bước click phía dưới.
                pass

            if attempt == 1:
                # Click chuẩn tạo đầy đủ chuỗi sự kiện mà React mong đợi.
                log("Nhấn Tiếp tục cuối sau khi chọn trường")
                final_continue.click(timeout=5_000, no_wait_after=True)
            elif attempt == 2:
                # Dự phòng khi lớp sticky/animation chặn hit target.
                log("Nút cuối bị che; thử click cưỡng bức")
                final_continue.click(
                    timeout=5_000,
                    force=True,
                    no_wait_after=True,
                )
            else:
                # Dự phòng cuối cho footer bị lớp giao diện khác che lên.
                log("Thử phát sự kiện click DOM cho nút Tiếp tục cuối")
                final_continue.dispatch_event("click")
        except Exception as exc:
            last_error = exc
            log(f"Lần click Tiếp tục cuối {attempt} chưa thành công: {exc}")
            continue

        click_dispatched = True
        break

    if not click_dispatched:
        detail = f": {last_error}" if last_error is not None else ""
        raise TimeoutError("Không thể phát click nút Tiếp tục cuối" + detail)

    log("Đã phát click Tiếp tục cuối; chờ Canva chuyển bước")
    while time.monotonic() < deadline:
        if identity_step_has_advanced(page):
            return
        page.wait_for_timeout(250)

    detail = f": {last_error}" if last_error is not None else ""
    raise TimeoutError(
        "Đã click Tiếp tục cuối nhưng Canva chưa xác nhận chuyển bước; "
        "tool không click lại để tránh gửi trùng"
        + detail
    )


def upload_pdf(page: Page, pdf_path: Path, timeout_ms: int) -> None:
    # Ưu tiên input file thật (thường bị Canva ẩn sau nút Chọn tệp). Việc
    # set_input_files không cần input hoặc nút phải nằm trong vùng nhìn thấy.
    deadline = time.monotonic() + timeout_ms / 1000
    file_input: Locator | None = None
    upload_scope: Locator | Page = page
    while time.monotonic() < deadline:
        document_dialog = find_document_upload_dialog(page)
        upload_scope = document_dialog if document_dialog is not None else page
        file_inputs = upload_scope.locator('input[type="file"]')
        if file_inputs.count() > 0:
            file_input = file_inputs.last
            break
        page.wait_for_timeout(200)

    if file_input is not None:
        log(f"Gắn PDF từ accounts.txt: {pdf_path}")
        file_input.set_input_files(str(pdf_path), timeout=timeout_ms)
    else:
        # Chỉ dùng file chooser khi UI chưa tạo input file trong DOM.
        choose_file_button = first_visible(upload_scope.get_by_role(
            "button",
            name=re.compile(r"^Chọn tệp(?:\s|$)", re.IGNORECASE),
        ))
        if choose_file_button is None:
            choose_file_button = first_visible(
                upload_scope.get_by_text("Chọn tệp", exact=True)
            )
        if choose_file_button is None:
            raise TimeoutError(
                "Không tìm thấy input file hoặc điều khiển Chọn tệp"
            )

        log(f"Mở hộp chọn và gắn PDF từ accounts.txt: {pdf_path}")
        with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
            choose_file_button.click()
        chooser_info.value.set_files(str(pdf_path))

    log(f"Đã chọn PDF: {pdf_path.name}")

    filename_candidates = page.get_by_text(
        re.compile(re.escape(pdf_path.name), re.IGNORECASE)
    )
    continue_button = page.get_by_role(
        "button",
        name="Tiếp tục",
        exact=True,
    ).last
    upload_error_candidates = page.get_by_text(
        re.compile(
            r"không thể tải|tải lên thất bại|upload failed|file không hợp lệ",
            re.IGNORECASE,
        )
    )

    deadline = time.monotonic() + (UPLOAD_TIMEOUT_MS / 1000)
    enabled_since: float | None = None

    while time.monotonic() < deadline:
        upload_error = first_visible(upload_error_candidates)
        if upload_error is not None:
            raise RuntimeError(f"Canva báo lỗi upload: {upload_error.inner_text()}")

        if first_visible(filename_candidates) is not None:
            log("Canva đã hiển thị đúng tên PDF; upload hoàn tất")
            return

        # Một số phiên bản không hiển thị nguyên tên file. Chỉ dùng nút
        # Tiếp tục như tín hiệu phụ nếu nó đã khả dụng; thông thường nút vẫn
        # bị khóa cho tới khi tích checkbox đồng ý ở bước kế tiếp.
        if is_visible(continue_button) and continue_button.is_enabled():
            if enabled_since is None:
                enabled_since = time.monotonic()
            elif time.monotonic() - enabled_since >= 3:
                log(
                    "Cảnh báo: UI không hiển thị đủ tên file, nhưng nút "
                    "Tiếp tục đã khả dụng ổn định"
                )
                return
        else:
            enabled_since = None

        page.wait_for_timeout(500)

    raise TimeoutError(f"Upload PDF quá {UPLOAD_TIMEOUT_MS // 1000} giây")


def find_school_address_control(identity_dialog: Locator) -> Locator | None:
    """Tìm ô địa chỉ trong biến thể form nhập trường thủ công."""
    for role in ("combobox", "textbox"):
        control = first_visible(identity_dialog.get_by_role(
            role,
            name="Địa chỉ trường",
            exact=True,
        ))
        if control is not None:
            return control

    return first_visible(identity_dialog.get_by_placeholder(
        re.compile(r"Nhập\s+và\s+chọn\s+địa\s+chỉ", re.IGNORECASE)
    ))


def fill_manual_school_details(page: Page, data: FlowData) -> None:
    """Điền địa chỉ và website khi Canva không nhận diện được tên trường."""
    identity_dialog = wait_for_identity_dialog(page, data.timeout_ms)

    manual_school_name = first_visible(identity_dialog.get_by_role(
        "textbox",
        name="Tên trường",
        exact=True,
    ))
    if manual_school_name is None:
        manual_school_name = first_visible(identity_dialog.get_by_role(
            "combobox",
            name="Tên trường",
            exact=True,
        ))
    if manual_school_name is not None:
        try:
            if manual_school_name.input_value().strip() != data.school:
                manual_school_name.fill(data.school)
        except Exception:
            manual_school_name.fill(data.school)
        page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)

    address_control = find_school_address_control(identity_dialog)
    if address_control is None:
        raise TimeoutError(
            "Canva không có kết quả tên trường nhưng chưa hiện ô Địa chỉ trường"
        )

    log(f"Nhập địa chỉ trường thủ công: {data.school_address}")
    address_control.click()
    page.wait_for_timeout(300)

    # Canva thường mở một searchbox riêng sau khi click combobox địa chỉ.
    # Nếu giao diện dùng chính input địa chỉ để tìm kiếm thì fill trực tiếp.
    address_search = last_visible(page.get_by_role(
        "searchbox",
        name="Tùy chọn tìm kiếm",
        exact=True,
    ))
    if address_search is not None:
        address_search.fill(data.school_address)
    else:
        try:
            address_control.fill(data.school_address)
        except Exception as error:
            raise RuntimeError("Không thể nhập địa chỉ trường") from error
    page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)

    address_option: Locator | None = None
    deadline = time.monotonic() + data.timeout_ms / 1000
    while time.monotonic() < deadline:
        address_option = first_visible(page.get_by_role("option"))
        if address_option is not None:
            break
        page.wait_for_timeout(200)

    if address_option is None:
        raise TimeoutError(
            f"Không có gợi ý địa chỉ cho: {data.school_address}"
        )

    log("Chọn gợi ý địa chỉ đầu tiên")
    address_option.click()
    page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)

    identity_dialog = wait_for_identity_dialog(page, data.timeout_ms)
    website_input = first_visible(identity_dialog.get_by_placeholder(
        re.compile(r"Nhập\s+trang\s+web.*trường", re.IGNORECASE)
    ))
    if website_input is None:
        website_input = first_visible(identity_dialog.get_by_role(
            "textbox",
            name=re.compile(r"Trang\s+web.*trường", re.IGNORECASE),
        ))
    if website_input is None:
        raise TimeoutError("Không tìm thấy ô Trang web trường học")

    log(f"Nhập trang web trường học: {data.school_website}")
    website_input.fill(data.school_website)
    website_input.press("Tab")
    page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)


def select_school_or_fill_manual(page: Page, data: FlowData) -> str:
    """Chọn trường có sẵn; nếu không có thì hoàn thiện form thủ công."""
    school_name = data.school.split(",", maxsplit=1)[0].strip()
    short_school_name = school_name.split(maxsplit=1)[0]
    queries = list(dict.fromkeys(
        query for query in (data.school, school_name, short_school_name)
        if query
    ))
    no_options_confirmed = False

    for attempt, query in enumerate(queries, start=1):
        identity_dialog = wait_for_identity_dialog(page, data.timeout_ms)
        school_combobox = first_visible(identity_dialog.get_by_role(
            "combobox",
            name="Tên trường",
            exact=True,
        ))
        if school_combobox is None:
            # Form đã chuyển hẳn sang nhập tay sau một kết quả rỗng.
            if find_school_address_control(identity_dialog) is not None:
                no_options_confirmed = True
                break
            raise TimeoutError("Không tìm thấy điều khiển Tên trường")

        school_combobox.click()
        page.wait_for_timeout(300)
        school_search = last_visible(page.get_by_role(
            "searchbox",
            name="Tùy chọn tìm kiếm",
            exact=True,
        ))
        if school_search is None:
            raise TimeoutError("Không thấy ô tìm kiếm Tên trường")

        log(f"Nhập tên trường, lần {attempt}: {query}")
        try:
            school_search.fill("", timeout=min(5_000, data.timeout_ms))
            page.wait_for_timeout(200)
            school_search.press_sequentially(
                query,
                delay=50,
                timeout=min(10_000, data.timeout_ms),
            )
        except PlaywrightTimeoutError:
            # React có thể render lại searchbox. Thử lại với từ khóa ngắn hơn
            # ở vòng sau; chưa được chuyển sang địa chỉ chỉ vì DOM thay đổi.
            log("Ô tìm kiếm vừa được Canva render lại; sẽ thử lại")

        # Delay một giây đúng như thao tác tay, sau đó mới đọc danh sách.
        page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)
        query_deadline = time.monotonic() + min(5, data.timeout_ms / 1000)

        while time.monotonic() < query_deadline:
            listbox = last_visible(page.get_by_role("listbox"))
            option_scope: Locator | Page = listbox if listbox is not None else page
            # Người dùng yêu cầu chọn đúng kết quả trên cùng.
            school_option = first_visible(option_scope.get_by_role("option"))
            if school_option is not None:
                try:
                    option_text = re.sub(
                        r"\s+",
                        " ",
                        school_option.inner_text(),
                    ).strip()
                except Exception:
                    option_text = "kết quả đầu tiên"
                log(f"Chọn trường ở đầu danh sách: {option_text}")
                school_option.click()
                page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)
                log("Đã chọn trường từ danh sách gợi ý")
                return "listed"

            if first_visible(page.get_by_text(
                SCHOOL_NO_OPTIONS_PATTERN,
            )) is not None:
                no_options_confirmed = True
                break
            page.wait_for_timeout(200)

        # Đóng kết quả cũ rồi mở lại ở vòng sau với từ khóa ngắn hơn.
        if is_visible(school_search):
            try:
                school_search.press("Escape", timeout=1_000)
            except Exception:
                pass
        page.wait_for_timeout(500)

    if not no_options_confirmed:
        raise TimeoutError(
            "Canva không tải được danh sách trường; không tự nhập địa chỉ "
            "vì chưa có thông báo Không có tùy chọn nào"
        )

    log("Không có trường phù hợp trong gợi ý; chuyển sang nhập thủ công")
    fill_manual_school_details(page, data)
    return "manual"


def complete_identity_steps(page: Page, data: FlowData) -> None:
    document_dialog = find_document_upload_dialog(page)
    consent_scope: Locator | Page = (
        document_dialog if document_dialog is not None else page
    )
    consent_candidates = consent_scope.get_by_role(
        "checkbox",
        name=re.compile(r"Tôi hiểu và đồng ý cho Canva", re.IGNORECASE),
    )
    deadline = time.monotonic() + data.timeout_ms / 1000
    consent: Locator | None = None
    while time.monotonic() < deadline:
        if consent_candidates.count() > 0:
            consent = consent_candidates.last
            break
        page.wait_for_timeout(200)

    if consent is None:
        raise TimeoutError("Không tìm thấy checkbox đồng ý xử lý thông tin")

    log("Cuộn tới và tích ô đồng ý xử lý thông tin")
    if not check_checkbox_via_dom(page, consent):
        raise RuntimeError("Không thể tích checkbox đồng ý xử lý thông tin")

    # Tìm lại sau khi React render và chỉ tiếp tục khi trạng thái thật là checked.
    deadline = time.monotonic() + data.timeout_ms / 1000
    while time.monotonic() < deadline:
        document_dialog = find_document_upload_dialog(page)
        consent_scope = document_dialog if document_dialog is not None else page
        current_candidates = consent_scope.get_by_role(
            "checkbox",
            name=re.compile(r"Tôi hiểu và đồng ý cho Canva", re.IGNORECASE),
        )
        if current_candidates.count() > 0:
            current_consent = current_candidates.last
            if checkbox_is_checked(current_consent):
                break
        page.wait_for_timeout(200)
    else:
        raise TimeoutError("Checkbox đồng ý chưa giữ được trạng thái đã tích")

    log("Đã tích ô đồng ý xử lý thông tin")
    document_dialog = find_document_upload_dialog(page)
    continue_scope: Locator | Page = (
        document_dialog if document_dialog is not None else page
    )
    continue_button = last_visible(continue_scope.get_by_role(
        "button",
        name="Tiếp tục",
        exact=True,
    ))
    if continue_button is None:
        raise TimeoutError("Không tìm thấy nút Tiếp tục ở bước tài liệu")
    expect(continue_button).to_be_enabled(timeout=data.timeout_ms)
    log("Nhấn Tiếp tục sau khi tải PDF và đồng ý")
    continue_button.click()

    identity_dialog = wait_for_identity_dialog(page, data.timeout_ms)
    full_name_input = identity_dialog.get_by_role("textbox", name="Tên đầy đủ")
    expect(full_name_input).to_be_visible(timeout=data.timeout_ms)
    log("Điền tên đầy đủ")
    full_name_input.fill(data.full_name)
    page.wait_for_timeout(FINAL_FORM_FIELD_DELAY_MS)

    # Tên trường luôn được nhập cuối cùng để Canva có đủ thời gian ổn định
    # các trường phía trước và giữ đúng lựa chọn từ danh sách gợi ý.
    school_mode = select_school_or_fill_manual(page, data)
    if school_mode == "manual":
        log("Đã điền tên, trường, địa chỉ và website")
    else:
        log("Đã điền tên và chọn trường")
    click_final_identity_continue(page, data.timeout_ms)

    if first_visible(page.get_by_role(
        "button",
        name="Xong",
        exact=True,
    )) is not None:
        log("Đã chuyển sang màn hình hoàn tất")
    else:
        log("Form họ tên và trường đã đóng sau nút Tiếp tục")


def finish_flow(page: Page, data: FlowData) -> None:
    done_button = page.get_by_role("button", name="Xong", exact=True).first
    expect(done_button).to_be_visible(timeout=data.timeout_ms)

    if not data.submit:
        log(
            "DRY RUN: đã dừng trước nút Xong. Chạy lại với --submit nếu "
            "muốn thực hiện hai nút cuối Xong và Đồng ý."
        )
        page.bring_to_front()
        return

    log("Đã bật --submit; thực hiện bước hoàn tất cuối")
    done_button.click()

    agree_button = page.get_by_role("button", name="Đồng ý", exact=True).first
    expect(agree_button).to_be_visible(timeout=data.timeout_ms)
    agree_button.click()

    log("Đã click Xong và Đồng ý")


def save_error_screenshot(page: Page | None, directory: Path) -> Path | None:
    if page is None or page.is_closed():
        return None

    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("error_%Y%m%d_%H%M%S.png")
    destination = directory / filename

    try:
        page.screenshot(path=str(destination), full_page=True)
    except Exception:
        return None

    return destination


def parse_args() -> FlowData:
    parser = argparse.ArgumentParser(
        description=(
            "Chạy một hồ sơ Canva Education qua Chrome đã mở CDP. "
            "Chỉ sử dụng tài khoản và tài liệu bạn có quyền sử dụng."
        )
    )
    parser.add_argument("--email", required=True, help="Email đăng nhập Canva")
    parser.add_argument(
        "--otp",
        help=(
            "OTP 6 số; ưu tiên giá trị này. Nếu bỏ trống, chương trình dùng "
            "Mail API khi có refresh token và client ID"
        ),
    )
    parser.add_argument(
        "--refresh-token",
        default=os.getenv("CANVA_MAIL_REFRESH_TOKEN"),
        help="Refresh token Hotmail; có thể dùng CANVA_MAIL_REFRESH_TOKEN",
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("CANVA_MAIL_CLIENT_ID"),
        help="Client ID Hotmail; có thể dùng CANVA_MAIL_CLIENT_ID",
    )
    parser.add_argument(
        "--mail-api-url",
        default=os.getenv("CANVA_MAIL_API_URL", DEFAULT_MAIL_API_URL),
        help="Endpoint đọc danh sách thư DongVanFB",
    )
    parser.add_argument(
        "--mail-request-timeout",
        type=int,
        default=DEFAULT_MAIL_REQUEST_TIMEOUT,
        help="Timeout cho mỗi request Mail API, đơn vị giây",
    )
    parser.add_argument(
        "--otp-initial-delay",
        type=float,
        default=DEFAULT_OTP_INITIAL_DELAY,
        help="Số giây chờ ban đầu sau khi Canva gửi OTP",
    )
    parser.add_argument(
        "--otp-interval",
        type=float,
        default=DEFAULT_OTP_INTERVAL,
        help="Khoảng cách giữa hai lần kiểm tra mailbox",
    )
    parser.add_argument(
        "--otp-timeout",
        type=float,
        default=DEFAULT_OTP_TIMEOUT,
        help="Tổng thời gian chờ OTP, đơn vị giây",
    )
    parser.add_argument("--pdf", required=True, type=Path, help="Đường dẫn PDF")
    parser.add_argument("--full-name", required=True, help="Tên đầy đủ")
    parser.add_argument("--school", required=True, help="Tên trường để tìm kiếm")
    parser.add_argument(
        "--school-address",
        default=DEFAULT_SCHOOL_ADDRESS,
        help="Địa chỉ dùng khi Canva yêu cầu nhập trường thủ công",
    )
    parser.add_argument(
        "--school-website",
        default=DEFAULT_SCHOOL_WEBSITE,
        help="Website dùng khi Canva yêu cầu nhập trường thủ công",
    )
    parser.add_argument("--country", default="Campuchia")
    parser.add_argument(
        "--school-level",
        default="Trường Tiểu học, THCS, THPT",
    )
    parser.add_argument("--teacher-role", default="Giáo viên")
    parser.add_argument("--grade", default="Lớp 9")
    parser.add_argument("--subject", default="Toán")
    parser.add_argument("--document-type", default="Bảng lương")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Cho phép click hai nút cuối Xong và Đồng ý",
    )
    parser.add_argument(
        "--screenshot-dir",
        type=Path,
        default=Path("screenshots"),
    )

    args = parser.parse_args()

    if args.timeout_ms <= 0:
        parser.error("--timeout-ms phải lớn hơn 0")
    if bool(args.refresh_token) != bool(args.client_id):
        parser.error("Phải cung cấp đồng thời --refresh-token và --client-id")
    if args.mail_request_timeout <= 0:
        parser.error("--mail-request-timeout phải lớn hơn 0")
    if args.otp_initial_delay < 0:
        parser.error("--otp-initial-delay không được âm")
    if args.otp_interval <= 0:
        parser.error("--otp-interval phải lớn hơn 0")
    if args.otp_timeout <= 0:
        parser.error("--otp-timeout phải lớn hơn 0")

    return FlowData(
        email=args.email.strip(),
        otp=args.otp.strip() if args.otp else None,
        refresh_token=args.refresh_token.strip() if args.refresh_token else None,
        client_id=args.client_id.strip() if args.client_id else None,
        mail_api_url=args.mail_api_url.strip(),
        mail_request_timeout=args.mail_request_timeout,
        otp_initial_delay=args.otp_initial_delay,
        otp_interval=args.otp_interval,
        otp_timeout=args.otp_timeout,
        pdf_path=validate_pdf(args.pdf),
        full_name=args.full_name.strip(),
        school=args.school.strip(),
        school_address=args.school_address.strip(),
        school_website=args.school_website.strip(),
        country=args.country.strip(),
        school_level=args.school_level.strip(),
        teacher_role=args.teacher_role.strip(),
        grade=args.grade.strip(),
        subject=args.subject.strip(),
        document_type=args.document_type.strip(),
        cdp_url=args.cdp_url.strip(),
        timeout_ms=args.timeout_ms,
        submit=args.submit,
        screenshot_dir=args.screenshot_dir,
    )


def run(data: FlowData) -> int:
    page: Page | None = None

    try:
        with sync_playwright() as playwright:
            log(f"Kết nối Chrome tại {data.cdp_url}")
            browser = playwright.chromium.connect_over_cdp(data.cdp_url)

            if not browser.contexts:
                raise RuntimeError("Không tìm thấy Chrome context qua CDP")

            context = browser.contexts[0]
            context.set_default_timeout(data.timeout_ms)
            page = open_tool_page(context, data.timeout_ms)

            log(f"Đang điều khiển tab: {page.url}")
            login_with_email(page, data)
            complete_eligibility_steps(page, data)
            choose_document_type(page, data.document_type, data.timeout_ms)
            upload_pdf(page, data.pdf_path, data.timeout_ms)
            complete_identity_steps(page, data)
            finish_flow(page, data)

            log("Luồng hiện tại đã hoàn thành")
            return 0

    except KeyboardInterrupt:
        log("Đã dừng theo yêu cầu người dùng")
        return 130
    except Exception as error:
        log(f"LỖI: {type(error).__name__}: {error}")
        screenshot = save_error_screenshot(page, data.screenshot_dir)
        if screenshot:
            log(f"Đã lưu screenshot lỗi: {screenshot.resolve()}")
        return 1


def main() -> None:
    try:
        data = parse_args()
    except (FileNotFoundError, ValueError) as error:
        print(f"LỖI DỮ LIỆU: {error}", file=sys.stderr)
        raise SystemExit(2) from error

    raise SystemExit(run(data))


if __name__ == "__main__":
    main()
