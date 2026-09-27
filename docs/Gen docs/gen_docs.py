#!/usr/bin/env python3
"""Generate numbered payslip PDFs from one-name-per-line text input."""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.getLogger("pypdf").setLevel(logging.ERROR)

try:
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import ContentStream
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
except ImportError as exc:  # pragma: no cover - friendly startup error
    print(
        "Thieu thu vien Python. Hay chay file run.bat de tool tu cai dat "
        "pypdf va reportlab.",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


TOOL_DIR = Path(__file__).resolve().parent
DOCS_DIR = TOOL_DIR.parent

DEFAULT_TEMPLATE = DOCS_DIR / "x.pdf"
DEFAULT_OUTPUT_DIR = DOCS_DIR
DEFAULT_RESULT_NAME = "ketqua.txt"
DEFAULT_ID_PREFIX = "BIS-2025-"

# Coordinates measured from x.pdf (Letter page, points, origin at bottom-left).
# The rectangles stay one point inside the table borders.
DETAIL_LEFT = 307.0
DETAIL_WIDTH = 215.0
NAME_BOX = (DETAIL_LEFT, 563.0, DETAIL_WIDTH, 19.0)
EMPLOYEE_ID_BOX = (DETAIL_LEFT, 522.5, DETAIL_WIDTH, 18.5)
TEXT_LEFT = 314.0
NAME_BASELINE = 569.70
EMPLOYEE_ID_BASELINE = 529.20
DEFAULT_FONT_SIZE = 11.02
MIN_FONT_SIZE = 7.5
MAX_TEXT_WIDTH = 201.0

FIXED_RESULT_FIELDS = (
    "Campuchia",
    "BELTEI International School, Phnom Penh",
    "THCS",
    "Giáo viên",
    "Lớp 9",
    "Bảng lương",
)


@dataclass(frozen=True)
class Record:
    line_number: int
    full_name: str
    requested_employee_id: str | None


def ascii_key(value: str) -> str:
    """Normalize a marker so both accented and unaccented spellings work."""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.casefold().split())


RANDOM_ID_MARKERS = {
    "",
    "random",
    "rand",
    "id random",
    "employee id random",
    "ngau nhien",
    "id ngau nhien",
}


