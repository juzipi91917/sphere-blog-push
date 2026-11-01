"""Shared configuration; environment variables override config.json."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_config():
    path = Path(os.environ.get("SPHERE_CONFIG", ROOT / "config.json"))
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key in ("sphere_email", "sphere_password", "qq_api_url", "qq_api_token",
                "qq_group_id", "cos_secret_id", "cos_secret_key", "cos_region",
                "cos_bucket", "gemini_api_key", "gemini_model", "sender_token",
                "sender_host", "sender_port", "ffmpeg_path", "ffprobe_path"):
        if key.upper() in os.environ:
            config[key] = os.environ[key.upper()]
    return config


def required(config, key):
    value = config.get(key)
    if value is None or value == "":
        raise ValueError(f"缺少配置项：{key}")
    return value


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
