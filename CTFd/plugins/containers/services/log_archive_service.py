"""Portable local container-log archives and an SQLite query index."""
import gzip
import json
import os
import sqlite3
import tarfile
import tempfile
import re
from datetime import datetime
from pathlib import Path


class LogArchiveService:
    _DOCKER_TIMESTAMP = re.compile(
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s(?P<message>.*)$"
    )

    def __init__(self):
        self.root = Path(os.environ.get("CONTAINER_LOG_DIR", ".data/CTFd/container_logs"))
        self.archive_dir = self.root / "archives"
        self.index_path = self.root / "index.sqlite3"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.index_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY, timestamp TEXT, account_id INTEGER,
                challenge_id INTEGER, instance_uuid TEXT, container_id TEXT,
                stream TEXT, message TEXT, archive TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS logs_filter ON logs(account_id, challenge_id, container_id)")

    def archive_instance(self, instance, logs):
        if not logs:
            return None
        name = f"{instance.uuid}.ndjson.gz"
        path = self.archive_dir / name
        fallback_timestamp = datetime.utcnow().isoformat() + "Z"
        lines = logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs)
        with gzip.open(path, "wt", encoding="utf-8") as output:
            for line in lines.splitlines():
                match = self._DOCKER_TIMESTAMP.match(line)
                timestamp = match.group("timestamp") if match else fallback_timestamp
                message = match.group("message") if match else line
                record = {"timestamp": timestamp, "account_id": instance.account_id,
                          "challenge_id": instance.challenge_id, "instance_uuid": instance.uuid,
                          "container_id": instance.container_id, "stream": "stdout", "message": message}
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        with sqlite3.connect(self.index_path) as conn:
            conn.execute("DELETE FROM logs WHERE instance_uuid = ?", (instance.uuid,))
            for line in lines.splitlines():
                match = self._DOCKER_TIMESTAMP.match(line)
                timestamp = match.group("timestamp") if match else fallback_timestamp
                message = match.group("message") if match else line
                conn.execute("INSERT INTO logs(timestamp,account_id,challenge_id,instance_uuid,container_id,stream,message,archive) VALUES(?,?,?,?,?,?,?,?)",
                    (timestamp, instance.account_id, instance.challenge_id, instance.uuid, instance.container_id, "stdout", message, name))
        return name

    def query(self, account_id=None, challenge_id=None, container_id=None, text_query=None, limit=500):
        clauses, values = [], []
        for column, value in (("account_id", account_id), ("challenge_id", challenge_id)):
            if value:
                clauses.append(column + " = ?"); values.append(value)
        if container_id:
            clauses.append("container_id LIKE ?"); values.append("%" + container_id + "%")
        if text_query:
            clauses.append("message LIKE ?"); values.append("%" + text_query + "%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with sqlite3.connect(self.index_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM logs" + where + " ORDER BY timestamp DESC LIMIT ?", values + [limit]).fetchall()]

    def export_archive(self):
        fd, filename = tempfile.mkstemp(suffix=".ctfdlogs.tar.gz")
        os.close(fd)
        with tarfile.open(filename, "w:gz") as archive:
            archive.add(self.archive_dir, arcname="archives")
            manifest = {"format": "ctfd-container-logs-v1", "created_at": datetime.utcnow().isoformat() + "Z"}
            info = tarfile.TarInfo("manifest.json")
            data = json.dumps(manifest).encode()
            info.size = len(data)
            import io
            archive.addfile(info, io.BytesIO(data))
        return filename

    def import_archive(self, uploaded):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "import.tar.gz"
            uploaded.save(path)
            with tarfile.open(path, "r:gz") as archive:
                members = [m for m in archive.getmembers() if m.name == "manifest.json" or m.name.startswith("archives/")]
                for member in members:
                    target = (Path(directory) / member.name).resolve()
                    if not str(target).startswith(str(Path(directory).resolve())):
                        raise ValueError("Unsafe archive path")
                    archive.extract(member, directory)
            imported = 0
            for source in (Path(directory) / "archives").glob("*.ndjson.gz"):
                target = self.archive_dir / source.name
                target.write_bytes(source.read_bytes())
                with gzip.open(source, "rt", encoding="utf-8") as stream, sqlite3.connect(self.index_path) as conn:
                    for line in stream:
                        record = json.loads(line)
                        conn.execute("INSERT INTO logs(timestamp,account_id,challenge_id,instance_uuid,container_id,stream,message,archive) VALUES(?,?,?,?,?,?,?,?)",
                            tuple(record.get(k) for k in ("timestamp","account_id","challenge_id","instance_uuid","container_id","stream","message")) + (source.name,))
                        imported += 1
            return imported
