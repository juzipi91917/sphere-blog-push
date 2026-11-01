"""Check four Ameblo authors independently; one complete message per new post."""
import argparse
import json
import re
from urllib.parse import urljoin, urlparse
from urllib.request import build_opener

import requests
from bs4 import BeautifulSoup

from cos_upload import upload_images
from delivery import DeliveryUncertain, check_uncertain
from main import pipeline_lock
from qq_sender import send_message
from settings import ROOT, load_config, save_json
from translator import translate

AUTHORS = {
    "kotobukiminako": "寿美菜子",
    "toyosakiaki": "丰崎爱生",
    "tomatsuharuka": "户松遥",
    "takagakiayahi": "高垣彩阳",
}


def entry_id(url, author):
    parsed = urlparse(url)
    match = re.fullmatch(rf"/{re.escape(author)}-blog/entry-(\d+)\.html", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "ameblo.jp" or not match:
        raise ValueError("文章地址不属于目标作者。")
    return match[1]


def get_html(url):
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=(10, 30))
    response.raise_for_status()
    return response.content.decode("utf-8").replace("\x00", "")


def parse_list(html, author):
    soup = BeautifulSoup(html.replace("\x00", ""), "html.parser")
    links = soup.select(".skin-archiveList h2 a[href]") or soup.select("h2 a[href]")
    for anchor in links:
        url = urljoin("https://ameblo.jp/", anchor["href"])
        try:
            entry_id(url, author)
        except ValueError:
            continue
        # Strip tracking query parameters to keep a stable identity.
        return url.split("?", 1)[0].split("#", 1)[0]
    raise RuntimeError("列表页没有找到该作者的最新文章，未更新缓存。")


def parse_entry(html, url, author):
    identity = entry_id(url, author)
    soup = BeautifulSoup(html.replace("\x00", ""), "html.parser")
    title_node = soup.select_one("h1.skin-entryTitle")
    body = soup.select_one("#entryBody")
    if title_node is None or body is None:
        raise RuntimeError("文章正文不完整或需要访问权限，未更新缓存。")
    title = title_node.get_text(" ", strip=True)
    published = ""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "BlogPosting":
                published = item.get("datePublished", "")
                break
    for node in body.select("noscript, script, style"):
        node.decompose()
    images = []
    # PhotoSwipeImage marks article photos; emoji and layout images are excluded.
    for image in body.select("img.PhotoSwipeImage"):
        src = image.get("data-src") or image.get("data-original") or image.get("src")
        parent = image.find_parent("a", href=True)
        if parent:
            candidate = urljoin(url, parent["href"])
            parsed = urlparse(candidate)
            if parsed.hostname == "stat.ameba.jp" and "/user_images/" in parsed.path:
                src = candidate
        if src:
            src = urljoin(url, src)
            parsed = urlparse(src)
            if parsed.scheme not in ("https", "http"):
                raise RuntimeError("照片地址格式异常。")
            if src not in images:
                images.append(src)
    # Preserve emoji meaning without treating them as separate photo attachments.
    for image in body.select("img"):
        if "PhotoSwipeImage" not in image.get("class", []):
            image.replace_with(image.get("alt", ""))
    embedded = []
    for frame in body.select("iframe[src], video[src], video source[src]"):
        media_url = urljoin(url, frame["src"])
        if urlparse(media_url).scheme in ("https", "http") and media_url not in embedded:
            embedded.append(media_url)
    for node in body.select("iframe, video"):
        node.decompose()
    for anchor in body.select("a[href]"):
        if anchor.select_one("img.PhotoSwipeImage"):
            continue
        href = urljoin(url, anchor["href"])
        text = anchor.get_text(" ", strip=True)
        if text and urlparse(href).scheme in ("https", "http") and href not in text:
            anchor.append(f"（{href}）")
    for br in body.select("br"):
        br.replace_with("\n")
    for block in body.select("p, div"):
        block.append("\n\n")
    text = body.get_text().replace("\r", "")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if embedded:
        text += "\n\n视频/内嵌内容：\n" + "\n".join(embedded)
    if not title or (not text and not images):
        raise RuntimeError("文章标题或正文为空。")
    return {"id": identity, "author": AUTHORS[author], "author_key": author,
            "title": title, "date": published, "text": text, "images": images,
            "url": url, "storage_prefix": f"ameblo/{author}"}


def format_message(blog, translated):
    message = f"Ameblo｜{blog['author']}\n{blog['title']}"
    if blog["date"]:
        message += f"\n{blog['date']}"
    message += f"\n\n{blog['text']}"
    if translated:
        message += f"\n\n{translated}"
    return message


def check_all(config, publish=False, initialize=False):
    if publish and initialize:
        raise ValueError("初始化基线和发送不能同时使用。")
    group = int(config["qq_group_id"])
    if group <= 0:
        raise ValueError("群号必须为正整数。")
    results = []
    with pipeline_lock():
        path = ROOT / "data/ameblo_sent.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for author, display_name in AUTHORS.items():
            result = {"author": display_name, "author_key": author}
            try:
                list_url = f"https://ameblo.jp/{author}-blog/entrylist.html"
                url = parse_list(get_html(list_url), author)
                identity = entry_id(url, author)
                state_key = f"{group}:{author}"
                previous = state.get(state_key, {})
                result.update({"id": identity, "url": url})
                # A stale list response must not cause an older article to be pushed.
                if previous and int(identity) <= int(previous["id"]):
                    result["status"] = "unchanged"
                elif initialize:
                    state[state_key] = {"id": identity, "url": url, "status": "baseline"}
                    save_json(path, state)
                    result["status"] = "initialized"
                else:
                    key = f"ameblo:{author}:{identity}"
                    if publish:
                        check_uncertain(group, key)
                    blog = parse_entry(get_html(url), url, author)
                    translated, translation_status = translate(blog["title"] + "\n\n" + blog["text"], config)
                    text = format_message(blog, translated)
                    urls = upload_images(blog, build_opener(), config) if publish else []
                    save_json(ROOT / f"data/ameblo/{author}/latest.json", {
                        "blog": blog, "text": text, "translation": translated,
                        "translation_status": translation_status, "cos_urls": urls,
                    })
                    result.update({"title": blog["title"], "image_count": len(blog["images"]),
                                   "translation_status": translation_status})
                    if publish:
                        sent = send_message(group, text, urls, config, idempotency_key=key)
                        state[state_key] = {"id": identity, "url": url, "status": "sent", **sent}
                        save_json(path, state)
                        result.update({"status": "sent", **sent})
                    else:
                        result["status"] = "preview"
            except DeliveryUncertain:
                result.update({"status": "needs_review", "error": "送达结果待核实，已阻止自动重发。"})
            except Exception as exc:
                # Avoid leaking SDK secrets or signed URLs in exception strings.
                result.update({"status": "error", "error_type": type(exc).__name__})
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description="检查四位成员的 Ameblo，更新几人就推送几条；默认仅预览")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="向默认群推送每人最新的新文章")
    mode.add_argument("--init", action="store_true", help="只记录当前基线，以后仅推送新发表的文章")
    parser.add_argument("--group-id", type=int)
    parser.add_argument("--no-translation", action="store_true")
    args = parser.parse_args()
    config = load_config()
    if args.group_id is not None:
        config["qq_group_id"] = args.group_id
    if args.no_translation:
        config["translation_enabled"] = False
    results = check_all(config, args.send, args.init)
    print(json.dumps({"sent": sum(r["status"] == "sent" for r in results),
                      "checked": len(results), "results": results}, ensure_ascii=False))
    return 1 if any(r["status"] in ("error", "needs_review") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
