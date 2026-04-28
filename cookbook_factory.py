#!/usr/bin/env python3
"""
Multimedia Cookbook Factory — GUI edition
Full-bleed food photos with per-page recipe name + content.
Outputs an interactive HTML flipbook and an MP4 video.
"""

import base64
import re
import shutil
import subprocess
import threading
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk


# ── constants ──────────────────────────────────────────────────────────────────

SPREAD_W, SPREAD_H = 1920, 1080
PAGE_W,   PAGE_H   = 960,  1080

PAGE_DUR  = 4
TRANS_DUR = 1

SUPPORTED_IMG = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".bmp", ".tiff", ".tif", ".gif",
}

FONT_CHOICES = [
    "Georgia",
    "Arial",
    "Times New Roman",
    "Courier New",
    "Palatino Linotype",
    "Custom (.ttf file)…",
]

# ── colour theme — warm terracotta kitchen palette ──────────────────────────────
BG     = "#120c08"
PANEL  = "#1e1208"
CARD   = "#251808"
ACCENT = "#d4783a"
BTN    = "#8a3a18"
BTNHOV = "#a84820"
FG     = "#f0e0d0"
MUTED  = "#9a7858"
ENTRY  = "#1a1008"
DANGER = "#c03020"

# ── JS library cache ───────────────────────────────────────────────────────────

_JS_LIBS = {
    "page-flip":   "https://cdn.jsdelivr.net/npm/page-flip@2.0.7/dist/js/page-flip.browser.js",
    "jspdf":       "https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js",
    "html2canvas": "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js",
}

def _get_js(name, cache_dir, log=None):
    """
    Return JS library text, downloading and caching on first use.
    cache_dir — where to store the .js files (chosen by the user, NOT hardcoded to C:).
    Three-strategy SSL fallback fixes macOS bundled-app cert errors.
    """
    import ssl
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{name}.js"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    url = _JS_LIBS[name]
    if log:
        log(f"  Downloading {name} (one-time) …")

    def _fetch(ctx=None):
        with urllib.request.urlopen(url, timeout=30,
                                    **({"context": ctx} if ctx else {})) as r:
            return r.read().decode("utf-8")

    content = last_err = None
    try:
        content = _fetch()
    except Exception as e:
        last_err = e
    if content is None:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
            content = _fetch(ctx)
        except Exception as e:
            last_err = e
    if content is None:
        try:
            if log:
                log(f"  SSL warning: unverified fallback for {name}")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            content = _fetch(ctx)
        except Exception as e:
            last_err = e
    if content is None:
        raise RuntimeError(f"Could not download {name}.\nDetail: {last_err}")
    cached.write_text(content, encoding="utf-8")
    return content


# ── recipe file parser ─────────────────────────────────────────────────────────

def _read_docx(path):
    """Extract text from .docx, promoting heading paragraphs to markdown headers."""
    try:
        import docx
    except ImportError:
        raise RuntimeError(
            "python-docx is not installed.\nRun:  pip install python-docx\n"
            "Then restart the app.")
    doc = docx.Document(str(path))
    lines = []
    for para in doc.paragraphs:
        t = para.text
        if not t.strip():
            lines.append("")
            continue
        style = (para.style.name or "").lower() if para.style else ""
        if "heading 1" in style or "title" in style:
            lines.append(f"# {t.strip()}")
        elif "heading 2" in style:
            lines.append(f"## {t.strip()}")
        else:
            lines.append(t)
    return "\n".join(lines)


def _read_pdf(path):
    """Extract plain text from a PDF using pypdf."""
    try:
        import pypdf
    except ImportError:
        raise RuntimeError(
            "pypdf is not installed.\nRun:  pip install pypdf\n"
            "Then restart the app.")
    parts = []
    with open(path, "rb") as fh:
        reader = pypdf.PdfReader(fh)
        for page in reader.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def extract_images_from_pdf(path, log=None):
    """
    Extract all embedded images from a PDF into a temp folder.
    Returns sorted list of Path objects.
    Uses pypdf >= 3.x  page.images API.
    """
    try:
        import pypdf
    except ImportError:
        return []

    import tempfile
    out_dir = Path(tempfile.mkdtemp(prefix="cb_pdf_imgs_"))
    saved   = []

    try:
        with open(path, "rb") as fh:
            reader = pypdf.PdfReader(fh)
            for pg_num, page in enumerate(reader.pages):
                try:
                    page_imgs = page.images          # pypdf >= 3.0
                except AttributeError:
                    continue                          # older pypdf — skip
                for img_obj in page_imgs:
                    try:
                        safe_name = re.sub(r'[^\w\-.]', '_', img_obj.name or "img")
                        out_path  = out_dir / f"p{pg_num:03d}_{safe_name}"
                        # Ensure a supported image extension
                        if not out_path.suffix.lower() in SUPPORTED_IMG:
                            out_path = out_path.with_suffix(".png")
                        img_obj.image.save(str(out_path))
                        saved.append(out_path)
                        if log:
                            log(f"  Extracted: {out_path.name}")
                    except Exception:
                        pass
    except Exception:
        pass

    return sorted(saved, key=lambda p: p.name)


def _is_title_line(line, next_line=""):
    """Return True if this line looks like a recipe title."""
    s = line.strip()
    if not s or len(s) > 80:
        return False
    # Underlined header (next line is --- or ===)
    if next_line and re.match(r'^[-=]{3,}\s*$', next_line.strip()):
        return True
    # Markdown header
    if re.match(r'^#{1,3}\s+\S', s):
        return True
    # Numbered: "1. Name" or "1) Name"
    if re.match(r'^\d+[\.\)]\s+\S', s) and len(s) < 80:
        return True
    # "Recipe N:" pattern
    if re.match(r'^Recipe\s+\d+\s*[:\-]', s, re.IGNORECASE):
        return True
    return False


def _clean_title(line):
    """Strip markdown #, leading numbers, etc. from a detected title line."""
    s = line.strip()
    s = re.sub(r'^#{1,3}\s+', '', s)
    s = re.sub(r'^\d+[\.\)]\s+', '', s)
    s = re.sub(r'^Recipe\s+\d+\s*[:\-]\s*', '', s, flags=re.IGNORECASE)
    return s.strip()


def _split_by_allcaps(text):
    """Fallback splitter: ALL-CAPS short lines become recipe headers."""
    lines  = text.splitlines()
    result = []
    cur_name, cur_body = "", []

    def _flush():
        body = "\n".join(cur_body).strip()
        if cur_name or body:
            result.append({"name": cur_name, "text": body})

    for line in lines:
        s = line.strip()
        if (s and s == s.upper() and 6 <= len(s) <= 60
                and not re.match(r'^[-=*\s\d]+$', s)
                and not re.match(r'^(NOTE|TIP|TIPS|YIELD|SERVES|MAKES)\b', s)):
            _flush()
            cur_name = s.title()   # ALL CAPS → Title Case
            cur_body = []
        else:
            cur_body.append(line)
    _flush()
    return result


def _split_into_recipes(text):
    """Split raw text into [{"name": str, "text": str}, ...]."""
    lines = text.splitlines()
    N     = len(lines)
    result, cur_name, cur_body = [], "", []

    def _flush():
        body = "\n".join(cur_body).strip()
        if cur_name or body:
            result.append({"name": cur_name, "text": body})

    i = 0
    while i < N:
        line = lines[i]
        nxt  = lines[i + 1] if i + 1 < N else ""

        # Bare separator line: ---, ===, ***
        if re.match(r'^[-=*]{3,}\s*$', line.strip()):
            _flush()
            cur_name, cur_body = "", []
            i += 1
            # Skip blanks; next non-blank may be a name
            while i < N and not lines[i].strip():
                i += 1
            if i < N:
                nxt2 = lines[i + 1] if i + 1 < N else ""
                if _is_title_line(lines[i], nxt2):
                    cur_name = _clean_title(lines[i])
                    i += 1
                    if i < N and re.match(r'^[-=]{3,}\s*$', lines[i].strip()):
                        i += 1   # skip underline
                else:
                    cur_body.append(lines[i])
                    i += 1
            continue

        # Title line (markdown, numbered, underlined)
        if _is_title_line(line, nxt):
            _flush()
            cur_name, cur_body = _clean_title(line), []
            i += 1
            # Skip underline
            if i < N and re.match(r'^[-=]{3,}\s*$', lines[i].strip()):
                i += 1
            continue

        cur_body.append(line)
        i += 1

    _flush()

    # Fallback: if we got only 1 recipe and text is long, try ALL-CAPS split
    if len(result) <= 1 and len(text) > 400:
        caps = _split_by_allcaps(text)
        if len(caps) > 1:
            return caps

    return result if result else [{"name": "", "text": text.strip()}]


def parse_recipes_from_file(path):
    """
    Parse a .txt, .docx, or .pdf file into recipe dicts.
    Returns:
      [{"name": str, "text": str}, ...]  — normal result
      None                               — PDF is scanned (no text, may have images)
    """
    path   = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        raw = path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".docx":
        raw = _read_docx(path)
    elif suffix == ".pdf":
        raw = _read_pdf(path)
        if not raw.strip():
            # Scanned PDF — caller should try extract_images_from_pdf instead
            return None
    else:
        raise ValueError(
            f"Unsupported file type: {suffix}\nSupported formats: .txt  .docx  .pdf")

    if not raw.strip():
        raise ValueError("The file appears to be empty or has no readable text.")

    return _split_into_recipes(raw)


# ───────────────────────────────────────────────────────────────────────────────

_FFMPEG_FALLBACKS = [r"C:\Program Files\ShareX\ffmpeg.exe"]

def _find_ffmpeg():
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    for fb in _FFMPEG_FALLBACKS:
        if Path(fb).is_file():
            return fb
    return None

FFMPEG = _find_ffmpeg()

# Preferred page-turn transitions in order — first one supported wins
_XFADE_PREFERENCE = ["pagecurlup", "fade"]

def _best_xfade_transition(ffmpeg_path):
    """
    Return the best xfade transition this FFmpeg build actually supports.
    Older/stripped builds (e.g. ShareX) don't have pagecurlup — fall back to fade.
    Result is cached after the first call.
    """
    if not ffmpeg_path:
        return "fade"
    for transition in _XFADE_PREFERENCE:
        try:
            r = subprocess.run(
                [ffmpeg_path,
                 "-f", "lavfi", "-i", "color=black:s=4x4:d=2",
                 "-f", "lavfi", "-i", "color=black:s=4x4:d=2",
                 "-filter_complex",
                 f"[0][1]xfade=transition={transition}:duration=1:offset=1[v]",
                 "-map", "[v]", "-f", "null", "-"],
                capture_output=True, timeout=10)
            if r.returncode == 0:
                return transition
        except Exception:
            pass
    return "fade"   # universal fallback


# ── image helpers ──────────────────────────────────────────────────────────────

def get_images(folder):
    return sorted(
        [f for f in Path(folder).iterdir() if f.suffix.lower() in SUPPORTED_IMG],
        key=lambda f: f.name,
    )

