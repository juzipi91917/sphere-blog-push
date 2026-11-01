"""Durable per-message send journal. Never retry an ambiguous send automatically."""
import json
import sqlite3
from pathlib import Path

from settings import ROOT


class DeliveryUncertain(RuntimeError):
    pass


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS parts (
        delivery TEXT NOT NULL, part INTEGER NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT, PRIMARY KEY(delivery, part))""")
    return db


def deliver_once(group_id, key, messages, post, path=None):
    """Freeze the message plan, then commit each transition before network I/O.

    'sending' survives a crash, 'unknown' survives a request failure; both stop
    later invocations. 'sent' parts are reused without another network request.
    """
    path = path or ROOT / "data/delivery.sqlite3"
    identity = f"{int(group_id)}:{key}"
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT part,payload,status,result FROM parts WHERE delivery=? ORDER BY part",
                          (identity,)).fetchall()
        if not rows:
            for index, message in enumerate(messages):
                db.execute("INSERT INTO parts VALUES (?,?,?,'prepared',NULL)", (identity, index, message))
        db.commit()
        results = []
        # Existing payloads are authoritative even if a later translation varies.
        rows = db.execute("SELECT part,payload,status,result FROM parts WHERE delivery=? ORDER BY part",
                          (identity,)).fetchall()
        for index, message, _, _ in rows:
            db.execute("BEGIN IMMEDIATE")
            status, result = db.execute("SELECT status,result FROM parts WHERE delivery=? AND part=?",
                                        (identity, index)).fetchone()
            if status == "sent":
                db.commit()
                results.append(json.loads(result))
                continue
            if status in ("sending", "unknown"):
                db.rollback()
                raise DeliveryUncertain(f"第 {index + 1} 条消息结果待核实，已阻止自动重发；请检查群记录。")
            db.execute("UPDATE parts SET status='sending' WHERE delivery=? AND part=?", (identity, index))
            db.commit()
            try:
                result = post(group_id, message)
            except Exception as exc:
                db.execute("UPDATE parts SET status='unknown' WHERE delivery=? AND part=?", (identity, index))
                db.commit()
                raise DeliveryUncertain(f"第 {index + 1} 条消息未确认成功，已保留此前进度并阻止自动重发。") from exc
            db.execute("UPDATE parts SET status='sent',result=? WHERE delivery=? AND part=?",
                       (json.dumps(result, ensure_ascii=False), identity, index))
            db.commit()
            results.append(result)
        return results
    finally:
        db.close()


def resolve(key, group_id, part, *, message_id=None, not_sent=False, path=None):
    """Explicit operator resolution after checking the actual group history."""
    if (message_id is not None) == not_sent:
        raise ValueError("请选择提供 message_id 或明确确认未发送。")
    if message_id is not None and (isinstance(message_id, bool) or not isinstance(message_id, int) or message_id == 0):
        raise ValueError("message_id 必须为非零整数。")
    identity = f"{int(group_id)}:{key}"
    db = connect(path or ROOT / "data/delivery.sqlite3")
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM parts WHERE delivery=? AND part=?", (identity, part - 1)).fetchone()
            if not row or row[0] not in ("sending", "unknown"):
                raise ValueError("该消息不是待核实状态。")
            db.execute("UPDATE parts SET status=?,result=? WHERE delivery=? AND part=?",
                       ("prepared" if not_sent else "sent",
                        None if not_sent else json.dumps({"message_id": message_id}), identity, part - 1))
    finally:
        db.close()


def check_uncertain(group_id, key, path=None):
    path = Path(path or ROOT / "data/delivery.sqlite3")
    if not path.exists():
        return
    db = connect(path)
    try:
        row = db.execute("SELECT part FROM parts WHERE delivery=? AND status IN ('sending','unknown') ORDER BY part LIMIT 1",
                         (f"{int(group_id)}:{key}",)).fetchone()
        if row:
            raise DeliveryUncertain(f"第 {row[0] + 1} 条消息结果待核实，本次未重新准备或发送消息。")
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="人工核实群记录后，处理待核实的分条发送记录")
    parser.add_argument("--key", required=True, help="主流程使用 blog:文章ID")
    parser.add_argument("--group-id", type=int, required=True)
    parser.add_argument("--part", type=int, required=True, help="第几条消息，从 1 开始")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--message-id", type=int, help="确认已发送，补记真实消息 ID")
    choice.add_argument("--not-sent", action="store_true", help="仅在确认未送达后允许重试该条")
    args = parser.parse_args()
    resolve(args.key, args.group_id, args.part, message_id=args.message_id, not_sent=args.not_sent)
    print("该条发送记录已更新。已成功的其他消息保持不变。")
