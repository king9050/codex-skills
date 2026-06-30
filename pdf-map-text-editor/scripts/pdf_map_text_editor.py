#!/usr/bin/env python3
"""Edit text on map/figure PDFs with style-aware visible overlays.

Subcommands:
- scan: extract text, coordinates, font names, sizes, and colors when available
- apply: add or cover-and-rewrite arbitrary text from JSON operations
- breach-summary: preset workflow for flood-map breach summaries
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any


DEFAULT_X = 1008.285706
DEFAULT_Y = 609.5
DEFAULT_FONT_SIZE = 7.47
DEFAULT_TEMPLATE = "{name}{level}级堤防，其溃口宽度为{width}m。"

FONT_CANDIDATES = [
    ("song", "/System/Library/Fonts/Supplemental/Songti.ttc", 6, "Songti SC Regular"),
    ("hei", "/System/Library/Fonts/STHeiti Light.ttc", 0, "STHeiti Light"),
    ("hei", "/System/Library/Fonts/STHeiti Medium.ttc", 0, "STHeiti Medium"),
    ("sans", "/System/Library/Fonts/Supplemental/NotoSansCJKsc-Regular.otf", 0, "Noto Sans CJK SC"),
]


@dataclass(frozen=True)
class TextBlock:
    page: int
    text: str
    x: float | None
    y: float | None
    font: str | None
    font_size: float | None
    color: str

    def distance_to(self, x: float, y: float) -> float:
        if self.x is None or self.y is None:
            return math.inf
        return math.hypot(self.x - x, self.y - y)


@dataclass(frozen=True)
class BreachInfo:
    page: int
    name: str
    level: str
    width: str

    def summary(self, template: str) -> str:
        return template.format(name=self.name, level=self.level, width=self.width)


def import_pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: pypdf. Install it with `python3 -m pip install pypdf`.") from exc
    return PdfReader, PdfWriter


def import_reportlab():
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: reportlab. Install it with `python3 -m pip install reportlab`.") from exc
    return pdfmetrics, TTFont, canvas


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan and edit text on map/figure PDFs with visible, style-aware overlays.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Extract text blocks and style hints from a PDF.")
    scan.add_argument("input_pdf", type=Path)
    scan.add_argument("--output", type=Path, help="JSON output path. Defaults to stdout.")
    scan.add_argument("--pages", help="Page selection such as '1,24-30'.")
    scan.add_argument("--text-filter", help="Only include blocks containing this substring.")

    apply = sub.add_parser("apply", help="Apply arbitrary text overlay/edit operations from JSON.")
    apply.add_argument("input_pdf", type=Path)
    apply.add_argument("--operations", type=Path, required=True, help="JSON list of edit operations.")
    apply.add_argument("--output", type=Path, required=True)
    apply.add_argument("--font", type=Path, help="Default Chinese TTF/TTC font path.")
    apply.add_argument("--font-subfont-index", type=int, default=0)
    apply.add_argument("--default-font-size", type=float, default=DEFAULT_FONT_SIZE)
    apply.add_argument("--default-color", default="#000000")
    apply.add_argument("--dry-run", action="store_true", help="Validate and summarize operations without writing PDF.")

    add = sub.add_parser("add", help="Add one text operation without creating a JSON file.")
    add.add_argument("input_pdf", type=Path)
    add.add_argument("--page", type=int, required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--x", type=float, required=True)
    add.add_argument("--y", type=float, required=True)
    add.add_argument("--output", type=Path, required=True)
    add.add_argument("--font-size", type=float)
    add.add_argument("--color")
    add.add_argument("--match-near", help="Coordinate 'x,y' used to infer font size/color from nearby existing text.")
    add.add_argument("--cover", help="Rectangle 'x,y,width,height[,fill]' to cover old text before adding new text.")
    add.add_argument("--align", choices=["left", "center", "right"], default="left")
    add.add_argument("--max-width", type=float)
    add.add_argument("--font", type=Path)
    add.add_argument("--font-subfont-index", type=int, default=0)

    breach = sub.add_parser("breach-summary", help="Preset: extract breach info and write formatted summaries.")
    breach.add_argument("input_pdf", type=Path)
    breach.add_argument("--output", type=Path)
    breach.add_argument("--report", type=Path)
    breach.add_argument("--dry-run", action="store_true")
    breach.add_argument("--pages", help="Page selection such as '24-93'.")
    breach.add_argument("--x", type=float, default=DEFAULT_X)
    breach.add_argument("--y", type=float, default=DEFAULT_Y)
    breach.add_argument("--font-size", type=float, default=DEFAULT_FONT_SIZE)
    breach.add_argument("--template", default=DEFAULT_TEMPLATE)
    breach.add_argument("--font", type=Path)
    breach.add_argument("--font-subfont-index", type=int, default=0)

    return parser.parse_args()


def parse_page_selection(spec: str | None, page_count: int) -> set[int] | None:
    if not spec:
        return None
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            if start > end:
                raise ValueError(f"Invalid descending page range: {part}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))
    invalid = sorted(p for p in selected if p < 1 or p > page_count)
    if invalid:
        raise ValueError(f"Page selection outside PDF range: {invalid[:10]}")
    return selected


def parse_xy(value: str) -> tuple[float, float]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2:
        raise ValueError(f"Expected 'x,y', got: {value}")
    return float(parts[0]), float(parts[1])


def parse_cover(value: str) -> dict[str, Any]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) not in (4, 5):
        raise ValueError(f"Expected 'x,y,width,height[,fill]', got: {value}")
    cover = {
        "x": float(parts[0]),
        "y": float(parts[1]),
        "width": float(parts[2]),
        "height": float(parts[3]),
    }
    if len(parts) == 5:
        cover["fill"] = parts[4]
    return cover


def normalize_hex_color(value: str | None, default: str = "#000000") -> str:
    if not value:
        return default
    value = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return value.upper()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return f"#{value.upper()}"
    if value.lower() in {"black", "k"}:
        return "#000000"
    if value.lower() in {"white", "w"}:
        return "#FFFFFF"
    return default


def hex_to_rgb01(value: str) -> tuple[float, float, float]:
    color = normalize_hex_color(value)
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def decode_hex(hex_bytes: bytes) -> str:
    raw = bytes.fromhex(hex_bytes.decode("ascii"))
    for encoding in ("gbk", "utf-16-be", "utf-8", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin1", "replace")


def decode_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == 92 and i + 1 < len(raw):
            i += 1
            out.append(raw[i])
        else:
            out.append(char)
        i += 1
    return out.decode("latin1", "replace")


def extract_color(block: bytes) -> str:
    rgb_matches = re.findall(rb"([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+rg", block)
    if rgb_matches:
        r, g, b = (float(v) for v in rgb_matches[-1])
        return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))
    gray_matches = re.findall(rb"([0-9.]+)\s+g", block)
    if gray_matches:
        gray = round(float(gray_matches[-1]) * 255)
        return "#%02X%02X%02X" % (gray, gray, gray)
    return "#000000"


def decode_text_from_block(block: bytes) -> str:
    text = ""
    for array_match in re.finditer(rb"\[(.*?)\]\s*TJ", block, re.S):
        array_body = array_match.group(1)
        for token in re.finditer(rb"<([0-9A-Fa-f]*)>|\((.*?)\)", array_body, re.S):
            if token.group(1) is not None:
                text += decode_hex(token.group(1))
            else:
                text += decode_literal(token.group(2))
    for token in re.finditer(rb"<([0-9A-Fa-f]*)>\s*Tj|\((.*?)\)\s*Tj", block, re.S):
        if token.group(1) is not None:
            text += decode_hex(token.group(1))
        else:
            text += decode_literal(token.group(2))
    return text


def extract_content_stream_blocks(page: Any, page_number: int) -> list[TextBlock]:
    try:
        contents = page.get_contents()
        if contents is None:
            return []
        data = contents.get_data()
    except Exception:
        return []

    blocks: list[TextBlock] = []
    for match in re.finditer(rb"BT\s*(.*?)\s*ET", data, re.S):
        block = match.group(1)
        text = decode_text_from_block(block)
        if not text:
            continue

        tm = re.search(rb"([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s+([0-9.\-]+)\s+Tm", block)
        font_match = re.search(rb"/([A-Za-z0-9._+-]+)\s+([0-9.]+)\s+Tf", block)
        x = float(tm.group(5)) if tm else None
        y = float(tm.group(6)) if tm else None
        font = font_match.group(1).decode("latin1", "replace") if font_match else None
        font_size = float(font_match.group(2)) if font_match else None
        blocks.append(TextBlock(page_number, text, x, y, font, font_size, extract_color(block)))
    return blocks


def extract_blocks(reader: Any, selected_pages: set[int] | None = None, text_filter: str | None = None) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        if selected_pages is not None and page_number not in selected_pages:
            continue
        page_blocks = extract_content_stream_blocks(page, page_number)
        if text_filter:
            page_blocks = [b for b in page_blocks if text_filter in b.text]
        blocks.extend(page_blocks)
    return blocks


def page_text_from_blocks(page: Any, page_number: int) -> str:
    texts = [block.text for block in extract_content_stream_blocks(page, page_number)]
    try:
        extracted = page.extract_text() or ""
    except Exception:
        extracted = ""
    if extracted:
        texts.append(extracted)
    return "\n".join(texts)


def block_to_dict(block: TextBlock) -> dict[str, Any]:
    return {
        "page": block.page,
        "text": block.text,
        "x": block.x,
        "y": block.y,
        "font": block.font,
        "font_size": block.font_size,
        "color": block.color,
    }


def find_nearest_block(blocks: list[TextBlock], page: int, x: float, y: float, radius: float | None = None) -> TextBlock | None:
    same_page = [b for b in blocks if b.page == page and b.x is not None and b.y is not None]
    if not same_page:
        return None
    nearest = min(same_page, key=lambda b: b.distance_to(x, y))
    if radius is not None and nearest.distance_to(x, y) > radius:
        return None
    return nearest


def choose_font_kind(font_hint: str | None) -> str:
    hint = (font_hint or "").lower()
    if any(token in hint for token in ("song", "simsun", "serif", "ming")):
        return "song"
    if any(token in hint for token in ("hei", "heiti", "sans", "gothic")):
        return "hei"
    return "song"


def register_font(font_path: Path | None, subfont_index: int, font_kind: str = "song") -> str:
    pdfmetrics, TTFont, _canvas = import_reportlab()
    if font_path:
        if not font_path.exists():
            raise FileNotFoundError(f"Font not found: {font_path}")
        font_name = "MapTextEditorFont"
        pdfmetrics.registerFont(TTFont(font_name, str(font_path), subfontIndex=subfont_index))
        return font_name

    ordered = [item for item in FONT_CANDIDATES if item[0] == font_kind]
    ordered += [item for item in FONT_CANDIDATES if item[0] != font_kind]
    for _kind, path_s, index, _label in ordered:
        path = Path(path_s)
        if not path.exists():
            continue
        try:
            font_name = f"MapTextEditorFont_{index}_{abs(hash(path_s))}"
            pdfmetrics.registerFont(TTFont(font_name, str(path), subfontIndex=index))
            return font_name
        except Exception:
            continue
    raise RuntimeError("No usable Chinese TTF/TTC font found. Pass --font /path/to/chinese-font.ttf.")


def wrap_line(text: str, max_width: float | None, font_name: str, font_size: float, pdfmetrics: Any) -> list[str]:
    if not max_width:
        return [text]
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def normalize_operation(raw: dict[str, Any], defaults: argparse.Namespace | None = None) -> dict[str, Any]:
    op = dict(raw)
    if "page" not in op or "text" not in op or "x" not in op or "y" not in op:
        raise ValueError(f"Operation requires page, text, x, y: {raw}")
    op["page"] = int(op["page"])
    op["x"] = float(op["x"])
    op["y"] = float(op["y"])
    if "font_size" in op and op["font_size"] is not None:
        op["font_size"] = float(op["font_size"])
    elif defaults is not None and hasattr(defaults, "default_font_size"):
        op["font_size"] = None
    if "max_width" in op and op["max_width"] is not None:
        op["max_width"] = float(op["max_width"])
    if "line_spacing" in op and op["line_spacing"] is not None:
        op["line_spacing"] = float(op["line_spacing"])
    return op


def resolve_operation_styles(operations: list[dict[str, Any]], blocks: list[TextBlock], default_font_size: float, default_color: str) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for op in operations:
        styled = dict(op)
        near = styled.get("match_near")
        match_x = styled["x"]
        match_y = styled["y"]
        radius = None
        if isinstance(near, dict):
            match_x = float(near.get("x", match_x))
            match_y = float(near.get("y", match_y))
            radius = float(near["radius"]) if "radius" in near else None
        elif isinstance(near, (list, tuple)) and len(near) >= 2:
            match_x = float(near[0])
            match_y = float(near[1])
        nearest = find_nearest_block(blocks, styled["page"], match_x, match_y, radius)

        styled["_matched_block"] = block_to_dict(nearest) if nearest else None
        styled["_font_kind"] = choose_font_kind(nearest.font if nearest else None)
        if not styled.get("font_size"):
            styled["font_size"] = nearest.font_size if nearest and nearest.font_size else default_font_size
        styled["color"] = normalize_hex_color(styled.get("color") or (nearest.color if nearest else default_color), default_color)
        styled["align"] = styled.get("align", "left")
        styled["line_spacing"] = float(styled.get("line_spacing", 1.2))
        resolved.append(styled)
    return resolved


def build_overlay_pdf(reader: Any, operations: list[dict[str, Any]], args: argparse.Namespace) -> Any:
    pdfmetrics, _TTFont, canvas = import_reportlab()
    buffer = BytesIO()
    first_page = reader.pages[0]
    canvas_obj = canvas.Canvas(buffer, pagesize=(float(first_page.mediabox.width), float(first_page.mediabox.height)))
    canvas_obj.setTitle("map-text-editor-overlay")
    ops_by_page: dict[int, list[dict[str, Any]]] = {}
    font_cache: dict[str, str] = {}
    for op in operations:
        ops_by_page.setdefault(op["page"], []).append(op)

    default_font_path = getattr(args, "font", None)
    default_subfont_index = int(getattr(args, "font_subfont_index", 0))

    for page_number, page in enumerate(reader.pages, start=1):
        canvas_obj.setPageSize((float(page.mediabox.width), float(page.mediabox.height)))
        for op in ops_by_page.get(page_number, []):
            cover = op.get("cover")
            if isinstance(cover, dict):
                fill = normalize_hex_color(cover.get("fill", "#FFFFFF"), "#FFFFFF")
                canvas_obj.setFillColorRGB(*hex_to_rgb01(fill))
                canvas_obj.setStrokeColorRGB(*hex_to_rgb01(fill))
                canvas_obj.rect(float(cover["x"]), float(cover["y"]), float(cover["width"]), float(cover["height"]), fill=1, stroke=0)

            font_key = str(op.get("_font_kind", "song"))
            if default_font_path:
                font_key = f"user:{default_font_path}:{default_subfont_index}"
            if font_key not in font_cache:
                font_cache[font_key] = register_font(default_font_path, default_subfont_index, op.get("_font_kind", "song"))
            font_name = font_cache[font_key]
            font_size = float(op["font_size"])
            canvas_obj.setFont(font_name, font_size)
            canvas_obj.setFillColorRGB(*hex_to_rgb01(op.get("color", "#000000")))

            raw_lines = str(op["text"]).splitlines() or [""]
            lines: list[str] = []
            for line in raw_lines:
                lines.extend(wrap_line(line, op.get("max_width"), font_name, font_size, pdfmetrics))
            line_height = font_size * float(op.get("line_spacing", 1.2))
            for index, line in enumerate(lines):
                x = float(op["x"])
                y = float(op["y"]) - index * line_height
                align = op.get("align", "left")
                if align != "left":
                    width = pdfmetrics.stringWidth(line, font_name, font_size)
                    if align == "center":
                        x -= width / 2
                    elif align == "right":
                        x -= width
                canvas_obj.drawString(x, y, line)
        canvas_obj.showPage()

    canvas_obj.save()
    buffer.seek(0)
    PdfReader, _PdfWriter = import_pypdf()
    return PdfReader(buffer)


def write_pdf_with_operations(input_pdf: Path, output_pdf: Path, operations: list[dict[str, Any]], args: argparse.Namespace) -> None:
    PdfReader, PdfWriter = import_pypdf()
    reader = PdfReader(str(input_pdf))
    blocks = extract_blocks(reader)
    default_font_size = float(getattr(args, "default_font_size", DEFAULT_FONT_SIZE))
    default_color = getattr(args, "default_color", "#000000")
    resolved = resolve_operation_styles(operations, blocks, default_font_size, default_color)
    overlay = build_overlay_pdf(reader, resolved, args)

    writer = PdfWriter()
    touched_pages = {op["page"] for op in resolved}
    for page_number, page in enumerate(reader.pages, start=1):
        if page_number in touched_pages:
            page.merge_page(overlay.pages[page_number - 1])
        writer.add_page(page)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with output_pdf.open("wb") as handle:
        writer.write(handle)


def default_output_path(input_pdf: Path) -> Path:
    return input_pdf.with_name(f"{input_pdf.stem}_文字编辑{input_pdf.suffix}")


def default_breach_output_path(input_pdf: Path) -> Path:
    return input_pdf.with_name(f"{input_pdf.stem}_补充溃口信息{input_pdf.suffix}")


def default_breach_report_path(input_pdf: Path) -> Path:
    return input_pdf.with_name(f"{input_pdf.stem}_溃口信息写回清单.tsv")


def extract_title_dike(text: str) -> str | None:
    for line in text.splitlines():
        if "溃口" not in line:
            continue
        match = re.search(r"(高桥圩|红桥堤|九合联圩|立新圩|三角联圩|[\u4e00-\u9fa5]{2,8}(?:圩|堤))溃口洪水", line)
        if match:
            return match.group(1)
    return None


def infer_raw_breach_info(text: str) -> tuple[str | None, str | None, str | None]:
    width_match = re.search(r"溃口宽度[：:]?\s*([0-9.]+)\s*m", text)
    dike_match = re.search(r"([\u4e00-\u9fa5]{2,8}(?:圩|堤))[：:]\s*([0-9]+)\s*级堤防", text)
    width = width_match.group(1) if width_match else None
    name = dike_match.group(1) if dike_match else extract_title_dike(text)
    level = dike_match.group(2) if dike_match else None
    return name, level, width


def infer_breach_infos(reader: Any, selected_pages: set[int] | None) -> list[BreachInfo]:
    raw_by_page: dict[int, tuple[str | None, str | None, str | None]] = {}
    level_cache: dict[str, str] = {}
    for page_number, page in enumerate(reader.pages, start=1):
        if selected_pages is not None and page_number not in selected_pages:
            continue
        text = page_text_from_blocks(page, page_number)
        if "溃口" not in text:
            continue
        name, level, width = infer_raw_breach_info(text)
        raw_by_page[page_number] = (name, level, width)
        if name and level:
            level_cache[name] = level

    infos: list[BreachInfo] = []
    for page_number in sorted(raw_by_page):
        name, level, width = raw_by_page[page_number]
        if name and not level:
            level = level_cache.get(name)
        if name and level and width:
            infos.append(BreachInfo(page_number, name, level, width))
    return infos


def write_breach_report(report_path: Path, infos: list[BreachInfo], template: str) -> None:
    rows = [("pdf_page", "name", "level", "width_m", "summary")]
    for info in infos:
        rows.append((str(info.page), info.name, info.level, info.width, info.summary(template)))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8")


def command_scan(args: argparse.Namespace) -> int:
    PdfReader, _PdfWriter = import_pypdf()
    input_pdf = args.input_pdf.expanduser().resolve()
    reader = PdfReader(str(input_pdf))
    selected = parse_page_selection(args.pages, len(reader.pages))
    blocks = extract_blocks(reader, selected, args.text_filter)
    payload = {
        "input_pdf": str(input_pdf),
        "page_count": len(reader.pages),
        "blocks": [block_to_dict(block) for block in blocks],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.expanduser().resolve().write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def command_apply(args: argparse.Namespace) -> int:
    input_pdf = args.input_pdf.expanduser().resolve()
    operations_raw = json.loads(args.operations.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(operations_raw, list):
        raise ValueError("Operations JSON must be a list.")
    operations = [normalize_operation(op, args) for op in operations_raw]
    print(f"operations={len(operations)}")
    if args.dry_run:
        return 0
    write_pdf_with_operations(input_pdf, args.output.expanduser().resolve(), operations, args)
    print(f"output={args.output.expanduser().resolve()}")
    return 0


def command_add(args: argparse.Namespace) -> int:
    op: dict[str, Any] = {
        "page": args.page,
        "text": args.text,
        "x": args.x,
        "y": args.y,
        "align": args.align,
    }
    if args.font_size:
        op["font_size"] = args.font_size
    if args.color:
        op["color"] = args.color
    if args.match_near:
        mx, my = parse_xy(args.match_near)
        op["match_near"] = {"x": mx, "y": my}
    if args.cover:
        op["cover"] = parse_cover(args.cover)
    if args.max_width:
        op["max_width"] = args.max_width
    setattr(args, "default_font_size", DEFAULT_FONT_SIZE)
    setattr(args, "default_color", "#000000")
    write_pdf_with_operations(args.input_pdf.expanduser().resolve(), args.output.expanduser().resolve(), [normalize_operation(op, args)], args)
    print(f"output={args.output.expanduser().resolve()}")
    return 0


def command_breach_summary(args: argparse.Namespace) -> int:
    PdfReader, _PdfWriter = import_pypdf()
    input_pdf = args.input_pdf.expanduser().resolve()
    reader = PdfReader(str(input_pdf))
    selected = parse_page_selection(args.pages, len(reader.pages))
    infos = infer_breach_infos(reader, selected)
    report_path = args.report.expanduser().resolve() if args.report else default_breach_report_path(input_pdf)
    write_breach_report(report_path, infos, args.template)
    print(f"input={input_pdf}")
    print(f"report={report_path}")
    print(f"pages_scanned={len(reader.pages) if selected is None else len(selected)}")
    print(f"pages_updated={len(infos)}")
    print(f"by_name={dict(Counter(info.name for info in infos))}")
    if args.dry_run:
        return 0

    operations = [
        {
            "page": info.page,
            "text": info.summary(args.template),
            "x": args.x,
            "y": args.y,
            "font_size": args.font_size,
            "color": "#000000",
            "align": "left",
        }
        for info in infos
    ]
    setattr(args, "default_font_size", args.font_size)
    setattr(args, "default_color", "#000000")
    output_pdf = args.output.expanduser().resolve() if args.output else default_breach_output_path(input_pdf)
    write_pdf_with_operations(input_pdf, output_pdf, operations, args)
    print(f"output={output_pdf}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "scan":
            return command_scan(args)
        if args.command == "apply":
            return command_apply(args)
        if args.command == "add":
            return command_add(args)
        if args.command == "breach-summary":
            return command_breach_summary(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
