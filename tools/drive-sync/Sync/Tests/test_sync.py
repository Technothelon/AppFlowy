# SPDX-License-Identifier: AGPL-3.0-only
"""Behavior checks for the private Drive sync protocol, without network calls."""
import fcntl
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive_sync as ds
from pycrdt import Doc, Map, Text
from rocksdict import Options, Rdict


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix="fixture-")
        self.root = Path(self.tmp.name)
        self.workspace = str(uuid.uuid4())
        self.ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.vault = str(uuid.uuid4())
        key = self.root / "key"
        key.write_bytes(os.urandom(32))
        self.configs = []
        docs = {self.workspace: Doc({"data": Map({"folder": Map({"name": "Test workspace"})})})}
        for oid in self.ids:
            docs[oid] = Doc({"data": Map({"text": Text("Original")})})
        for label in ("a", "b"):
            profile = self.root / label
            (profile / "Media/Assets").mkdir(parents=True)
            (profile / "State").mkdir()
            dbpath = profile / "Data/AppFlowy/data_beta.appflowy.cloud/1/collab_db"
            dbpath.parent.mkdir(parents=True)
            raw = {"protocol": 1, "vault_id": self.vault, "device_id": str(uuid.uuid4()),
                   "workspace_id": self.workspace, "uid": 1, "profile": str(profile),
                   "key_file": str(key), "rclone_config": str(self.root / "rclone.conf"),
                   "remote": "gdrive:AppFlowy-Experiment-Sync/" + self.vault,
                   "media": {"Assets": "Media/Assets"}}
            path = self.root / (label + ".json")
            path.write_text(json.dumps(raw))
            opts = ds.options()
            opts.create_if_missing(True)
            db = Rdict(str(dbpath), opts)
            for index, (oid, doc) in enumerate(docs.items()):
                did = (index + 10).to_bytes(8, "big")
                db[ds.mapping_key(raw, oid)] = did
                db[ds.prefix(did) + b"\x00"] = doc.get_update()
                db[ds.prefix(did) + b"\x01"] = doc.get_state()
            db[b"\x03\x01" + uuid.UUID(self.workspace).bytes + b"\x00"] = (123).to_bytes(8, "little")
            db.close()
            self.configs.append(ds.load_config(path))

    def test_config_accepts_another_named_drive_remote(self):
        path = self.configs[0]['_config']
        raw = json.loads(path.read_text())
        raw['remote'] = 'personal-drive:AppFlowy-Experiment-Sync/' + self.vault
        path.write_text(json.dumps(raw))
        self.assertEqual(ds.load_config(path)['remote'], raw['remote'])

    def test_config_rejects_other_drive_folders_and_vaults(self):
        path = self.configs[0]['_config']
        raw = json.loads(path.read_text())
        for remote in ('gdrive:Unrelated-Folder/' + self.vault,
                       'gdrive:AppFlowy-Experiment-Sync/' + str(uuid.uuid4())):
            raw['remote'] = remote
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, 'dedicated experiment folder'):
                ds.load_config(path)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, config):
        db = ds.open_db(config)
        try:
            return ds.object_records(config, db)
        finally:
            db.close()

    def edit(self, config, oid, change, client_id=None):
        db = ds.open_db(config, readonly=False)
        try:
            key = ds.mapping_key(config, oid)
            did = db[key]
            if client_id is None:
                doc = ds.decoded(ds.raw_chunks(db, did))
            else:
                doc = Doc(client_id=client_id)
                for chunk in ds.raw_chunks(db, did):
                    doc.apply_update(chunk)
            change(doc.get("data", type=Map))
            # This fixture simulates AppFlowy's compaction, which may change
            # physical storage without changing document semantics.
            p = ds.prefix(did)
            for k in list(db.keys()):
                if k.startswith(p):
                    del db[k]
            db[p + b"\x00"] = doc.get_update()
            db[p + b"\x01"] = doc.get_state()
        finally:
            db.close()

    def text(self, config, oid):
        return ds.decoded(self.record(config)[oid]["chunks"]).get("data", type=Map)["text"].to_py()

    def append_edit(self, config, oid, token, client_id):
        db = ds.open_db(config, readonly=False)
        try:
            did = db[ds.mapping_key(config, oid)]
            raw = ds.raw_chunks(db, did)
            doc = Doc(client_id=client_id)
            for chunk in raw:
                doc.apply_update(chunk)
            before = doc.get_state()
            text = doc.get("data", type=Map)["text"]
            text.insert(len(text), token)
            p = ds.prefix(did)
            clocks = [int.from_bytes(k[11:15], "big") for k in db.keys()
                      if k.startswith(p+b"\x02") and len(k)==16]
            clock = max(clocks, default=-1)+1
            db[p+b"\x02"+clock.to_bytes(4,"big")+b"\x00"] = doc.get_update(before)
        finally:
            db.close()

    def assert_bounded_roundtrip_history(self, transfer):
        a, b = self.configs
        for round_index in range(10):
            self.append_edit(a,self.ids[0],f" A{round_index}",81234)
            transfer(a,b)
            self.append_edit(b,self.ids[0],f" B{round_index}",92345)
            transfer(b,a)
        self.assertEqual(self.text(a,self.ids[0]), self.text(b,self.ids[0]))
        for config in (a,b):
            db = ds.open_db(config)
            try:
                raw = ds.raw_chunks(db,db[ds.mapping_key(config,self.ids[0])])
            finally:
                db.close()
            self.assertEqual(len(raw),21, 'Each distinct edit should be stored once')

    def test_alternating_edits_do_not_reappend_old_history(self):
        self.assert_bounded_roundtrip_history(lambda a,b:ds.import_pack(b,self.pack(a)))

    def pack(self, config, objects=None):
        return ds.make_pack(config, objects or self.record(config), ds.media_inventory(config))

    def test_distinct_page_edits_survive_both_directions(self):
        a, b = self.configs
        self.edit(a, self.ids[0], lambda m: m["text"].insert(8, " from A"))
        self.edit(b, self.ids[1], lambda m: m["text"].insert(8, " from B"))
        pa, pb = self.pack(a), self.pack(b)
        ds.import_pack(a, pb)
        ds.import_pack(b, pa)
        for c in (a, b):
            self.assertEqual(self.text(c, self.ids[0]), "Original from A")
            self.assertEqual(self.text(c, self.ids[1]), "Original from B")

    def test_same_page_concurrent_edits_converge(self):
        a, b = self.configs
        self.edit(a, self.ids[0], lambda m: m["text"].insert(8, " A"))
        self.edit(b, self.ids[0], lambda m: m["text"].insert(8, " B"))
        pa, pb = self.pack(a), self.pack(b)
        ds.import_pack(a, pb)
        ds.import_pack(b, pa)
        self.assertEqual(self.text(a, self.ids[0]), self.text(b, self.ids[0]))
        self.assertIn(" A", self.text(a, self.ids[0]))
        self.assertIn(" B", self.text(a, self.ids[0]))

    def test_duplicate_editor_identity_is_rejected_before_writing(self):
        a, b = self.configs
        self.edit(a, self.ids[0], lambda m: m["text"].insert(8, " A"), client_id=12345)
        self.edit(b, self.ids[0], lambda m: m["text"].insert(8, " B"), client_id=12345)
        self.assertEqual(self.record(a)[self.ids[0]]["hash"], self.record(b)[self.ids[0]]["hash"])
        with self.assertRaisesRegex(ValueError, "editing identity"):
            ds.import_pack(b, self.pack(a))
        self.assertEqual(self.text(b, self.ids[0]), "Original B")

    def test_deletion_only_update_is_detected_and_merged(self):
        a, b = self.configs
        before = self.record(a)[self.ids[0]]["hash"]
        self.edit(a, self.ids[0], lambda m: m.pop("text"))
        after = self.record(a)[self.ids[0]]["hash"]
        self.assertNotEqual(before, after)
        ds.import_pack(b, self.pack(a))
        self.assertNotIn("text", ds.decoded(self.record(b)[self.ids[0]]["chunks"]).get("data", type=Map))

    def test_repeat_import_changes_nothing(self):
        a, b = self.configs
        self.edit(a, self.ids[0], lambda m: m["text"].insert(8, " edited"))
        data = self.pack(a)
        self.assertEqual(ds.import_pack(b, data)["objects_merged"], 1)
        before = self.record(b)
        self.assertEqual(ds.import_pack(b, data)["objects_merged"], 0)
        self.assertEqual(before, self.record(b))

    def test_compacted_encoding_does_not_trigger_new_changes(self):
        a, _ = self.configs
        before = self.record(a)
        self.edit(a, self.ids[0], lambda m: None)
        after = self.record(a)
        self.assertEqual({k: v["hash"] for k, v in before.items()}, {k: v["hash"] for k, v in after.items()})

    def test_new_object_and_attachment_arrive(self):
        a, b = self.configs
        oid = str(uuid.uuid4())
        doc = Doc({"data": Map({"text": Text("A new page")})})
        record = {"chunks": [doc.get_update()], "hash": ds.doc_fingerprint(doc), "opaque": False, "extra": None}
        (a["_profile"] / "Media/Assets/picture.txt").write_text("Attachment contents")
        result = ds.import_pack(b, ds.make_pack(a, {oid: record}, ds.media_inventory(a)))
        self.assertEqual(result["objects_merged"], 1)
        self.assertEqual(self.text(b, oid), "A new page")
        self.assertEqual((b["_profile"] / "Media/Assets/picture.txt").read_text(), "Attachment contents")

    def test_conflicting_attachment_does_not_modify_database(self):
        a, b = self.configs
        (a["_profile"] / "Media/Assets/shared.txt").write_text("A")
        (b["_profile"] / "Media/Assets/shared.txt").write_text("B")
        self.edit(a, self.ids[0], lambda m: m["text"].insert(8, " changed"))
        before = self.record(b)
        with self.assertRaises(ValueError):
            ds.import_pack(b, self.pack(a))
        self.assertEqual(before, self.record(b))
        self.assertEqual((b["_profile"] / "Media/Assets/shared.txt").read_text(), "B")

    def test_tampered_ciphertext_and_wrong_vault_are_rejected(self):
        a, b = self.configs
        data = bytearray(self.pack(a))
        data[-1] ^= 1
        with self.assertRaises(Exception):
            ds.read_pack(b, bytes(data))
        wrong = dict(b, vault_id=str(uuid.uuid4()))
        with self.assertRaises(Exception):
            ds.read_pack(wrong, self.pack(a))

    def test_error_after_commit_preserves_attachment_and_can_retry(self):
        a, b = self.configs
        self.edit(a, self.ids[0], lambda m: m["text"].insert(8, " committed"))
        (a["_profile"] / "Media/Assets/new.txt").write_text("Attached to committed data")
        data = self.pack(a)
        real_open = ds.open_db

        class FailOnFlush:
            def __init__(self, db):
                self.db = db
            def __getattr__(self, name):
                return getattr(self.db, name)
            def flush(self):
                raise RuntimeError("Simulated failure after an atomic commit")

        def open_with_failure(config, readonly=True):
            db = real_open(config, readonly)
            return db if readonly else FailOnFlush(db)

        with patch.object(ds, "open_db", side_effect=open_with_failure):
            with self.assertRaises(RuntimeError):
                ds.import_pack(b, data)
        self.assertEqual(self.text(b, self.ids[0]), "Original committed")
        self.assertTrue((b["_profile"] / "Media/Assets/new.txt").is_file())
        self.assertEqual(ds.import_pack(b, data)["objects_merged"], 0)

    def test_open_profile_lock_blocks_sync(self):
        a, _ = self.configs
        with (a["_profile"] / "State/session.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):
                with ds.closed_profile(a):
                    self.fail("Lock should block import")

    def test_original_profile_path_is_rejected(self):
        a, _ = self.configs
        raw = json.loads(a["_config"].read_text())
        raw["profile"] = str(Path.home() / ".local/share/AppFlowy")
        bad = self.root / "bad.json"
        bad.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            ds.load_config(bad)

    def test_workspace_identity_is_rotated_and_history_is_preserved(self):
        a, b = self.configs
        before = self.record(a)
        with ds.closed_profile(a):
            first = ds.initialize_editor_identity(a)
            again = ds.initialize_editor_identity(a)
        with ds.closed_profile(b):
            second = ds.initialize_editor_identity(b)
        self.assertEqual(first, again)
        self.assertNotEqual(first, 123)
        self.assertNotEqual(first, second)
        self.assertEqual(before, self.record(a))


if __name__ == "__main__":
    unittest.main(verbosity=2)
