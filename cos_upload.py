"""Download post images, upload to COS, and return verified public URLs."""
import hashlib
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from qcloud_cos import CosConfig, CosS3Client

from settings import ROOT, required


def upload_images(blog, opener, config):
    if not blog["images"]:
        return []
    region = required(config, "cos_region")
    bucket = required(config, "cos_bucket")
    client = CosS3Client(CosConfig(
        Region=region, SecretId=required(config, "cos_secret_id"),
        SecretKey=required(config, "cos_secret_key"), Scheme="https",
        Timeout=30,
    ))
    directory = ROOT / "data/images" / blog["id"]
    directory.mkdir(parents=True, exist_ok=True)
    urls = []
    for index, source in enumerate(blog["images"], 1):
        # Send the login session only to the site's own host.
        open_image = opener.open if urlparse(source).hostname == "sphere.m-rayn.jp" else urlopen
        with open_image(source, timeout=30) as response:
            mime = response.headers.get_content_type()
            image = response.read(20 * 1024 * 1024 + 1)
        if len(image) > 20 * 1024 * 1024:
            raise RuntimeError("图片超过 20 MiB，停止推送。")
        if not image or not mime.startswith("image/"):
            raise RuntimeError("下载结果不是图片，停止推送。")
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                     "image/webp": ".webp"}.get(mime)
        if not extension:
            raise RuntimeError(f"暂不支持的图片类型：{mime}")
        name = f"{index:02d}-{hashlib.sha256(image).hexdigest()[:16]}{extension}"
        (directory / name).write_bytes(image)
        object_key = f"{blog.get('storage_prefix', 'sphere_space')}/{blog['id']}/{name}"
        client.put_object(Bucket=bucket, Key=object_key, Body=image,
                          ContentType=mime, EnableMD5=True)
        public_url = f"https://{bucket}.cos.{region}.myqcloud.com/{quote(object_key, safe='/')}"
        # Match the Go program's public URL workflow; do not silently change bucket ACLs.
        with urlopen(public_url, timeout=30) as response:
            if not response.headers.get_content_type().startswith("image/"):
                raise RuntimeError("COS 图片 URL 无法公开访问。")
        urls.append(public_url)
    return urls


def upload_videos(blog, files, config):
    if not files:
        return []
    region = required(config, "cos_region")
    bucket = required(config, "cos_bucket")
    client = CosS3Client(CosConfig(
        Region=region, SecretId=required(config, "cos_secret_id"),
        SecretKey=required(config, "cos_secret_key"), Scheme="https", Timeout=60,
    ))
    urls = []
    for path in files:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        key = f"sphere_space/{blog['id']}/video-{digest.hexdigest()[:16]}.mp4"
        with Path(path).open("rb") as stream:
            client.put_object(Bucket=bucket, Key=key, Body=stream,
                              ContentType="video/mp4", EnableMD5=True)
        url = f"https://{bucket}.cos.{region}.myqcloud.com/{quote(key, safe='/')}"
        from urllib.request import Request
        with urlopen(Request(url, method="HEAD"), timeout=30) as response:
            if response.headers.get_content_type() != "video/mp4":
                raise RuntimeError("COS 视频地址验证失败。")
        cover = Path(path).with_suffix(".jpg")
        if not cover.is_file():
            raise RuntimeError("视频封面缺失，停止发送。")
        cover_key = key[:-4] + "-cover.jpg"
        with cover.open("rb") as stream:
            client.put_object(Bucket=bucket, Key=cover_key, Body=stream,
                              ContentType="image/jpeg", EnableMD5=True)
        cover_url = f"https://{bucket}.cos.{region}.myqcloud.com/{quote(cover_key, safe='/')}"
        with urlopen(Request(cover_url, method="HEAD"), timeout=30) as response:
            if response.headers.get_content_type() != "image/jpeg":
                raise RuntimeError("COS 视频封面地址验证失败。")
        urls.append({"url": url, "cover": cover_url})
    return urls
