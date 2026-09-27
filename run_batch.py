from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

from canva_education_flow import (
    DEFAULT_CDP_URL,
    DEFAULT_MAIL_API_URL,
    DEFAULT_MAIL_REQUEST_TIMEOUT,
    DEFAULT_OTP_INITIAL_DELAY,
    DEFAULT_OTP_INTERVAL,
    DEFAULT_OTP_TIMEOUT,
    DEFAULT_SCHOOL_ADDRESS,
    DEFAULT_SCHOOL_WEBSITE,
    DEFAULT_TIMEOUT_MS,
    FlowData,
    run,
    validate_pdf,
)


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_ACCOUNTS_FILE = Path("accounts.txt")
DEFAULT_RESULTS_FILE = Path("logs/batch_results.jsonl")
DEFAULT_DELAY_BETWEEN_ACCOUNTS_SECONDS = 60
REQUIRED_FIELD_COUNT = 9
MAX_FIELD_COUNT = 14


@dataclass(frozen=True)
class AccountRecord:
    line_number: int
    email: str
    password: str
    refresh_token: str
    client_id: str
    recovery_email: str
    full_name: str
    country: str
    school: str
    pdf_path: Path
    school_level: str
    teacher_role: str
    grade: str
    subject: str
    document_type: str


@dataclass(frozen=True)
class BatchSettings:
    accounts_file: Path
    results_file: Path
    cdp_url: str
    mail_api_url: str
    mail_request_timeout: int
    otp_initial_delay: float
    otp_interval: float
    otp_timeout: float
    timeout_ms: int
    submit: bool
    screenshot_dir: Path
    school_level: str
    teacher_role: str
    grade: str
    subject: str
    document_type: str
    school_address: str
    school_website: str
    delay_between_accounts: int


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [BATCH] {message}", flush=True)


