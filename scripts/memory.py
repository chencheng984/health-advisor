"""Store a user's confirmed review context locally as versioned JSON."""

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from privacy import EMAIL, META_VALUE, NAME, PHONE, SERIAL
from storage import HealthError, SOURCE_LABELS


COLLECTIONS = {"goal": "goals", "constraint": "constraints", "device": "devices", "review": "reviews"}
ID = re.compile(r"[0-9a-f]{12}\Z")
PRIVATE = re.compile(
    r"(?:手机号|住址|账号|微信号|患者|姓名|序列号|latitude|longitude)\s*[:：=]|[\w.+-]+@[\w.-]+|"
    r"[+-]?\d{1,3}\.\d{3,}\s*[,，]\s*[+-]?\d{1,3}\.\d{3,}|https?://|"
    r"(?:街|路|巷)\s*\d{1,6}\s*号|(?:的|['’]s)\s*(?:Apple Watch|iPhone|OPPO)|\bOPPO\s+[\w-]+", re.I,
)
SERIES = re.compile(r"(?:\d+(?:\.\d+)?\s*[,，、]\s*){2}\d|(?:\d{4}-\d\d-\d\d.*){2}")
WEEKDAY = re.compile(r"(?:每周|周)[一二三四五六日天]")


def empty_memory():
    return {"version": 1, "user": {"birth_year": None}, "goals": [], "constraints": [],
            "devices": [], "reviews": [], "prefs": {}}


def checked_text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or value != value.strip() or not value.isprintable():
        raise HealthError("记忆文本须为一行，长度为 1 至 160 字符。")
    encoded = value.encode()
    if (PRIVATE.search(value) or META_VALUE.search(value) or SERIES.search(value)
            or any(pattern.search(encoded) for pattern in (EMAIL, PHONE, SERIAL))
            or NAME.search(WEEKDAY.sub("星期", value).encode())):
        raise HealthError("记忆文本含疑似身份、原始路径、位置或数值序列。请先改为一句脱敏摘要。")
    return value


def valid_date(value):
    return isinstance(value, str) and date.fromisoformat(value).isoformat() == value


def validate(data):
    """Reject unknown fields so imports and hand-edited files obey the same rules."""
    template = empty_memory()
    try:
        if (not isinstance(data, dict) or data.keys() != template.keys() or type(data["version"]) is not int
                or data["version"] != 1 or data["prefs"] != {} or set(data["user"]) != {"birth_year"}):
            raise ValueError
        year = data["user"]["birth_year"]
        if year is not None and (type(year) is not int or not 1900 <= year <= date.today().year):
            raise ValueError
        seen = set()
        for key in COLLECTIONS.values():
            if not isinstance(data[key], list):
                raise ValueError
            for row in data[key]:
                fields = {"id", "updated", "one_line", "recheck_on"} if key == "reviews" else {"id", "updated", "text"}
                if row.keys() != fields or not ID.fullmatch(row["id"]) or row["id"] in seen:
                    raise ValueError
                seen.add(row["id"])
                stamp = datetime.fromisoformat(row["updated"])
                if stamp.tzinfo is None or stamp.isoformat(timespec="seconds") != row["updated"]:
                    raise ValueError
                if key == "reviews":
                    checked_text(row["one_line"])
                    if not valid_date(row["recheck_on"]):
                        raise ValueError
                elif key == "devices":
                    if row["text"] not in SOURCE_LABELS.values():
                        raise ValueError
                else:
                    checked_text(row["text"])
    except (AttributeError, KeyError, TypeError, ValueError):
        raise HealthError("记忆文件含未允许的字段、设备标签或记录格式。") from None
    return data


def unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def load(path, *, missing_ok=True):
    try:
        with path.open(encoding="utf-8") as stream:
            return validate(json.load(stream, object_pairs_hook=unique_keys))
    except FileNotFoundError:
        if missing_ok:
            return empty_memory()
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise HealthError("记忆文件不是有效的 UTF-8 JSON。") from None


def export(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def save(path, data):
    content = export(validate(data))
    fd, name = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


@contextmanager
def memory_lock(path):
    # Keep a stable lock file so pending writers always lock the same file.
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def upsert(data, key, values, identifier):
    entries = data[COLLECTIONS[key]]
    if identifier is not None:
        before = len(entries)
        entries[:] = [entry for entry in entries if entry["id"] != identifier]
        if len(entries) == before:
            raise HealthError("该类别没有这个记忆 ID。请用 memory show 查看可用 ID。")
    record = {"id": identifier or uuid4().hex[:12], **values,
              "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    entries.append(record)
    return record


def set_goal(data, text, identifier=None):
    return upsert(data, "goal", {"text": checked_text(text)}, identifier)


def add_constraint(data, text, identifier=None):
    return upsert(data, "constraint", {"text": checked_text(text)}, identifier)


def record_review(data, one_line, recheck_on, identifier=None):
    return upsert(data, "review", {"one_line": checked_text(one_line), "recheck_on": recheck_on.isoformat()}, identifier)


def forget(data, identifier=None):
    if identifier is None:
        data.update(empty_memory())
        return
    for key in COLLECTIONS.values():
        for row in data[key]:
            if row["id"] == identifier:
                data[key].remove(row)
                return
    raise HealthError("没有这个记忆 ID。请用 memory show 查看可用 ID。")


def run_memory(args, path):
    command = args.memory_command
    if command in ("show", "export"):
        return export(load(path))
    if command == "forget" and not args.yes:
        target = "全部记忆" if args.all else "指定的记忆条目"
        raise HealthError(f"即将删除{target}。确认后请在同一条命令追加 --yes。")
    path.parent.mkdir(parents=True, exist_ok=True)
    with memory_lock(path):
        data = load(path)
        if command == "init":
            if not path.exists():
                save(path, data)
            return export({"status": "ready", "version": 1})
        if command == "forget":
            forget(data, args.id)
            result = {"status": "forgotten"}
        elif command == "review":
            result = record_review(data, args.one_line, args.recheck_on, args.id)
        elif args.kind == "device":
            result = upsert(data, "device", {"text": args.text}, args.id)
        else:
            setter = set_goal if args.kind == "goal" else add_constraint
            result = setter(data, args.text, args.id)
        save(path, data)
    return export(result)


def review_context(data, today=None):
    latest = data["reviews"][-1] if data["reviews"] else {"one_line": None, "recheck_on": None}
    recheck = latest["recheck_on"]
    return {"goals": [row["text"] for row in data["goals"]],
            "constraints": [row["text"] for row in data["constraints"]],
            "one_line": latest["one_line"], "recheck_on": recheck,
            "due": recheck is not None and date.fromisoformat(recheck) <= (today or date.today())}


def next_review_note(context):
    date_label = context["recheck_on"]
    if context["due"]:
        return f"复核已到期（{date_label}）。请先确认上次安排的行动有没有完成。"
    return f"计划于 {date_label} 复核。"
