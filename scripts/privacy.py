"""Local cache diagnostics and explicit removal of generated data."""

import re
import shutil
import stat
from pathlib import Path

from storage import APPLICATION_ID, CHUNK_BYTES, SCHEMA_VERSION, SOURCE_PURGE_KEY, HealthError, cache_version


MIGRATE = "先 migrate 再复检。"
SIDECARS = ("-journal", "-wal", "-shm")
MEMORY_FILE = "memory.json"
HAN = rb"[\xe4-\xe9][\x80-\xbf]{2}"
SURNAMES = b"|".join(name.encode() for name in "王李张刘陈杨黄赵吴周徐孙马朱郑何郭胡林罗高梁宋谢唐许韩邓曹彭曾萧肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜钟")
BRAND = re.compile(rb"Apple Watch|iPhone|OPPO", re.IGNORECASE)
BRAND_LABEL = re.compile(rb"(?:Apple Watch|iPhone|OPPO)(?: #[1-9][0-9]{0,8})?(?![A-Za-z0-9_ #'-]|" + HAN + rb")")
NAME = re.compile(
    rb"(?:" + "姓名|真实姓名|患者姓名|用户姓名".encode() + rb")[\s,:=\"']{0,8}(?:"
    + "：".encode() + rb"\s{0,4})?(?:" + HAN + rb"){2,4}(?![\x80-\xbf])"
    + rb"|(?<!" + HAN + rb")(?:" + SURNAMES + rb")(?:" + HAN + rb"){1,2}(?=" + "的".encode()
    + rb"[ \t]{0,4}(?:Apple Watch|iPhone|OPPO)|[^\x80-\xbf\xe4-\xe9]|\Z)"
)
EMAIL = re.compile(rb"[A-Za-z0-9_+.%\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,4}")
PHONE = re.compile(rb"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
SERIAL = re.compile(
    rb"(?i:serial(?:[ _-]?(?:number|no))?|s/n|" + "序列号".encode()
    + rb")[ \t]{0,4}[:=#][ \t]{0,4}[A-Za-z0-9-]{5,32}"
    + rb"|(?<![A-Za-z0-9])(?=[A-Z0-9]{0,11}[0-9])(?=[A-Z0-9]{0,11}[A-Z])[A-Z0-9]{10,12}(?![A-Za-z0-9])"
)
FINGERPRINT = re.compile(rb"[0-9a-f]{64}")
META_KEY = re.compile(r"path|file|directory|export|zip|token|secret|password|credential|api.?key|salt|密钥|路径", re.I)
META_VALUE = re.compile(r"[A-Za-z]:[\\/]|\\\\|(?:^|\s)(?:/|~[/\\]|\.\.?[/\\])|\.(?:zip|xml|gpx|csv|sqlite3?)$|-----BEGIN [^-]*PRIVATE KEY|\b(?:sk-|Bearer\s+)\S+", re.I)
ROUTE_FIELDS = {"start_utc", "name", "points", "distance_m", "duration_s", "ascent_m"}


def brand_is_raw(data):
    for match in BRAND.finditer(data):
        before = data[max(0, match.start() - 4):match.start()]
        if before.endswith((b"'s ", "的".encode(), "的 ".encode())) or not BRAND_LABEL.match(data, match.start()):
            return True
    return False


def byte_findings(path):
    patterns = {"name": NAME, "email": EMAIL, "phone": PHONE, "serial": SERIAL}
    found = set()
    with path.open("rb") as stream:
        overlap = b""
        block = stream.read(CHUNK_BYTES)
        while block:
            following = stream.read(CHUNK_BYTES)
            # Include both neighbors so a match near a chunk edge sees its real context.
            data = overlap + block + following[:512]
            values = data if "phone" in found else FINGERPRINT.sub(b"\x00", data)
            found.update(
                key for key, pattern in patterns.items()
                if key not in found and pattern.search(values if key == "phone" else data)
            )
            if "device_label" not in found and brand_is_raw(data):
                found.add("device_label")
            overlap, block = block[-512:], following
    return found


def scan_cache(connection, path):
    path = Path(path)
    checks = []

    def check(name, failed, detail):
        checks.append({"check": name, "status": "fail" if failed else "pass",
                       "detail": detail if failed else "未发现异常。", "fix": MIGRATE if failed else ""})

    def columns(table):
        return {row[1] for row in connection.execute(f"PRAGMA table_xinfo({table})")}

    sidecars = [path.with_name(path.name + suffix) for suffix in SIDECARS[:2]]
    existing = [item for item in sidecars if item.is_file()]
    findings = byte_findings(path)
    for item in existing:
        try:
            findings.update(byte_findings(item))
        except FileNotFoundError:
            # SQLite may finish a transaction and remove its sidecar during a scan.
            continue
    for name, label in (("name", "中文姓名"), ("email", "邮箱"), ("phone", "手机号"),
                        ("serial", "设备序列号"), ("device_label", "原始设备名称")):
        check(f"bytes_{name}", name in findings, f"匹配到疑似{label}。")

    version = cache_version(connection)
    check("sources_schema", version != SCHEMA_VERSION or columns("sources") != {"id", "kind", "brand", "fingerprint"},
          f"缓存 schema {version} 或来源列不符合 schema {SCHEMA_VERSION}。")
    for item in sidecars:
        present = item in existing
        checks.append({"check": item.name[len(path.name) + 1:], "status": "warn" if present else "pass",
                       "detail": "存在 SQLite 附属文件。" if present else "无残留。",
                       "fix": "关闭占用缓存的程序，再复检。" if present else ""})
    raw_device = "device" in columns("ecg") and any(
        SERIAL.search(str(device).encode()) for (device,) in connection.execute("SELECT device FROM ecg")
    )
    check("ecg_device", raw_device, "设备字段含疑似序列号。")
    check("routes_columns", columns("routes") - ROUTE_FIELDS, "存在路线摘要以外的列，可能保存坐标。")
    meta = list(connection.execute("SELECT key, value FROM meta")) if {"key", "value"} <= columns("meta") else []
    raw_meta = any(META_KEY.search(str(key)) or META_VALUE.search(str(value)) for key, value in meta)
    pending = any(key == SOURCE_PURGE_KEY for key, _ in meta)
    check("meta", raw_meta or pending, "疑似残留原始路径或密钥。" if raw_meta else "来源清理未完成。")
    return [*checks, scan_memory(path.parent / MEMORY_FILE)]


def scan_memory(path):
    from memory import load

    result = {"check": "memory", "status": "pass", "detail": "记忆格式符合白名单。", "fix": ""}
    try:
        load(path, missing_ok=False)
    except FileNotFoundError:
        result["detail"] = "未创建记忆文件。"
    except (HealthError, OSError):
        result.update(status="fail", detail="记忆文件不可读，或含可识别信息、未知字段或无效格式。",
                      fix="检查本地记忆文件，移除身份字段或执行 purge --scope memory。")
    return result


def entry_info(path):
    entry = path.lstat()
    if getattr(entry, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT:
        return 0, "junction"
    if stat.S_ISDIR(entry.st_mode):
        return sum(entry_info(child)[0] for child in path.iterdir()), "directory"
    return entry.st_size, "file"


def purge(database, scope, execute):
    """Plan or remove only managed artifacts beside the selected cache."""
    root = database.parent.resolve()
    targets = {}
    if scope in ("cache", "all"):
        main = root / database.name
        if not main.is_symlink() and main.is_file():
            with main.open("rb") as stream:
                header = stream.read(72)
            if not header.startswith(b"SQLite format 3\x00") or int.from_bytes(header[68:72], "big") != APPLICATION_ID:
                raise HealthError("指定文件不是本工具的主缓存。请检查 --db 路径。")
        targets[main] = "主缓存 SQLite"
        targets[root / ".run"] = ".run/"
        temporary = set(root.glob(".health-*.sqlite3"))
        for suffix in SIDECARS:
            temporary.update(path.with_name(path.name.removesuffix(suffix))
                             for path in root.glob(f".health-*.sqlite3{suffix}"))
        for index, path in enumerate(sorted(temporary), 1):
            targets.setdefault(path, f"临时缓存 #{index}")
    if scope in ("memory", "all"):
        targets[root / MEMORY_FILE] = "长期记忆 JSON"
        targets.update((path, "临时记忆文件") for path in root.glob(".memory-*.tmp"))
    results = []
    for path, label in targets.items():
        candidates = [(path, label)]
        if label == "主缓存 SQLite" or path.suffix == ".sqlite3":
            candidates += [(path.with_name(path.name + suffix), label + suffix) for suffix in SIDECARS]
        for candidate, name in candidates:
            try:
                size, kind = entry_info(candidate)
            except FileNotFoundError:
                continue
            if kind == "directory" and candidate.name != ".run":
                continue
            results.append(f"{name}\t{size} 字节")
            if execute:
                if kind == "directory":
                    shutil.rmtree(candidate)
                elif kind == "junction":
                    candidate.rmdir()
                else:
                    candidate.unlink(missing_ok=True)
    heading = "已清理" if execute else "预览清理清单（使用 --yes 执行）"
    return "\n".join([heading, *(results or ["没有匹配的数据。"])]) + "\n"