def load_font(name_or_path, size):
    if isinstance(name_or_path, Path):
        try:
            return ImageFont.truetype(str(name_or_path), size)
        except (OSError, IOError):
            pass
    else:
        for candidate in [
            f"C:/Windows/Fonts/{name_or_path}.ttf",
            f"C:/Windows/Fonts/{name_or_path.replace(' ', '')}.ttf",
            f"C:/Windows/Fonts/{name_or_path.replace(' ', '').lower()}.ttf",
        ]:
            try:
                return ImageFont.truetype(candidate, size)
            except (OSError, IOError):
                pass
    return ImageFont.load_default()

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def fit_bg(img_path, w, h):
    src = Image.open(img_path).convert("RGB")
    r   = src.width / src.height
    tr  = w / h
    bw, bh = (w, int(w / r)) if r > tr else (int(h * r), h)
    bg = src.resize((bw * 2, bh * 2), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    bg = bg.resize((w, h), Image.LANCZOS)
    fw, fh = (w, int(w / r)) if r > tr else (int(h * r), h)
    fg = src.resize((fw, fh), Image.LANCZOS)
    canvas = bg.copy()
    canvas.paste(fg, ((w - fw) // 2, (h - fh) // 2))
    return canvas

def wrap_lines(text, font, draw, max_w):
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split():
            test = f"{cur} {word}".strip()
            if draw.textbbox((0, 0), test, font=font)[2] <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines

def wrap_and_fit(text, font_path, max_pt, min_pt, box_w, box_h):
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for pt in range(max_pt, min_pt - 1, -2):
        font  = load_font(font_path, pt)
        lines = wrap_lines(text, font, dummy, box_w)
        if len(lines) * (font.size * 1.65) <= box_h:
            return font, lines
    return load_font(font_path, min_pt), wrap_lines(text, load_font(font_path, min_pt), dummy, box_w)

def gradient_band(w, h, alpha_max=195):
    band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd   = ImageDraw.Draw(band)
    for row in range(h):
        bd.line([(0, row), (w, row)], fill=(0, 0, 0, int(alpha_max * row / h)))
    return band


# ── frame helpers ───────────────────────────────────────────────────────────────

def apply_frame(img, style, color_hex, thickness, frame_img_path=None):
    """Decorative frame on a PIL Image. Same engine as journal factory."""
    if style == "None" and not frame_img_path:
        return img
    w, h   = img.size
    draw   = ImageDraw.Draw(img)
    color  = hex_to_rgb(color_hex)
    t      = max(1, thickness)
    pad    = 18

    if style == "Simple":
        for i in range(t):
            draw.rectangle([pad+i, pad+i, w-pad-i-1, h-pad-i-1], outline=color)

    elif style == "Double":
        gap = max(4, t * 2)
        for i in range(t):
            draw.rectangle([pad+i, pad+i, w-pad-i-1, h-pad-i-1], outline=color)
            draw.rectangle([pad+gap+i, pad+gap+i, w-pad-gap-i-1, h-pad-gap-i-1], outline=color)

    elif style == "Vintage":
        gap = max(5, t * 2)
        for i in range(t):
            draw.rectangle([pad+i, pad+i, w-pad-i-1, h-pad-i-1], outline=color)
            draw.rectangle([pad+gap+i, pad+gap+i, w-pad-gap-i-1, h-pad-gap-i-1], outline=color)
        for cx, cy in [(pad, pad), (w-pad-gap*2, pad),
                       (pad, h-pad-gap*2), (w-pad-gap*2, h-pad-gap*2)]:
            draw.rectangle([cx, cy, cx+gap*2, cy+gap*2], outline=color, width=t)

    elif style == "Ornate Corners":
        for i in range(t):
            draw.rectangle([pad+i, pad+i, w-pad-i-1, h-pad-i-1], outline=color)
        arm = min(80, w // 8)
        corners = [
            (pad, pad, pad+arm, pad, pad, pad+arm),
            (w-pad-arm, pad, w-pad, pad, w-pad, pad+arm),
            (pad, h-pad-arm, pad, h-pad, pad+arm, h-pad),
            (w-pad-arm, h-pad, w-pad, h-pad, w-pad, h-pad-arm),
        ]
        for x1,y1,x2,y2,x3,y3 in corners:
            draw.line([(x1,y1),(x2,y2)], fill=color, width=t*2)
            draw.line([(x1,y1),(x3,y3)], fill=color, width=t*2)
        mid_pts = [(w//2, pad), (w//2, h-pad), (pad, h//2), (w-pad, h//2)]
        d = max(5, t*3)
        for mx, my in mid_pts:
            draw.polygon([(mx,my-d),(mx+d,my),(mx,my+d),(mx-d,my)], fill=color)

    elif style == "3D Bevel":
        bw    = t * 5 + 12
        o, i_ = pad, pad + bw
        light = tuple(min(255, int(v * 1.40 + 55)) for v in color)
        dark  = tuple(max(0,   int(v * 0.40))       for v in color)
        draw.rectangle([o, o, w-o, h-o], outline=color, width=bw)
        draw.polygon([(o,o),(w-o,o),(w-i_,i_),(i_,i_)], fill=light)
        draw.polygon([(o,o),(i_,i_),(i_,h-i_),(o,h-o)], fill=light)
        draw.polygon([(o,h-o),(i_,h-i_),(w-i_,h-i_),(w-o,h-o)], fill=dark)
        draw.polygon([(w-o,o),(w-o,h-o),(w-i_,h-i_),(w-i_,i_)], fill=dark)
        lip = max(1, t // 2)
        for j in range(lip):
            draw.line([(i_+j,i_+j),(w-i_-j,i_+j)],   fill=dark)
            draw.line([(i_+j,i_+j),(i_+j,h-i_-j)],   fill=dark)
            draw.line([(i_+j,h-i_-j),(w-i_-j,h-i_-j)], fill=light)
            draw.line([(w-i_-j,i_+j),(w-i_-j,h-i_-j)], fill=light)

    if frame_img_path and Path(frame_img_path).exists():
        try:
            frame = Image.open(frame_img_path).convert("RGBA").resize((w,h), Image.LANCZOS)
            base  = img.convert("RGBA")
            base.alpha_composite(frame)
            img   = base.convert("RGB")
        except Exception:
            pass

    return img


def _calc_frame_inset(frame_style, frame_thickness, frame_img_path,
                      user_extra=0, base_pad=18):
    t   = max(1, frame_thickness)
    pad = base_pad
    gap = max(4, t * 2)
    if frame_style == "Simple":
        auto = pad + t + 14
    elif frame_style == "Double":
        auto = pad + t * 2 + gap + 16
    elif frame_style == "Vintage":
        auto = pad + t * 2 + gap + gap + 16
    elif frame_style == "Ornate Corners":
        auto = pad + min(80, 960 // 8) + 24
    elif frame_style == "3D Bevel":
        auto = pad + t * 5 + 12 + 16
    else:
        auto = 0
    if frame_img_path and Path(frame_img_path).exists():
        auto = max(auto, 70)
    return auto + user_extra


# ── video renderers ────────────────────────────────────────────────────────────

def render_video_spread(img_path, recipe_name, text, font_path, color_hex,
                        page_num, page_bg="#fdf8f0", font_size=0,
                        page_bg_img=None, page_img_opacity=80,
                        frame_style="None", frame_color="#8B7355",
                        frame_thickness=4, frame_img=None,
                        page_num_pos="Bottom Center", frame_padding=0,
                        page_num_size=14):
    """
    1920×1080 book-spread video frame.
    Left 960 px  = food photo.
    Right 960 px = recipe card (name heading + ingredients/instructions).
    """
    spread = Image.new("RGB", (SPREAD_W, SPREAD_H))
    spread.paste(fit_bg(img_path, PAGE_W, PAGE_H), (0, 0))

    bg      = hex_to_rgb(page_bg) if page_bg else (253, 248, 240)
    txt_img = Image.new("RGB", (PAGE_W, PAGE_H), bg)

    if page_bg_img and Path(page_bg_img).exists():
        try:
            bg_src = fit_bg(page_bg_img, PAGE_W, PAGE_H).convert("RGBA")
            alpha  = int(255 * page_img_opacity / 100)
            r, g, b, a = bg_src.split()
            a = a.point(lambda x: int(x * alpha / 255))
            bg_src = Image.merge("RGBA", (r, g, b, a))
            base   = txt_img.convert("RGBA")
            base.alpha_composite(bg_src)
            txt_img = base.convert("RGB")
        except Exception:
            pass

    draw = ImageDraw.Draw(txt_img)
    draw.rectangle([18, 18, PAGE_W - 18, PAGE_H - 18], outline=(200, 192, 180), width=1)

    _fi  = _calc_frame_inset(frame_style, frame_thickness, frame_img,
                              user_extra=frame_padding)
    mx   = max(72, _fi + 12)
    my   = max(72, _fi + 12)

    color = hex_to_rgb(color_hex)

    # ── Recipe name — prominent heading at top ────────────────────────────────
    name_text = recipe_name.strip() if recipe_name and recipe_name.strip() else f"Recipe {page_num}"
    name_font = load_font(font_path, 44)
    # Auto-shrink name to fit width
    for name_pt in range(44, 22, -2):
        name_font = load_font(font_path, name_pt)
        nb = draw.textbbox((0, 0), name_text, font=name_font)
        if nb[2] - nb[0] <= PAGE_W - mx * 2:
            break
    name_h = name_font.size + 8
    draw.text((mx, my), name_text, font=name_font, fill=color)

    # Thin rule under recipe name
    rule_y = my + name_h + 6
    rule_color = tuple(min(255, int(v * 0.65)) for v in color)
    draw.line([(mx, rule_y), (PAGE_W - mx, rule_y)], fill=rule_color, width=1)

    # ── Recipe content below the rule ─────────────────────────────────────────
    content_y  = rule_y + 16
    box_h_cont = PAGE_H - content_y - my - 60

    if text and text.strip():
        if font_size > 0:
            cont_font  = load_font(font_path, font_size)
            cont_lines = wrap_lines(text, cont_font, draw, PAGE_W - mx * 2)
        else:
            cont_font, cont_lines = wrap_and_fit(
                text, font_path, max_pt=34, min_pt=20,
                box_w=PAGE_W - mx * 2, box_h=box_h_cont)
    else:
        # blank recipe card — show light ruled lines
        cont_font  = load_font(font_path, 24)
        cont_lines = []
        line_color = (220, 212, 200)
        for ly in range(content_y, PAGE_H - 80, 48):
            draw.line([(mx, ly), (PAGE_W - mx, ly)], fill=line_color, width=1)

    lh = int(cont_font.size * 1.65)
    y  = content_y
    for line in cont_lines:
        if line:
            draw.text((mx, y), line, font=cont_font, fill=color)
        y += lh if line else lh // 2

    # ── Page number ────────────────────────────────────────────────────────────
    num_font = load_font(font_path, max(8, page_num_size))
    nb  = draw.textbbox((0, 0), str(page_num), font=num_font)
    nw  = nb[2] - nb[0]
    if "Left"   in page_num_pos: nx = mx
    elif "Right" in page_num_pos: nx = PAGE_W - nw - mx
    else:                          nx = (PAGE_W - nw) // 2
    ny = 28 if "Top" in page_num_pos else PAGE_H - 46
    draw.text((nx, ny), str(page_num), font=num_font, fill=(158, 148, 135))

    # ── 3D depth on text page ─────────────────────────────────────────────────
    for i in range(32):
        shade = int(130 * ((1 - i / 32) ** 1.6))
        draw.line([(i, 0), (i, PAGE_H)], fill=(35, 22, 10, shade))
    for i in range(10):
        shade = int(35 * (1 - i / 10))
        draw.line([(32+i, 0), (32+i, PAGE_H)], fill=(255, 245, 225, shade))
    for i in range(10):
        shade = int(70 * (1 - i / 10))
        draw.line([(PAGE_W-1-i, 0), (PAGE_W-1-i, PAGE_H)], fill=(0,0,0,shade))
    for i in range(6):
        shade = int(45 * (1 - i / 6))
        draw.line([(0, i), (PAGE_W, i)], fill=(20, 14, 8, shade))
    for i in range(6):
        shade = int(35 * (1 - i / 6))
        draw.line([(0, PAGE_H-1-i), (PAGE_W, PAGE_H-1-i)], fill=(0,0,0,shade))

    spread.paste(txt_img, (PAGE_W, 0))

    # ── 3D spine on full spread ────────────────────────────────────────────────
    sd = ImageDraw.Draw(spread)
    for i in range(22):
        shade = int(160 * ((1 - i / 22) ** 1.5))
        sd.line([(PAGE_W-6+i, 0), (PAGE_W-6+i, SPREAD_H)], fill=(25,15,6,shade))
    for i in range(5):
        shade = int(55 * (1 - i / 5))
        sd.line([(PAGE_W-8-i, 0), (PAGE_W-8-i, SPREAD_H)], fill=(220,195,155,shade))
    for i in range(8):
        sd.line([(i, 0), (i, SPREAD_H)], fill=(0,0,0,int(60*(1-i/8))))
    for i in range(8):
        sd.line([(SPREAD_W-1-i, 0), (SPREAD_W-1-i, SPREAD_H)], fill=(0,0,0,int(50*(1-i/8))))
    for i in range(8):
        sd.line([(0, i), (SPREAD_W, i)], fill=(15,10,5,int(55*(1-i/8))))
    for i in range(14):
        sd.line([(0, SPREAD_H-1-i), (SPREAD_W, SPREAD_H-1-i)], fill=(0,0,0,int(50*(1-i/14))))

    txt_img = apply_frame(txt_img, frame_style, frame_color, frame_thickness, frame_img)
    spread.paste(txt_img, (PAGE_W, 0))
    return spread


def render_cover(img_path, title, subtitle, font_path, w, h):
    img  = fit_bg(img_path, w, h).convert("RGBA")
    img.alpha_composite(gradient_band(w, 320, 180), (0, h - 320))
    img  = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    tf = load_font(font_path, 64 if w < 1000 else 82)
    tb = draw.textbbox((0, 0), title, font=tf)
    draw.text(((w-(tb[2]-tb[0]))//2, h-258), title, font=tf, fill=(255,252,245))
    if subtitle:
        sf = load_font(font_path, 32 if w < 1000 else 40)
        sb = draw.textbbox((0, 0), subtitle, font=sf)
        draw.text(((w-(sb[2]-sb[0]))//2, h-152), subtitle, font=sf, fill=(215,207,190))
    return img


def render_back_cover(img_path, chef_name, chef_bio, chef_photo, font_path, w, h):
    img    = fit_bg(img_path, w, h).convert("RGBA")
    pw     = min(PAGE_W, w // 2)
    panel  = Image.new("RGBA", (pw, h), (252, 248, 240, 228))
    img.alpha_composite(panel, (w - pw, 0))
    img  = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    px, py = w - pw + 50, 70

    if chef_photo and Path(chef_photo).exists():
        ap   = Image.open(chef_photo).convert("RGB").resize((160, 160), Image.LANCZOS)
        mask = Image.new("L", (160, 160), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 160, 160), fill=255)
        img.paste(ap, (w - pw + (pw - 160) // 2, py), mask)
        py += 190

    draw.text((px, py), "About the Chef", font=load_font(font_path, 30), fill=(108, 80, 50))
    py += 52
    draw.text((px, py), chef_name, font=load_font(font_path, 44), fill=(38, 26, 14))
    py += 68

    bio_font, bio_lines = wrap_and_fit(chef_bio, font_path,
                                       max_pt=28, min_pt=18,
                                       box_w=pw - 100, box_h=h - py - 50)
    lh = int(bio_font.size * 1.65)
    for line in bio_lines:
        draw.text((px, py), line, font=bio_font, fill=(52, 38, 22))
        py += lh if line else lh // 2
    return img


# ── filename helper ────────────────────────────────────────────────────────────

def _safe_stem(title: str) -> str:
    stem  = re.sub(r'[\\/:*?"<>|]+', '', title)
    stem  = re.sub(r'\s+', '_', stem.strip())[:60]
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{stem}_{stamp}" if stem else f"cookbook_{stamp}"


# ── asset helpers ──────────────────────────────────────────────────────────────

def _img_mime(path):
    return {".jpg":"image/jpeg",".jpeg":"image/jpeg",
            ".png":"image/png",".webp":"image/webp"
            }.get(Path(path).suffix.lower(),"image/jpeg")

def _audio_mime(path):
    return {".mp3":"audio/mpeg",".ogg":"audio/ogg",
            ".wav":"audio/wav", ".m4a":"audio/mp4",".aac":"audio/aac"
            }.get(Path(path).suffix.lower(),"audio/mpeg")

def _asset_src(path, mime, embed, assets_dir):
    if embed:
        return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"
    dst = assets_dir / Path(path).name
    if not dst.exists():
        shutil.copy2(path, dst)
    return f"assets/{Path(path).name}"

def _hex_to_rgba(hex_color, opacity_pct):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    return f"rgba({r},{g},{b},{opacity_pct/100:.2f})"


# ── HTML builder ───────────────────────────────────────────────────────────────

def build_html(cfg, log):
    photos     = cfg["photos"]
    embed      = cfg["embed_assets"]
    output_dir = cfg["output_dir"]
    assets_dir = output_dir / "assets"
    fn   = cfg["font_name"]
    tc   = cfg["text_color"]
    N    = len(photos)
    pg_bg  = _hex_to_rgba(cfg["page_bg_color"], cfg["page_opacity"])

    _pg_img_path    = cfg.get("page_bg_img", "")
    _pg_img_opacity = cfg.get("page_img_opacity", 80) / 100.0
    has_pg_img      = bool(_pg_img_path and Path(_pg_img_path).exists())
    pg_dark         = cfg["page_darkness"] / 100.0

    if not embed:
        assets_dir.mkdir(exist_ok=True)

    def isrc(p):  return _asset_src(p, _img_mime(p), embed, assets_dir)
    def esc(s):   return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    title_e   = esc(cfg["title"])
    sub_e     = esc(cfg["subtitle"])
    cover_src = isrc(cfg["cover_img"])
    back_src  = isrc(cfg["back_img"])

    if has_pg_img:
        pg_img_src = isrc(_pg_img_path)
        pg_img_css = (f"background-image:url('{pg_img_src}');"
                      f"background-size:cover;background-position:center;")
        pg_img_overlay = (f"position:absolute;inset:0;z-index:0;"
                          f"background:{pg_bg};opacity:{_pg_img_opacity:.2f};pointer-events:none")
    else:
        pg_img_css     = f"background:{pg_bg};"
        pg_img_overlay = None

    # ── frame CSS ─────────────────────────────────────────────────────────────
    _frame_style = cfg.get("frame_style",    "None")
    _frame_color = cfg.get("frame_color",    "#8B7355")
    _frame_thick = cfg.get("frame_thickness", 4)
    _frame_img_p = cfg.get("frame_img",      "")
    _frame_pad   = 18

    _frame_inset = _calc_frame_inset(_frame_style, _frame_thick, _frame_img_p,
                                     user_extra=cfg.get("frame_padding", 0))
    if _frame_inset > 0:
        _fp = _frame_inset
        _inner_pad_override = f"padding:{_fp}px {_fp}px {max(28,_fp)}px {_fp}px;"
    else:
        _inner_pad_override = ""

    def _css_frame():
        t, p, c = _frame_thick, _frame_pad, _frame_color
        g = max(4, t * 2)
        if _frame_style == "Simple":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};")
        elif _frame_style == "Double":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};outline:{t}px solid {c};outline-offset:{g}px;")
        elif _frame_style == "Vintage":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};outline:{t}px solid {c};outline-offset:{g}px;")
        elif _frame_style == "Ornate Corners":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};"
                    f"box-shadow:inset 0 0 0 {t*2}px {c};")
        elif _frame_style == "3D Bevel":
            bw = t * 5 + 12
            _h = c.lstrip("#")
            _r,_g,_b = int(_h[0:2],16),int(_h[2:4],16),int(_h[4:6],16)
            lc = f"#{min(255,int(_r*1.4+55)):02x}{min(255,int(_g*1.4+55)):02x}{min(255,int(_b*1.4+55)):02x}"
            dc = f"#{max(0,int(_r*.4)):02x}{max(0,int(_g*.4)):02x}{max(0,int(_b*.4)):02x}"
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border-style:solid;border-width:{bw}px;"
                    f"border-color:{lc} {dc} {dc} {lc};"
                    f"box-shadow:4px 4px 14px rgba(0,0,0,.65),-1px -1px 5px rgba(255,255,255,.15),"
                    f"inset 0 0 0 2px rgba(0,0,0,.28),inset 0 0 0 {bw-2}px rgba(255,255,255,.07);")
        return ""

    frame_div_css  = _css_frame()
    frame_div_html = f'<div style="{frame_div_css}"></div>' if frame_div_css else ""

    frame_img_html = ""
    if _frame_img_p and Path(_frame_img_p).exists():
        fi_src = isrc(_frame_img_p)
        frame_img_html = (f'<img src="{fi_src}" style="position:absolute;inset:0;'
                          f'width:100%;height:100%;object-fit:fill;z-index:3;'
                          f'pointer-events:none;" alt="">')

    # ── page number style ─────────────────────────────────────────────────────
    _pnum_pos   = cfg.get("page_num_pos",  "Bottom Center")
    _pnum_sz    = cfg.get("page_num_size", 14)
    _pnum_vert  = "bottom:8px;top:auto" if "Bottom" in _pnum_pos else "top:8px;bottom:auto"
    _pnum_align = "left" if "Left" in _pnum_pos else ("right" if "Right" in _pnum_pos else "center")
    _pnum_style = (f"position:absolute;{_pnum_vert};left:0;right:0;width:100%;"
                   f"text-align:{_pnum_align};font-size:{_pnum_sz}px;opacity:.60;"
                   f"z-index:4;pointer-events:none;padding:0 18px")

    # ── build pages ───────────────────────────────────────────────────────────
    pages = []

    pages.append(f"""
  <div class="page page-cover" data-density="hard"><div class="pc">
    <img class="full" src="{cover_src}" alt="Cover">
    <div class="cov-ov">
      <h1 class="cov-title">{title_e}</h1>
      {"<p class='cov-sub'>" + sub_e + "</p>" if sub_e else ""}
    </div>
  </div></div>""")

    for i, ph in enumerate(photos, 1):
        log(f"  HTML: page {i}/{N} …")
        src         = isrc(ph["path"])
        rname       = esc(ph.get("name", "").strip())
        rcontent    = esc(ph["text"]).replace("\n", "<br>") if ph["text"].strip() else ""
        rname_html  = f'<div class="recipe-name">{rname}</div>' if rname else ""
        rcontent_html = f'<div class="prompt">{rcontent}</div>' if rcontent else ""

        pages.append(f"""
  <div class="page" data-density="soft"><div class="pc">
    <img class="full" src="{src}" alt="Recipe {i}">
    <div class="pnum">{i}</div>
  </div></div>""")

        _img_wash = (f'<div style="{pg_img_overlay}"></div>' if pg_img_overlay else "")
        pages.append(f"""
  <div class="page write-pg" data-density="soft" data-idx="{i}"><div class="pc">
    <div class="write-wrap" style="{pg_img_css}">
      {_img_wash}
      <div class="dark-ov" style="background:rgba(0,0,0,{pg_dark:.2f})"></div>
      <div class="write-inner" style="{_inner_pad_override}font-family:'{fn}',Georgia,serif;color:{tc}">
        {rname_html}
        <div class="recipe-divider"></div>
        {rcontent_html}
        <div class="notes-label">My Notes</div>
        <textarea class="entry" data-key="cb{i}"
          placeholder="Write your notes, variations, or memories about this recipe…"
          style="font-family:'{fn}',Georgia,serif;color:{tc}"></textarea>
      </div>
      <div style="{_pnum_style}">{i}</div>
      {frame_div_html}
      {frame_img_html}
    </div>
  </div></div>""")

    chef_name = esc(cfg["author_name"])
    chef_bio  = esc(cfg["author_bio"]).replace("\n","<br>")
    pages.append(f"""
  <div class="page page-cover" data-density="hard"><div class="pc">
    <img class="full" src="{back_src}" alt="Back Cover">
    <div class="auth-panel" style="font-family:'{fn}',Georgia,serif">
      <h2 class="ab-head">About the Chef</h2>
      <p class="ab-name">{chef_name}</p>
      <p class="ab-bio">{chef_bio}</p>
    </div>
  </div></div>""")

    mus_html = mus_btn = mus_js = ""
    if cfg["music_file"] and Path(cfg["music_file"]).exists():
        ms = _asset_src(cfg["music_file"], _audio_mime(cfg["music_file"]), embed, assets_dir)
        mus_html = f'<audio id="bgm" loop><source src="{ms}"></audio>'
        mus_btn  = "<button id='mb' onclick='tm()'>&#9834; Music</button>"
        mus_js   = ("function tm(){const a=document.getElementById('bgm'),b=document.getElementById('mb');"
                    "if(a.paused){a.play();b.textContent='\\u266a Pause';}"
                    "else{a.pause();b.textContent='\\u266a Music';}}")

    # JS cache lives inside the user's chosen output folder — never on C: by default
    js_cache = output_dir / ".cb_cache"
    log(f"  JS cache → {js_cache}")
    js_pageflip    = _get_js("page-flip",   js_cache, log)
    js_jspdf       = _get_js("jspdf",       js_cache, log)
    js_html2canvas = _get_js("html2canvas", js_cache, log)

    import json as _json
    spreads_data = _json.dumps([
        {"src": isrc(ph["path"]),
         "name": ph.get("name",""),
         "prompt": ph["text"],
         "key": f"cb{i}"}
        for i, ph in enumerate(photos, 1)
    ])

    stem = _safe_stem(cfg["title"])
    out  = output_dir / f"{stem}_flipbook.html"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_e}</title>
<script>{js_pageflip}</script>
<script>{js_jspdf}</script>
<script>{js_html2canvas}</script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  background:radial-gradient(ellipse at 50% 38%,#2e1a0a 0%,#140804 52%,#050201 100%);
  min-height:100vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:20px;
  font-family:'{fn}',Georgia,serif}}
#book{{
  box-shadow:
    0 80px 100px rgba(0,0,0,.98),
    0 30px 55px  rgba(0,0,0,.80),
    -10px 15px 35px rgba(0,0,0,.55),
    10px  15px 35px rgba(0,0,0,.45),
    0 2px  8px  rgba(0,0,0,.9);
  border-radius:2px 4px 4px 2px;
}}
.pc{{width:100%;height:100%;overflow:hidden;position:relative;
  box-shadow:inset -6px 0 18px rgba(0,0,0,.30),inset 0 -4px 12px rgba(0,0,0,.18);}}
img.full{{width:100%;height:100%;object-fit:cover;display:block}}
.cov-ov{{position:absolute;bottom:0;left:0;right:0;
  padding:36px 24px 28px;
  background:linear-gradient(transparent,rgba(0,0,0,.83));text-align:center}}
.cov-title{{font-size:clamp(16px,4vw,52px);color:#faf8f2;font-style:italic;
  text-shadow:2px 2px 10px rgba(0,0,0,.9);margin-bottom:8px}}
.cov-sub{{font-size:clamp(10px,2vw,28px);color:#ddd5be;
  text-shadow:1px 1px 6px rgba(0,0,0,.8)}}
.pnum{{position:absolute;bottom:4px;width:100%;text-align:center;
  font-size:11px;color:rgba(220,205,180,.6)}}
.write-wrap{{
  position:absolute;inset:0;display:flex;flex-direction:column;
  box-shadow:inset 18px 0 30px rgba(255,245,225,.07),
             inset -8px 0 20px rgba(0,0,0,.18),
             inset 0 -8px 20px rgba(0,0,0,.12);}}
.dark-ov{{position:absolute;inset:0;pointer-events:none;z-index:0}}
.write-inner{{position:relative;z-index:1;flex:1;display:flex;flex-direction:column;
  padding:clamp(14px,3vw,32px) clamp(16px,3.5vw,36px) 24px;overflow:hidden}}
/* ── Recipe name heading ── */
.recipe-name{{
  font-size:clamp(13px,2.2vw,26px);font-weight:bold;font-style:italic;
  line-height:1.2;margin-bottom:4px;flex-shrink:0;
  pointer-events:none;user-select:none}}
.recipe-divider{{
  height:1px;background:currentColor;opacity:.3;margin:6px 0 10px;flex-shrink:0}}
.prompt{{font-style:italic;font-size:clamp(9px,1.4vw,15px);line-height:1.65;
  margin-bottom:clamp(6px,1.2vw,14px);opacity:.85;flex-shrink:0;
  pointer-events:none;user-select:none;
  max-height:45%;overflow:hidden}}
.notes-label{{font-size:clamp(8px,1.1vw,13px);opacity:.6;font-style:italic;
  margin-bottom:4px;flex-shrink:0;pointer-events:none;user-select:none}}
.entry{{flex:1;border:none;background:transparent;resize:none;outline:none;
  font-size:clamp(10px,1.4vw,16px);line-height:1.9;
  width:100%;min-height:0;cursor:text;position:relative;z-index:10}}
.entry:focus{{box-shadow:inset 0 0 0 1px rgba(200,180,140,.35)}}
.auth-panel{{position:absolute;top:0;right:0;bottom:0;width:46%;
  background:rgba(252,248,240,.93);padding:44px 28px;
  display:flex;flex-direction:column;gap:12px;overflow:hidden}}
.ab-head{{font-size:clamp(11px,1.4vw,20px);color:#8a5e38;font-style:italic;
  border-bottom:1px solid #d8cfc0;padding-bottom:8px}}
.ab-name{{font-size:clamp(13px,1.8vw,26px);font-weight:bold;color:#2c1810}}
.ab-bio{{font-size:clamp(9px,1.1vw,15px);line-height:1.8;color:#4a3520}}
/* ── controls bar ── */
#controls{{
  margin-top:14px;display:flex;gap:8px;align-items:center;
  flex-wrap:wrap;justify-content:center;
  background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.18);
  border-radius:10px;padding:10px 20px;
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);}}
button{{background:#b87828;color:#fff8ee;border:1px solid rgba(255,255,255,.25);
  border-radius:6px;padding:8px 18px;font-size:13px;font-weight:600;
  cursor:pointer;font-family:'{fn}',Georgia,serif;
  transition:background .15s,box-shadow .15s;
  box-shadow:0 2px 6px rgba(0,0,0,.45);letter-spacing:.02em;}}
button:hover{{background:#d4922a;box-shadow:0 3px 10px rgba(0,0,0,.55)}}
#saveBtn{{background:#2a7a50}}
#saveBtn:hover{{background:#34a066}}
#dlBtn{{background:#2a5a9a}}
#dlBtn:hover{{background:#3470c0}}
#dlBtn:disabled{{background:#1e3a60;cursor:not-allowed;opacity:.55}}
#mb{{background:#6a3a9a}}
#mb:hover{{background:#8050b8}}
#pinfo{{color:#f5e8c8;font-size:14px;font-weight:600;
  text-shadow:0 1px 4px rgba(0,0,0,.8);min-width:110px;text-align:center;}}
#save-note{{font-size:11px;font-style:italic;color:#c8d8c0;min-width:80px}}
#xpanel{{position:fixed;left:-9999px;top:0;width:1920px;height:1080px;
  overflow:hidden;background:#fff}}
</style>
</head>
<body>
{mus_html}
<div id="book">{"".join(pages)}</div>
<div id="controls">
  <button onclick="pf.flipPrev()">&#9664; Prev</button>
  <span id="pinfo">Cover</span>
  <button onclick="pf.flipNext()">Next &#9654;</button>
  {mus_btn}
  <button id="saveBtn" onclick="saveRecipes()">&#128190; Save Recipes</button>
  <button id="dlBtn"   onclick="dlPDF()">&#8659; Export PDF</button>
  <span id="save-note"></span>
</div>
<div id="xpanel"></div>
<script>
const PG_BG="{cfg['page_bg_color']}",TC="{tc}",FONT="{fn}",
      PG_DARK={pg_dark:.2f},TITLE="{title_e}",
      COVER="{cover_src}",BACK="{back_src}";
const SPREADS={spreads_data};
const PW=1920,PH=1080;

const pf=new St.PageFlip(document.getElementById('book'),{{
  width:550,height:733,
  size:'stretch',
  minWidth:300,minHeight:400,
  maxWidth:1400,maxHeight:1800,
  showCover:true,
  mobileScrollSupport:false,
  swipeDistance:50,
}});
pf.loadFromHTML(document.querySelectorAll('.page'));
pf.on('flip',function(e){{
  const c=e.data,N={N};
  let lbl='Cover';
  if(c===0)lbl='Cover';
  else if(c>=N*2+1)lbl='Back Cover';
  else{{const pg=Math.ceil(c/2);lbl=`Recipe ${{pg}} of {N}`;}}
  document.getElementById('pinfo').textContent=lbl;
}});

// Prevent page-flip on textarea interaction
document.querySelectorAll('.entry').forEach(function(el){{
  ['mousedown','touchstart','pointerdown'].forEach(function(evt){{
    el.addEventListener(evt,function(e){{e.stopPropagation();}},{{capture:true}});
  }});
}});

// localStorage auto-save
document.querySelectorAll('.entry').forEach(function(ta){{
  const k=ta.dataset.key;
  const saved=localStorage.getItem(k);
  if(saved)ta.value=saved;
  ta.addEventListener('input',function(){{localStorage.setItem(k,ta.value);}});
}});

{mus_js}

function saveRecipes(){{
  let out='';
  SPREADS.forEach(function(sp,idx){{
    const entry=localStorage.getItem(sp.key)||'';
    out+='========================================\\n';
    out+=(sp.name?sp.name:`Recipe ${{idx+1}}`)+'\\n';
    out+='========================================\\n';
    if(sp.prompt)out+='--- Recipe ---\\n'+sp.prompt+'\\n\\n';
    out+='--- My Notes ---\\n'+(entry||'(no notes yet)')+'\\n\\n';
  }});
  const b=new Blob([out],{{type:'text/plain'}});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b);
  a.download=TITLE.replace(/[^a-z0-9]/gi,'_')+'_recipes.txt';
  a.click();
  const n=document.getElementById('save-note');
  n.textContent='Saved!';n.style.color='#8fbe5a';
  setTimeout(function(){{n.textContent='';}},2500);
}}

/* ── PDF export ── */
async function dlPDF(){{
  const btn=document.getElementById('dlBtn');
  btn.disabled=true;btn.textContent='Building PDF…';
  const xp=document.getElementById('xpanel');
  const pdf=new jspdf.jsPDF({{orientation:'landscape',unit:'px',format:[PW,PH]}});
  async function addImg(el){{
    xp.innerHTML='';xp.appendChild(el);
    const c=await html2canvas(el,{{scale:1,useCORS:true,width:PW,height:PH}});
    return c.toDataURL('image/jpeg',0.88);
  }}
  function imgSlide(src){{
    const d=document.createElement('div');
    d.style.cssText=`width:${{PW}}px;height:${{PH}}px;overflow:hidden`;
    const im=document.createElement('img');
    im.src=src;im.style.cssText=`width:100%;height:100%;object-fit:cover`;
    d.appendChild(im);return d;
  }}
  function recipeSlide(sp){{
    const entry=localStorage.getItem(sp.key)||'';
    const d=document.createElement('div');
    d.style.cssText=`width:${{PW}}px;height:${{PH}}px;display:flex;overflow:hidden`;
    const ph=document.createElement('div');
    ph.style.cssText=`width:960px;height:${{PH}}px;flex-shrink:0;overflow:hidden`;
    const im=document.createElement('img');
    im.src=sp.src;im.style.cssText=`width:100%;height:100%;object-fit:cover`;
    ph.appendChild(im);
    const wr=document.createElement('div');
    wr.style.cssText=`width:960px;height:${{PH}}px;position:relative;background:${{PG_BG}};`+
      `display:flex;flex-direction:column;padding:52px 48px 36px;overflow:hidden;`+
      `font-family:'${{FONT}}',Georgia,serif;color:${{TC}}`;
    if(sp.name){{
      const nm=document.createElement('div');
      nm.style.cssText=`font-size:28px;font-weight:bold;font-style:italic;margin-bottom:12px`;
      nm.textContent=sp.name;wr.appendChild(nm);
      const hr=document.createElement('div');
      hr.style.cssText=`height:1px;background:currentColor;opacity:.3;margin-bottom:16px`;
      wr.appendChild(hr);
    }}
    if(sp.prompt){{
      const pr=document.createElement('div');
      pr.style.cssText=`font-size:16px;line-height:1.6;margin-bottom:16px;opacity:.85;white-space:pre-wrap;flex-shrink:0`;
      pr.textContent=sp.prompt;wr.appendChild(pr);
    }}
    if(entry){{
      const nl=document.createElement('div');
      nl.style.cssText=`font-size:14px;opacity:.6;font-style:italic;margin-bottom:6px`;
      nl.textContent='My Notes';wr.appendChild(nl);
      const et=document.createElement('div');
      et.style.cssText=`font-size:16px;line-height:1.9;white-space:pre-wrap;word-wrap:break-word;flex:1;overflow:hidden`;
      et.textContent=entry;wr.appendChild(et);
    }}
    d.appendChild(ph);d.appendChild(wr);return d;
  }}
  try{{
    let data=await addImg(imgSlide(COVER));
    pdf.addImage(data,'JPEG',0,0,PW,PH);
    for(const sp of SPREADS){{
      pdf.addPage([PW,PH],'landscape');
      data=await addImg(recipeSlide(sp));
      pdf.addImage(data,'JPEG',0,0,PW,PH);
    }}
    pdf.addPage([PW,PH],'landscape');
    data=await addImg(imgSlide(BACK));
    pdf.addImage(data,'JPEG',0,0,PW,PH);
    pdf.save(TITLE.replace(/[^a-z0-9]/gi,'_')+'.pdf');
  }}catch(e){{
    alert('PDF error: '+e.message);console.error(e);
  }}
  xp.innerHTML='';
  btn.disabled=false;btn.textContent='\\u21d3 Export PDF';
}}
</script>
</body>
</html>"""

    out.write_text(html, encoding="utf-8")
    log(f"  Saved → {out.name}")


# ── video builder ──────────────────────────────────────────────────────────────

def build_video(cfg, log):
    photos  = cfg["photos"]
    tmp     = cfg["output_dir"] / "_frames"
    tmp.mkdir(exist_ok=True)

    # Detect best transition once — avoids crash on older/stripped FFmpeg builds
    _xfade = _best_xfade_transition(FFMPEG)
    log(f"  Transition: {_xfade}")

    log("  Rendering cover …")
    render_cover(cfg["cover_img"], cfg["title"], cfg["subtitle"],
                 cfg["font_path"], SPREAD_W, SPREAD_H).save(tmp / "f0000.png")

    for i, ph in enumerate(photos, 1):
        log(f"  Rendering frame {i}/{len(photos)} …")
        render_video_spread(
            ph["path"],
            ph.get("name", ""),
            ph["text"],
            cfg["font_path"], cfg["text_color"], i,
            page_bg          = cfg.get("page_bg_color",    "#fdf8f0"),
            font_size        = cfg.get("video_font_size",  0),
            page_bg_img      = cfg.get("page_bg_img",      None),
            page_img_opacity = cfg.get("page_img_opacity", 80),
            frame_style      = cfg.get("frame_style",      "None"),
            frame_color      = cfg.get("frame_color",      "#8B7355"),
            frame_thickness  = cfg.get("frame_thickness",  4),
            frame_img        = cfg.get("frame_img",        None),
            page_num_pos     = cfg.get("page_num_pos",     "Bottom Center"),
            frame_padding    = cfg.get("frame_padding",    0),
            page_num_size    = cfg.get("page_num_size",    14),
        ).save(tmp / f"f{i:04d}.png")

    n_back = len(photos) + 1
    log("  Rendering back cover …")
    render_back_cover(cfg["back_img"], cfg["author_name"], cfg["author_bio"],
                      cfg["author_photo"], cfg["font_path"],
                      SPREAD_W, SPREAD_H).save(tmp / f"f{n_back:04d}.png")

    frames = sorted(tmp.glob("f*.png"))
    N      = len(frames)

    cmd = [FFMPEG, "-y"]
    for i, f in enumerate(frames):
        t = (PAGE_DUR + TRANS_DUR) if (i == 0 or i == N-1) else (PAGE_DUR + 2*TRANS_DUR)
        cmd += ["-loop","1","-t",str(t),"-i",str(f)]

    filt_parts, last = [], "0"
    for n in range(N - 1):
        offset = (n + 1) * PAGE_DUR + n * TRANS_DUR
        nxt    = f"v{n}"
        filt_parts.append(
            f"[{last}][{n+1}]xfade=transition={_xfade}:duration={TRANS_DUR}:offset={offset}[{nxt}]")
        last = nxt

    filter_complex = ";".join(filt_parts)
    cmd += ["-filter_complex", filter_complex, "-map", f"[{last}]",
            "-c:v","libx264","-pix_fmt","yuv420p","-r","24"]

    stem = _safe_stem(cfg["title"])
    out  = cfg["output_dir"] / f"{stem}.mp4"
    cmd += [str(out)]

    log("  Running FFmpeg …")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{proc.stderr[-2000:]}")
    log(f"  Saved → {out.name}")

    try:
        shutil.rmtree(tmp)
    except Exception:
        pass


# ── tkinter helpers ────────────────────────────────────────────────────────────

def _row(parent, label, var, row, browse_cmd=None):
    tk.Label(parent, text=label, bg=CARD, fg=FG, font=("Segoe UI",9),
             anchor="w", width=24).grid(row=row, column=0, sticky="w", padx=(12,4), pady=5)
    e = tk.Entry(parent, textvariable=var, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat", font=("Segoe UI",9))
    e.grid(row=row, column=1, sticky="ew", padx=4, pady=5)
    if browse_cmd:
        tk.Button(parent, text="Browse…", command=browse_cmd,
                  bg=BTN, fg=FG, relief="flat", font=("Segoe UI",8),
                  padx=8, pady=3, cursor="hand2",
                  activebackground=BTNHOV, activeforeground=FG).grid(
            row=row, column=2, padx=(4,12), pady=5)

def _btn(parent, text, cmd, danger=False, **kw):
    return tk.Button(parent, text=text, command=cmd,
                     bg=DANGER if danger else BTN,
                     fg=FG, relief="flat", font=("Segoe UI",9),
                     padx=10, pady=4, cursor="hand2",
                     activebackground="#c04030" if danger else BTNHOV,
                     activeforeground=FG, **kw)


# ── main app ───────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multimedia Cookbook Factory")
        self.geometry("1020x720")
        self.minsize(840, 580)
        self.configure(bg=BG)
        self._setup_style()
        self._init_vars()
        self._photos  = []   # [{"path": Path, "name": str, "text": str}, …]
        self._sel     = None
        self._thumb   = None
        self._build_ui()

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                    padding=[16,7], font=("Segoe UI",9))
        s.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", ACCENT)])
        s.configure("TCheckbutton", background=CARD, foreground=FG,
                    font=("Segoe UI",9))
        s.map("TCheckbutton", background=[("active", CARD)], foreground=[("active", FG)])
        s.configure("Vertical.TScrollbar", background=PANEL,
                    troughcolor=BG, borderwidth=0, arrowsize=12)

    def _init_vars(self):
        self.v_cover        = tk.StringVar()
        self.v_back         = tk.StringVar()
        self.v_music        = tk.StringVar()
        self.v_output       = tk.StringVar()
        self.v_title        = tk.StringVar()
        self.v_subtitle     = tk.StringVar()
        self.v_author_name  = tk.StringVar()
        self.v_author_photo = tk.StringVar()
        self.v_font         = tk.StringVar(value=FONT_CHOICES[0])
        self.v_font_path    = tk.StringVar()
        self.v_color        = tk.StringVar(value="#2c1810")
        self.b_html         = tk.BooleanVar(value=True)
        self.b_embed        = tk.BooleanVar(value=False)
        self.b_video        = tk.BooleanVar(value=True)
        self.v_page_bg        = tk.StringVar(value="#fdf8f0")
        self.v_pg_opacity     = tk.IntVar(value=96)
        self.v_pg_dark        = tk.IntVar(value=0)
        self.v_video_font_size= tk.IntVar(value=0)
        self.v_page_bg_img    = tk.StringVar()
        self.v_page_img_opacity= tk.IntVar(value=80)
        self.v_frame_style    = tk.StringVar(value="None")
        self.v_frame_color    = tk.StringVar(value="#8B7355")
        self.v_frame_thickness= tk.IntVar(value=4)
        self.v_frame_img      = tk.StringVar()
        self.v_frame_padding  = tk.IntVar(value=0)
        self.v_page_num_pos   = tk.StringVar(value="Bottom Center")
        self.v_page_num_size  = tk.IntVar(value=14)
        self.v_recipe_name    = tk.StringVar()   # current page recipe name entry
        self.v_font.trace_add("write", self._on_font_change)
        self.v_color.trace_add("write", lambda *_: self._refresh_swatch())
        self.v_page_bg.trace_add("write", lambda *_: self._refresh_pg_swatch())

    # ── UI ──────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=PANEL, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Multimedia Cookbook Factory",
                 bg=PANEL, fg=ACCENT, font=("Georgia",20,"italic")).pack()
        tk.Label(hdr, text="Create beautiful HTML flipbooks and MP4 video cookbooks",
                 bg=PANEL, fg=MUTED, font=("Segoe UI",9)).pack(pady=(2,0))
        ffcolor = ACCENT if FFMPEG else DANGER
        ffmsg   = f"FFmpeg: {FFMPEG}" if FFMPEG else "FFmpeg: NOT FOUND — video export disabled"
        tk.Label(hdr, text=ffmsg, bg=PANEL, fg=ffcolor, font=("Consolas",8)).pack(pady=(4,0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=8)

        t_photos  = tk.Frame(nb, bg=CARD)
        t_details = self._card(nb)
        t_style   = tk.Frame(nb, bg=CARD)
        t_build   = self._card(nb)

        nb.add(t_photos,  text="   Recipes   ")
        nb.add(t_details, text="   Details   ")
        nb.add(t_style,   text="   Style   ")
        nb.add(t_build,   text="   Build   ")

        self._tab_photos(t_photos)
        self._tab_details(t_details)
        self._tab_style(t_style)
        self._tab_build(t_build)

    def _card(self, parent):
        f = tk.Frame(parent, bg=CARD)
        f.columnconfigure(1, weight=1)
        return f

    # ── Recipes tab ─────────────────────────────────────────────────────────────

    def _tab_photos(self, p):
        p.columnconfigure(1, weight=1)
        p.rowconfigure(1, weight=1)

        bar = tk.Frame(p, bg=CARD, pady=6)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10)
        _btn(bar, "📁  Load Folder",     self._load_folder).pack(side="left", padx=4)
        _btn(bar, "+ Add Photos",        self._add_photos).pack(side="left", padx=4)
        _btn(bar, "📄  Load from File",  self._import_from_file).pack(side="left", padx=4)
        _btn(bar, "✕  Clear All",        self._clear_photos, danger=True).pack(side="left", padx=4)
        tk.Label(bar,
                 text="  Load Folder / Add Photos for images · Load from File imports .txt/.docx/.pdf recipes",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

        left = tk.Frame(p, bg=CARD, width=270)
        left.grid(row=1, column=0, sticky="nsew", padx=(10,0), pady=(0,10))
        left.pack_propagate(False)

        tk.Label(left, text="RECIPE PAGES",
                 bg="#1a0e06", fg=MUTED, font=("Segoe UI",8),
                 pady=4).pack(fill="x")

        lf = tk.Frame(left, bg=CARD)
        lf.pack(fill="both", expand=True)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self._lb = tk.Listbox(
            lf, bg="#1a0e06", fg=FG,
            selectbackground="#5a2010", selectforeground=ACCENT,
            activestyle="none", font=("Segoe UI",10,"bold"),
            relief="flat", borderwidth=0, highlightthickness=0, cursor="hand2")
        self._lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._lb.configure(yscrollcommand=sb.set)
        self._lb.bind("<<ListboxSelect>>", self._on_select)

        br = tk.Frame(left, bg=CARD, pady=4)
        br.pack(fill="x")
        _btn(br, "↑ Move Up",   self._move_up).pack(side="left", padx=3)
        _btn(br, "↓ Move Down", self._move_down).pack(side="left", padx=3)
        _btn(br, "✕ Remove",    self._remove, danger=True).pack(side="left", padx=3)

        # ── right panel: recipe editor ─────────────────────────────────────────
        right = tk.Frame(p, bg=CARD)
        right.grid(row=1, column=1, sticky="nsew", padx=(6,10), pady=(0,10))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)   # text area expands

        nav_bar = tk.Frame(right, bg="#1a0e06", pady=6)
        nav_bar.grid(row=0, column=0, sticky="ew")
        nav_bar.columnconfigure(1, weight=1)

        self._prev_btn = tk.Button(
            nav_bar, text="◀ Prev", command=self._prev_page,
            bg=BTN, fg=FG, relief="flat", font=("Segoe UI",9),
            padx=10, cursor="hand2",
            activebackground=BTNHOV, activeforeground=FG, state="disabled")
        self._prev_btn.grid(row=0, column=0, padx=(10,4), pady=2)

        self._page_badge = tk.Label(
            nav_bar, text="← Load photos, then select a page to enter the recipe",
            bg="#1a0e06", fg=ACCENT, font=("Georgia",11,"italic"))
        self._page_badge.grid(row=0, column=1, sticky="ew", padx=8)

        self._next_btn = tk.Button(
            nav_bar, text="Next ▶", command=self._next_page,
            bg=BTN, fg=FG, relief="flat", font=("Segoe UI",9),
            padx=10, cursor="hand2",
            activebackground=BTNHOV, activeforeground=FG, state="disabled")
        self._next_btn.grid(row=0, column=2, padx=(4,10), pady=2)

        # thumbnail row
        thumb_row = tk.Frame(right, bg=CARD)
        thumb_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(8,4))
        thumb_row.columnconfigure(1, weight=1)

        self._thumb_lbl = tk.Label(thumb_row, bg=CARD,
                                   text="No photo selected", fg=MUTED, font=("Segoe UI",9))
        self._thumb_lbl.grid(row=0, column=0, rowspan=3, padx=(0,12))

        self._fname_lbl = tk.Label(thumb_row, text="", bg=CARD, fg=FG,
                                   font=("Segoe UI",9,"bold"), anchor="w")
        self._fname_lbl.grid(row=0, column=1, sticky="w")

        tk.Label(thumb_row,
                 text="Enter the recipe name below, then add ingredients & instructions.",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8), justify="left",
                 ).grid(row=1, column=1, sticky="w", pady=(4,0))

        self._change_photo_btn = _btn(thumb_row, "📷  Change Photo…", self._change_photo)
        self._change_photo_btn.grid(row=2, column=1, sticky="w", pady=(6,0))

        # Recipe Name field  (row 2)
        rn_frame = tk.Frame(right, bg=CARD)
        rn_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(8,2))
        rn_frame.columnconfigure(1, weight=1)
        tk.Label(rn_frame, text="🍽  Recipe Name:",
                 bg=CARD, fg=ACCENT, font=("Georgia",10,"italic"),
                 anchor="w").grid(row=0, column=0, sticky="w", padx=(0,8))
        self._rname_entry = tk.Entry(
            rn_frame, textvariable=self.v_recipe_name,
            bg=ENTRY, fg=FG, insertbackground=FG,
            relief="flat", font=("Georgia",12),
            state="disabled")
        self._rname_entry.grid(row=0, column=1, sticky="ew")
        self.v_recipe_name.trace_add("write", self._on_rname_edit)

        # Recipe content text box (row 3 — expands)
        txt_label = tk.Frame(right, bg=CARD)
        txt_label.grid(row=3, column=0, sticky="nsew", padx=10, pady=(6,4))
        txt_label.columnconfigure(0, weight=1)
        txt_label.rowconfigure(1, weight=1)

        tk.Label(txt_label, text="✏  Recipe Content  (ingredients, instructions, notes)",
                 bg=CARD, fg=ACCENT, font=("Georgia",10,"italic"),
                 anchor="w").grid(row=0, column=0, sticky="w", pady=(0,4))

        tf = tk.Frame(txt_label, bg=ENTRY, bd=0)
        tf.grid(row=1, column=0, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)

        self._txt = tk.Text(
            tf, bg=ENTRY, fg=FG, insertbackground=FG,
            relief="flat", font=("Georgia",12),
            wrap="word", padx=12, pady=10,
            state="disabled", spacing1=2, spacing3=2)
        self._txt.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self._txt.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self._txt.configure(yscrollcommand=tsb.set)
        self._txt.bind("<<Modified>>", self._on_txt_edit)

        self._char_lbl = tk.Label(right, text="", bg=CARD, fg=MUTED,
                                  font=("Segoe UI",8), anchor="e")
        self._char_lbl.grid(row=4, column=0, sticky="e", padx=12, pady=(0,4))

    # ── Details tab ─────────────────────────────────────────────────────────────

    def _tab_details(self, p):
        img_t   = [("Images","*.jpg *.jpeg *.png *.webp *.bmp"),("All","*.*")]
        audio_t = [("Audio","*.mp3 *.wav *.ogg *.m4a *.aac *.flac"),("All","*.*")]

        self._sec(p, "Cover & Back Cover", 0)
        _row(p, "Cover image *",       self.v_cover, 1, lambda: self._file(self.v_cover, img_t))
        _row(p, "Back cover image *",  self.v_back,  2, lambda: self._file(self.v_back,  img_t))

        self._sec(p, "Output", 3)
        _row(p, "Output folder *",       self.v_output, 4, lambda: self._folder(self.v_output))
        _row(p, "Music file (optional)", self.v_music,  5, lambda: self._file(self.v_music, audio_t))

        self._sec(p, "Cookbook Info", 6)
        _row(p, "Cookbook title *",    self.v_title,    7)
        _row(p, "Subtitle (optional)", self.v_subtitle, 8)

        self._sec(p, "About the Chef  (back cover)", 9)
        _row(p, "Chef name *",            self.v_author_name,  10)
        _row(p, "Chef photo (optional)",  self.v_author_photo, 11,
             lambda: self._file(self.v_author_photo, img_t))

        tk.Label(p, text="Chef bio *", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w").grid(
            row=12, column=0, sticky="nw", padx=(12,4), pady=(8,0))

        bf = tk.Frame(p, bg=CARD)
        bf.grid(row=12, column=1, columnspan=2, sticky="ew", padx=(4,12), pady=(8,12))
        bf.columnconfigure(0, weight=1)
        self._bio = tk.Text(bf, bg=ENTRY, fg=FG, insertbackground=FG,
                            relief="flat", font=("Segoe UI",9),
                            wrap="word", height=5, padx=6, pady=4)
        self._bio.grid(row=0, column=0, sticky="ew")
        bsb = ttk.Scrollbar(bf, orient="vertical", command=self._bio.yview)
        bsb.grid(row=0, column=1, sticky="ns")
        self._bio.configure(yscrollcommand=bsb.set)

    # ── Style tab (scrollable) ──────────────────────────────────────────────────

    def _tab_style(self, outer):
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0, bd=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        p = tk.Frame(canvas, bg=CARD)
        p.columnconfigure(1, weight=1)
        win_id = canvas.create_window((0,0), window=p, anchor="nw")

        def _on_inner_cfg(e): canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_cfg(e): canvas.itemconfig(win_id, width=e.width)
        p.bind("<Configure>", _on_inner_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)

        def _on_enter(_):
            canvas.bind_all("<MouseWheel>",
                lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        def _on_leave(_):
            canvas.unbind_all("<MouseWheel>")
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)

        # ── Typography ────────────────────────────────────────────────────────
        self._sec(p, "Typography & Text Colour", 0)
        tk.Label(p, text="Font", bg=CARD, fg=FG, font=("Segoe UI",9),
                 anchor="w", width=24).grid(row=1, column=0, sticky="w", padx=(12,4), pady=5)
        ttk.Combobox(p, textvariable=self.v_font, values=FONT_CHOICES,
                     state="readonly", font=("Segoe UI",9)).grid(
            row=1, column=1, sticky="ew", padx=4, pady=5)

        self._cfr = tk.Frame(p, bg=CARD)
        self._cfr.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._cfr.columnconfigure(1, weight=1)
        _row(self._cfr, ".ttf / .otf path *", self.v_font_path, 0,
             lambda: self._file(self.v_font_path, [("Fonts","*.ttf *.otf"),("All","*.*")]))
        self._cfr.grid_remove()

        tk.Label(p, text="Text colour", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=3, column=0, sticky="w", padx=(12,4), pady=5)
        cr = tk.Frame(p, bg=CARD)
        cr.grid(row=3, column=1, sticky="w", padx=4, pady=5)
        tk.Entry(cr, textvariable=self.v_color, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas",9), width=10).pack(side="left")
        self._swatch = tk.Label(cr, width=3, bg=self.v_color.get(),
                                relief="flat", cursor="hand2")
        self._swatch.pack(side="left", padx=(8,0))
        self._swatch.bind("<Button-1>", lambda e: self._pick_color())
        _btn(cr, "Pick colour…", self._pick_color).pack(side="left", padx=(8,0))

        pr = tk.Frame(p, bg=CARD)
        pr.grid(row=4, column=1, sticky="w", padx=4, pady=(0,8))
        for name, hx in [("Dark Brown","#2c1810"),("Warm Black","#1a0e06"),
                          ("Deep Red","#5a1a0a"),("Black","#000000"),("White","#ffffff")]:
            _btn(pr, name, lambda h=hx: self.v_color.set(h)).pack(side="left", padx=3)

        # ── Video Text Size ────────────────────────────────────────────────────
        self._sec(p, "Video Text Size", 5)
        tk.Label(p, text="Recipe text size", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=6, column=0, sticky="w", padx=(12,4), pady=5)
        vfs_row = tk.Frame(p, bg=CARD)
        vfs_row.grid(row=6, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(vfs_row, variable=self.v_video_font_size, from_=0, to=72,
                 orient="horizontal", length=220, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(vfs_row, text="pt  (0 = auto-fit)",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

        # ── Page Background ────────────────────────────────────────────────────
        self._sec(p, "Recipe Page Background  (HTML & Video)", 7)
        tk.Label(p, text="Page colour", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=8, column=0, sticky="w", padx=(12,4), pady=5)
        pgcr = tk.Frame(p, bg=CARD)
        pgcr.grid(row=8, column=1, sticky="w", padx=4, pady=5)
        tk.Entry(pgcr, textvariable=self.v_page_bg, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas",9), width=10).pack(side="left")
        self._pg_swatch = tk.Label(pgcr, width=3, bg=self.v_page_bg.get(),
                                   relief="flat", cursor="hand2")
        self._pg_swatch.pack(side="left", padx=(8,0))
        self._pg_swatch.bind("<Button-1>", lambda e: self._pick_pg_color())
        _btn(pgcr, "Pick…", self._pick_pg_color).pack(side="left", padx=(8,0))

        pg_pre = tk.Frame(p, bg=CARD)
        pg_pre.grid(row=9, column=1, sticky="w", padx=4, pady=(0,6))
        for name, hx in [("Cream","#fdf8f0"),("White","#ffffff"),
                          ("Warm Grey","#e8e4de"),("Parchment","#f4ead5"),
                          ("Linen","#f0ece0"),("Black","#111111")]:
            _btn(pg_pre, name, lambda h=hx: self.v_page_bg.set(h)).pack(side="left", padx=3)

        self._sec(p, "Page Background Image  (optional)", 10)
        img_t = [("Images","*.jpg *.jpeg *.png *.webp *.bmp"),("All","*.*")]
        tk.Label(p, text="Background image", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=11, column=0, sticky="w", padx=(12,4), pady=5)
        bi_row = tk.Frame(p, bg=CARD)
        bi_row.grid(row=11, column=1, sticky="ew", padx=4, pady=5)
        bi_row.columnconfigure(0, weight=1)
        tk.Entry(bi_row, textvariable=self.v_page_bg_img, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI",9)).grid(row=0, column=0, sticky="ew")
        _btn(bi_row, "Browse…",
             lambda: self._file(self.v_page_bg_img, img_t)).grid(row=0, column=1, padx=(6,0))
        _btn(bi_row, "✕ Clear",
             lambda: self.v_page_bg_img.set("")).grid(row=0, column=2, padx=(4,0))

        tk.Label(p, text="Image opacity", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=12, column=0, sticky="w", padx=(12,4), pady=5)
        imo_row = tk.Frame(p, bg=CARD)
        imo_row.grid(row=12, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(imo_row, variable=self.v_page_img_opacity, from_=20, to=100,
                 orient="horizontal", length=220, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(imo_row, text="% (lower = more see-through)",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

        tk.Label(p, text="Flat colour opacity", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=13, column=0, sticky="w", padx=(12,4), pady=5)
        op_row = tk.Frame(p, bg=CARD)
        op_row.grid(row=13, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(op_row, variable=self.v_pg_opacity, from_=40, to=100,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(op_row, text="% opaque",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

        tk.Label(p, text="Background darkness", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=14, column=0, sticky="w", padx=(12,4), pady=5)
        dk_row = tk.Frame(p, bg=CARD)
        dk_row.grid(row=14, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(dk_row, variable=self.v_pg_dark, from_=0, to=60,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(dk_row, text="% dark overlay",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

        # ── Page Frame ────────────────────────────────────────────────────────
        self._sec(p, "Page Frame & Border", 15)
        FRAME_STYLES = ["None","Simple","Double","Vintage","Ornate Corners","3D Bevel"]
        tk.Label(p, text="Frame style", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=16, column=0, sticky="w", padx=(12,4), pady=5)
        ttk.Combobox(p, textvariable=self.v_frame_style,
                     values=FRAME_STYLES, state="readonly",
                     font=("Segoe UI",9)).grid(row=16, column=1, sticky="w", padx=4, pady=5)

        tk.Label(p, text="Frame colour", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=17, column=0, sticky="w", padx=(12,4), pady=5)
        fc_row = tk.Frame(p, bg=CARD)
        fc_row.grid(row=17, column=1, sticky="w", padx=4, pady=5)
        tk.Entry(fc_row, textvariable=self.v_frame_color, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas",9), width=10).pack(side="left")
        self._frame_swatch = tk.Label(fc_row, width=3,
                                      bg=self.v_frame_color.get(),
                                      relief="flat", cursor="hand2")
        self._frame_swatch.pack(side="left", padx=(8,0))
        self.v_frame_color.trace_add("write", lambda *_: self._refresh_frame_swatch())
        self._frame_swatch.bind("<Button-1>", lambda e: self._pick_frame_color())
        _btn(fc_row, "Pick…", self._pick_frame_color).pack(side="left", padx=(8,0))

        fp_row = tk.Frame(p, bg=CARD)
        fp_row.grid(row=18, column=1, sticky="w", padx=4, pady=(0,6))
        for name, hx in [("Gold","#c9a84c"),("Rose Gold","#b76e79"),
                          ("Dusty Sage","#7a9e7e"),("Warm Brown","#8B7355"),
                          ("Navy","#1a2a4a"),("Black","#111111")]:
            _btn(fp_row, name, lambda h=hx: self.v_frame_color.set(h)).pack(side="left", padx=3)

        tk.Label(p, text="Frame thickness", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=19, column=0, sticky="w", padx=(12,4), pady=5)
        ft_row = tk.Frame(p, bg=CARD)
        ft_row.grid(row=19, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(ft_row, variable=self.v_frame_thickness, from_=1, to=20,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(ft_row, text="px", bg=CARD, fg=MUTED,
                 font=("Segoe UI",8)).pack(side="left", padx=4)

        self._sec(p, "Custom Frame Image  (optional — PNG with transparent centre)", 20)
        tk.Label(p, text="Frame PNG", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=21, column=0, sticky="w", padx=(12,4), pady=5)
        fi_row = tk.Frame(p, bg=CARD)
        fi_row.grid(row=21, column=1, sticky="ew", padx=4, pady=5)
        fi_row.columnconfigure(0, weight=1)
        tk.Entry(fi_row, textvariable=self.v_frame_img, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI",9)).grid(row=0, column=0, sticky="ew")
        _btn(fi_row, "Browse…",
             lambda: self._file(self.v_frame_img, img_t)).grid(row=0, column=1, padx=(6,0))
        _btn(fi_row, "✕ Clear",
             lambda: self.v_frame_img.set("")).grid(row=0, column=2, padx=(4,0))
        tk.Label(p, text="Load any decorative PNG frame. Use a transparent-centre PNG.",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8), justify="left").grid(
            row=22, column=1, sticky="w", padx=4, pady=(0,8))

        tk.Label(p, text="Frame content padding", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=23, column=0, sticky="w", padx=(12,4), pady=5)
        fcp_row = tk.Frame(p, bg=CARD)
        fcp_row.grid(row=23, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(fcp_row, variable=self.v_frame_padding, from_=0, to=150,
                 orient="horizontal", length=220, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(fcp_row, text="px extra  (0 = auto — increase for large PNG corners)",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

        # ── Page Numbers ──────────────────────────────────────────────────────
        self._sec(p, "Recipe Page Numbers  (interactive & video)", 25)
        NUM_POS = ["Bottom Left","Bottom Center","Bottom Right",
                   "Top Left","Top Center","Top Right"]
        tk.Label(p, text="Page number position", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=26, column=0, sticky="w", padx=(12,4), pady=5)
        ttk.Combobox(p, textvariable=self.v_page_num_pos,
                     values=NUM_POS, state="readonly",
                     font=("Segoe UI",9)).grid(row=26, column=1, sticky="w", padx=4, pady=5)
        tk.Label(p, text="Only applies to recipe pages, not photo pages.",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).grid(
            row=27, column=1, sticky="w", padx=4, pady=(0,8))

        tk.Label(p, text="Page number size", bg=CARD, fg=FG,
                 font=("Segoe UI",9), anchor="w", width=24).grid(
            row=28, column=0, sticky="w", padx=(12,4), pady=5)
        pns_row = tk.Frame(p, bg=CARD)
        pns_row.grid(row=28, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(pns_row, variable=self.v_page_num_size, from_=8, to=48,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI",8)).pack(side="left")
        tk.Label(pns_row, text="pt  (HTML & video)",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=8)

    # ── Build tab ────────────────────────────────────────────────────────────────

    def _tab_build(self, p):
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        opts = tk.Frame(p, bg=CARD)
        opts.grid(row=0, column=0, sticky="ew", padx=12, pady=(12,6))
        tk.Label(opts, text="Output options:", bg=CARD, fg=ACCENT,
                 font=("Georgia",10,"italic")).pack(side="left", padx=(0,10))
        ttk.Checkbutton(opts, text="HTML Flipbook", variable=self.b_html).pack(side="left", padx=8)
        ttk.Checkbutton(opts, text="Embed assets",  variable=self.b_embed).pack(side="left", padx=8)
        ttk.Checkbutton(opts, text="MP4 Video",     variable=self.b_video).pack(side="left", padx=8)

        lw = tk.Frame(p, bg=CARD)
        lw.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        lw.columnconfigure(0, weight=1)
        lw.rowconfigure(0, weight=1)
        self._log_widget = tk.Text(lw, bg="#120805", fg="#d0b898",
                                   font=("Consolas",9), wrap="word",
                                   state="disabled", relief="flat", padx=8, pady=6)
        self._log_widget.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(lw, orient="vertical", command=self._log_widget.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._log_widget.configure(yscrollcommand=vsb.set)

        self._gen_btn = tk.Button(p, text="  Generate Cookbook  ",
                                  command=self._start_build,
                                  bg=BTN, fg="#fdf0da",
                                  font=("Georgia",13,"italic"),
                                  relief="flat", padx=24, pady=11,
                                  cursor="hand2",
                                  activebackground=BTNHOV, activeforeground="#fff")
        self._gen_btn.grid(row=2, column=0, pady=(8,14))

    # ── helpers ──────────────────────────────────────────────────────────────────

    def _sec(self, parent, text, row):
        tk.Label(parent, text=text, bg=CARD, fg=ACCENT,
                 font=("Georgia",11,"italic")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(14,2))

    def _folder(self, var):
        d = filedialog.askdirectory()
        if d: var.set(d)

    def _file(self, var, ft):
        f = filedialog.askopenfilename(filetypes=ft)
        if f: var.set(f)

    def _pick_color(self):
        r = colorchooser.askcolor(color=self.v_color.get(), title="Text colour")
        if r and r[1]: self.v_color.set(r[1])

    def _refresh_swatch(self):
        try: self._swatch.configure(bg=self.v_color.get())
        except Exception: pass

    def _refresh_pg_swatch(self):
        try: self._pg_swatch.configure(bg=self.v_page_bg.get())
        except Exception: pass

    def _pick_pg_color(self):
        r = colorchooser.askcolor(color=self.v_page_bg.get(), title="Recipe page colour")
        if r and r[1]: self.v_page_bg.set(r[1])

    def _refresh_frame_swatch(self):
        try: self._frame_swatch.configure(bg=self.v_frame_color.get())
        except Exception: pass

    def _pick_frame_color(self):
        r = colorchooser.askcolor(color=self.v_frame_color.get(), title="Frame colour")
        if r and r[1]: self.v_frame_color.set(r[1])

    def _on_font_change(self, *_):
        if self.v_font.get() == FONT_CHOICES[-1]: self._cfr.grid()
        else: self._cfr.grid_remove()

    # ── photo / recipe management ─────────────────────────────────────────────

    def _load_folder(self):
        d = filedialog.askdirectory(title="Select photo folder")
        if not d: return
        imgs = get_images(d)
        if not imgs:
            messagebox.showinfo("No images", "No supported images found.", parent=self)
            return
        existing = {ph["path"] for ph in self._photos}
        for p in imgs:
            if p not in existing:
                self._photos.append({"path": p, "name": "", "text": ""})
        self._refresh_lb()
        if self._photos and self._sel is None:
            self._lb.selection_set(0)
            self._on_select(None)

    def _add_photos(self):
        files = filedialog.askopenfilenames(
            title="Select food photos",
            filetypes=[("Images","*.jpg *.jpeg *.png *.webp *.bmp *.tiff"),("All","*.*")])
        existing = {ph["path"] for ph in self._photos}
        for f in files:
            p = Path(f)
            if p not in existing:
                self._photos.append({"path": p, "name": "", "text": ""})
        self._refresh_lb()

    def _reset_editor(self, msg="← Load photos, then select a page to enter the recipe"):
        self._page_badge.configure(text=msg)
        self._fname_lbl.configure(text="")
        self._thumb_lbl.configure(image="", text="No photo selected")
        self.v_recipe_name.set("")
        self._rname_entry.configure(state="disabled")
        self._txt.configure(state="disabled")
        self._txt.delete("1.0", "end")
        self._char_lbl.configure(text="")
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")

    def _clear_photos(self):
        if not self._photos: return
        if messagebox.askyesno("Clear all", "Remove all recipes?", parent=self):
            self._photos.clear()
            self._sel = None
            self._refresh_lb()
            self._reset_editor()

    def _remove(self):
        if self._sel is None: return
        self._save_txt()
        self._photos.pop(self._sel)
        if self._photos:
            self._sel = min(self._sel, len(self._photos) - 1)
            self._refresh_lb()
            self._lb.selection_set(self._sel)
            self._load_editor()
        else:
            self._sel = None
            self._refresh_lb()
            self._reset_editor()

    def _move_up(self):
        if self._sel is None or self._sel == 0: return
        self._save_txt()
        i = self._sel
        self._photos[i-1], self._photos[i] = self._photos[i], self._photos[i-1]
        self._sel = i - 1
        self._refresh_lb()
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)

    def _move_down(self):
        if self._sel is None or self._sel >= len(self._photos) - 1: return
        self._save_txt()
        i = self._sel
        self._photos[i+1], self._photos[i] = self._photos[i], self._photos[i+1]
        self._sel = i + 1
        self._refresh_lb()
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)

    def _refresh_lb(self):
        self._lb.delete(0, "end")
        total = len(self._photos)
        for i, ph in enumerate(self._photos):
            has_data = "✎" if (ph["name"].strip() or ph["text"].strip()) else "  "
            name_disp = ph["name"].strip() or ph["path"].name
            self._lb.insert("end", f"  {has_data}  {i+1:02d}/{total}  {name_disp}")
        if self._sel is not None and self._sel < len(self._photos):
            self._lb.selection_set(self._sel)
            self._lb.see(self._sel)

    def _save_txt(self):
        if self._sel is not None and self._sel < len(self._photos):
            self._photos[self._sel]["name"] = self.v_recipe_name.get()
            self._photos[self._sel]["text"] = self._txt.get("1.0", "end-1c")

    def _go_to_page(self, idx):
        if not self._photos or idx < 0 or idx >= len(self._photos): return
        self._save_txt()
        self._sel = idx
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)
        self._load_editor()

    def _prev_page(self):
        if self._sel is not None and self._sel > 0:
            self._go_to_page(self._sel - 1)

    def _next_page(self):
        if self._sel is not None and self._sel < len(self._photos) - 1:
            self._go_to_page(self._sel + 1)

    def _load_editor(self):
        ph    = self._photos[self._sel]
        total = len(self._photos)
        idx   = self._sel
        self._page_badge.configure(text=f"Recipe {idx+1} of {total}")
        self._fname_lbl.configure(text=ph["path"].name)
        self._prev_btn.configure(state="normal" if idx > 0         else "disabled")
        self._next_btn.configure(state="normal" if idx < total - 1 else "disabled")
        try:
            img = Image.open(ph["path"])
            img.thumbnail((220, 138), Image.LANCZOS)
            self._thumb = ImageTk.PhotoImage(img)
            self._thumb_lbl.configure(image=self._thumb, text="")
        except Exception:
            self._thumb_lbl.configure(image="", text="(preview unavailable)")
        # Recipe name
        self.v_recipe_name.set(ph.get("name", ""))
        self._rname_entry.configure(state="normal")
        # Recipe content
        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.insert("1.0", ph["text"])
        self._txt.edit_modified(False)
        self._txt.focus_set()
        self._update_char_count()
        self._refresh_lb()

    def _update_char_count(self):
        if self._sel is None: return
        chars = len(self._txt.get("1.0","end-1c"))
        colour = "#e08040" if chars > 800 else MUTED
        self._char_lbl.configure(
            text=f"{chars} characters  {'(long — may auto-shrink)' if chars > 800 else ''}",
            fg=colour)

    def _on_select(self, _):
        sel = self._lb.curselection()
        if not sel: return
        self._save_txt()
        self._sel = sel[0]
        self._load_editor()

    def _on_txt_edit(self, _):
        if self._txt.edit_modified():
            self._save_txt()
            self._txt.edit_modified(False)
            self._update_char_count()
            self._refresh_lb()

    def _on_rname_edit(self, *_):
        if self._sel is not None and self._sel < len(self._photos):
            self._photos[self._sel]["name"] = self.v_recipe_name.get()
            self._refresh_lb()

    def _change_photo(self):
        """Replace the photo for the currently selected recipe page."""
        if self._sel is None:
            return
        f = filedialog.askopenfilename(
            title="Select a new photo for this recipe",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp *.tiff"), ("All", "*.*")])
        if f:
            self._photos[self._sel]["path"] = Path(f)
            self._load_editor()
            self._refresh_lb()

    def _import_from_file(self):
        """
        Parse a .txt / .docx / .pdf into recipes, then open ImportDialog
        so the user can assign photos before adding them to the project.
        Handles scanned PDFs by extracting embedded images as photo slots.
        """
        ft = [
            ("Recipe files",  "*.txt *.docx *.pdf"),
            ("Text files",    "*.txt"),
            ("Word documents","*.docx"),
            ("PDF files",     "*.pdf"),
            ("All files",     "*.*"),
        ]
        f = filedialog.askopenfilename(title="Select recipe file", filetypes=ft)
        if not f:
            return

        self.configure(cursor="watch")
        self.update()
        try:
            recipes = parse_recipes_from_file(f)
        except Exception as e:
            self.configure(cursor="")
            messagebox.showerror("Error reading file", str(e), parent=self)
            return
        self.configure(cursor="")

        pdf_path     = Path(f) if f.lower().endswith(".pdf") else None
        preset_imgs  = []

        # ── Scanned PDF: recipes is None (no extractable text) ─────────────────
        if recipes is None:
            # Work out where to save extracted images — user's output folder,
            # or a subfolder next to the PDF if no output folder is set yet.
            out_base = self.v_output.get().strip()
            if out_base:
                pdf_img_dir = Path(out_base) / "_pdf_import"
            else:
                pdf_img_dir = Path(f).parent / "_pdf_import"
            pdf_img_dir.mkdir(parents=True, exist_ok=True)

            # Extract embedded images from the PDF pages
            self.configure(cursor="watch")
            self.update()
            imgs = extract_images_from_pdf(Path(f), output_dir=pdf_img_dir)
            self.configure(cursor="")

            if not imgs:
                messagebox.showerror(
                    "Scanned PDF — no images found",
                    "This PDF contains scanned pages (images, not text) and no\n"
                    "extractable embedded images were found either.\n\n"
                    "Options:\n"
                    "  • Export the PDF pages as JPG/PNG images and use\n"
                    "    'Load Folder' or 'Add Photos' instead.\n"
                    "  • Use a PDF with selectable text for recipe content.",
                    parent=self)
                return

            # One blank recipe slot per extracted image
            recipes     = [{"name": f"Recipe {i+1}", "text": ""} for i in range(len(imgs))]
            preset_imgs = imgs
            messagebox.showinfo(
                "Scanned PDF detected",
                f"This PDF is scanned (image-based), so recipe text could not be read.\n\n"
                f"Found {len(imgs)} embedded image{'s' if len(imgs)!=1 else ''} — "
                f"each has been assigned as a recipe photo.\n\n"
                f"Enter the recipe name and content for each page in the editor after importing.",
                parent=self)

        # ── Normal case: no recipes detected in a text file ────────────────────
        elif not recipes:
            messagebox.showinfo(
                "No recipes found",
                "Could not detect any recipe sections in that file.\n\n"
                "Tips for .txt files:\n"
                "  • Put each recipe name on its own line followed by '---'\n"
                "  • Or number them:  1. Recipe Name\n"
                "  • Or use ALL CAPS names on their own line",
                parent=self)
            return

        dlg = ImportDialog(self, recipes, pdf_path=pdf_path, preset_photos=preset_imgs)
        imported = dlg.result
        if not imported:
            return

        existing = {ph["path"] for ph in self._photos}
        added = 0
        for ph in imported:
            if ph["path"] not in existing:
                self._photos.append(ph)
                added += 1

        self._refresh_lb()
        if added and self._sel is None and self._photos:
            self._lb.selection_set(0)
            self._on_select(None)

        messagebox.showinfo(
            "Recipes imported",
            f"Added {added} recipe{'s' if added != 1 else ''} to your cookbook.\n"
            "You can now edit names, content, or swap photos in the editor on the right.",
            parent=self)

    # ── build ────────────────────────────────────────────────────────────────────

    def _start_build(self):
        self._save_txt()
        errors = []
        if not self._photos:
            errors.append("  • No recipes — add photos in the Recipes tab")
        for label, var in [("Cover image", self.v_cover), ("Back cover image", self.v_back),
                           ("Output folder", self.v_output), ("Cookbook title", self.v_title),
                           ("Chef name", self.v_author_name)]:
            if not var.get().strip():
                errors.append(f"  • {label} is required")
        if not self._bio.get("1.0","end").strip():
            errors.append("  • Chef bio is required (Details tab)")
        if self.v_font.get() == FONT_CHOICES[-1] and not self.v_font_path.get().strip():
            errors.append("  • Custom font path required")
        if not self.b_html.get() and not self.b_video.get():
            errors.append("  • Select at least one output type")
        if errors:
            messagebox.showerror("Missing fields", "\n".join(errors), parent=self)
            return

        is_custom = self.v_font.get() == FONT_CHOICES[-1]
        font_path = Path(self.v_font_path.get().strip()) if is_custom else self.v_font.get()
        font_name = Path(self.v_font_path.get().strip()).stem if is_custom else self.v_font.get()
        music = self.v_music.get().strip()
        photo = self.v_author_photo.get().strip()

        cfg = dict(
            photos            = self._photos,
            cover_img         = Path(self.v_cover.get().strip()),
            back_img          = Path(self.v_back.get().strip()),
            music_file        = Path(music) if music else None,
            output_dir        = Path(self.v_output.get().strip()),
            title             = self.v_title.get().strip(),
            subtitle          = self.v_subtitle.get().strip(),
            author_name       = self.v_author_name.get().strip(),
            author_bio        = self._bio.get("1.0","end").strip(),
            author_photo      = Path(photo) if photo else None,
            font_path         = font_path,
            font_name         = font_name,
            text_color        = self.v_color.get(),
            page_bg_color     = self.v_page_bg.get(),
            page_opacity      = self.v_pg_opacity.get(),
            page_darkness     = self.v_pg_dark.get(),
            video_font_size   = self.v_video_font_size.get(),
            page_bg_img       = self.v_page_bg_img.get().strip(),
            page_img_opacity  = self.v_page_img_opacity.get(),
            frame_style       = self.v_frame_style.get(),
            frame_color       = self.v_frame_color.get(),
            frame_thickness   = self.v_frame_thickness.get(),
            frame_img         = self.v_frame_img.get().strip(),
            frame_padding     = self.v_frame_padding.get(),
            page_num_pos      = self.v_page_num_pos.get(),
            page_num_size     = self.v_page_num_size.get(),
            build_html        = self.b_html.get(),
            build_video       = self.b_video.get(),
            embed_assets      = self.b_embed.get(),
        )
        self._gen_btn.configure(state="disabled", text="  Working…  ")
        self._log_clear()
        self._log("=" * 52)
        self._log("    MULTIMEDIA COOKBOOK FACTORY")
        self._log("=" * 52)
        self._log(f"  {len(self._photos)} recipes")
        threading.Thread(target=self._run, args=(cfg,), daemon=True).start()

    def _run(self, cfg):
        try:
            cfg["output_dir"].mkdir(parents=True, exist_ok=True)
            if cfg["build_html"]:
                self._log("\n-- BUILDING HTML FLIPBOOK --")
                build_html(cfg, self._log)
            if cfg["build_video"]:
                if not FFMPEG:
                    self._log("\n  ERROR: FFmpeg not found — skipping video.")
                else:
                    self._log("\n-- BUILDING VIDEO --")
                    build_video(cfg, self._log)
            self._log("\n" + "=" * 52)
            self._log("  Done!  Opening output folder …")
            self._log("=" * 52)
            import os
            self.after(0, lambda: os.startfile(str(cfg["output_dir"])))
        except Exception:
            self._log("\n  ERROR:")
            self._log(traceback.format_exc())
        finally:
            self.after(0, lambda: self._gen_btn.configure(
                state="normal", text="  Generate Cookbook  "))

    def _log(self, msg):
        def _do():
            self._log_widget.configure(state="normal")
            self._log_widget.insert("end", msg + "\n")
            self._log_widget.see("end")
            self._log_widget.configure(state="disabled")
        self.after(0, _do)

    def _log_clear(self):
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0","end")
        self._log_widget.configure(state="disabled")


# ── import dialog ──────────────────────────────────────────────────────────────

class ImportDialog(tk.Toplevel):
    """
    Modal dialog shown after parsing a recipe file.
    Lists every parsed recipe and lets the user assign a photo to each one.
    Supports:
      • Manual Browse per recipe
      • Auto-assign from a folder (alphabetical order)
      • Auto-extract photos embedded in a PDF
    """

    def __init__(self, parent, recipes, pdf_path=None, preset_photos=None):
        super().__init__(parent)
        self.title("Import Recipes")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.geometry("960x660")
        self.minsize(720, 480)
        self.transient(parent)
        self.grab_set()

        self._recipes       = [dict(r) for r in recipes]
        self._pdf_path      = pdf_path          # set when source was a PDF
        self._preset_photos = preset_photos or []  # pre-assigned image paths
        self._photo_vars    = []                # StringVar per recipe
        self._name_vars     = []                # StringVar per recipe
        self._status_lbl    = {}                # idx -> tk.Label
        self._result        = None              # filled on confirm

        self._build()
        # Apply any pre-assigned photos after widgets exist
        for i, img_path in enumerate(self._preset_photos):
            if i < len(self._photo_vars):
                self._photo_vars[i].set(str(img_path))
        self.wait_window()

    @property
    def result(self):
        return self._result

    # ── layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        n = len(self._recipes)

        # Header
        hdr = tk.Frame(self, bg=PANEL, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"Found {n} recipe{'s' if n != 1 else ''}",
                 bg=PANEL, fg=ACCENT, font=("Georgia", 17, "italic")).pack()
        tk.Label(hdr,
                 text="Assign a food photo to each recipe — or use the helpers below.",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(pady=(2, 0))

        # Toolbar
        tbar = tk.Frame(self, bg=CARD, pady=8, padx=10)
        tbar.pack(fill="x")
        _btn(tbar, "📁  Auto-assign from folder…",
             self._auto_assign_folder).pack(side="left", padx=(0, 6))
        if self._pdf_path:
            _btn(tbar, "📄  Extract photos from this PDF",
                 self._extract_pdf_photos).pack(side="left", padx=(0, 6))
        tk.Label(tbar,
                 text="  Photos assigned left-to-right to recipes in order",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left")

        # Scrollable recipe list
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=10, pady=6)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner  = tk.Frame(canvas, bg=BG)
        inner.columnconfigure(0, weight=1)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.bind("<Enter>", lambda _: canvas.bind_all(
            "<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")))
        canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

        for idx, recipe in enumerate(self._recipes):
            pv = tk.StringVar()
            nv = tk.StringVar(value=recipe.get("name", ""))
            self._photo_vars.append(pv)
            self._name_vars.append(nv)
            self._build_row(inner, idx, nv, pv, recipe.get("text", ""))

        # Footer
        foot = tk.Frame(self, bg=PANEL, pady=10)
        foot.pack(fill="x")
        _btn(foot, "✓  Import All Recipes", self._do_import).pack(side="left", padx=(20, 8))
        _btn(foot, "Cancel", self.destroy, danger=True).pack(side="left", padx=4)
        tk.Label(foot,
                 text="  Recipes without a photo will be skipped during Generate.",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=10)

    def _build_row(self, parent, idx, name_var, photo_var, content):
        row_bg = CARD if idx % 2 == 0 else PANEL
        row    = tk.Frame(parent, bg=row_bg, pady=8, padx=10)
        row.grid(row=idx, column=0, sticky="ew", pady=1)
        row.columnconfigure(1, weight=1)

        # Number badge
        tk.Label(row, text=f"{idx + 1:02d}", bg=row_bg, fg=ACCENT,
                 font=("Georgia", 14, "bold"), width=3,
                 anchor="n").grid(row=0, column=0, rowspan=3, sticky="nw", padx=(0, 10))

        # Name entry
        name_frame = tk.Frame(row, bg=row_bg)
        name_frame.grid(row=0, column=1, sticky="ew", pady=(0, 2))
        name_frame.columnconfigure(0, weight=1)
        tk.Label(name_frame, text="Recipe name:",
                 bg=row_bg, fg=MUTED, font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
        tk.Entry(name_frame, textvariable=name_var,
                 bg=ENTRY, fg=FG, insertbackground=FG,
                 relief="flat", font=("Georgia", 11)).grid(row=1, column=0, sticky="ew")

        # Content preview
        preview = (content[:140].replace("\n", "  ").strip() + ("…" if len(content) > 140 else ""))
        tk.Label(row, text=preview, bg=row_bg, fg=MUTED,
                 font=("Segoe UI", 8), anchor="w",
                 wraplength=560, justify="left").grid(row=1, column=1, sticky="w", pady=(0, 4))

        # Photo picker row
        ph_row = tk.Frame(row, bg=row_bg)
        ph_row.grid(row=2, column=1, sticky="ew")
        ph_row.columnconfigure(1, weight=1)

        tk.Label(ph_row, text="📷 Photo:", bg=row_bg, fg=FG,
                 font=("Segoe UI", 9)).grid(row=0, column=0, padx=(0, 6))

        tk.Entry(ph_row, textvariable=photo_var,
                 bg=ENTRY, fg=FG, insertbackground=FG,
                 relief="flat", font=("Segoe UI", 8)).grid(row=0, column=1, sticky="ew")

        status = tk.Label(ph_row, text="(none)", bg=row_bg, fg=MUTED,
                          font=("Segoe UI", 8), width=6, anchor="w")
        status.grid(row=0, column=3, padx=(6, 0))
        self._status_lbl[idx] = status

        def _browse(pv=photo_var, sl=status):
            f = filedialog.askopenfilename(
                title="Select photo for this recipe",
                filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All", "*.*")])
            if f:
                pv.set(f)
                sl.configure(text="✓", fg="#6dbe45")

        def _on_change(*_, pv=photo_var, sl=status):
            p = pv.get().strip()
            if p and Path(p).exists():
                sl.configure(text="✓", fg="#6dbe45")
            else:
                sl.configure(text="(none)", fg=MUTED)

        photo_var.trace_add("write", _on_change)
        _btn(ph_row, "Browse…", _browse).grid(row=0, column=2, padx=(6, 0))

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _assign_photos(self, img_paths):
        """Assign a list of image paths to recipes in order."""
        for i, pv in enumerate(self._photo_vars):
            if i < len(img_paths):
                pv.set(str(img_paths[i]))
                if i in self._status_lbl:
                    self._status_lbl[i].configure(text="✓", fg="#6dbe45")
        n_done    = min(len(img_paths), len(self._photo_vars))
        n_missing = max(0, len(self._photo_vars) - len(img_paths))
        msg = f"Assigned {n_done} photo{'s' if n_done != 1 else ''}."
        if n_missing:
            msg += f"\n{n_missing} recipe{'s' if n_missing != 1 else ''} still need a photo."
        messagebox.showinfo("Photos assigned", msg, parent=self)

    def _auto_assign_folder(self):
        d = filedialog.askdirectory(title="Select folder of food photos")
        if not d:
            return
        imgs = sorted(
            [f for f in Path(d).iterdir() if f.suffix.lower() in SUPPORTED_IMG],
            key=lambda p: p.name)
        if not imgs:
            messagebox.showinfo("No images",
                "No supported images found in that folder.", parent=self)
            return
        self._assign_photos(imgs)

    def _extract_pdf_photos(self):
        if not self._pdf_path:
            return
        # Save extracted images alongside the PDF so nothing lands on C:
        pdf_img_dir = self._pdf_path.parent / "_pdf_import"
        pdf_img_dir.mkdir(parents=True, exist_ok=True)
        self.configure(cursor="watch")
        self.update()
        imgs = extract_images_from_pdf(self._pdf_path, output_dir=pdf_img_dir)
        self.configure(cursor="")
        if not imgs:
            messagebox.showinfo(
                "No images found",
                "No embedded images were found in the PDF.\n\n"
                "The PDF may use scanned pages (not extractable) or vector graphics.\n"
                "Try 'Auto-assign from folder' with exported page images instead.",
                parent=self)
            return
        self._assign_photos(imgs)

    def _do_import(self):
        results  = []
        no_photo = []
        for i, recipe in enumerate(self._recipes):
            name      = self._name_vars[i].get().strip()
            photo_str = self._photo_vars[i].get().strip()
            p         = Path(photo_str) if photo_str else None
            if p and p.exists():
                results.append({"path": p, "name": name, "text": recipe["text"]})
            else:
                no_photo.append(name or f"Recipe {i + 1}")

        if no_photo:
            bullet_list = "\n".join(f"  • {n}" for n in no_photo[:8])
            if len(no_photo) > 8:
                bullet_list += f"\n  … and {len(no_photo) - 8} more"
            if results:
                ok = messagebox.askyesno(
                    "Some recipes have no photo",
                    f"{len(no_photo)} recipe(s) have no photo and will be skipped:\n"
                    f"{bullet_list}\n\n"
                    f"Import the {len(results)} recipe(s) that do have photos?",
                    parent=self)
                if not ok:
                    return
            else:
                messagebox.showerror(
                    "No photos assigned",
                    "No recipes have photos assigned.\n"
                    "Please assign at least one photo before importing.",
                    parent=self)
                return

        if results:
            self._result = results
            self.destroy()


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()
