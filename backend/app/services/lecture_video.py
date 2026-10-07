"""Turn a finished PPTX and per-slide spoken scripts into one lecture MP4.

Each slide image is shown while that slide's script is spoken. PowerPoint or
LibreOffice export the designed slides when available; otherwise the PPTX is
painted from its own pictures, shapes and text at their original positions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from io import BytesIO
from pathlib import Path
from typing import Callable

from pptx import Presentation
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

from ..models.lecture_script import LectureScript
from .text_utils import stable_hash

logger = logging.getLogger("ailecturegen")

WIDTH, HEIGHT = 1920, 1080
RENDER_VERSION = "raster-v2"
TIMING_VERSION = "speech-v2"
OUTPUT_NAME = "lecture.mp4"
PROGRESS_NAME = "progress.json"
RESULT_NAME = "result.json"
JOB_NAME = "job.json"
_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_HEARTBEAT_SECONDS = 5
_HEARTBEAT_STALE_SECONDS = 180


def video_fingerprint(pptx: Path, script: LectureScript, intro: Path | None = None) -> str:
    digest = pptx.stat().st_size
    try:
        data = pptx.read_bytes()
        digest = len(data)
        from hashlib import sha256

        digest = sha256(data).hexdigest()
    except OSError:
        digest = str(digest)
    intro_key = None
    if intro is not None and intro.is_file():
        from .intro_library import intro_digest

        intro_key = intro_digest(intro)
    return stable_hash(
        {
            "pptx": digest,
            "render": RENDER_VERSION,
            "timing": TIMING_VERSION,
            "slides": [(s.slide_number, s.title, s.text) for s in script.slides],
            "intro": intro_key,
        }
    )


def write_progress(job_dir: Path, *, progress: int, stage: str) -> None:
    payload = {"progress": max(0, min(100, progress)), "stage": stage, "ts": time.time()}
    tmp = job_dir / (PROGRESS_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, job_dir / PROGRESS_NAME)


def read_progress(job_dir: Path) -> dict:
    try:
        data = json.loads((job_dir / PROGRESS_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class VideoPipeline:
    """Override render/speak/mux in tests. The default uses local tools only."""

    def render_slides(self, pptx: Path, dest: Path) -> tuple[list[Path], str | None]:
        return render_slides(pptx, dest)

    def speak(self, text: str, dest: Path, min_seconds: float = 0) -> Path:
        return speak(text, dest, min_seconds)

    def mux(self, pairs: list[tuple[Path, Path]], dest: Path) -> tuple[Path, float]:
        return mux_clips(pairs, dest)

    def prepend(self, intro: Path, lecture: Path) -> float:
        return prepend_intro(intro, lecture)


def run_job(job_dir: Path, pipeline: VideoPipeline | None = None) -> None:
    job_dir = Path(job_dir)
    pipeline = pipeline or VideoPipeline()
    job = json.loads((job_dir / JOB_NAME).read_text(encoding="utf-8"))
    stop = threading.Event()

    def beat() -> None:
        while not stop.is_set():
            try:
                (job_dir / "heartbeat.txt").write_text(str(time.time()), encoding="utf-8")
            except OSError:
                pass
            stop.wait(_HEARTBEAT_SECONDS)

    threading.Thread(target=beat, daemon=True).start()
    report: dict = {"ok": False, "error": None, "slide_count": 0, "duration_seconds": 0, "note": None}
    try:
        write_progress(job_dir, progress=5, stage="슬라이드 화면을 준비하는 중")
        slides_dir = job_dir / "slides"
        slides_dir.mkdir(exist_ok=True)
        images, note = pipeline.render_slides(Path(job["pptx_path"]), slides_dir)
        scripts = job["scripts"]
        n = min(len(images), len(scripts))
        if n < 1:
            raise RuntimeError("슬라이드 화면을 만들지 못했습니다.")
        if len(images) != len(scripts):
            note = (note + " " if note else "") + (
                f"PPT {len(images)}장과 대본 {len(scripts)}장을 앞에서부터 {n}장만 맞췄습니다."
            )
        pairs: list[tuple[Path, Path]] = []
        audio_dir = job_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        for i in range(n):
            write_progress(
                job_dir,
                progress=10 + int(70 * (i / n)),
                stage=f"{i + 1}/{n}장 음성을 만드는 중",
            )
            item = scripts[i]
            text = (item.get("text") or "").strip()
            audio = pipeline.speak(text, audio_dir / f"s{i + 1:03d}.wav")
            pairs.append((images[i], audio))
        write_progress(job_dir, progress=88, stage="대본을 끝까지 읽은 뒤 다음 장으로 넘기는 중")
        out = job_dir / OUTPUT_NAME
        _, duration = pipeline.mux(pairs, out)
        intro = job.get("intro_path")
        if intro:
            write_progress(job_dir, progress=94, stage="인트로를 앞에 붙이는 중")
            duration += pipeline.prepend(Path(intro), out)
            note = (note + " " if note else "") + "강의 앞에 인트로를 넣었습니다."
        if not out.is_file() or out.stat().st_size < 32:
            raise RuntimeError("영상 파일을 만들지 못했습니다.")
        report.update(ok=True, slide_count=n, duration_seconds=int(round(duration)), note=note)
        write_progress(job_dir, progress=100, stage="완료")
    except Exception as exc:
        logger.exception("Lecture video job failed")
        report["error"] = str(exc) or "강의 영상을 만들지 못했습니다."
        write_progress(job_dir, progress=0, stage="실패")
    finally:
        stop.set()
        tmp = job_dir / (RESULT_NAME + ".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, job_dir / RESULT_NAME)


def launch_thread(job_dir: Path, pipeline: VideoPipeline | None = None) -> None:
    threading.Thread(target=run_job, args=(job_dir, pipeline), daemon=True).start()


# --------------------------------------------------------------------------- render
def render_slides(pptx: Path, dest: Path) -> tuple[list[Path], str | None]:
    dest.mkdir(parents=True, exist_ok=True)
    exported = _export_with_office(pptx, dest)
    if exported:
        return exported, None
    return _rasterize_pptx(pptx, dest), None


def _export_with_office(pptx: Path, dest: Path) -> list[Path]:
    ppt = _find_powerpoint()
    if ppt:
        try:
            return _export_powerpoint(pptx, dest)
        except Exception:
            logger.info("PowerPoint export failed; trying another renderer")
    soffice = _find_soffice()
    if soffice:
        try:
            return _export_libreoffice(soffice, pptx, dest)
        except Exception:
            logger.info("LibreOffice export failed; drawing slides from the PPTX text")
    return []


def _find_powerpoint() -> str | None:
    for path in (
        r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
        r"C:\Program Files\Microsoft Office\Office16\POWERPNT.EXE",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
    ):
        if Path(path).is_file():
            return path
    return shutil.which("POWERPNT.EXE")


def _find_soffice() -> str | None:
    for path in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if Path(path).is_file():
            return path
    return shutil.which("soffice")


def _export_powerpoint(pptx: Path, dest: Path) -> list[Path]:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = win32com.client.Dispatch("PowerPoint.Application")
    try:
        pres = app.Presentations.Open(str(pptx.resolve()), WithWindow=False)
        try:
            pres.Export(str(dest.resolve()), "PNG", WIDTH, HEIGHT)
        finally:
            pres.Close()
    finally:
        app.Quit()
        pythoncom.CoUninitialize()
    return _sorted_images(dest)


def _export_libreoffice(soffice: str, pptx: Path, dest: Path) -> list[Path]:
    subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(dest), str(pptx)],
        check=True, capture_output=True, timeout=180,
    )
    pdfs = list(dest.glob("*.pdf"))
    if not pdfs:
        return []
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdfs[0]))
    paths: list[Path] = []
    for i, page in enumerate(pdf, 1):
        pil = page.render(scale=2).to_pil()
        path = dest / f"slide-{i:03d}.png"
        pil.convert("RGB").save(path)
        paths.append(path)
    return paths


def _sorted_images(dest: Path) -> list[Path]:
    files = [p for p in dest.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    files.sort(key=lambda p: p.name.lower())
    return files


def _rasterize_pptx(pptx: Path, dest: Path) -> list[Path]:
    """Paint each PPTX slide from its own pictures, shapes and text (no Office)."""
    from PIL import Image, ImageDraw

    dest.mkdir(parents=True, exist_ok=True)
    prs = Presentation(str(pptx))
    sw, sh = int(prs.slide_width), int(prs.slide_height)
    sx, sy = WIDTH / sw, HEIGHT / sh
    font_scale = HEIGHT / float(prs.slide_height.pt)
    paths: list[Path] = []
    for i, slide in enumerate(prs.slides, 1):
        img = Image.new("RGB", (WIDTH, HEIGHT), _slide_bg(slide))
        draw = ImageDraw.Draw(img)
        for shape in slide.shapes:
            _paint_shape(img, draw, shape, sx, sy, font_scale)
        path = dest / f"slide-{i:03d}.png"
        img.save(path)
        paths.append(path)
    return paths


def _slide_bg(slide) -> tuple[int, int, int]:
    try:
        rgb = slide.background.fill.fore_color.rgb
        return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except Exception:
        return (255, 255, 255)


def _paint_shape(img, draw, shape, sx: float, sy: float, font_scale: float) -> None:
    kind = getattr(shape, "shape_type", None)
    if kind == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            _paint_shape(img, draw, child, sx, sy, font_scale)
        return
    box = _shape_box(shape, sx, sy)
    if box is None:
        return
    if kind == MSO_SHAPE_TYPE.PICTURE or _has_picture(shape):
        _paint_picture(img, shape, box)
    elif kind in (MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.TEXT_BOX, MSO_SHAPE_TYPE.PLACEHOLDER):
        fill = _shape_fill(shape)
        if fill is not None:
            _paint_fill(draw, shape, box, fill)
    if getattr(shape, "has_text_frame", False):
        _paint_text(img, draw, shape, box, sx, sy, font_scale)


def _paint_fill(draw, shape, box: tuple[int, int, int, int], fill: tuple[int, int, int]) -> None:
    x, y, w, h = box
    rect = (x, y, x + w, y + h)
    auto = None
    try:
        auto = shape.auto_shape_type
    except Exception:
        auto = None
    if auto == MSO_AUTO_SHAPE_TYPE.OVAL:
        draw.ellipse(rect, fill=fill)
        return
    if auto == MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE:
        radius = max(4, int(min(w, h) * 0.12))
        draw.rounded_rectangle(rect, radius=radius, fill=fill)
        return
    draw.rectangle(rect, fill=fill)


def _has_picture(shape) -> bool:
    try:
        return shape.image is not None
    except Exception:
        return False


def _shape_box(shape, sx: float, sy: float) -> tuple[int, int, int, int] | None:
    try:
        x = int((shape.left or 0) * sx)
        y = int((shape.top or 0) * sy)
        w = max(1, int((shape.width or 0) * sx))
        h = max(1, int((shape.height or 0) * sy))
    except Exception:
        return None
    return x, y, w, h


def _shape_fill(shape) -> tuple[int, int, int] | None:
    try:
        from pptx.enum.dml import MSO_FILL

        fill = shape.fill
        if fill.type != MSO_FILL.SOLID:
            return None
        rgb = fill.fore_color.rgb
        return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except Exception:
        return None


def _paint_picture(img, shape, box: tuple[int, int, int, int]) -> None:
    from PIL import Image

    try:
        blob = shape.image.blob
    except Exception:
        return
    try:
        pic = Image.open(BytesIO(blob)).convert("RGBA")
    except Exception:
        return
    x, y, w, h = box
    pic = pic.resize((max(1, w), max(1, h)), Image.Resampling.LANCZOS)
    img.paste(pic, (x, y), pic)


def _paint_text(img, draw, shape, box: tuple[int, int, int, int], sx: float, sy: float, font_scale: float) -> None:
    from PIL import Image, ImageDraw
    from pptx.enum.text import PP_ALIGN

    x, y, w, h = box
    if w < 1 or h < 1:
        return
    left_in, top_in, right_in, bottom_in = _text_insets(shape, sx, sy)
    max_w = max(8, w - left_in - right_in)
    max_h = max(8, h - top_in - bottom_in)
    wrap = bool(getattr(shape.text_frame, "word_wrap", False))
    fallback = _contrast_color(img, box)
    paragraphs: list[tuple[list[list[tuple[str, int, tuple[int, int, int], bool]]], object]] = []
    for para in shape.text_frame.paragraphs:
        paragraphs.append((_para_hard_lines(para, font_scale, fallback), getattr(para, "alignment", None)))

    def layout(scale: float) -> list[tuple[list[tuple[str, int, tuple[int, int, int], bool]], object, int]]:
        lines: list[tuple[list[tuple[str, int, tuple[int, int, int], bool]], object, int]] = []
        for hard_lines, align in paragraphs:
            for spans in hard_lines:
                scaled = [(t, max(8, int(round(px * scale))), c, b) for t, px, c, b in spans]
                for group in _wrap_spans(draw, scaled, max_w, wrap):
                    line_h = max((max(int(px * 1.12), px) for _t, px, _c, _b in group), default=12)
                    lines.append((group, align, line_h))
        return lines

    painted = layout(1.0)
    total_h = sum(line_h for *_rest, line_h in painted)
    if total_h > max_h and total_h > 0:
        painted = layout(max(0.35, max_h / total_h))
        total_h = sum(line_h for *_rest, line_h in painted)
    anchor = _text_anchor(shape)
    if anchor == "ctr":
        cy = top_in + max(0, (max_h - total_h) // 2)
    elif anchor == "b":
        cy = h - bottom_in - total_h
    else:
        cy = top_in
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    for spans, align, line_h in painted:
        line_w = 0.0
        fonts = []
        for text, px, color, bold in spans:
            font = _font(px, bold=bold, latin=_is_latin(text))
            fonts.append(font)
            line_w += ldraw.textlength(text, font=font) if text else 0
        if align == PP_ALIGN.CENTER:
            tx = (w - line_w) / 2
        elif align == PP_ALIGN.RIGHT:
            tx = w - right_in - line_w
        else:
            tx = left_in
        for (text, px, color, bold), font in zip(spans, fonts):
            if text:
                ldraw.text((tx, cy), text, font=font, fill=(*color, 255))
                tx += ldraw.textlength(text, font=font)
        cy += line_h
    img.paste(layer, (x, y), layer)


def _para_hard_lines(para, font_scale: float, fallback: tuple[int, int, int]) -> list[list[tuple[str, int, tuple[int, int, int], bool]]]:
    runs = list(para.runs) or []
    if not runs:
        text = (para.text or "").replace("\r", "\n")
        px = max(8, int(round(16 * font_scale)))
        return [[(line, px, fallback, False)] for line in text.split("\n")] or [[("", px, fallback, False)]]
    lines: list[list[tuple[str, int, tuple[int, int, int], bool]]] = [[]]
    for run in runs:
        size_pt = float(run.font.size.pt) if run.font.size is not None else 16.0
        px = max(8, int(round(size_pt * font_scale)))
        color = _run_color(run, None) or fallback
        bold = bool(run.font.bold)
        parts = (run.text or "").replace("\r", "\n").split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append([])
            lines[-1].append((part, px, color, bold))
    return lines or [[("", 16, fallback, False)]]


def _wrap_spans(
    draw,
    spans: list[tuple[str, int, tuple[int, int, int], bool]],
    max_w: int,
    wrap: bool,
) -> list[list[tuple[str, int, tuple[int, int, int], bool]]]:
    if not wrap:
        return [spans or [("", 12, (26, 39, 68), False)]]
    lines: list[list[tuple[str, int, tuple[int, int, int], bool]]] = []
    current: list[tuple[str, int, tuple[int, int, int], bool]] = []
    width = 0.0
    for text, px, color, bold in spans:
        font = _font(px, bold=bold, latin=_is_latin(text))
        tokens = _tokens(text) if text else [""]
        for tok in tokens:
            tw = draw.textlength(tok, font=font) if tok else 0
            if current and tok.strip() and width + tw > max_w:
                lines.append(current)
                current = []
                width = 0.0
                tok = tok.lstrip()
                tw = draw.textlength(tok, font=font) if tok else 0
            if tok or not current:
                current.append((tok, px, color, bold))
                width += tw
    if current:
        lines.append(current)
    return lines or [[("", 12, (26, 39, 68), False)]]


def _tokens(text: str) -> list[str]:
    parts: list[str] = []
    buf = ""
    for ch in text:
        if ch == " ":
            if buf:
                parts.append(buf)
                buf = ""
            if parts and parts[-1].endswith(" "):
                parts[-1] += " "
            else:
                parts.append(" ")
        else:
            buf += ch
    if buf:
        parts.append(buf)
    return parts or [text]


def _is_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    return all(ord(c) < 128 for c in letters)


def _text_insets(shape, sx: float, sy: float) -> tuple[int, int, int, int]:
    left, top, right, bottom = 91440 * sx, 45720 * sy, 91440 * sx, 45720 * sy
    try:
        body = shape.text_frame._txBody.find(qn("a:bodyPr"))
        if body is not None:
            if body.get("lIns") is not None:
                left = float(body.get("lIns")) * sx
            if body.get("tIns") is not None:
                top = float(body.get("tIns")) * sy
            if body.get("rIns") is not None:
                right = float(body.get("rIns")) * sx
            if body.get("bIns") is not None:
                bottom = float(body.get("bIns")) * sy
    except Exception:
        pass
    return int(left), int(top), int(right), int(bottom)


def _text_anchor(shape) -> str:
    try:
        body = shape.text_frame._txBody.find(qn("a:bodyPr"))
        if body is not None:
            return (body.get("anchor") or "t").lower()
    except Exception:
        pass
    return "t"


def _run_color(run, default: tuple[int, int, int] | None) -> tuple[int, int, int] | None:
    try:
        rgb = run.font.color.rgb
        return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except Exception:
        return default


def _contrast_color(img, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    x, y, w, h = box
    x2, y2 = min(WIDTH - 1, x + max(w, 1) - 1), min(HEIGHT - 1, y + max(h, 1) - 1)
    x, y = max(0, x), max(0, y)
    try:
        r, g, b = img.getpixel((x + (x2 - x) // 2, y + (y2 - y) // 2))[:3]
    except Exception:
        return (26, 39, 68)
    return (245, 240, 230) if (r * 3 + g * 6 + b) / 10 < 140 else (26, 39, 68)


def _wrap_words(draw, text: str, font, max_width: int) -> list[str]:
    if not text:
        return []
    if draw.textlength(text, font=font) <= max_width:
        return [text]
    words = text.split(" ")
    lines: list[str] = []
    line = ""
    for word in words:
        trial = word if not line else f"{line} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
            continue
        if line:
            lines.append(line)
        if draw.textlength(word, font=font) <= max_width:
            line = word
        else:
            lines.extend(_wrap(draw, word, font, max_width))
            line = ""
    if line:
        lines.append(line)
    return lines or [text]


def _font(size: int, *, bold: bool = False, latin: bool = False):
    from PIL import ImageFont

    names: list[str] = []
    if latin:
        names.extend(
            (
                r"C:\Windows\Fonts\calibrib.ttf" if bold else r"C:\Windows\Fonts\calibri.ttf",
                r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            )
        )
    names.extend(
        (
            r"C:\Windows\Fonts\malgunbd.ttf" if bold else r"C:\Windows\Fonts\malgun.ttf",
            r"C:\Windows\Fonts\malgun.ttf",
            r"C:\Windows\Fonts\malgunsl.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        )
    )
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def _fit(draw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    cut = text
    while cut and draw.textlength(cut + "…", font=font) > max_width:
        cut = cut[:-1]
    return cut + "…"


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    out: list[str] = []
    line = ""
    for ch in text:
        trial = line + ch
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
        else:
            if line:
                out.append(line)
            line = ch
    if line:
        out.append(line)
    return out or [text]


# --------------------------------------------------------------------------- speech
def speak(text: str, dest: Path, min_seconds: float = 0) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    clean = " ".join((text or "").split())
    if not clean:
        return write_silence(dest, 0.8)
    mp3 = dest.with_suffix(".mp3")
    if _speak_edge(clean, mp3):
        return mp3
    wav = dest.with_suffix(".wav")
    if _speak_sapi(clean, wav):
        return dest if dest.is_file() else wav
    logger.warning("No TTS available; using silence for a slide")
    return write_silence(dest, 0.8)


def _speak_edge(text: str, dest: Path) -> bool:
    try:
        import asyncio
        import edge_tts
    except ImportError:
        return False
    voice = os.getenv("VIDEO_VOICE") or "ko-KR-SunHiNeural"

    async def _run() -> None:
        await edge_tts.Communicate(text, voice).save(str(dest))

    try:
        asyncio.run(_run())
    except Exception:
        logger.info("edge-tts failed; trying the system voice")
        return False
    return dest.is_file() and dest.stat().st_size > 16


def _speak_sapi(text: str, dest: Path) -> bool:
    if os.name != "nt":
        return False
    txt = dest.with_suffix(".txt")
    wav = dest.with_suffix(".wav")
    txt.write_text(text, encoding="utf-8")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SetOutputToWaveFile($args[1]); "
        "$s.Speak([IO.File]::ReadAllText($args[0], [Text.Encoding]::UTF8)); "
        "$s.Dispose();"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script, str(txt), str(wav)],
            check=True, capture_output=True, timeout=300,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    if wav.is_file() and wav.stat().st_size > 44:
        if dest != wav:
            shutil.copyfile(wav, dest)
        return True
    return False


def write_silence(dest: Path, seconds: float, rate: int = 24000) -> Path:
    dest = Path(dest).with_suffix(".wav")
    n = max(1, int(rate * max(0.4, seconds)))
    with wave.open(str(dest), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n)
    return dest


# --------------------------------------------------------------------------- mux
def ffmpeg_exe() -> str:
    bundled = shutil.which("ffmpeg")
    if bundled:
        return bundled
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("ffmpeg를 찾지 못했습니다. imageio-ffmpeg를 설치해 주세요.") from exc


HOLD_AFTER_SPEECH = 0.3  # keep the slide up after the last word, then advance


def mux_clips(pairs: list[tuple[Path, Path]], dest: Path) -> tuple[Path, float]:
    """One still per slide. Audio is the clock: the next slide starts right after the last word."""
    if not pairs:
        raise RuntimeError("합칠 슬라이드가 없습니다.")
    ffmpeg = ffmpeg_exe()
    dest = Path(dest)
    work = dest.parent / "clips"
    work.mkdir(exist_ok=True)
    clips: list[Path] = []
    total = 0.0
    scale = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,fps=30[v];"
        f"[1:a]apad=pad_dur={HOLD_AFTER_SPEECH}[a]"
    )
    for i, (image, audio) in enumerate(pairs, 1):
        spoken = audio_seconds(audio, ffmpeg)
        total += spoken + HOLD_AFTER_SPEECH
        clip = work / f"clip-{i:03d}.mp4"
        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(image),
                "-i", str(audio),
                "-filter_complex", scale,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-tune", "stillimage",
                "-c:a", "aac", "-ac", "2", "-ar", "44100",
                "-shortest",
                str(clip),
            ]
        )
        clips.append(clip)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips), encoding="utf-8")
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy",
            str(dest),
        ]
    )
    return dest, total


def prepend_intro(intro: Path, lecture: Path) -> float:
    """Rewrite `lecture` so `intro` plays first. Returns the intro length in seconds."""
    intro = Path(intro)
    lecture = Path(lecture)
    if not intro.is_file():
        raise RuntimeError("인트로 영상 파일을 찾을 수 없습니다.")
    if not lecture.is_file():
        raise RuntimeError("강의 영상 파일을 찾을 수 없습니다.")
    ffmpeg = ffmpeg_exe()
    seconds = audio_seconds(intro, ffmpeg)
    work = lecture.parent / "intro"
    work.mkdir(exist_ok=True)
    normalized = work / "intro-norm.mp4"
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,fps=30"
    )
    try:
        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(intro),
                "-vf", vf,
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "libx264", "-c:a", "aac", "-ac", "2", "-ar", "44100",
                str(normalized),
            ]
        )
    except RuntimeError:
        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(intro),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", vf,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-c:a", "aac", "-ac", "2", "-ar", "44100",
                "-shortest",
                str(normalized),
            ]
        )
    listing = work / "concat.txt"
    tmp = work / "with-intro.mp4"
    listing.write_text(
        f"file '{normalized.resolve().as_posix()}'\nfile '{lecture.resolve().as_posix()}'\n",
        encoding="utf-8",
    )
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy",
            str(tmp),
        ]
    )
    os.replace(tmp, lecture)
    return seconds


def audio_seconds(path: Path, ffmpeg: str | None = None) -> float:
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "r") as w:
                n, rate = w.getnframes(), w.getframerate()
                if rate:
                    return max(0.2, n / float(rate))
        except wave.Error:
            pass
    exe = ffmpeg or ffmpeg_exe()
    proc = subprocess.run(
        [exe, "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return 0.2
    return max(0.2, int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))


def _run(command: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(command, capture_output=True, text=True, creationflags=flags)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ffmpeg failed").strip()[-400:]
        raise RuntimeError(f"영상을 합치지 못했습니다. {err}")


Launcher = Callable[[Path], None]
