#!/usr/bin/env python3
"""Log in to Sphere over HTTPS using only the Python standard library."""

import argparse
import getpass
import http.cookiejar
import json
import os
from pathlib import Path
import sys
from settings import ROOT, load_config
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

BASE_URL = "https://sphere.m-rayn.jp"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf_token = None
        self.authenticity_token = None
        self.signed_in = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "csrf-token":
            self.csrf_token = attrs.get("content")
        if tag == "input" and attrs.get("name") == "authenticity_token":
            self.authenticity_token = attrs.get("value")
        if tag == "a" and attrs.get("href", "").split("?")[0] in (
            "/signout", "/users/sign_out"
        ):
            self.signed_in = True


def parse_page(html):
    page = PageParser()
    page.feed(html)
    return page


def login(email, password, timeout=30):
    """Return (authenticated opener, cookie jar, current CSRF token).

    Every call creates a fresh session and performs a new login.
    """
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 SphereLogin/1.0")]
    with opener.open(BASE_URL + "/signin", timeout=timeout) as response:
        page = parse_page(response.read().decode("utf-8"))
    csrf = page.authenticity_token or page.csrf_token
    if not csrf:
        raise RuntimeError("登录页没有 CSRF token，网站登录流程可能已变更。")

    payload = urlencode({
        "authenticity_token": csrf,
        "user[login]": email,
        "user[password]": password,
        "user[remember_me]": "true",
    }).encode("utf-8")
    request = Request(BASE_URL + "/users/sign_in", data=payload, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrf,
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/signin",
    })
    with opener.open(request, timeout=timeout) as response:
        try:
            result = json.loads(response.read().decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError("登录接口没有返回预期的 JSON。") from exc
    if not isinstance(result, dict):
        raise RuntimeError("登录接口返回格式异常。")
    if result.get("errors") or result.get("status") not in (None, 200, "200"):
        raise RuntimeError("登录被拒绝，请检查账号密码或是否需要额外验证。")

    # A 200 response or a session cookie alone does not prove authentication.
    with opener.open(BASE_URL + "/", timeout=timeout) as response:
        home = parse_page(response.read().decode("utf-8"))
    if not home.signed_in:
        raise RuntimeError("登录后未检测到退出登录入口，无法确认登录成功。")
    if not any(c.name == "_skiyaki_session" for c in jar):
        raise RuntimeError("登录成功页面缺少预期的会话 Cookie。")
    return opener, jar, home.csrf_token or csrf


def save_session(jar, csrf_token, output):
    """Persist actual cookies, including their scope and expiration metadata."""
    cookies = [{
        "name": c.name,
        "value": c.value,
        "domain": c.domain,
        "path": c.path,
        "secure": c.secure,
        "expires": c.expires,
        "discard": c.discard,
        "domain_specified": c.domain_specified,
        "domain_initial_dot": c.domain_initial_dot,
        "path_specified": c.path_specified,
        "rest": dict(c._rest),
    } for c in jar]
    probe = Request(BASE_URL + "/")
    jar.add_cookie_header(probe)
    data = {
        "base_url": BASE_URL,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "auth_type": "cookie",
        "csrf_token": csrf_token,
        "cookie_header": probe.get_header("Cookie", ""),
        "cookies": cookies,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Restrict creation permissions on platforms that implement POSIX modes.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return output.resolve()


def load_session(path):
    """Restore saved cookies; re-run login() when authentication expires."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    jar = http.cookiejar.CookieJar()
    for c in data["cookies"]:
        if c["domain"].lstrip(".") != "sphere.m-rayn.jp":
            raise ValueError("会话文件包含非 Sphere 域名。")
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=c["name"], value=c["value"],
            port=None, port_specified=False,
            domain=c["domain"], domain_specified=c["domain_specified"],
            domain_initial_dot=c["domain_initial_dot"],
            path=c["path"], path_specified=c["path_specified"],
            secure=c["secure"], expires=c["expires"], discard=c["discard"],
            comment=None, comment_url=None, rest=c.get("rest", {}),
            rfc2109=False,
        ))
    return build_opener(HTTPCookieProcessor(jar)), data["csrf_token"]


def main():
    config = load_config()
    parser = argparse.ArgumentParser(description="重新登录 Sphere 并保存 Cookie / CSRF token")
    parser.add_argument("--email", default=config.get("sphere_email"))
    parser.add_argument("--output", default=str(Path(__file__).with_name("sphere_session.json")))
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    email = args.email or input("邮箱: ").strip()
    password = config.get("sphere_password") or getpass.getpass("密码: ")
    if not email or not password or args.timeout <= 0:
        parser.error("邮箱和密码不能为空，timeout 必须大于 0。")
    try:
        _, jar, csrf = login(email, password, args.timeout)
        path = save_session(jar, csrf, args.output)
    except HTTPError as exc:
        print(f"失败：HTTP {exc.code}。请检查登录信息或网站验证要求。", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError, RuntimeError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print(f"登录成功，会话已保存：{path}")
    print("Cookie 名称：" + ", ".join(c.name for c in jar))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
