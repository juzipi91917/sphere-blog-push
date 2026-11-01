"""Fetch only the first SPHERE SPACE post, with a newly authenticated session."""
import argparse
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from settings import ROOT, load_config, required, save_json
from sphere_login import BASE_URL, login, parse_page, save_session

BLOG_URL = BASE_URL + "/member/contents/sphere_space"
AUTHORS = {"minako": "寿美菜子", "ayahi": "高垣彩阳", "haruka": "户松遥", "aki": "丰崎爱生"}


def parse_latest(html, article_id=None):
    soup = BeautifulSoup(html, "html.parser")
    panels = soup.select(".sphere_space .content-panel")
    if article_id is None:
        panel = panels[0] if panels else None
    else:
        panel = next((p for p in panels if p.select_one(
            f'a.comment_btn[href="/member/contents/{int(article_id)}"]')), None)
    if panel is None:
        raise RuntimeError("未找到博客，可能登录失效、会员权限不足或网页结构已变化。")
    body = panel.select_one(".content-body")
    link = panel.select_one("a.comment_btn[href]")
    published = panel.select_one(".content-published")
    if body is None or link is None or published is None:
        raise RuntimeError("博客字段不完整，停止抓取。")
    url = urljoin(BLOG_URL, link["href"])
    match = re.fullmatch(r"/member/contents/(\d+)", urlparse(url).path)
    if urlparse(url).netloc != "sphere.m-rayn.jp" or not match:
        raise RuntimeError("文章链接异常。")
    date = published.get_text(strip=True)
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", date):
        raise RuntimeError("文章日期格式异常。")
    images = []
    for image in body.select("img"):
        src = image.get("data-src") or image.get("src")
        if src:
            src = urljoin(BLOG_URL, src)
            if urlparse(src).scheme not in ("https", "http"):
                raise RuntimeError("博客图片地址异常。")
            if src not in images:
                images.append(src)
    videos = []
    for frame in body.select("iframe[src]"):
        source = urljoin(BLOG_URL, frame["src"])
        if urlparse(source).hostname != "player-api.p.uliza.jp":
            raise RuntimeError("发现尚不支持的内嵌播放器，请添加解析支持。")
        videos.append({"kind": "uliza", "url": source})
    for element in body.select("video[src], video source[src]"):
        source = urljoin(BLOG_URL, element["src"])
        item = {"kind": "direct", "url": source}
        if item not in videos:
            videos.append(item)
    for element in body.select(".content-published, script, style, iframe, video"):
        element.decompose()
    for br in body.select("br"):
        br.replace_with("\n")
    for block in body.select("p, div"):
        block.append("\n\n")
    text = body.get_text().replace("\r", "")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text and not images and not videos:
        raise RuntimeError("文章正文和图片均为空。")
    author = next((AUTHORS[c] for c in panel.get("class", []) if c in AUTHORS), "未知作者")
    return {"id": match[1], "author": author, "date": date, "text": text,
            "images": images, "videos": videos, "url": url}


def fetch_latest(config, page=1, article_id=None):
    opener, jar, csrf = login(required(config, "sphere_email"), required(config, "sphere_password"))
    save_session(jar, csrf, ROOT / "sphere_session.json")
    page_url = BLOG_URL if int(page) == 1 else BLOG_URL + f"/page/{int(page)}"
    with opener.open(page_url, timeout=30) as response:
        html = response.read().decode("utf-8")
    if not parse_page(html).signed_in:
        raise RuntimeError("会员页面没有保持登录状态。")
    blog = parse_latest(html, article_id)
    save_json(ROOT / "data/latest_blog.json", blog)
    return blog, opener


def main():
    argparse.ArgumentParser(description="重新登录并抓取最新一篇 SPHERE SPACE 博客").parse_args()
    blog, _ = fetch_latest(load_config())
    print(f"已抓取：{blog['author']} / {blog['date']} / ID {blog['id']} / {len(blog['images'])} 张图")
    print(ROOT / "data/latest_blog.json")


if __name__ == "__main__":
    main()
