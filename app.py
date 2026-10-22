"""Media Downloader - backend Flask + yt-dlp.
Chạy:  pip install -r requirements.txt && python app.py  ->  http://localhost:5000
Khuyến nghị cài ffmpeg (ghép video+audio độ phân giải cao, chuyển mp3, đổi phụ đề sang srt).
"""
import os
import re
import shutil
import tempfile
import threading
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

app = Flask(__name__, static_folder=None)
BASE = os.path.dirname(os.path.abspath(__file__))
HAS_FFMPEG = shutil.which("ffmpeg") is not None
COOKIES = os.environ.get("COOKIES_FILE")  # tùy chọn: cookies.txt cho Facebook/Douyin/video giới hạn
ALLOWED = ("youtube.com", "youtu.be", "facebook.com", "fb.watch", "fb.com",
           "tiktok.com", "douyin.com", "iesdouyin.com")
slots = threading.Semaphore(int(os.environ.get("MAX_JOBS", "3")))
SAFE = re.compile(r"^[\w\-\.\+]+$")


def valid(url: str) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False
    h = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(h == d or h.endswith("." + d) for d in ALLOWED)


def base_opts():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}
    if COOKIES and os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def clean(e: Exception) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:400]


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.post("/api/info")
def info():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not valid(url):
        return jsonify(error="Link không hợp lệ hoặc nền tảng chưa được hỗ trợ."), 400
    try:
        with yt_dlp.YoutubeDL(base_opts()) as y:
            d = y.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=clean(e)), 422

    best = {}
    for f in d.get("formats") or []:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        tag = ((f.get("format_note") or "") + " " + (f.get("format_id") or "")).lower()
        if "watermark" in tag:  # loại bản có watermark (TikTok/Douyin)
            continue
        has_audio = f.get("acodec") not in (None, "none")
        if not HAS_FFMPEG and not has_audio:
            continue
        score = (f.get("ext") == "mp4", has_audio, f.get("tbr") or 0)
        if h not in best or score > best[h][0]:
            best[h] = (score, f, has_audio)

    videos = []
    for h in sorted(best, reverse=True):
        _, f, ha = best[h]
        videos.append({"format": f["format_id"], "height": h, "ext": f.get("ext"),
                       "fps": f.get("fps"), "size": f.get("filesize") or f.get("filesize_approx"),
                       "has_audio": ha})

    subs = [{"lang": k, "auto": False} for k in (d.get("subtitles") or {})
            if k != "live_chat"]
    subs += [{"lang": k, "auto": True} for k in (d.get("automatic_captions") or {})]

    return jsonify(title=d.get("title"), uploader=d.get("uploader"), duration=d.get("duration"),
                   thumbnail=d.get("thumbnail"), videos=videos, subs=subs, ffmpeg=HAS_FFMPEG)


@app.get("/api/download")
def download():
    url = request.args.get("url", "")
    kind = request.args.get("type", "video")
    if not valid(url) or kind not in ("video", "audio", "sub"):
        abort(400)

    tmp = tempfile.mkdtemp(prefix="dl_")
    o = base_opts()
    o["outtmpl"] = os.path.join(tmp, "%(title).80B [%(id)s].%(ext)s")
    nowm = "[format_note!*=watermark]?"

    if kind == "video":
        fid = request.args.get("format", "best")
        if fid == "best":
            o["format"] = (f"bv*{nowm}+ba/b{nowm}/b") if HAS_FFMPEG else f"b{nowm}/b"
        else:
            if not SAFE.match(fid):
                abort(400)
            need_audio = request.args.get("ha") != "1"
            o["format"] = f"{fid}+ba/{fid}" if (need_audio and HAS_FFMPEG) else fid
        if HAS_FFMPEG:
            o["merge_output_format"] = "mp4"
    elif kind == "audio":
        o["format"] = "ba/b"
        if request.args.get("fmt") == "mp3" and HAS_FFMPEG:
            o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        lang = request.args.get("lang", "")
        if not SAFE.match(lang):
            abort(400)
        o.update(skip_download=True, writesubtitles=True, writeautomaticsub=True,
                 subtitleslangs=[lang], subtitlesformat="srt/vtt/best")
        if HAS_FFMPEG:
            o["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}]

    try:
        with slots, yt_dlp.YoutubeDL(o) as y:
            y.download([url])
        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Không tìm thấy dữ liệu (video không có phụ đề này?).")
        path = max((os.path.join(tmp, f) for f in files), key=os.path.getsize)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error=clean(e)), 422

    resp = send_file(path, as_attachment=True, download_name=os.path.basename(path))
    resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
    return resp


if __name__ == "__main__":
    print("ffmpeg:", "có" if HAS_FFMPEG else "KHÔNG (chỉ tải được định dạng đã gồm sẵn âm thanh)")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))