def clean_pasted_field(value: str) -> str:
    cleaned = value.strip()

    # Cho phép người dùng vô tình dán chuỗi do giao diện chat chuyển thành:
    # [email@example.com](mailto:email@example.com)
    mailto_match = re.fullmatch(
        r"\[([^\]]+)\]\(mailto:[^)]+\)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if mailto_match:
        cleaned = mailto_match.group(1).strip()

    # Hoàn nguyên các ký tự Markdown thường bị thêm dấu gạch chéo.
    cleaned = cleaned.replace(r"\_", "_").replace(r"\*", "*")
    return cleaned


def parse_account_line(
    raw_line: str,
    line_number: int,
    settings: BatchSettings,
) -> AccountRecord:
    fields = [clean_pasted_field(field) for field in raw_line.split("|")]

    if len(fields) < REQUIRED_FIELD_COUNT:
        raise ValueError(
            f"Dòng {line_number} chỉ có {len(fields)} trường; cần ít nhất "
            f"{REQUIRED_FIELD_COUNT}. Hãy thêm full_name, country, school "
            "và pdf_path sau 5 trường xác thực."
        )
    if len(fields) > MAX_FIELD_COUNT:
        raise ValueError(
            f"Dòng {line_number} có {len(fields)} trường; tối đa "
            f"{MAX_FIELD_COUNT}. Kiểm tra ký tự | trong dữ liệu."
        )

    # Ba định dạng được hỗ trợ rõ ràng:
    # 9 cột  : chỉ dữ liệu bắt buộc, mọi lựa chọn dùng mặc định.
    # 13 cột : thêm school_level, role, grade, document_type;
    #           subject dùng mặc định từ --subject (mặc định là Toán).
    # 14 cột : giống trên nhưng có subject riêng trước document_type.
    if len(fields) == 9:
        fields.extend(
            [
                settings.school_level,
                settings.teacher_role,
                settings.grade,
                settings.subject,
                settings.document_type,
            ]
        )
    elif len(fields) == 13:
        fields.insert(12, settings.subject)
    elif len(fields) != 14:
        raise ValueError(
            f"Dòng {line_number} có {len(fields)} trường. Chỉ chấp nhận "
            "định dạng 9, 13 hoặc 14 trường."
        )

    (
        email,
        password,
        refresh_token,
        client_id,
        recovery_email,
        full_name,
        country,
        school,
        pdf_path_text,
        school_level,
        teacher_role,
        grade,
        subject,
        document_type,
    ) = fields

    required_values = {
        "email": email,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "full_name": full_name,
        "country": country,
        "school": school,
        "pdf_path": pdf_path_text,
    }
    missing = [name for name, value in required_values.items() if not value]
    if missing:
        raise ValueError(
            f"Dòng {line_number} thiếu trường bắt buộc: {', '.join(missing)}"
        )
    if "@" not in email:
        raise ValueError(f"Dòng {line_number} có email không hợp lệ")

    return AccountRecord(
        line_number=line_number,
        email=email,
        password=password,
        refresh_token=refresh_token,
        client_id=client_id,
        recovery_email=recovery_email,
        full_name=full_name,
        country=country,
        school=school,
        pdf_path=validate_pdf(Path(pdf_path_text)),
        school_level=school_level or settings.school_level,
        teacher_role=teacher_role or settings.teacher_role,
        grade=grade or settings.grade,
        subject=subject or settings.subject,
        document_type=document_type or settings.document_type,
    )


def load_account_lines(path: Path) -> list[tuple[int, str]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Không tìm thấy file tài khoản: {resolved}")

    records: list[tuple[int, str]] = []
    with resolved.open("r", encoding="utf-8-sig") as account_file:
        for line_number, raw_line in enumerate(account_file, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            records.append((line_number, stripped))

    if not records:
        raise ValueError(f"File tài khoản không có dòng dữ liệu: {resolved}")

    return records


def build_flow_data(record: AccountRecord, settings: BatchSettings) -> FlowData:
    account_screenshot_dir = settings.screenshot_dir / f"line_{record.line_number}"

    return FlowData(
        email=record.email,
        otp=None,
        refresh_token=record.refresh_token,
        client_id=record.client_id,
        mail_api_url=settings.mail_api_url,
        mail_request_timeout=settings.mail_request_timeout,
        otp_initial_delay=settings.otp_initial_delay,
        otp_interval=settings.otp_interval,
        otp_timeout=settings.otp_timeout,
        pdf_path=record.pdf_path,
        full_name=record.full_name,
        school=record.school,
        school_address=settings.school_address,
        school_website=settings.school_website,
        country=record.country,
        school_level=record.school_level,
        teacher_role=record.teacher_role,
        grade=record.grade,
        subject=record.subject,
        document_type=record.document_type,
        cdp_url=settings.cdp_url,
        timeout_ms=settings.timeout_ms,
        submit=settings.submit,
        screenshot_dir=account_screenshot_dir,
    )


def write_result(
    results_file: Path,
    *,
    line_number: int,
    email: str | None,
    status: str,
    error: str | None = None,
) -> None:
    results_file.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "line": line_number,
        "email": email,
        "status": status,
        "error": error,
    }

    with results_file.open("a", encoding="utf-8") as output:
        output.write(json.dumps(result, ensure_ascii=False) + "\n")


def check_cdp(cdp_url: str) -> None:
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urlopen(version_url, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"CDP trả HTTP {response.status}")
    except Exception as error:
        raise RuntimeError(
            f"Không kết nối được Chrome CDP tại {cdp_url}. "
            "Hãy mở profile automation trước."
        ) from error


def clear_canva_session(cdp_url: str) -> None:
    """Xóa riêng phiên Canva trong profile automation giữa hai tài khoản."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            return

        context = browser.contexts[0]
        canva_pages = []

        for page in list(context.pages):
            hostname = (urlparse(page.url).hostname or "").lower()
            if hostname == "canva.com" or hostname.endswith(".canva.com"):
                canva_pages.append(page)

        # Chỉ xóa cookie Canva, không ảnh hưởng các website khác trong profile.
        context.clear_cookies(domain=re.compile(r"(^|\.)canva\.com$"))

        for page in canva_pages:
            if page.is_closed():
                continue
            try:
                session = context.new_cdp_session(page)
                parsed = urlparse(page.url)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                session.send(
                    "Storage.clearDataForOrigin",
                    {
                        "origin": origin,
                        "storageTypes": (
                            "cookies,local_storage,indexeddb,cache_storage,"
                            "service_workers"
                        ),
                    },
                )
            except Exception:
                # Cookie đã được xóa ở trên; lỗi dọn thêm storage không được
                # phép làm dừng toàn bộ batch.
                pass
            finally:
                try:
                    page.close()
                except Exception:
                    pass


def parse_args() -> BatchSettings:
    parser = argparse.ArgumentParser(
        description="Chạy tuần tự nhiều tài khoản Canva từ accounts.txt"
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=DEFAULT_ACCOUNTS_FILE,
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_FILE,
    )
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--mail-api-url", default=DEFAULT_MAIL_API_URL)
    parser.add_argument(
        "--mail-request-timeout",
        type=int,
        default=DEFAULT_MAIL_REQUEST_TIMEOUT,
    )
    parser.add_argument(
        "--otp-initial-delay",
        type=float,
        default=DEFAULT_OTP_INITIAL_DELAY,
    )
    parser.add_argument(
        "--otp-interval",
        type=float,
        default=DEFAULT_OTP_INTERVAL,
    )
    parser.add_argument(
        "--otp-timeout",
        type=float,
        default=DEFAULT_OTP_TIMEOUT,
    )
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--screenshot-dir",
        type=Path,
        default=Path("screenshots"),
    )
    parser.add_argument(
        "--school-level",
        default="Trường Tiểu học, THCS, THPT",
    )
    parser.add_argument("--teacher-role", default="Giáo viên")
    parser.add_argument("--grade", default="Lớp 9")
    parser.add_argument("--subject", default="Toán")
    parser.add_argument("--document-type", default="Bảng lương")
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
    parser.add_argument(
        "--delay-between-accounts",
        type=int,
        default=DEFAULT_DELAY_BETWEEN_ACCOUNTS_SECONDS,
        help=(
            "Số giây nghỉ sau khi xử lý xong một tài khoản và trước tài "
            "khoản kế tiếp (mặc định: 60; dùng 0 để tắt)"
        ),
    )

    args = parser.parse_args()

    numeric_values = {
        "--mail-request-timeout": args.mail_request_timeout,
        "--otp-interval": args.otp_interval,
        "--otp-timeout": args.otp_timeout,
        "--timeout-ms": args.timeout_ms,
    }
    for option, value in numeric_values.items():
        if value <= 0:
            parser.error(f"{option} phải lớn hơn 0")
    if args.otp_initial_delay < 0:
        parser.error("--otp-initial-delay không được âm")
    if args.delay_between_accounts < 0:
        parser.error("--delay-between-accounts không được âm")

    return BatchSettings(
        accounts_file=args.accounts_file,
        results_file=args.results_file,
        cdp_url=args.cdp_url.strip(),
        mail_api_url=args.mail_api_url.strip(),
        mail_request_timeout=args.mail_request_timeout,
        otp_initial_delay=args.otp_initial_delay,
        otp_interval=args.otp_interval,
        otp_timeout=args.otp_timeout,
        timeout_ms=args.timeout_ms,
        submit=args.submit,
        screenshot_dir=args.screenshot_dir,
        school_level=args.school_level.strip(),
        teacher_role=args.teacher_role.strip(),
        grade=args.grade.strip(),
        subject=args.subject.strip(),
        document_type=args.document_type.strip(),
        school_address=args.school_address.strip(),
        school_website=args.school_website.strip(),
        delay_between_accounts=args.delay_between_accounts,
    )


def notify_account_completed(
    *,
    position: int,
    total: int,
    line_number: int,
    email: str,
    status: str,
) -> None:
    """In thông báo dễ nhận biết khi một tài khoản đã chạy xong."""
    log("=" * 68)
    log(
        f"HOÀN THÀNH TÀI KHOẢN {position}/{total} | "
        f"dòng {line_number} | {email} | {status}"
    )
    log("=" * 68)


def wait_before_next_account(seconds: int, next_position: int, total: int) -> None:
    """Nghỉ có hiển thị đếm ngược để người dùng biết batch vẫn hoạt động."""
    if seconds <= 0:
        return

    log(
        f"Nghỉ {seconds} giây trước khi chạy tài khoản "
        f"{next_position}/{total}"
    )
    deadline = time.monotonic() + seconds
    report_points = {60, 30, 10, 5, 4, 3, 2, 1}
    last_reported: int | None = None

    while True:
        remaining_raw = deadline - time.monotonic()
        if remaining_raw <= 0:
            break

        remaining = math.ceil(remaining_raw)
        if (
            remaining in report_points
            and remaining != seconds
            and remaining != last_reported
        ):
            log(f"Còn {remaining} giây trước tài khoản kế tiếp")
            last_reported = remaining

        time.sleep(min(1.0, remaining_raw))

    log(f"Hết thời gian nghỉ; bắt đầu tài khoản {next_position}/{total}")


def run_batch(settings: BatchSettings) -> int:
    check_cdp(settings.cdp_url)
    raw_records = load_account_lines(settings.accounts_file)
    failures = 0
    successes = 0
    processed = 0
    infrastructure_failure = False

    log(f"Tìm thấy {len(raw_records)} dòng tài khoản")
    log("Chế độ SUBMIT đang bật" if settings.submit else "Chế độ DRY RUN")

    for position, (line_number, raw_line) in enumerate(raw_records, start=1):
        email_for_log: str | None = None
        cleanup_failed = False
        flow_started = False
        account_completed = False
        processed += 1
        log(f"Bắt đầu tài khoản {position}/{len(raw_records)}, dòng {line_number}")

        try:
            record = parse_account_line(raw_line, line_number, settings)
            email_for_log = record.email
            data = build_flow_data(record, settings)
            flow_started = True
            result_code = run(data)

            if result_code == 130:
                write_result(
                    settings.results_file,
                    line_number=line_number,
                    email=record.email,
                    status="INTERRUPTED",
                )
                return 130

            if result_code == 0:
                status = "SUCCESS" if settings.submit else "DRY_RUN_OK"
                successes += 1
                account_completed = True
                write_result(
                    settings.results_file,
                    line_number=line_number,
                    email=record.email,
                    status=status,
                )
                log(f"Hoàn thành dòng {line_number}: {status}")
                notify_account_completed(
                    position=position,
                    total=len(raw_records),
                    line_number=line_number,
                    email=record.email,
                    status=status,
                )
            else:
                failures += 1
                write_result(
                    settings.results_file,
                    line_number=line_number,
                    email=record.email,
                    status="FAILED",
                    error=f"Flow trả mã {result_code}",
                )
                log(f"Dòng {line_number} chạy lỗi; chuyển tài khoản kế tiếp")

        except Exception as error:
            failures += 1
            write_result(
                settings.results_file,
                line_number=line_number,
                email=email_for_log,
                status="INVALID_OR_FAILED",
                error=f"{type(error).__name__}: {error}",
            )
            log(f"Dòng {line_number} lỗi: {type(error).__name__}: {error}")
        finally:
            if flow_started:
                try:
                    clear_canva_session(settings.cdp_url)
                    log(
                        "Đã đăng xuất và dọn dữ liệu phiên Canva "
                        "(cookie + storage Canva) trước tài khoản kế tiếp"
                    )
                except Exception as cleanup_error:
                    cleanup_failed = True
                    infrastructure_failure = True
                    log(f"Cảnh báo: không dọn được phiên Canva: {cleanup_error}")

        if cleanup_failed:
            log(
                "Dừng batch vì không thể xác nhận phiên Canva đã được dọn; "
                "tiếp tục có thể dùng nhầm tài khoản."
            )
            break

        if account_completed and position < len(raw_records):
            wait_before_next_account(
                settings.delay_between_accounts,
                position + 1,
                len(raw_records),
            )

    remaining = len(raw_records) - processed
    log(
        f"Batch kết thúc: {successes} thành công, {failures} lỗi, "
        f"{remaining} chưa chạy"
    )
    return 1 if failures or infrastructure_failure else 0


def main() -> None:
    try:
        settings = parse_args()
        exit_code = run_batch(settings)
    except KeyboardInterrupt:
        log("Đã dừng theo yêu cầu người dùng")
        exit_code = 130
    except Exception as error:
        log(f"LỖI KHỞI TẠO: {type(error).__name__}: {error}")
        exit_code = 2

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
