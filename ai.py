"""Chat buyruqlarini montaj amallariga aylantiruvchi AI (Claude)."""
from __future__ import annotations

import json
import os
import re
from typing import Optional

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

SYSTEM = """Sen video montaj yordamchisisan. Foydalanuvchi videoga qanday o'zgartirish \
kiritishni oddiy tilda (o'zbekcha, ruscha yoki inglizcha) yozadi. Sen buni FAQAT \
quyidagi amallar ro'yxatiga aylantirasan.

Amallar (har biri JSON obyekt, "op" maydoni bilan). Barcha vaqtlar soniyada:
- {"op":"trim","start":S,"end":E} — videoni faqat S..E oralig'iga qisqartirish
- {"op":"cut","ranges":[[S,E],...]} — ko'rsatilgan oraliqlarni kesib tashlash (hammasi HOZIRGI vaqt chizig'iga nisbatan)
- {"op":"remove_silence","min_silence":0.7,"threshold_db":-35} — jim joylarni olib tashlash
- {"op":"speed","factor":1.25} — butun video tezligi (0.25..4)
- {"op":"normalize_audio","denoise":true|false} — ovozni tekislash / shovqinni kamaytirish
- {"op":"fade","in":1,"out":1} — boshida/oxirida silliq so'nish
- {"op":"color","brightness":0,"contrast":1,"saturation":1} — brightness -0.5..0.5, contrast 0.5..2, saturation 0..3 (oq-qora = 0)
- {"op":"resize","height":720} — video balandligi (piksel)
- {"op":"aspect","ratio":"9:16","mode":"crop"|"pad"|"blur"} — format (9:16, 1:1, 16:9, 4:5)
- {"op":"text","text":"...","start":S,"end":E,"position":"top"|"center"|"bottom","size":0.06,"color":"white"} — ekranga matn (size: 0.03 kichik .. 0.10 katta)
- {"op":"subtitles","language":"uz"|"ru"|"en"|"tr"|null,"position":"bottom","font_size":18,"color":"white"|"yellow"} — nutqdan avtomatik subtitr
- {"op":"logo","position":"top-left"|"top-right"|"bottom-left"|"bottom-right"|"center","scale":0.15,"opacity":1} — foydalanuvchi yuklagan logoni qo'yish
- {"op":"music","volume":0.2,"duck":true} — foydalanuvchi yuklagan musiqani fon qilib qo'yish

Qoidalar:
1. Amallar ketma-ket bajariladi. Har bir amalning vaqtlari o'sha amal boshlanishidagi videoga nisbatan. Agar bir nechta kesish kerak bo'lsa, BITTA "cut" amaliga "ranges" qilib yoz.
2. Foydalanuvchi aytmagan narsani qo'shma. Aniq bo'lmagan qiymat uchun oqilona standart tanla.
3. Foydalanuvchi matn qo'shishni aytsa, matnni aynan u aytgan tilda va yozuvda qoldir.
4. Video davomiyligi va logo/musiqa yuklanganligi haqida ma'lumot beriladi. Logo/musiqa yuklanmagan bo'lsa va foydalanuvchi so'rasa, amal qo'shma, "reply"da yuklashni so'ra.
5. So'rov montajga aloqasiz bo'lsa yoki amallar bilan bajarib bo'lmasa (masalan, "videoga yangi kadr yarat"), "ops" bo'sh bo'lsin va "reply"da nima qila olishingni qisqa tushuntir.
6. Vaqt aniq aytilmagan bo'lsa (masalan "boshidagi jim joyni kes") va taxmin qilib bo'lmasa, "ops" bo'sh bo'lsin va "reply"da aniq vaqt so'ra.

Javobing FAQAT bitta JSON obyekt bo'lsin, boshqa hech narsa yozma:
{"reply":"foydalanuvchiga qisqa o'zbekcha izoh (nima qilaman)","ops":[...]}"""


class AIError(Exception):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError("AI javobi tushunarsiz bo'ldi.")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        raise AIError("AI javobini o'qib bo'lmadi.")


def plan_edits(message: str, *, duration: float, has_audio: bool, has_logo: bool,
               has_music: bool, history: Optional[list[dict]] = None) -> dict:
    """Foydalanuvchi xabarini {"reply":..., "ops":[...]} ga aylantiradi."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise AIError("Serverda ANTHROPIC_API_KEY o'rnatilmagan, chat buyruqlari ishlamaydi.")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    context = (f"[Video holati: davomiyligi {duration:.1f} soniya; "
               f"ovoz: {'bor' if has_audio else 'yoq'}; "
               f"logo: {'yuklangan' if has_logo else 'yuklanmagan'}; "
               f"musiqa: {'yuklangan' if has_music else 'yuklanmagan'}]")
    messages = []
    for h in (history or [])[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": f"{context}\n\n{message}"})
    try:
        resp = client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM,
                                      messages=messages)
    except anthropic.APIError as e:
        raise AIError(f"AI bilan bog'lanishda xato: {e}")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = _extract_json(text)
    ops = data.get("ops") or []
    if not isinstance(ops, list) or not all(isinstance(o, dict) for o in ops):
        raise AIError("AI noto'g'ri amallar ro'yxati qaytardi.")
    return {"reply": str(data.get("reply", "")), "ops": ops}
