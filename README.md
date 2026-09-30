# AI Video Montaj

Video yuklaysiz, sayt uni avtomatik montaj qiladi, siz yuklab olasiz. Keyin chatga
«boshidan 5 soniyani kes, tepaga sarlavha qo'y» deb yozasiz, AI shuni qilib yangi versiya beradi.

## Nimalar qila oladi

| Tugma orqali (avto montaj) | Chat orqali (qo'shimcha) |
|---|---|
| Jim joylarni olib tashlash | Istalgan oraliqni kesish / qisqartirish |
| Ovozni tekislash, shovqinni kamaytirish | Tezlashtirish / sekinlashtirish |
| Nutqdan avtomatik subtitr (o'zbekcha, ruscha, inglizcha...) | Ekranga matn/sarlavha qo'yish |
| Boshi-oxiriga silliq so'nish | Rang: yorqinlik, kontrast, oq-qora |
| Fon musiqasi (gapirganda o'zi pasayadi) | Subtitr rangi/o'rnini o'zgartirish |
| Logo qo'yish | Video o'lchamini kichraytirish |
| Vertikal 9:16, 1:1, 4:5 formatga o'tkazish | Logo/musiqa o'rnini va balandligini sozlash |

Har bir montaj **yangi versiya** bo'lib saqlanadi; xohlagan versiyaga qaytib, undan davom ettirish mumkin.

## Kompyuterda ishga tushirish (sinash uchun)

```bash
# 1) ffmpeg o'rnating (Ubuntu: sudo apt install ffmpeg fonts-dejavu-core)
# 2) Python paketlari
pip install -r requirements.txt
# 3) AI kaliti (chat uchun)
export ANTHROPIC_API_KEY=sk-ant-...
# 4) Ishga tushirish
uvicorn app.main:app --port 8000
```
So'ng brauzerda http://localhost:8000 ni oching.

## Internetda hamma uchun ishga tushirish

Server kerak (VPS yoki Render/Railway/Fly.io kabi Docker qo'llaydigan hosting).
Tavsiya: **kamida 2 CPU va 4 GB RAM** (subtitr modeli uchun), 20+ GB disk.

```bash
docker build -t video-montaj .
docker run -d --name montaj -p 8000:8000 -v montaj-data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e APP_PASSWORD=o'zingiz-o'ylagan-parol \
  -e TRUST_PROXY=1 \
  video-montaj
```
Domen va HTTPS uchun oldiga Caddy yoki nginx qo'ying (Caddy: `sizning-domen.uz { reverse_proxy localhost:8000 }`).
Hostingda `.env.example` dagi o'zgaruvchilarni «Environment variables» bo'limiga kiriting.

## Muhim eslatmalar

- **Xarajat:** har bir chat buyrug'i Claude API ga bitta kichik so'rov (arzon). Asosiy yuk serverning protsessorida (ffmpeg va subtitr). Ommaviy ochsangiz `APP_PASSWORD`, `UPLOADS_PER_HOUR`, `MAX_VIDEO_MIN` cheklovlarini qo'ying, aks holda kimdir serveringizni band qilib qo'yishi mumkin.
- **Subtitr sifati:** o'zbekcha uchun `WHISPER_MODEL=medium` yoki `large-v3` aniqroq, lekin sekinroq va ko'proq xotira talab qiladi. Til aniq ma'lum bo'lsa, tilni qo'lda tanlang.
- **Musiqa:** dastur o'zi musiqa yaratmaydi va tayyor musiqa bermaydi (mualliflik huquqi). O'zingizning yoki litsenziyasi bor musiqani yuklang.
- **Cheklov:** AI faqat yuqoridagi amallarni bajaradi. «Yangi kadr yarat», «odamni videodan o'chir» kabi narsalar hozircha yo'q.
- **Maxfiylik:** yuklangan fayllar `RETENTION_HOURS` (standart 24 soat) dan keyin serverdan avtomatik o'chiriladi.

## Tuzilishi

```
app/main.py      server (yuklash, versiyalar, fon ishlari, cheklovlar)
app/editing.py   montaj dvigateli (ffmpeg)
app/ai.py        chat buyrug'ini montaj amallariga aylantiruvchi Claude
app/static/      sayt interfeysi
```
