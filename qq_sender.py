"""QQ send function and a small authenticated HTTP receiving service."""
import argparse
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

from settings import load_config, required
from delivery import deliver_once, DeliveryUncertain


def escape_cq(value, parameter=False):
    value = value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    return value.replace(",", "&#44;") if parameter else value


def compose_message(text, images):
    if not isinstance(text, str) or not isinstance(images, list):
        raise ValueError("text 必须为字符串，images 必须为 URL 数组。")
    message = escape_cq(text)
    for image in images:
        if not isinstance(image, str) or urlparse(image).scheme not in ("http", "https"):
            raise ValueError("图片地址必须为 HTTP/HTTPS URL。")
        message += f"[CQ:image,file={escape_cq(image, parameter=True)}]"
    if not message.strip():
        raise ValueError("消息不能为空。")
    return message


def send_message(group_id, text, images, config, videos=None, idempotency_key=None):
    if not isinstance(text, str) or not isinstance(images, list):
        raise ValueError("text 必须为字符串，images 必须为 URL 数组。")
    if isinstance(group_id, bool):
        raise ValueError("group_id 必须为正整数。")
    if not isinstance(group_id, (str, int)) or not str(group_id).isdigit() or int(group_id) <= 0:
        raise ValueError("group_id 必须为正整数。")
    if videos is None:
        videos = []
    if not isinstance(videos, list):
        raise ValueError("videos 必须为 URL 数组。")
    video_messages = []
    for video in videos:
        if isinstance(video, dict):
            url, cover = video.get("url"), video.get("cover")
        else:
            url, cover = video, None
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            raise ValueError("视频地址必须为 HTTP/HTTPS URL。")
        if cover is not None and (not isinstance(cover, str) or urlparse(cover).scheme not in ("http", "https")):
            raise ValueError("视频封面必须为 HTTP/HTTPS URL。")
        segment = f"[CQ:video,file={escape_cq(url, parameter=True)}"
        if cover:
            escaped = escape_cq(cover, parameter=True)
            # go-cqhttp uses cover; NapCat uses thumb. Supplying both keeps the
            # thumbnail explicit with either implementation.
            segment += f",cover={escaped},thumb={escaped}"
        video_messages.append(segment + "]")
    messages = []
    if text.strip() or images:
        messages.append(compose_message(text, images))
    elif not videos:
        raise ValueError("消息不能为空。")
    # Each QQ video is a standalone message, avoiding mixed video/text rejection.
    messages.extend(video_messages)
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("idempotency_key 必须为 1 到 200 字符的字符串。")
        required(config, "qq_api_url")
        required(config, "qq_api_token")
        results = deliver_once(group_id, idempotency_key, messages,
                               lambda group, message: _post_message(group, message, config))
    else:
        results = [_post_message(group_id, message, config) for message in messages]
    if len(results) == 1:
        return results[0]
    return {"message_id": results[0].get("message_id"),
            "message_ids": [result.get("message_id") for result in results]}


def _post_message(group_id, message, config):
    response = requests.post(required(config, "qq_api_url"),
                             headers={"Authorization": "Bearer " + required(config, "qq_api_token")},
                             json={"group_id": int(group_id), "message": message}, timeout=(10, 180))
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"QQ 接口 HTTP {response.status_code}，未确认发送成功。")
    result = response.json()
    if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode") != 0:
        raise RuntimeError("QQ 接口返回发送失败，未更新推送记录。")
    return result.get("data") or {}


def make_handler(config):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404,
                       {"status": "ok" if self.path == "/health" else "not_found"})

        def do_POST(self):
            if self.path != "/send":
                self.reply(404, {"error": "not_found"})
                return
            expected = "Bearer " + required(config, "sender_token")
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self.reply(401, {"error": "unauthorized"})
                return
            try:
                self.connection.settimeout(15)
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1024 * 1024:
                    raise ValueError("请求体必须在 1 字节到 1 MiB 之间。")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("JSON 请求体必须为对象。")
                arguments = (body.get("group_id", config.get("qq_group_id")),
                             body.get("text", ""), body.get("images", []), config)
                options = {key: body[key] for key in ("videos", "idempotency_key") if key in body}
                result = send_message(*arguments, **options)
                self.reply(200, {"status": "ok", "data": result})
            except DeliveryUncertain as exc:
                self.reply(409, {"status": "needs_review", "error": str(exc)})
            except (ValueError, UnicodeError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception:
                # Avoid leaking upstream tokens or signed URLs in exception strings.
                self.reply(502, {"error": "发送失败；请求可能已送达上游，重试前请先核实。"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="HTTP 接收服务：POST /send")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = load_config()
    required(config, "sender_token")
    host = args.host or config.get("sender_host", "127.0.0.1")
    port = args.port or int(config.get("sender_port", 8090))
    server = ThreadingHTTPServer((host, port), make_handler(config))
    print(f"QQ 发送服务：http://{host}:{port}/send", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
