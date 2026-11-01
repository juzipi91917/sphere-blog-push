"""Resolve ordinary ULIZA HLS playback and remux it to a local MP4."""
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from settings import ROOT


USER_AGENT = "Mozilla/5.0"


def read_url(url, referer):
    with urlopen(Request(url, headers={"Referer": referer, "User-Agent": USER_AGENT}), timeout=30) as response:
        return response.read().decode("utf-8-sig")


def resolve_video(video, referer, with_cover=False):
    url = video["url"]
    cover = video.get("cover")
    if video["kind"] == "uliza":
        if urlparse(url).hostname != "player-api.p.uliza.jp":
            raise ValueError("ULIZA 播放器域名异常。")
        soup = BeautifulSoup(read_url(url, referer), "html.parser")
        params = None
        for script in soup.select("script"):
            match = re.search(r"\bvar\s+params\s*=\s*", script.get_text())
            if match:
                params, _ = json.JSONDecoder().raw_decode(script.get_text()[match.end():])
                break
        if not params or not params.get("src", {}).get("video"):
            raise RuntimeError("无法从 ULIZA 播放器提取视频地址。")
        url = params["src"]["video"]
        cover = params["src"].get("poster")
    if urlparse(url).scheme not in ("https", "http"):
        raise ValueError("视频地址必须为 HTTP/HTTPS。")
    if ".m3u8" in urlparse(url).path.lower():
        manifest = read_url(url, referer)
        lines = [line.strip() for line in manifest.splitlines() if line.strip()]
        variants = []
        for index, line in enumerate(lines[:-1]):
            if line.startswith("#EXT-X-STREAM-INF:"):
                bandwidth = re.search(r"(?:^|[:,])BANDWIDTH=(\d+)", line)
                if bandwidth and not lines[index + 1].startswith("#"):
                    variants.append((int(bandwidth[1]), urljoin(url, lines[index + 1])))
        if variants:
            # Prefer medium quality (up to 2.5 Mbps) to keep QQ videos compact.
            candidates = [v for v in variants if v[0] <= 2500000]
            _, url = max(candidates) if candidates else min(variants)
            manifest = read_url(url, referer)
        # No DRM license acquisition or bypass. Ordinary AES-128 HLS is handled
        # by ffmpeg using the playback key URI supplied by the player itself.
        for line in manifest.splitlines():
            if line.startswith("#EXT-X-KEY:"):
                if "METHOD=SAMPLE-AES" in line or ("KEYFORMAT=" in line and 'KEYFORMAT="identity"' not in line):
                    raise RuntimeError("视频使用 DRM，本模块不处理。")
        if "#EXT-X-ENDLIST" not in manifest:
            raise RuntimeError("只支持已结束的点播视频。")
    return (url, cover) if with_cover else url


def download_videos(blog, config):
    videos = blog.get("videos", [])
    if not videos:
        return []
    ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg")
    ffprobe = config.get("ffprobe_path") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("视频下载需要 ffmpeg 和 ffprobe，请安装或配置路径。")
    directory = ROOT / "data/videos" / blog["id"]
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for index, video in enumerate(videos, 1):
        path = directory / f"{index:02d}.mp4"
        # Always resolve a fresh playback URL. A failed process leaves only .part.mp4.
        source, cover = resolve_video(video, blog["url"], with_cover=True)
        temporary = path.with_suffix(".part.mp4")
        command = [str(ffmpeg), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                   "-rw_timeout", "30000000", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                   "-user_agent", USER_AGENT, "-referer", blog["url"], "-i", source,
                   "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                   "-movflags", "+faststart", str(temporary)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        if result.returncode != 0:
            raise RuntimeError("ffmpeg 下载或封装视频失败。")
        probe = subprocess.run([str(ffprobe), "-v", "error", "-show_entries",
                                "format=duration:stream=codec_type,codec_name,width,height", "-of", "json",
                                str(temporary)], capture_output=True, timeout=30, check=True)
        info = json.loads(probe.stdout)
        if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
            raise RuntimeError("下载结果没有视频轨道。")
        if float(info.get("format", {}).get("duration", 0)) <= 0:
            raise RuntimeError("下载视频时长无效。")
        if temporary.stat().st_size > 100 * 1024 * 1024:
            raise RuntimeError("视频超过当前设定的 100 MiB 上限，请调整编码后发送。")
        temporary.replace(path)
        cover_path = path.with_suffix(".jpg")
        if cover:
            with urlopen(Request(cover, headers={"Referer": blog["url"], "User-Agent": USER_AGENT}), timeout=30) as response:
                cover_data = response.read(10 * 1024 * 1024 + 1)
            if len(cover_data) > 10 * 1024 * 1024:
                raise RuntimeError("视频封面超过 10 MiB。")
            original = directory / f"{index:02d}-poster.source"
            original.write_bytes(cover_data)
            # QQ implementations expect JPEG thumbnails; normalize the supplied poster.
            subprocess.run([str(ffmpeg), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(original), "-frames:v", "1", str(cover_path)],
                           capture_output=True, check=True, timeout=30)
        else:
            subprocess.run([str(ffmpeg), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(path), "-frames:v", "1", str(cover_path)],
                           capture_output=True, check=True, timeout=30)
        files.append(path)
    return files
