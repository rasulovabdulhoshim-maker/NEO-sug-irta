"""Video montaj dvigateli (ffmpeg asosida).

Har bir "amal" (operation) bitta kirish videoni oladi va yangi video yaratadi.
Amallar ketma-ket qo'llanadi. Vaqtlar (soniyada) amal boshlanish paytidagi
videoning vaqt chizig'iga nisbatan olinadi.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class EditError(Exception):
    """Foydalanuvchiga ko'rsatiladigan montaj xatosi."""


# --------------------------------------------------------------------------
# Umumiy yordamchilar
# --------------------------------------------------------------------------

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
SUB_FONT_NAME = os.getenv("SUBTITLE_FONT", "DejaVu Sans")

VENC = ["-c:v", "libx264", "-preset", os.getenv("X264_PRESET", "veryfast"),
        "-crf", "22", "-pix_fmt", "yuv420p"]
AENC = ["-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
BASE = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error"]


def find_font() -> str:
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    raise EditError("Matn uchun shrift topilmadi (fonts-dejavu o'rnating).")


def run(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 3600):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise EditError("Montaj juda uzoq davom etdi va to'xtatildi.")
    if p.returncode != 0:
        raise EditError("ffmpeg xatosi: " + p.stderr.strip()[-500:])
    return p


@dataclass
class Info:
    duration: float
    has_audio: bool
    width: int   # aylantirish (rotation) hisobga olingan
    height: int


def probe(path: Path) -> Info:
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    d = json.loads(p.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise EditError("Faylda video topilmadi.")
    has_audio = any(s.get("codec_type") == "audio" for s in d["streams"])
    dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    try:
        rot = int(v.get("tags", {}).get("rotate", 0))
    except (TypeError, ValueError):
        pass
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    if dur <= 0:
        raise EditError("Video davomiyligini aniqlab bo'lmadi.")
    return Info(dur, has_audio, w, h)


@dataclass
class Ctx:
    workdir: Path
    music: Optional[Path] = None
    logo: Optional[Path] = None
    transcribe: Optional[Callable[[Path, Optional[str]], list]] = None


def num(p: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = p.get(key, default)
    if v is None:
        v = default
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise EditError(f"'{key}' qiymati noto'g'ri: {v!r}")
    return min(max(v, lo), hi)


def encode(src: Path, dst: Path, info: Info, *, vf: str | None = None,
           af: str | None = None, pre: list[str] | None = None) -> None:
    cmd = BASE + (pre or []) + ["-i", str(src)]
    if vf:
        cmd += ["-vf", vf]
    if af and info.has_audio:
        cmd += ["-af", af]
    cmd += VENC + (AENC if info.has_audio else ["-an"])
    cmd += ["-movflags", "+faststart", str(dst)]
    run(cmd)


def even(x: float) -> int:
    return max(2, int(x) // 2 * 2)


# --------------------------------------------------------------------------
# Kesish, qisqartirish, jimlikni olib tashlash
# --------------------------------------------------------------------------

def keep_segments(src: Path, dst: Path, segs: list[tuple[float, float]],
                  info: Info, ctx: Ctx) -> None:
    segs = [(max(0.0, a), min(info.duration, b)) for a, b in segs if b - a > 0.05]
    if not segs:
        raise EditError("Kesishdan keyin videodan hech narsa qolmaydi.")
    parts = []
    for i, (a, b) in enumerate(segs):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if info.has_audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    n = len(segs)
    if info.has_audio:
        cat = "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    else:
        cat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    script = ctx.workdir / "filter_script.txt"
    script.write_text(";\n".join(parts + [cat]))
    cmd = BASE + ["-i", str(src), "-filter_complex_script", str(script), "-map", "[v]"]
    if info.has_audio:
        cmd += ["-map", "[a]"] + AENC
    else:
        cmd += ["-an"]
    cmd += VENC + ["-movflags", "+faststart", str(dst)]
    run(cmd)


def op_trim(src, dst, p, ctx, info):
    start = num(p, "start", 0, 0, info.duration)
    end = num(p, "end", info.duration, 0, info.duration)
    if end <= start:
        raise EditError("Kesish oralig'i noto'g'ri: oxiri boshidan keyin bo'lishi kerak.")
    keep_segments(src, dst, [(start, end)], info, ctx)
    return f"Video {start:.1f}s dan {end:.1f}s gacha qisqartirildi."


def _ranges(p: dict, info: Info) -> list[tuple[float, float]]:
    raw = p.get("ranges")
    if not raw:
        raw = [[p.get("start"), p.get("end")]]
    out = []
    for r in raw:
        try:
            a, b = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            raise EditError("Kesiladigan oraliq noto'g'ri.")
        a, b = max(0.0, a), min(info.duration, b)
        if b > a:
            out.append((a, b))
    if not out:
        raise EditError("Kesiladigan oraliq topilmadi.")
    out.sort()
    merged = [out[0]]
    for a, b in out[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _complement(cuts: list[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    keep, pos = [], 0.0
    for a, b in cuts:
        if a > pos:
            keep.append((pos, a))
        pos = max(pos, b)
    if pos < total:
        keep.append((pos, total))
    return keep


def op_cut(src, dst, p, ctx, info):
    cuts = _ranges(p, info)
    keep = _complement(cuts, info.duration)
    keep_segments(src, dst, keep, info, ctx)
    removed = sum(b - a for a, b in cuts)
    return f"{removed:.1f} soniya kesib olib tashlandi."


def op_remove_silence(src, dst, p, ctx, info):
    if not info.has_audio:
        return "Videoda ovoz yo'q, jimlikni olib tashlab bo'lmadi."
    min_sil = num(p, "min_silence", 0.7, 0.2, 5)
    db = num(p, "threshold_db", -35, -60, -15)
    pad = 0.08
    r = run(BASE[:-2] + ["-loglevel", "info", "-i", str(src), "-vn",
                         "-af", f"silencedetect=noise={db}dB:d={min_sil}", "-f", "null", "-"])
    sil, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        sil.append((cur, info.duration))
    # chetlaridan biroz qoldiramiz, so'zlar kesilib ketmasin
    cuts = [(a + pad, b - pad) for a, b in sil if b - a > 2 * pad + 0.05]
    if not cuts:
        shutil.copy(src, dst)
        return "Kesiladigan jim joylar topilmadi."
    keep = _complement(cuts, info.duration)
    # bo'laklar juda ko'p bo'lsa, qisqa jimliklarni saqlab qolamiz
    gap = min_sil
    while len(keep) > 150 and gap < 30:
        gap *= 1.5
        cuts = [(a, b) for a, b in cuts if b - a >= gap]
        keep = _complement(cuts, info.duration)
    if not keep:
        raise EditError("Butun video jim ekan (ovoz topilmadi).")
    keep_segments(src, dst, keep, info, ctx)
    new_len = sum(b - a for a, b in keep)
    return f"Jim joylar olib tashlandi: {info.duration:.0f}s dan {new_len:.0f}s ga."


# --------------------------------------------------------------------------
# Tezlik, ovoz, so'nish, rang, o'lcham
# --------------------------------------------------------------------------

def _atempo(f: float) -> str:
    parts = []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def op_speed(src, dst, p, ctx, info):
    f = num(p, "factor", 1.0, 0.25, 4.0)
    encode(src, dst, info, vf=f"setpts=PTS/{f:.4f}", af=_atempo(f))
    return f"Tezlik {f:g}x qilindi."


def op_normalize_audio(src, dst, p, ctx, info):
    if not info.has_audio:
        return "Videoda ovoz yo'q, ovozni tekislash o'tkazib yuborildi."
    af = "loudnorm=I=-16:TP=-1.5:LRA=11"
    if p.get("denoise"):
        af = "afftdn=nf=-25," + af
    cmd = BASE + ["-i", str(src), "-af", af, "-c:v", "copy"] + AENC + \
        ["-movflags", "+faststart", str(dst)]
    run(cmd)
    return "Ovoz balandligi tekislandi" + (" va shovqin kamaytirildi." if p.get("denoise") else ".")


def op_fade(src, dst, p, ctx, info):
    fin = num(p, "in", 1.0, 0, 10)
    fout = num(p, "out", 1.0, 0, 10)
    fin = min(fin, info.duration / 2)
    fout = min(fout, info.duration / 2)
    vf, af = [], []
    if fin > 0:
        vf.append(f"fade=t=in:st=0:d={fin:.2f}")
        af.append(f"afade=t=in:st=0:d={fin:.2f}")
    if fout > 0:
        st = info.duration - fout
        vf.append(f"fade=t=out:st={st:.2f}:d={fout:.2f}")
        af.append(f"afade=t=out:st={st:.2f}:d={fout:.2f}")
    if not vf:
        shutil.copy(src, dst)
        return None
    encode(src, dst, info, vf=",".join(vf), af=",".join(af))
    return "Boshi va oxiriga silliq so'nish qo'shildi."


def op_color(src, dst, p, ctx, info):
    b = num(p, "brightness", 0, -0.5, 0.5)
    c = num(p, "contrast", 1, 0.5, 2)
    s = num(p, "saturation", 1, 0, 3)
    encode(src, dst, info, vf=f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}")
    return "Rang sozlandi."


def op_resize(src, dst, p, ctx, info):
    h = int(num(p, "height", 720, 240, 2160))
    encode(src, dst, info, vf=f"scale=-2:{h // 2 * 2}")
    return f"Video o'lchami {h}p ga o'zgartirildi."


def op_aspect(src, dst, p, ctx, info):
    ratio = str(p.get("ratio", "9:16"))
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", ratio)
    if not m or int(m.group(2)) == 0:
        raise EditError("Format noto'g'ri. Masalan: 9:16, 1:1, 16:9, 4:5.")
    r = int(m.group(1)) / int(m.group(2))
    mode = p.get("mode", "crop")
    base = min(info.width, info.height)
    if r < 1:
        W, H = even(base), even(base / r)
    else:
        W, H = even(base * r), even(base)
    if mode == "pad":
        fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v]")
    elif mode == "blur":
        fc = (f"[0:v]split[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:5[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    else:
        fc = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v]"
    cmd = BASE + ["-i", str(src), "-filter_complex", fc, "-map", "[v]"]
    if info.has_audio:
        cmd += ["-map", "0:a"] + AENC
    else:
        cmd += ["-an"]
    cmd += VENC + ["-movflags", "+faststart", str(dst)]
    run(cmd)
    return f"Format {ratio} ga o'zgartirildi ({W}x{H})."


# --------------------------------------------------------------------------
# Matn, subtitr, logo, musiqa
# --------------------------------------------------------------------------

POSITIONS_Y = {
    "top": "h*0.08",
    "center": "(h-text_h)/2",
    "bottom": "h*0.88-text_h",
}


def op_text(src, dst, p, ctx, info):
    text = str(p.get("text", "")).strip()
    if not text:
        raise EditError("Qo'shiladigan matn bo'sh.")
    start = num(p, "start", 0, 0, info.duration)
    end = num(p, "end", info.duration, 0, info.duration)
    if end <= start:
        end = info.duration
    ratio = num(p, "size", 0.06, 0.02, 0.15)
    color = str(p.get("color", "white"))
    if not re.fullmatch(r"[A-Za-z0-9#]{3,20}", color):
        color = "white"
    pos = p.get("position", "bottom")
    y = POSITIONS_Y.get(pos, POSITIONS_Y["bottom"])
    fs = int(min(info.width, info.height) * ratio)
    max_chars = max(8, int(info.width * 0.9 / (fs * 0.6)))
    wrapped = "\n".join(textwrap.wrap(text, max_chars)) if "\n" not in text else text
    tf = ctx.workdir / "overlay_text.txt"
    tf.write_text(wrapped, encoding="utf-8")
    vf = (f"drawtext=fontfile={find_font()}:textfile=overlay_text.txt:expansion=none:"
          f"fontsize={fs}:fontcolor={color}:borderw={max(2, fs // 18)}:bordercolor=black:"
          f"line_spacing={fs // 6}:x=(w-text_w)/2:y={y}:"
          f"enable='between(t,{start:.2f},{end:.2f})'")
    cmd = BASE + ["-i", str(src), "-vf", vf] + VENC
    cmd += (["-c:a", "copy"] if info.has_audio else ["-an"])
    cmd += ["-movflags", "+faststart", str(dst)]
    run(cmd, cwd=ctx.workdir)
    return f"Matn qo'shildi: «{text[:40]}»."


ASS_COLORS = {"white": "&H00FFFFFF", "yellow": "&H0000FFFF", "cyan": "&H00FFFF00",
              "green": "&H0000FF00", "orange": "&H000080FF"}
ASS_ALIGN = {"bottom": 2, "center": 5, "top": 8}

_whisper_model = None
_whisper_lock = threading.Lock()


def whisper_transcribe(path: Path, language: Optional[str]) -> list[tuple[float, float, str]]:
    """Nutqni matnga aylantiradi: [(boshlanish, tugash, matn), ...]."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise EditError("Subtitr uchun 'faster-whisper' o'rnatilmagan (pip install faster-whisper).")
    wav = path.with_suffix(".16k.wav")
    run(BASE + ["-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model = WhisperModel(os.getenv("WHISPER_MODEL", "small"),
                                          device="cpu", compute_type="int8")
        segments, _ = _whisper_model.transcribe(
            str(wav), language=language or None, word_timestamps=True, vad_filter=True)
        words = [(w.start, w.end, w.word.strip()) for s in segments for w in (s.words or []) if w.word.strip()]
    wav.unlink(missing_ok=True)
    # so'zlarni qisqa subtitr bo'laklariga birlashtiramiz
    cues, cur = [], []
    for w in words:
        if cur:
            length = len(" ".join(x[2] for x in cur)) + len(w[2]) + 1
            if length > 38 or w[1] - cur[0][0] > 3.2 or w[0] - cur[-1][1] > 0.8:
                cues.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
                cur = []
        cur.append(w)
    if cur:
        cues.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
    return cues


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(cues, path: Path) -> None:
    lines = []
    for i, (a, b, text) in enumerate(cues, 1):
        lines += [str(i), f"{_srt_time(a)} --> {_srt_time(b)}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def op_subtitles(src, dst, p, ctx, info):
    if not info.has_audio:
        return "Videoda ovoz yo'q, subtitr yaratib bo'lmadi."
    lang = p.get("language") or None
    fn = ctx.transcribe or whisper_transcribe
    cues = fn(src, lang)
    if not cues:
        shutil.copy(src, dst)
        return "Videoda nutq topilmadi, subtitr qo'shilmadi."
    write_srt(cues, ctx.workdir / "subs.srt")
    size = int(num(p, "font_size", 18, 10, 40))
    color = ASS_COLORS.get(str(p.get("color", "white")), ASS_COLORS["white"])
    align = ASS_ALIGN.get(p.get("position", "bottom"), 2)
    style = (f"Fontname={SUB_FONT_NAME},Fontsize={size},Bold=1,PrimaryColour={color},"
             f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
             f"Alignment={align},MarginV=22")
    vf = f"subtitles=subs.srt:force_style='{style}'"
    cmd = BASE + ["-i", str(src), "-vf", vf] + VENC
    cmd += ["-c:a", "copy", "-movflags", "+faststart", str(dst)]
    run(cmd, cwd=ctx.workdir)
    return f"Subtitr qo'shildi ({len(cues)} ta bo'lak)."


LOGO_POS = {
    "top-left": ("W*0.03", "W*0.03"),
    "top-right": ("W-w-W*0.03", "W*0.03"),
    "bottom-left": ("W*0.03", "H-h-W*0.03"),
    "bottom-right": ("W-w-W*0.03", "H-h-W*0.03"),
    "center": ("(W-w)/2", "(H-h)/2"),
}


def op_logo(src, dst, p, ctx, info):
    if not ctx.logo or not ctx.logo.exists():
        raise EditError("Logo yuklanmagan. Avval logo rasmini yuklang.")
    scale = num(p, "scale", 0.15, 0.03, 0.6)
    opacity = num(p, "opacity", 1.0, 0.1, 1.0)
    x, y = LOGO_POS.get(p.get("position", "top-right"), LOGO_POS["top-right"])
    fc = (f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.2f}[l0];"
          f"[l0][0:v]scale2ref=w=main_w*{scale:.3f}:h=ow/a[lg][bg];"
          f"[bg][lg]overlay=x={x}:y={y}[v]")
    cmd = BASE + ["-i", str(src), "-i", str(ctx.logo), "-filter_complex", fc, "-map", "[v]"]
    cmd += (["-map", "0:a", "-c:a", "copy"] if info.has_audio else ["-an"])
    cmd += VENC + ["-movflags", "+faststart", str(dst)]
    run(cmd)
    return "Logo qo'yildi."


def op_music(src, dst, p, ctx, info):
    if not ctx.music or not ctx.music.exists():
        raise EditError("Musiqa yuklanmagan. Avval musiqa faylini yuklang.")
    vol = num(p, "volume", 0.2, 0.02, 1.0)
    duck = bool(p.get("duck", True))
    D = info.duration
    fo = min(2.0, D / 2)
    mus = f"volume={vol:.3f},afade=t=in:st=0:d=1,afade=t=out:st={D - fo:.2f}:d={fo:.2f}"
    if info.has_audio and duck:
        fc = (f"[0:a]asplit=2[sc][voice];[1:a]{mus}[m0];"
              f"[m0][sc]sidechaincompress=threshold=0.03:ratio=10:attack=20:release=500[duck];"
              f"[voice][duck]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]")
    elif info.has_audio:
        fc = (f"[1:a]{mus}[m0];"
              f"[0:a][m0]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]")
    else:
        fc = f"[1:a]{mus}[a]"
    cmd = BASE + ["-i", str(src), "-stream_loop", "-1", "-i", str(ctx.music),
                  "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                  "-c:v", "copy"] + AENC + ["-t", f"{D:.3f}", "-movflags", "+faststart", str(dst)]
    run(cmd)
    return "Fon musiqasi qo'shildi" + (" (gapirganda o'zi pasayadi)." if info.has_audio and duck else ".")


OPS: dict[str, Callable] = {
    "trim": op_trim,
    "cut": op_cut,
    "remove_silence": op_remove_silence,
    "speed": op_speed,
    "normalize_audio": op_normalize_audio,
    "fade": op_fade,
    "color": op_color,
    "resize": op_resize,
    "aspect": op_aspect,
    "text": op_text,
    "subtitles": op_subtitles,
    "logo": op_logo,
    "music": op_music,
}


# --------------------------------------------------------------------------
# Amallarni ketma-ket qo'llash va avto montaj rejasi
# --------------------------------------------------------------------------

def apply_ops(src: Path, ops: list[dict], ctx: Ctx,
              progress: Optional[Callable[[int, int, str], None]] = None):
    """Amallarni ketma-ket bajaradi. (yakuniy_fayl, izohlar) qaytaradi."""
    if not ops:
        raise EditError("Bajariladigan amal yo'q.")
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    cur, notes, prev_tmp = Path(src), [], None
    for i, op in enumerate(ops):
        name = op.get("op")
        fn = OPS.get(name)
        if not fn:
            raise EditError(f"Noma'lum amal: {name}")
        if progress:
            progress(i, len(ops), name)
        info = probe(cur)
        dst = ctx.workdir / f"step{i}_{name}.mp4"
        note = fn(cur, dst, op, ctx, info)
        if not dst.exists():
            shutil.copy(cur, dst)
        if note:
            notes.append(note)
        if prev_tmp and prev_tmp.exists():
            prev_tmp.unlink()
        prev_tmp = cur = dst
    return cur, notes


def auto_plan(o: dict) -> list[dict]:
    """Avto montaj tanlovlaridan amallar ro'yxatini tuzadi."""
    ops: list[dict] = []
    if o.get("silence"):
        ops.append({"op": "remove_silence"})
    if o.get("aspect"):
        ops.append({"op": "aspect", "ratio": o["aspect"], "mode": o.get("aspect_mode", "crop")})
    if o.get("normalize"):
        ops.append({"op": "normalize_audio", "denoise": True})
    if o.get("subtitles"):
        ops.append({"op": "subtitles", "language": o.get("language") or None})
    if o.get("music"):
        ops.append({"op": "music", "volume": 0.2, "duck": True})
    if o.get("logo"):
        ops.append({"op": "logo", "position": "top-right"})
    if o.get("fade"):
        ops.append({"op": "fade", "in": 0.8, "out": 1.0})
    return ops
