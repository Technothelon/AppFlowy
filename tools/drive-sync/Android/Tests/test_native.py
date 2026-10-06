# SPDX-License-Identifier: AGPL-3.0-only
"""Compare the portable adapter with the verified desktop implementation."""
import base64
import ctypes
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent/'Sync'))
sys.path.insert(0, str(ROOT.parent/'Sync/Tests'))
import drive_sync as ds
from test_sync import SyncTests

lib = ctypes.CDLL(os.environ.get('AFDRIVE_LIBRARY', str(ROOT/'Native/target/release/libafdrive.so')))
lib.afdrive_json.argtypes = [ctypes.c_char_p]
lib.afdrive_json.restype = ctypes.c_void_p
lib.afdrive_free.argtypes = [ctypes.c_void_p]

def engine(config, action, **args):
    raw = {k:v for k,v in config.items() if not k.startswith('_')}
    raw['uid'] = str(raw['uid'])
    raw['profile'] = str(raw['profile'])
    ptr = lib.afdrive_json(json.dumps(dict(config=raw,action=action,**args)).encode())
    try:
        result = json.loads(ctypes.string_at(ptr))
    finally:
        lib.afdrive_free(ptr)
    if not result['ok']:
        raise RuntimeError(result['error'])
    return result['result']

def wire(records):
    result = {}
    for oid,r in records.items():
        item = dict(hash=r['hash'],opaque=r['opaque'],chunks=[base64.b64encode(b).decode() for b in r['chunks']])
        if r['extra']:
            item['extra'] = [r['extra'][0].hex(),base64.b64encode(r['extra'][1]).decode()]
        result[oid] = item
    return result

class PortableTests(SyncTests):
    # Inherit fixture utilities without rerunning the desktop-only test suite.
    for _name in [n for n in vars(SyncTests) if n.startswith('test_')]:
        locals()[_name] = None

    def test_native_alternating_edits_keep_history_bounded(self):
        self.assert_bounded_roundtrip_history(lambda a,b:engine(b,'import',objects=wire(self.record(a))))

    def test_native_accepts_large_legacy_duplicate_history(self):
        a,b = self.configs
        self.append_edit(a,self.ids[0],' legacy duplicate record',81234)
        incoming=wire({self.ids[0]:self.record(a)[self.ids[0]]})
        incoming[self.ids[0]]['chunks'] *= 60000
        self.assertEqual(engine(b,'import',objects=incoming)['objects_merged'],1)
        self.assertIn(' legacy duplicate record',self.text(b,self.ids[0]))
        db=ds.open_db(b)
        try:raw=ds.raw_chunks(db,db[ds.mapping_key(b,self.ids[0])])
        finally:db.close()
        self.assertEqual(len(raw),2)

    def test_native_merge_repeat_and_deletion(self):
        a,b = self.configs
        self.edit(a,self.ids[0],lambda m:m['text'].insert(8,' from desktop'),client_id=81234)
        self.edit(b,self.ids[0],lambda m:m['text'].insert(8,' from phone'),client_id=92345)
        incoming = wire(self.record(a))
        self.assertEqual(engine(b,'import',objects=incoming)['objects_merged'],1)
        self.assertIn(' from phone',self.text(b,self.ids[0]))
        self.assertIn(' from desktop',self.text(b,self.ids[0]))
        self.assertEqual(engine(b,'import',objects=incoming)['objects_merged'],0)
        self.edit(a,self.ids[1],lambda m:m.pop('text'),client_id=81234)
        engine(b,'import',objects=wire(self.record(a)))
        self.assertNotIn('text',ds.decoded(self.record(b)[self.ids[1]]['chunks']).get('data',type=ds.Map))

    def test_native_rejects_colliding_identity_and_bad_hash(self):
        a,b = self.configs
        self.edit(a,self.ids[0],lambda m:m['text'].insert(8,' A'),client_id=12345)
        self.edit(b,self.ids[0],lambda m:m['text'].insert(8,' B'),client_id=12345)
        incoming = wire(self.record(a))
        with self.assertRaisesRegex(RuntimeError,'editing identity'):
            engine(b,'import',objects=incoming)
        incoming[self.ids[0]]['hash'] = '0'*64
        with self.assertRaisesRegex(RuntimeError,'checksum'):
            engine(b,'import',objects=incoming)
        self.assertEqual(self.text(b,self.ids[0]),'Original B')

    def test_native_unique_identity_and_locked_store(self):
        a,_ = self.configs
        identity = engine(a,'identity')
        self.assertNotEqual(identity['editor_client_id'],123)
        a.update(identity)
        self.assertEqual(engine(a,'identity'),identity)
        db = ds.open_db(a,readonly=False)
        try:
            check = subprocess.run([sys.executable,__file__,'--lock-probe',str(a['_config'])],capture_output=True)
            self.assertEqual(check.returncode,0)
        finally:
            db.close()

    def test_native_new_object_roundtrip(self):
        a,b = self.configs
        doc = ds.Doc({'data':ds.Map({'text':ds.Text('A new mobile collaboration')})})
        oid = str(uuid.uuid4())
        record = {'chunks':[doc.get_update()],'hash':ds.doc_fingerprint(doc),'opaque':False,'extra':None}
        engine(b,'import',objects=wire({oid:record}))
        exported = engine(b,'export',ids=[oid])['objects']
        self.assertEqual(exported,wire({oid:record}))
        self.assertEqual(self.text(b,oid),'A new mobile collaboration')

class NativeFilter(unittest.TestCase):
    def test_editor_filter_denies_ipv4_ipv6_and_keeps_files_working(self):
        pid = os.fork()
        if pid == 0:
            try:
                fn = lib.Java_io_appflowy_appflowy_MainActivity_enforceOffline
                fn.restype=ctypes.c_uint8
                assert fn(None,None)==1
                for family in (socket.AF_INET,socket.AF_INET6):
                    try: socket.socket(family,socket.SOCK_STREAM)
                    except OSError as e: assert e.errno==errno.EPERM
                    else: raise AssertionError('Internet socket opened')
                target = ROOT/'Tests/offline-file-test.txt'
                target.write_text('Filesystem remains available')
                assert target.read_text()=='Filesystem remains available'
                target.unlink()
                os._exit(0)
            except BaseException:
                os._exit(1)
        self.assertEqual(os.waitpid(pid,0)[1],0)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--lock-probe':
        try:
            engine(ds.load_config(sys.argv[2]),'inspect')
        except RuntimeError as error:
            sys.exit(0 if 'Close' in str(error) else 2)
        sys.exit(1)
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (PortableTests,NativeFilter))
    sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
