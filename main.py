"""One-shot pipeline. Default is preview; --send explicitly publishes to QQ."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import uuid

from cos_upload import upload_images, upload_videos
from video_download import download_videos
from qq_sender import send_message
from delivery import DeliveryUncertain, check_uncertain
from settings import ROOT, load_config, save_json
from sphere_blog import fetch_latest
from translator import translate


@contextmanager
def pipeline_lock():
    directory = ROOT / "data"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "pipeline.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt
            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            raise RuntimeError("已有推送任务运行，请稍后再试。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            unlock()


def format_text(blog, translated):
    text = f"SPHERE SPACE｜{blog['author']}｜{blog['date']}\n\n{blog['text']}"
    if translated:
        text += f"\n\n{translated}"
    return text


def run(config, publish=False, force=False, upload_preview=False, page=1, article_id=None):
    with pipeline_lock():
        blog, opener = fetch_latest(config, page=page, article_id=article_id)
        group = int(config["qq_group_id"])
        if group <= 0:
            raise ValueError("群号必须大于 0。")
        state_path = ROOT / "data/sent.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        key = f"{group}:{blog['id']}"
        if publish and key in state and not force:
            return {"status": "skipped", "reason": "already_sent", "blog_id": blog["id"], "group_id": group}
        if publish and not force:
            check_uncertain(group, f"blog:{blog['id']}")
        translated, translation_status = translate(blog["text"], config)
        text = format_text(blog, translated)
        urls = upload_images(blog, opener, config) if publish or upload_preview else []
        video_files = download_videos(blog, config) if publish or upload_preview else []
        video_urls = upload_videos(blog, video_files, config) if video_files else []
        preview = {"blog": blog, "group_id": group, "text": text, "translation": translated,
                   "translation_status": translation_status, "cos_urls": urls,
                   "images_uploaded": publish or upload_preview, "cos_video_urls": video_urls,
                   "video_files": [str(path) for path in video_files]}
        save_json(ROOT / "data/latest_preview.json", preview)
        (ROOT / "data/latest_message.txt").write_text(text, encoding="utf-8")
        if not publish:
            return {"status": "preview", "blog_id": blog["id"], "translation_status": translation_status,
                    "image_count": len(blog["images"]), "video_count": len(blog.get("videos", [])),
                    "images_uploaded": upload_preview}
        # No automatic send retry: a timeout can happen after the bot has accepted the message.
        delivery_key = f"blog:{blog['id']}"
        if force:
            delivery_key += ":force:" + uuid.uuid4().hex
        options = {"idempotency_key": delivery_key}
        if video_urls:
            options["videos"] = video_urls
        result = send_message(group, text, urls, config, **options)
        state[key] = {**result, "url": blog["url"]}
        save_json(state_path, state)
        return {"status": "sent", "group_id": group, "blog_id": blog["id"],
                "translation_status": translation_status, **result}


def main():
    parser = argparse.ArgumentParser(description="登录→最新博客→翻译→COS→QQ；默认仅预览")
    parser.add_argument("--send", action="store_true", help="上传图片并发送到 QQ 群")
    parser.add_argument("--upload-images", "--upload-media", action="store_true", help="预览时也下载并上传图片、视频及封面到 COS")
    parser.add_argument("--force", action="store_true", help="允许重复发送同一篇博客")
    parser.add_argument("--group-id", type=int)
    parser.add_argument("--no-translation", action="store_true")
    parser.add_argument("--page", type=int, default=1, help="抓取指定列表页，默认第 1 页")
    parser.add_argument("--article-id", type=int, help="选择该列表页中指定的博客")
    args = parser.parse_args()
    config = load_config()
    if args.group_id is not None:
        config["qq_group_id"] = args.group_id
    if args.no_translation:
        config["translation_enabled"] = False
    try:
        if args.page < 1:
            raise ValueError("页码必须大于 0。")
        result = run(config, args.send, args.force, args.upload_images, args.page, args.article_id)
    except DeliveryUncertain as exc:
        print(f"待核实：{exc} 进度保存在 data/delivery.sqlite3；不要使用 --force 自动重试。", file=sys.stderr)
        return 2
    except Exception as exc:
        # SDK exceptions can contain credentials or signed request URLs.
        print(f"执行失败（{type(exc).__name__}）；请检查配置及 data/delivery.sqlite3 中的分条进度，勿盲目重发。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
