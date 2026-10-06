# SPDX-License-Identifier: AGPL-3.0-only
"""Experimental, closed-app AppFlowy 0.14.4 synchronization over Google Drive.

Drive holds immutable AES-GCM encrypted ZIP records. Each changed collab sends
its original Yjs v1 state and update bytes, never a physical database image.
Import appends updates atomically to the local RocksDB; AppFlowy does the merge.
This tool deliberately supports only profiles below AppFlowy-Experiment.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pycrdt import Array, Doc, Map, Text
from rocksdict import AccessType, BlockBasedOptions, DBCompressionType, Options, Rdict, WriteBatch, WriteOptions

EXPERIMENT = Path(os.environ.get(
    "APPFLOWY_EXPERIMENT_ROOT", str(Path(__file__).resolve().parents[1])
)).resolve()
MAGIC = b"AFDRIVE1\n"
PACK_RE = re.compile(r"[0-9a-f]{32}\.afsync\Z")
MAX_PACK_SIZE = 1024 * 1024 * 1024
MAX_ENTRY_SIZE = 256 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def beneath(path, parent):
    path = Path(path).resolve()
    parent = Path(parent).resolve()
    if not path.is_relative_to(parent):
        raise ValueError("Refusing a path outside the experimental profile")
    return path


def load_config(path):
    path = beneath(path, EXPERIMENT)
    config = json.loads(path.read_text())
    if config.get("protocol") != 1:
        raise ValueError("Unsupported sync protocol")
    uuid.UUID(config["vault_id"])
    uuid.UUID(config["workspace_id"])
    uuid.UUID(config["device_id"])
    profile = beneath(config["profile"], EXPERIMENT)
    if profile == EXPERIMENT / "Sync":
        raise ValueError("Invalid profile directory")
    for key in ("key_file", "rclone_config"):
        beneath(config[key], EXPERIMENT)
    remote = config["remote"]
    remote_name, separator, remote_path = remote.partition(":")
    if (not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+", remote_name)
            or remote_path != "AppFlowy-Experiment-Sync/" + config["vault_id"]):
        raise ValueError("Drive target must be the dedicated experiment folder")
    config["_config"] = path
    config["_profile"] = profile
    config["_state"] = profile / "State/drive-sync-state.json"
    config["_db"] = profile / "Data/AppFlowy/data_beta.appflowy.cloud" / str(config["uid"]) / "collab_db"
    beneath(config["_db"], profile)
    if not (config["_db"] / "CURRENT").is_file():
        raise ValueError("The experimental collab database is missing")
    for group, relative in config["media"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", group):
            raise ValueError("Invalid media group")
        beneath(profile / relative, profile / "Media")
    return config


def fresh_state():
    return {"protocol": 1, "seen": [], "published_objects": {}, "published_media": {}}


def read_state(config):
    p = config["_state"]
    state = json.loads(p.read_text()) if p.exists() else fresh_state()
    if state.get("vault_id", config["vault_id"]) != config["vault_id"]:
        raise ValueError("Sync state belongs to another vault")
    state["vault_id"] = config["vault_id"]
    return state


@contextlib.contextmanager
def closed_profile(config):
    """Use the same lock as the GUI, plus check its executable independently."""
    lock = config["_profile"] / "State/session.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Close AppFlowy Experiment before syncing") from None
        # Native AppFlowy and test sessions must also be closed. Conservatively
        # refuse while any AppFlowy executable is running; never inspect content.
        for process in Path("/proc").iterdir():
            if process.name.isdigit():
                try:
                    if (process / "exe").resolve().name.casefold() == "appflowy":
                        raise RuntimeError("Close AppFlowy before syncing")
                except (FileNotFoundError, PermissionError):
                    pass
        yield


def options():
    opt = Options(raw_mode=True)
    opt.create_if_missing(False)
    # The installed native build includes Zstd and omits Snappy.
    opt.set_compression_type(DBCompressionType.zstd())
    opt.set_bottommost_compression_type(DBCompressionType.zstd())
    # Older AppFlowy RocksDB builds can read format 5. Do not let the binding
    # upgrade the SST format or introduce an incompatible compression codec.
    table = BlockBasedOptions()
    table.set_format_version(5)
    opt.set_block_based_table_factory(table)
    return opt


def open_db(config, readonly=True):
    access = AccessType.read_only() if readonly else AccessType.read_write()
    db = Rdict(str(config["_db"]), options(), column_families={"default": options()}, access_type=access)
    if not readonly:
        # Existing column-family options may be auto-loaded by the binding.
        # Explicitly set its mutable compression options before any writes.
        db.set_options({"compression": "kZSTD", "bottommost_compression": "kZSTD"})
    return db


def initialize_editor_identity(config):
    """Give a copied workspace its own native editing client ID.

    The installed backend caches this per workspace independently of the
    Flutter device preference. This key is local and is never transmitted.
    Call only with closed_profile held, before making edits on a fresh replica.
    """
    key = b"\x03\x01" + uuid.UUID(config["workspace_id"]).bytes + b"\x00"
    db = open_db(config)
    try:
        encoded = db.get(key)
        if encoded is None or len(encoded) != 8:
            raise ValueError("Unknown workspace editing-identity schema; refusing sync")
        current = int.from_bytes(encoded, "little")
        if config.get("editor_device_id") == config["device_id"]:
            if current != config["editor_client_id"]:
                raise ValueError("The local editing identity changed; inspect the profile before syncing")
            return current
        clients = set()
        for record in object_records(config, db).values():
            if record["opaque"]:
                continue
            state = decoded(record["chunks"]).get_state()
            count, offset = read_varint(state, 0)
            for _ in range(count):
                client, offset = read_varint(state, offset)
                _, offset = read_varint(state, offset)
                clients.add(client)
    finally:
        db.close()
    new = int.from_bytes(os.urandom(4), "little")
    while new == 0 or new in clients:
        new = int.from_bytes(os.urandom(4), "little")
    backup = config["_profile"] / "SyncBackups" / ("editor-identity-" + uuid.uuid4().hex[:8])
    shutil.copytree(config["_db"], backup / "collab_db")
    db = open_db(config, readonly=False)
    try:
        batch = WriteBatch(raw_mode=True)
        batch.put(key, new.to_bytes(8, "little"))
        write_options = WriteOptions()
        write_options.sync = True
        db.write(batch, write_options)
        db.flush()
    finally:
        db.close()
    raw = json.loads(config["_config"].read_text())
    raw.update(editor_client_id=new, editor_device_id=config["device_id"])
    atomic_json(config["_config"], raw)
    config.update(editor_client_id=new, editor_device_id=config["device_id"])
    return new


def mapping_key(config, oid):
    uuid.UUID(oid)
    return b"\x01\x00" + int(config["uid"]).to_bytes(8, "big", signed=True) + config["workspace_id"].encode() + oid.encode() + b"\x00"


def prefix(did):
    if len(did) != 8:
        raise ValueError("Unexpected AppFlowy document ID")
    return b"\x01\x01" + did


def raw_chunks(db, did):
    p = prefix(did)
    state = db.get(p + b"\x00")
    if state is None:
        raise ValueError("Missing AppFlowy document state")
    chunks = [state]
    for k, v in db.items(from_key=p + b"\x02"):
        if not k.startswith(p + b"\x02"):
            break
        if len(k) == 16:
            chunks.append(v)
    return chunks


def decoded(chunks):
    # Match AppFlowy's yrs 0.21.3, including its historical move records.
    # This decoder is used only for comparison; original bytes are transmitted
    # and persisted, so its garbage-collection setting cannot rewrite history.
    doc = Doc()
    for chunk in chunks:
        doc.apply_update(chunk)
    return doc


def read_varint(data, offset):
    result = shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        result |= (byte & 127) << shift
        if byte < 128:
            return result, offset
        shift += 7
    raise ValueError("Invalid Yjs varint")


def doc_fingerprint(doc):
    # Yrs iterates hash maps in a different order on each load. Compare a
    # canonical state vector AND delete set, so deletion-only edits also sync.
    state = doc.get_state()
    count, offset = read_varint(state, 0)
    vector = []
    for _ in range(count):
        client, offset = read_varint(state, offset)
        clock, offset = read_varint(state, offset)
        vector.append((client, clock))
    if offset != len(state):
        raise ValueError("Unexpected state-vector data")
    deletions = doc.get_update(state)
    structs, offset = read_varint(deletions, 0)
    if structs != 0:
        raise ValueError("Unexpected structures in deletion-only update")
    count, offset = read_varint(deletions, offset)
    ranges = []
    for _ in range(count):
        client, offset = read_varint(deletions, offset)
        n, offset = read_varint(deletions, offset)
        for _ in range(n):
            clock, offset = read_varint(deletions, offset)
            length, offset = read_varint(deletions, offset)
            ranges.append((client, clock, length))
    if offset != len(deletions):
        raise ValueError("Unexpected deletion-set data")
    return sha(json.dumps([sorted(vector), sorted(ranges)], separators=(",", ":")).encode())


def fingerprint(chunks):
    try:
        return doc_fingerprint(decoded(chunks)), False
    except Exception:
        # Preserve pre-existing opaque/quarantined records byte for byte.
        # They cannot be merged if two replicas independently change them.
        raw = b"".join(struct.pack(">I", len(c)) + c for c in chunks)
        return sha(raw), True


def semantic_value(value):
    if isinstance(value, Map):
        return {k: semantic_value(v) for k, v in value.items()}
    if isinstance(value, Array):
        return [semantic_value(v) for v in value]
    if isinstance(value, Text):
        return {"text_delta": semantic_value(value.diff())}
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {k: semantic_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [semantic_value(v) for v in value]
    return value


def semantic_fingerprint(chunks):
    doc = decoded(chunks)
    roots = {k: semantic_value(doc.get(k, type=Map)) for k in doc.keys()}
    return sha(json.dumps(roots, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def object_records(config, db):
    start = b"\x01\x00" + int(config["uid"]).to_bytes(8, "big", signed=True) + config["workspace_id"].encode()
    metadata = b"\x03\x03" + uuid.UUID(config["workspace_id"]).bytes
    records = {}
    for key, did in db.items(from_key=start):
        if not key.startswith(start):
            break
        if len(key) != 83 or key[-1] != 0:
            raise ValueError("Unknown AppFlowy key schema; refusing sync")
        oid = key[len(start):-1].decode()
        uuid.UUID(oid)
        original = raw_chunks(db, did)
        chunks = list(dict.fromkeys(original))
        digest, opaque = fingerprint(chunks)
        if opaque:
            chunks = original
            digest, opaque = fingerprint(chunks)
        extra_key = metadata + uuid.UUID(oid).bytes
        extra_value = db.get(extra_key)
        records[oid] = {"chunks": chunks, "hash": digest, "opaque": opaque,
                        "extra": (extra_key, extra_value) if extra_value is not None else None}
    if not records or config["workspace_id"] not in records:
        raise ValueError("The expected workspace is not present")
    return records


def media_inventory(config):
    media = {}
    for group, relative in config["media"].items():
        root = beneath(config["_profile"] / relative, config["_profile"] / "Media")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Media symlinks are not supported")
            if path.is_file():
                if path.stat().st_size > MAX_ENTRY_SIZE:
                    raise ValueError("An attachment exceeds the experimental size limit")
                relative_path = path.relative_to(root).as_posix()
                media[f"{group}/{relative_path}"] = {"hash": sha(path.read_bytes()), "path": path}
    return media


def seal(config, plaintext):
    key = Path(config["key_file"]).read_bytes()
    if len(key) != 32:
        raise ValueError("Expected a 256-bit vault key")
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, MAGIC + config["vault_id"].encode())


def unseal(config, data):
    if not data.startswith(MAGIC) or len(data) > MAX_PACK_SIZE:
        raise ValueError("Invalid encrypted sync record")
    offset = len(MAGIC)
    return AESGCM(Path(config["key_file"]).read_bytes()).decrypt(
        data[offset:offset + 12], data[offset + 12:], MAGIC + config["vault_id"].encode())


def make_pack(config, records, media):
    manifest = {"protocol": 1, "vault_id": config["vault_id"], "workspace_id": config["workspace_id"],
                "device_id": config["device_id"], "created_at": stamp(), "objects": {}, "media": {}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for oid, record in records.items():
            names = []
            for index, chunk in enumerate(record["chunks"]):
                name = f"objects/{oid}/{index}.bin"
                archive.writestr(name, chunk)
                names.append(name)
            obj = {"hash": record["hash"], "opaque": record["opaque"], "chunks": names}
            if record["extra"] is not None:
                key, value = record["extra"]
                obj["extra"] = [key.hex(), base64.b64encode(value).decode()]
            manifest["objects"][oid] = obj
        for logical, record in media.items():
            name = "media/" + record["hash"]
            if name not in archive.namelist():
                archive.write(record["path"], name)
            manifest["media"][logical] = {"hash": record["hash"], "entry": name}
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
    data = seal(config, buffer.getvalue())
    if len(data) > MAX_PACK_SIZE:
        raise ValueError("Encrypted record exceeds the experimental size limit")
    return data


def read_pack(config, data):
    plaintext = unseal(config, data)
    with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
        infos = archive.infolist()
        if len(infos) != len({i.filename for i in infos}):
            raise ValueError("Duplicate ZIP entries")
        if sum(i.file_size for i in infos) > 3 * MAX_PACK_SIZE or any(i.file_size > MAX_ENTRY_SIZE for i in infos):
            raise ValueError("Sync record expands beyond the size limit")
        manifest = json.loads(archive.read("manifest.json"))
        if (manifest.get("protocol") != 1 or manifest.get("vault_id") != config["vault_id"]
                or manifest.get("workspace_id") != config["workspace_id"]):
            raise ValueError("Sync record belongs to another vault")
        records, media = {}, {}
        for oid, record in manifest["objects"].items():
            uuid.UUID(oid)
            if not record["chunks"]:
                raise ValueError("Empty collab record")
            if any(not re.fullmatch(rf"objects/{re.escape(oid)}/[0-9]+\.bin", x) for x in record["chunks"]):
                raise ValueError("Invalid collab entry path")
            chunks = [archive.read(x) for x in record["chunks"]]
            digest, opaque = fingerprint(chunks)
            if digest != record["hash"] or opaque != record["opaque"]:
                raise ValueError("Collab checksum mismatch")
            if not opaque:
                chunks = list(dict.fromkeys(chunks))
            extra = None
            if "extra" in record:
                key, value = record["extra"]
                key = bytes.fromhex(key)
                expected = b"\x03\x03" + uuid.UUID(config["workspace_id"]).bytes + uuid.UUID(oid).bytes
                if key != expected:
                    raise ValueError("Invalid object metadata key")
                extra = key, base64.b64decode(value, validate=True)
            records[oid] = {"chunks": chunks, "hash": digest, "opaque": opaque, "extra": extra}
        for logical, record in manifest["media"].items():
            parts = PurePosixPath(logical).parts
            if (len(parts) < 2 or parts[0] not in config["media"] or ".." in parts
                    or "\\" in logical or logical.startswith("/")):
                raise ValueError("Invalid attachment path")
            if record["entry"] != "media/" + record["hash"]:
                raise ValueError("Invalid attachment entry")
            content = archive.read(record["entry"])
            if sha(content) != record["hash"]:
                raise ValueError("Attachment checksum mismatch")
            media[logical] = {"hash": record["hash"], "content": content}
    return records, media


def import_pack(config, data):
    records, media = read_pack(config, data)
    db = open_db(config)
    try:
        current = object_records(config, db)
        planned = {}
        for oid, record in records.items():
            local = current.get(oid)
            if local is not None:
                if record["hash"] == local["hash"]:
                    if (not local["opaque"] and not record["opaque"]
                            and semantic_fingerprint(local["chunks"]) != semantic_fingerprint(record["chunks"])):
                        raise ValueError("The copies share an editing identity and have diverged. Distinct AppFlowy device identities are required.")
                    continue
                if local["opaque"] or record["opaque"]:
                    raise ValueError("An undecodable collab record changed on two copies; manual review required")
                merged = decoded(local["chunks"])
                before = doc_fingerprint(merged)
                for chunk in record["chunks"]:
                    merged.apply_update(chunk)
                if doc_fingerprint(merged) == before:
                    continue
            planned[oid] = record
    finally:
        db.close()
    media_writes = []
    for logical, record in media.items():
        group, relative = logical.split("/", 1)
        path = beneath(config["_profile"] / config["media"][group] / relative,
                       config["_profile"] / config["media"][group])
        if path.exists():
            if not path.is_file() or sha(path.read_bytes()) != record["hash"]:
                raise ValueError("An attachment changed on two copies; existing bytes preserved")
        else:
            media_writes.append((path, record["content"]))
    # Take a recoverable backup before the first local mutation in this record.
    backup = None
    if planned:
        backup = config["_profile"] / "SyncBackups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        shutil.copytree(config["_db"], backup / "collab_db")
    written = []
    committed = False
    try:
        for path, content in media_writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL prevents overwriting a file created during import.
            with path.open("xb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            written.append(path)
        if planned:
            db = open_db(config, readonly=False)
            try:
                batch = WriteBatch(raw_mode=True)
                existing_ids = {v for k, v in db.items() if k.startswith(b"\x01\x00")}
                for oid, record in planned.items():
                    key = mapping_key(config, oid)
                    did = db.get(key)
                    new = did is None
                    if new:
                        did = os.urandom(8)
                        while did in existing_ids:
                            did = os.urandom(8)
                        existing_ids.add(did)
                        batch.put(key, did)
                    p = prefix(did)
                    if new:
                        batch.put(p + b"\x00", record["chunks"][0])
                        chunks = record["chunks"][1:]
                        if not record["opaque"]:
                            batch.put(p + b"\x01", decoded(record["chunks"]).get_state())
                        if record["extra"] is not None:
                            batch.put(*record["extra"])
                        clock = 0
                    else:
                        known = set(current[oid]["chunks"])
                        chunks = [chunk for chunk in record["chunks"] if chunk not in known]
                        clocks = [int.from_bytes(k[11:15], "big") for k, _ in db.items(from_key=p + b"\x02")
                                  if k.startswith(p + b"\x02") and len(k) == 16]
                        clock = max(clocks, default=-1) + 1
                    for chunk in chunks:
                        if clock >= 0xFFFFFFFF:
                            raise ValueError("AppFlowy update clock exhausted")
                        batch.put(p + b"\x02" + clock.to_bytes(4, "big") + b"\x00", chunk)
                        clock += 1
                write_options = WriteOptions()
                write_options.sync = True
                db.write(batch, write_options)
                committed = True
                db.flush()
            finally:
                db.close()
    except BaseException:
        # Media entries are additions, so roll back only files created here.
        if not committed:
            for path in written:
                path.unlink(missing_ok=True)
        raise
    return {"objects_merged": len(planned), "attachments_added": len(written),
            "backup": str(backup) if backup else None}


class Drive:
    def __init__(self, config):
        self.config = config
        self.remote = config["remote"].rstrip("/") + "/packs"

    def run(self, *args):
        result = subprocess.run(["/usr/bin/rclone", "--config", self.config["rclone_config"],
                                 "--contimeout", "15s", "--timeout", "60s", "--retries", "2",
                                 "--low-level-retries", "2", *args], capture_output=True, text=True)
        if result.returncode:
            # Do not echo config contents or authorization responses.
            log = self.config["_profile"] / "State/drive-transfer-error.log"
            log.write_text(stamp() + "\n" + result.stderr)
            os.chmod(log, 0o600)
            raise RuntimeError("Google Drive transfer failed; local edits are retained. See the private transfer log.")
        return result.stdout

    def list(self):
        self.run("mkdir", self.remote)
        listing = json.loads(self.run("lsjson", self.remote, "--files-only", "--max-depth", "1"))
        files = []
        for entry in listing:
            name = entry["Name"]
            if PACK_RE.fullmatch(name):
                if entry["Size"] < len(MAGIC) + 28 or entry["Size"] > MAX_PACK_SIZE:
                    raise ValueError("Drive sync record has an invalid size")
                files.append(name)
        if len(files) != len(set(files)):
            raise ValueError("Duplicate sync records in Google Drive")
        return sorted(files)

    def get(self, name, temporary):
        if not PACK_RE.fullmatch(name):
            raise ValueError("Invalid record name")
        target = temporary / name
        self.run("copyto", self.remote + "/" + name, str(target), "--checksum")
        return target.read_bytes()

    def put(self, name, data, temporary):
        target = temporary / name
        target.write_bytes(data)
        os.chmod(target, 0o600)
        self.run("copyto", str(target), self.remote + "/" + name, "--immutable", "--checksum")


def synchronize(config, progress=print):
    with closed_profile(config):
        initialize_editor_identity(config)
        state = read_state(config)
        drive = Drive(config)
        progress("Checking Google Drive…")
        remote = drive.list()
        seen = set(state["seen"])
        report = {"started_at": stamp(), "records_received": 0, "objects_merged": 0,
                  "attachments_added": 0, "records_uploaded": 0, "uploaded_bytes": 0}
        with tempfile.TemporaryDirectory(prefix="drive-sync-", dir=EXPERIMENT / "Temp") as tmp:
            temporary = Path(tmp)
            for name in remote:
                if name in seen:
                    continue
                progress("Receiving and merging encrypted changes…")
                result = import_pack(config, drive.get(name, temporary))
                report["records_received"] += 1
                report["objects_merged"] += result["objects_merged"]
                report["attachments_added"] += result["attachments_added"]
                seen.add(name)
                state["seen"] = sorted(seen)
                atomic_json(config["_state"], state)
            progress("Checking local pages and attachments…")
            db = open_db(config)
            try:
                records = object_records(config, db)
            finally:
                db.close()
            media = media_inventory(config)
            changed = {k: v for k, v in records.items() if state["published_objects"].get(k) != v["hash"]}
            changed_media = {k: v for k, v in media.items() if state["published_media"].get(k) != v["hash"]}
            # Bound each attachment batch to limit memory and allow retries.
            batches = []
            group, amount = {}, 0
            for logical, record in changed_media.items():
                size = record["path"].stat().st_size
                if group and amount + size > 48 * 1024 * 1024:
                    batches.append(group)
                    group, amount = {}, 0
                group[logical] = record
                amount += size
            if group:
                batches.append(group)
            if changed and not batches:
                batches = [{}]
            for index, batch in enumerate(batches):
                objects = changed if index == 0 else {}
                progress(f"Sending encrypted changes ({index + 1}/{len(batches)})…")
                data = make_pack(config, objects, batch)
                name = uuid.uuid4().hex + ".afsync"
                drive.put(name, data, temporary)
                state["published_objects"].update({k: v["hash"] for k, v in objects.items()})
                state["published_media"].update({k: v["hash"] for k, v in batch.items()})
                seen.add(name)
                state["seen"] = sorted(seen)
                atomic_json(config["_state"], state)
                report["records_uploaded"] += 1
                report["uploaded_bytes"] += len(data)
            report.update(completed_at=stamp(), local_objects=len(records), local_attachments=len(media),
                          opaque_objects_preserved=sum(v["opaque"] for v in records.values()))
            state["last_success"] = report
            atomic_json(config["_state"], state)
            atomic_json(config["_profile"] / "State/drive-sync-last-result.json", report)
            progress("Google Drive sync finished.")
            return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=EXPERIMENT / "Sync/config.json")
    parser.add_argument("command", choices=("status", "sync", "inspect"))
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "status":
        print(json.dumps(read_state(config).get("last_success", {"status": "Not yet synchronized"}), indent=2))
    elif args.command == "inspect":
        with closed_profile(config):
            db = open_db(config)
            try:
                records = object_records(config, db)
            finally:
                db.close()
            print(json.dumps({"objects": len(records), "opaque_objects_preserved": sum(v["opaque"] for v in records.values()),
                              "attachments": len(media_inventory(config))}, indent=2))
    else:
        print(json.dumps(synchronize(config, progress=lambda x: print(x, flush=True)), indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Sync did not finish: {error}", file=sys.stderr)
        raise SystemExit(1)