def read_text_compat(path: Path) -> str:
    """Read the usual Windows/Vietnamese text encodings."""
    errors: list[str] = []
    for encoding in ("utf-8-sig", "cp1258", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError(
        f"Khong doc duoc ma hoa cua {path}. Hay luu file o dang UTF-8. "
        + "; ".join(errors)
    )


def parse_records(path: Path) -> list[Record]:
    records: list[Record] = []
    for line_number, raw_line in enumerate(read_text_compat(path).splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("|", 1)
        full_name = unicodedata.normalize("NFC", parts[0].strip())
        if not full_name:
            raise ValueError(f"Dong {line_number}: ho ten dang bi trong.")
        if any(ch in full_name for ch in "\r\n|"):
            raise ValueError(f"Dong {line_number}: ho ten khong hop le.")

        requested_id: str | None = None
        if len(parts) == 2:
            candidate = parts[1].strip()
            if ascii_key(candidate) not in RANDOM_ID_MARKERS:
                if any(ch in candidate for ch in "\r\n|"):
                    raise ValueError(
                        f"Dong {line_number}: Employee ID khong hop le."
                    )
                requested_id = unicodedata.normalize("NFC", candidate)

        records.append(Record(line_number, full_name, requested_id))
    return records


def file_has_records(path: Path) -> bool:
    try:
        return bool(parse_records(path))
    except (OSError, ValueError):
        return False


def find_default_input() -> Path:
    """Pick the only populated txt file, preferring danh_sach.txt/input.txt."""
    candidates = [
        path
        for path in sorted(TOOL_DIR.glob("*.txt"), key=lambda item: item.name.casefold())
        if path.name.casefold()
        not in {DEFAULT_RESULT_NAME.casefold(), "requirements.txt"}
    ]
    populated = [path for path in candidates if file_has_records(path)]

    for preferred_name in ("danh_sach.txt", "input.txt", "names.txt"):
        preferred = TOOL_DIR / preferred_name
        if preferred in populated:
            if len(populated) > 1:
                others = ", ".join(path.name for path in populated if path != preferred)
                raise ValueError(
                    f"Co nhieu file TXT co du lieu ({preferred.name}, {others}). "
                    "Hay chi de du lieu trong mot file hoac dung --input."
                )
            return preferred

    if len(populated) == 1:
        return populated[0]
    if len(populated) > 1:
        names = ", ".join(path.name for path in populated)
        raise ValueError(
            f"Co nhieu file TXT co du lieu ({names}). "
            "Hay chi de du lieu trong mot file hoac dung --input."
        )

    placeholder = TOOL_DIR / "danh_sach.txt"
    if placeholder.exists():
        return placeholder
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError(
        f"Khong tim thay file TXT dau vao trong thu muc {TOOL_DIR}."
    )


def register_font(required_texts: list[str]) -> str:
    """Register the closest available font that contains every required glyph."""
    local_font = TOOL_DIR / "font.ttf"
    font_candidates = (
        local_font,
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "ARIALN.TTF",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "LeelawUI.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    required_chars = {
        char
        for text in required_texts
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("C")
    }
    for index, font_path in enumerate(font_candidates):
        if font_path.is_file():
            font_name = f"PayslipOverlayFont{index}"
            candidate = TTFont(font_name, str(font_path))
            if all(ord(char) in candidate.face.charToGlyph for char in required_chars):
                pdfmetrics.registerFont(candidate)
                return font_name
    raise FileNotFoundError(
        "Khong tim thay font Unicode chua du cac ky tu can thiet. "
        "Hay chep mot font TrueType phu hop "
        f"vao {local_font}."
    )


def fit_font_size(text: str, font_name: str) -> float:
    size = DEFAULT_FONT_SIZE
    width = pdfmetrics.stringWidth(text, font_name, size)
    if width <= MAX_TEXT_WIDTH:
        return size
    fitted = size * MAX_TEXT_WIDTH / width
    if fitted < MIN_FONT_SIZE:
        raise ValueError(
            f"Noi dung qua dai de vua o trong PDF: {text!r}. "
            "Hay rut gon noi dung."
        )
    return fitted


def make_overlay(
    page_width: float,
    page_height: float,
    full_name: str,
    employee_id: str,
    font_name: str,
) -> PdfReader:
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(page_width, page_height), pageCompression=1)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setStrokeColorRGB(1, 1, 1)
    pdf.rect(*NAME_BOX, stroke=0, fill=1)
    pdf.rect(*EMPLOYEE_ID_BOX, stroke=0, fill=1)

    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont(font_name, fit_font_size(full_name, font_name))
    pdf.drawString(TEXT_LEFT, NAME_BASELINE, full_name)
    pdf.setFont(font_name, fit_font_size(employee_id, font_name))
    pdf.drawString(TEXT_LEFT, EMPLOYEE_ID_BASELINE, employee_id)
    pdf.save()
    stream.seek(0)
    return PdfReader(stream)


def validate_template(reader: PdfReader, template: Path) -> None:
    if not reader.pages:
        raise ValueError(f"Template khong co trang nao: {template}")
    first_page = reader.pages[0]
    width = float(first_page.mediabox.width)
    height = float(first_page.mediabox.height)
    if abs(width - 612.0) > 1.0 or abs(height - 792.0) > 1.0:
        raise ValueError(
            "Template khong dung kich thuoc cua x.pdf (can 612 x 792 point). "
            f"Kich thuoc hien tai: {width:.2f} x {height:.2f}."
        )


def locate_replaceable_text_blocks(page) -> tuple[set[int], dict[str, str]]:
    """Locate the Canva text objects for the name and ID by their text matrix."""
    state = {"block": -1}
    found: dict[str, list[tuple[int, str]]] = {"name": [], "employee_id": []}

    def before(operator, operands, current_matrix, text_matrix) -> None:
        del operands, current_matrix, text_matrix
        if operator == b"BT":
            state["block"] += 1

    def on_text(text, current_matrix, text_matrix, font_dictionary, font_size) -> None:
        del font_dictionary, font_size
        value = text.strip()
        if not value:
            return
        canvas_x = float(current_matrix[4])
        row_y = float(text_matrix[5])
        if abs(canvas_x - 314.0) <= 0.5 and abs(row_y - 45.0) <= 0.5:
            found["name"].append((state["block"], value))
        elif abs(canvas_x - 314.0) <= 0.5 and abs(row_y - 99.0) <= 0.5:
            found["employee_id"].append((state["block"], value))

    page.extract_text(visitor_operand_before=before, visitor_text=on_text)
    for field, matches in found.items():
        if len(matches) != 1:
            raise ValueError(
                f"Khong xac dinh duy nhat vung chu cu cho {field}: {matches!r}."
            )

    blocks = {found["name"][0][0], found["employee_id"][0][0]}
    originals = {
        "name": found["name"][0][1],
        "employee_id": found["employee_id"][0][1],
    }
    return blocks, originals


def remove_text_blocks(page, pdf_owner, blocks_to_remove: set[int]) -> None:
    """Remove complete BT/ET objects so replaced values are not searchable."""
    content = ContentStream(page.get_contents(), pdf_owner)
    new_operations = []
    block_index = -1
    skipping = False
    for operands, operator in content.operations:
        if operator == b"BT":
            block_index += 1
            skipping = block_index in blocks_to_remove
        if not skipping:
            new_operations.append((operands, operator))
        if operator == b"ET":
            skipping = False
    content.operations = new_operations
    page.replace_contents(content)


def create_payslip(
    template_bytes: bytes,
    template_path: Path,
    output_path: Path,
    full_name: str,
    employee_id: str,
    font_name: str,
) -> None:
    reader = PdfReader(io.BytesIO(template_bytes), strict=False)
    validate_template(reader, template_path)
    page = reader.pages[0]
    replaceable_blocks, original_values = locate_replaceable_text_blocks(page)
    overlay_reader = make_overlay(
        float(page.mediabox.width),
        float(page.mediabox.height),
        full_name,
        employee_id,
        font_name,
    )

    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    remove_text_blocks(writer.pages[0], writer, replaceable_blocks)
    # Canva's template has a non-zero MediaBox bottom (7.92 pt). Translate the
    # normal-origin ReportLab overlay so its visible coordinates align exactly.
    writer.pages[0].merge_transformed_page(
        overlay_reader.pages[0],
        Transformation().translate(
            tx=float(page.mediabox.left),
            ty=float(page.mediabox.bottom),
        ),
        over=True,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{output_path.stem}.",
        suffix=".tmp.pdf",
        dir=output_path.parent,
        delete=False,
    )
    temp_path = Path(temp_handle.name)
    try:
        with temp_handle:
            writer.write(temp_handle)

        check = PdfReader(str(temp_path), strict=False)
        if len(check.pages) != len(reader.pages):
            raise ValueError("So trang PDF dau ra khong khop template.")
        extracted = check.pages[0].extract_text() or ""
        if full_name not in extracted:
            raise ValueError("Khong xac minh duoc Employee Name trong PDF dau ra.")
        if employee_id not in extracted:
            raise ValueError("Khong xac minh duoc Employee ID trong PDF dau ra.")
        if original_values["name"] != full_name and original_values["name"] in extracted:
            raise ValueError("Employee Name cu van con trong lop chu PDF.")
        if (
            original_values["employee_id"] != employee_id
            and original_values["employee_id"] in extracted
        ):
            raise ValueError("Employee ID cu van con trong lop chu PDF.")
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def collect_used_ids(output_dir: Path, id_prefix: str) -> set[str]:
    """Avoid IDs visible in already-numbered PDFs where extraction is available."""
    pattern = re.compile(re.escape(id_prefix) + r"\d{4}")
    used: set[str] = set()
    for path in output_dir.glob("*.pdf"):
        if not path.stem.isdigit():
            continue
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.pages:
                used.update(pattern.findall(reader.pages[0].extract_text() or ""))
        except Exception:
            # An unrelated/broken old PDF should not block creation of new files.
            continue
    return used


def random_employee_id(prefix: str, used_ids: set[str]) -> str:
    for _ in range(20_000):
        employee_id = f"{prefix}{secrets.randbelow(9000) + 1000}"
        if employee_id not in used_ids:
            used_ids.add(employee_id)
            return employee_id
    raise RuntimeError("Khong con Employee ID ngau nhien kha dung.")


def next_pdf_number(output_dir: Path) -> int:
    numbers = [
        int(path.stem)
        for path in output_dir.glob("*.pdf")
        if path.stem.isdigit() and int(path.stem) > 0
    ]
    return max(numbers, default=0) + 1


def append_result_line(result_path: Path, line: str) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if result_path.exists() and result_path.stat().st_size:
        with result_path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_newline = existing.read(1) not in (b"\n", b"\r")
    with result_path.open("a", encoding="utf-8", newline="") as result:
        if needs_newline:
            result.write("\n")
        result.write(line + "\n")


def result_line(full_name: str, output_path: Path) -> str:
    country, school, level, role, grade, document_type = FIXED_RESULT_FIELDS
    return "|".join(
        (
            full_name,
            country,
            school,
            str(output_path.resolve()),
            level,
            role,
            grade,
            document_type,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Tao PDF bang luong tu x.pdf. Moi dong TXT: "
            "Ho ten hoac Ho ten | Employee ID."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="File TXT dau vao; mac dinh tu dong tim trong thu muc cua tool.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help=f"PDF mau (mac dinh: {DEFAULT_TEMPLATE}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Thu muc luu PDF danh so (mac dinh: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--result",
        type=Path,
        help="File ket qua; mac dinh la ketqua.txt trong thu muc output.",
    )
    parser.add_argument(
        "--id-prefix",
        default=DEFAULT_ID_PREFIX,
        help=f"Tien to ID ngau nhien (mac dinh: {DEFAULT_ID_PREFIX}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_path = args.input.resolve() if args.input else find_default_input().resolve()
        template_path = args.template.resolve()
        output_dir = args.output_dir.resolve()
        result_path = (
            args.result.resolve()
            if args.result
            else (output_dir / DEFAULT_RESULT_NAME).resolve()
        )

        if not input_path.is_file():
            raise FileNotFoundError(f"Khong tim thay file dau vao: {input_path}")
        if not template_path.is_file():
            raise FileNotFoundError(f"Khong tim thay PDF mau: {template_path}")
        records = parse_records(input_path)
        if not records:
            raise ValueError(
                f"File {input_path.name} chua co dong du lieu nao. "
                "Moi dong can co dang: Ho ten hoac Ho ten | random."
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        template_bytes = template_path.read_bytes()
        font_name = register_font(
            [
                text
                for record in records
                for text in (record.full_name, record.requested_employee_id or "")
            ]
        )
        for record in records:
            fit_font_size(record.full_name, font_name)
            if record.requested_employee_id:
                fit_font_size(record.requested_employee_id, font_name)
        fit_font_size(f"{args.id_prefix}9999", font_name)

        used_ids = collect_used_ids(output_dir, args.id_prefix)
        requested_ids: set[str] = set()
        for record in records:
            employee_id = record.requested_employee_id
            if not employee_id:
                continue
            if employee_id in used_ids:
                raise ValueError(
                    f"Dong {record.line_number}: Employee ID {employee_id!r} da ton tai."
                )
            if employee_id in requested_ids:
                raise ValueError(
                    f"Dong {record.line_number}: Employee ID {employee_id!r} bi lap."
                )
            requested_ids.add(employee_id)
        used_ids.update(requested_ids)

        number = next_pdf_number(output_dir)
        created: list[tuple[Path, str, str]] = []

        for record in records:
            while (output_dir / f"{number}.pdf").exists():
                number += 1
            output_path = output_dir / f"{number}.pdf"
            employee_id = record.requested_employee_id or random_employee_id(
                args.id_prefix, used_ids
            )

            create_payslip(
                template_bytes,
                template_path,
                output_path,
                record.full_name,
                employee_id,
                font_name,
            )
            try:
                append_result_line(
                    result_path, result_line(record.full_name, output_path)
                )
            except Exception:
                # Keep output and log consistent if appending the result fails.
                output_path.unlink(missing_ok=True)
                raise

            created.append((output_path, record.full_name, employee_id))
            number += 1

        print(f"Da tao {len(created)} file PDF:")
        for output_path, full_name, employee_id in created:
            print(f"  {output_path.name}: {full_name} | {employee_id}")
        print(f"Thu muc PDF: {output_dir}")
        print(f"File ket qua: {result_path}")
        return 0
    except Exception as exc:
        print(f"LOI: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
