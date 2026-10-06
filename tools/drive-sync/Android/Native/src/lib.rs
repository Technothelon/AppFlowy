// SPDX-License-Identifier: AGPL-3.0-only
//! Closed-store adapter for the desktop AFDRIVE1 collaboration records.
//! AppFlowy and this adapter must use separate processes on Android.
use anyhow::{anyhow, bail, ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use rocksdb::{BlockBasedOptions, DBCompressionType, Direction, IteratorMode, Options, WriteBatch, WriteOptions, DB};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::{BTreeMap, BTreeSet}, ffi::{CStr, CString}, fs, os::raw::{c_char, c_void}, path::{Path, PathBuf}};
use uuid::Uuid;
use yrs::{Array, Doc, Map, Out, ReadTxn, Text, Transact, Update, types::{ToJson, text::YChange}, updates::{decoder::Decode, encoder::Encode}};

#[derive(Deserialize)]
struct Config {
    profile: PathBuf,
    uid: String,
    workspace_id: Uuid,
    device_id: Uuid,
    editor_client_id: Option<u64>,
    editor_device_id: Option<Uuid>,
}

#[derive(Clone, Serialize, Deserialize)]
struct Record {
    hash: String,
    opaque: bool,
    chunks: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    extra: Option<(String, String)>,
}

fn sha(bytes: &[u8]) -> String { hex::encode(Sha256::digest(bytes)) }
fn ws(c: &Config) -> String { c.workspace_id.to_string() }
fn mapping_prefix(c: &Config) -> Result<Vec<u8>> {
    let mut p = vec![1, 0];
    p.extend(c.uid.parse::<i64>()?.to_be_bytes());
    p.extend(ws(c).as_bytes());
    Ok(p)
}
fn mapping(c: &Config, oid: Uuid) -> Result<Vec<u8>> {
    let mut p = mapping_prefix(c)?; p.extend(oid.to_string().as_bytes()); p.push(0); Ok(p)
}
fn doc_prefix(did: &[u8]) -> Result<Vec<u8>> {
    ensure!(did.len() == 8, "Unknown document identity schema");
    let mut p = vec![1, 1]; p.extend(did); Ok(p)
}
fn db_path(c: &Config) -> Result<PathBuf> {
    ensure!(c.profile.is_absolute() && !c.profile.components().any(|x| matches!(x, std::path::Component::ParentDir)), "Invalid profile path");
    let p = c.profile.join("Data/AppFlowy/data_beta.appflowy.cloud").join(&c.uid).join("collab_db");
    ensure!(p.join("CURRENT").is_file(), "Pair this device before synchronizing");
    Ok(p)
}
fn open(c: &Config) -> Result<DB> {
    let mut o = Options::default(); o.create_if_missing(false);
    o.set_compression_type(DBCompressionType::Zstd);
    o.set_bottommost_compression_type(DBCompressionType::Zstd);
    let mut b = BlockBasedOptions::default(); b.set_format_version(5);
    o.set_block_based_table_factory(&b);
    // A read-write open also proves the editor has released RocksDB's lock.
    let db = DB::open(&o, db_path(c)?).context("Close the experimental editor before syncing")?;
    db.set_options(&[("compression", "kZSTD"), ("bottommost_compression", "kZSTD")])?;
    Ok(db)
}
fn varint(data: &[u8], offset: &mut usize) -> Result<u64> {
    let mut n = 0u64;
    for shift in (0..70).step_by(7) {
        ensure!(*offset < data.len(), "Truncated collaboration integer");
        let b = data[*offset]; *offset += 1;
        ensure!(shift < 63 || b < 2, "Invalid collaboration integer");
        n |= u64::from(b & 127) << shift;
        if b < 128 { return Ok(n); }
    }
    bail!("Invalid collaboration integer")
}
fn decode(chunks: &[Vec<u8>]) -> Result<Doc> {
    let doc = Doc::new();
    { let mut txn = doc.transact_mut();
      for chunk in chunks { txn.apply_update(Update::decode_v1(chunk)?)?; }
    }
    Ok(doc)
}
fn fingerprint(doc: &Doc) -> Result<String> {
    let txn = doc.transact(); let sv = txn.state_vector(); let data = sv.encode_v1();
    let mut offset = 0; let count = varint(&data, &mut offset)?;
    let mut vectors = Vec::new();
    for _ in 0..count { vectors.push((varint(&data, &mut offset)?, varint(&data, &mut offset)?)); }
    ensure!(offset == data.len(), "Unknown state-vector encoding");
    let deleted = txn.encode_state_as_update_v1(&sv); offset = 0;
    ensure!(varint(&deleted, &mut offset)? == 0, "Unexpected deletion-only structures");
    let count = varint(&deleted, &mut offset)?; let mut ranges = Vec::new();
    for _ in 0..count {
        let client = varint(&deleted, &mut offset)?; let count = varint(&deleted, &mut offset)?;
        for _ in 0..count { ranges.push((client, varint(&deleted, &mut offset)?, varint(&deleted, &mut offset)?)); }
    }
    ensure!(offset == deleted.len(), "Unknown deletion-set encoding");
    vectors.sort_unstable(); ranges.sort_unstable();
    Ok(sha(&serde_json::to_vec(&(vectors, ranges))?))
}
fn unpack(r: &Record) -> Result<Vec<Vec<u8>>> {
    // Old records may repeat an entire history on each import. Bound the
    // unique updates after removing exact duplicates; preserve opaque bytes.
    ensure!(!r.chunks.is_empty() && r.chunks.len() < 1000000, "Invalid update count");
    let mut seen = BTreeSet::new();
    let mut result = Vec::new();
    for chunk in &r.chunks {
        if r.opaque || seen.insert(chunk) { result.push(B64.decode(chunk)?); }
    }
    ensure!(result.len() < 100000, "Too many unique collaboration updates");
    Ok(result)
}
fn digest(chunks: &[Vec<u8>]) -> Result<(String, bool)> {
    match decode(chunks).and_then(|d| fingerprint(&d)) {
        Ok(h) => Ok((h, false)),
        Err(_) => {
            let mut b = Vec::new();
            for c in chunks { b.extend(u32::try_from(c.len())?.to_be_bytes()); b.extend(c); }
            Ok((sha(&b), true))
        }
    }
}
fn chunks(db: &DB, did: &[u8]) -> Result<Vec<Vec<u8>>> {
    let mut p = doc_prefix(did)?; p.push(0);
    let mut result = vec![db.get(&p)?.ok_or_else(|| anyhow!("Document state is missing"))?];
    *p.last_mut().unwrap() = 2;
    for row in db.iterator(IteratorMode::From(&p, Direction::Forward)) {
        let (k, v) = row?; if !k.starts_with(&p) { break; }
        ensure!(k.len() == 16, "Unknown update-log schema"); result.push(v.to_vec());
    }
    Ok(result)
}
fn records(c: &Config, db: &DB) -> Result<BTreeMap<String, Record>> {
    let p = mapping_prefix(c)?; let mut result = BTreeMap::new();
    for row in db.iterator(IteratorMode::From(&p, Direction::Forward)) {
        let (k, did) = row?; if !k.starts_with(&p) { break; }
        ensure!(k.len() == 83 && k[82] == 0, "Unknown collaboration mapping");
        let oid = std::str::from_utf8(&k[p.len()..82])?; let id = Uuid::parse_str(oid)?;
        let original = chunks(db, &did)?;
        let mut seen = BTreeSet::new();
        let unique: Vec<Vec<u8>> = original.iter().filter(|v| seen.insert(v.as_slice())).cloned().collect();
        let (mut hash, opaque) = digest(&unique)?;
        let raw = if opaque { hash = digest(&original)?.0; original } else { unique };
        let mut extra_key = vec![3, 3]; extra_key.extend(c.workspace_id.as_bytes()); extra_key.extend(id.as_bytes());
        let extra = db.get(&extra_key)?.map(|v| (hex::encode(extra_key), B64.encode(v)));
        result.insert(oid.to_owned(), Record { hash, opaque, chunks: raw.iter().map(|v| B64.encode(v)).collect(), extra });
    }
    ensure!(result.contains_key(&ws(c)), "The paired workspace is missing"); Ok(result)
}
fn backup(c: &Config, db: &DB) -> Result<PathBuf> {
    let target = c.profile.join("SyncBackups").join(Uuid::new_v4().simple().to_string()).join("rocksdb-backup");
    fs::create_dir_all(&target)?;
    // Android's app filesystem may reject hard links. BackupEngine copies a
    // stable snapshot while retaining the live database's consistency rules.
    let options = rocksdb::backup::BackupEngineOptions::new(&target)?;
    let env = rocksdb::Env::new()?;
    let mut engine = rocksdb::backup::BackupEngine::open(&options, &env)?;
    engine.create_new_backup_flush(db, true)?;
    let info = engine.get_backup_info();
    ensure!(info.len() == 1, "The pre-sync backup is missing");
    engine.verify_backup(info[0].backup_id)?;
    Ok(target)
}
fn write(db: &DB, b: WriteBatch) -> Result<()> {
    let mut o = WriteOptions::default(); o.set_sync(true); db.write_opt(b, &o)?; db.flush()?; Ok(())
}
fn semantic(raw: &[Vec<u8>]) -> Result<Value> {
    fn value<T: ReadTxn>(out: &Out, txn: &T) -> Result<Value> {
        match out {
            Out::YMap(map) => {
                let mut values = BTreeMap::new();
                for (k,v) in map.iter(txn) { values.insert(k.to_owned(), value(&v, txn)?); }
                Ok(serde_json::to_value(values)?)
            },
            Out::YArray(array) => Ok(Value::Array(array.iter(txn).map(|v| value(&v,txn)).collect::<Result<Vec<_>>>()?)),
            Out::YText(text) => {
                let mut delta = Vec::new();
                for d in text.diff(txn, YChange::identity) {
                    let mut attributes = BTreeMap::new();
                    if let Some(a) = d.attributes {
                        for (k,v) in a.iter() {
                            let mut encoded = String::new(); v.to_json(&mut encoded);
                            attributes.insert(k.to_string(), serde_json::from_str::<Value>(&encoded)?);
                        }
                    }
                    delta.push(json!({"insert":value(&d.insert,txn)?,"attributes":attributes}));
                }
                Ok(json!({"text_delta":delta}))
            },
            _ => { let mut text = String::new(); out.to_json(txn).to_json(&mut text); Ok(serde_json::from_str(&text)?) }
        }
    }
    let doc = decode(raw)?;
    let names: Vec<String> = doc.transact().root_refs().map(|(name,_)|name.to_owned()).collect();
    let mut roots = BTreeMap::new();
    // AppFlowy's collaboration roots are maps, as in the matching desktop
    // decoder. Update bytes alone do not declare the root's shared type.
    for name in names { let map = doc.get_or_insert_map(name.as_str()); roots.insert(name, value(&Out::YMap(map), &doc.transact())?); }
    Ok(serde_json::to_value(roots)?)
}
fn identity(c: &Config, db: &DB, current: &BTreeMap<String, Record>) -> Result<Value> {
    let mut key = vec![3, 1]; key.extend(c.workspace_id.as_bytes()); key.push(0);
    let stored = db.get(&key)?.ok_or_else(|| anyhow!("Unknown workspace editing-identity schema"))?;
    let old = u64::from_le_bytes(stored.as_slice().try_into()?);
    if c.editor_device_id == Some(c.device_id) {
        ensure!(c.editor_client_id == Some(old), "Local editor identity changed");
        return Ok(json!({"editor_client_id":old,"editor_device_id":c.device_id}));
    }
    let mut used = BTreeSet::new();
    for r in current.values().filter(|r| !r.opaque) {
        let doc = decode(&unpack(r)?)?; let data = doc.transact().state_vector().encode_v1();
        let mut offset = 0; let count = varint(&data, &mut offset)?;
        for _ in 0..count { used.insert(varint(&data, &mut offset)?); varint(&data, &mut offset)?; }
    }
    let mut id = u64::from(u32::from_le_bytes(Uuid::new_v4().as_bytes()[0..4].try_into()?));
    while id == 0 || used.contains(&id) { id = u64::from(u32::from_le_bytes(Uuid::new_v4().as_bytes()[0..4].try_into()?)); }
    backup(c, db)?; let mut batch = WriteBatch::default(); batch.put(key, id.to_le_bytes()); write(db, batch)?;
    Ok(json!({"editor_client_id":id,"editor_device_id":c.device_id}))
}
fn import(c: &Config, db: &DB, current: &BTreeMap<String, Record>, objects: BTreeMap<String, Record>) -> Result<Value> {
    let mut planned = Vec::new();
    for (oid, r) in objects {
        let id = Uuid::parse_str(&oid)?; let incoming = unpack(&r)?; let (h, opaque) = digest(&incoming)?;
        ensure!(h == r.hash && opaque == r.opaque, "Collaboration checksum mismatch");
        if let Some((k, v)) = &r.extra {
            let mut expected = vec![3, 3]; expected.extend(c.workspace_id.as_bytes()); expected.extend(id.as_bytes());
            ensure!(hex::decode(k)? == expected, "Invalid object metadata key"); B64.decode(v)?;
        }
        if let Some(local) = current.get(&oid) {
            if local.hash == h {
                if !local.opaque && !opaque {
                    ensure!(semantic(&unpack(local)?)? == semantic(&incoming)?, "The copies share an editing identity and have diverged");
                }
                continue;
            }
            ensure!(!local.opaque && !opaque, "An opaque collaboration record needs manual review");
            let doc = decode(&unpack(local)?)?; let before = fingerprint(&doc)?;
            { let mut t = doc.transact_mut(); for b in &incoming { t.apply_update(Update::decode_v1(b)?)?; } }
            if fingerprint(&doc)? == before { continue; }
        }
        planned.push((id, r, incoming));
    }
    if planned.is_empty() { return Ok(json!({"objects_merged":0})); }
    backup(c, db)?; let mut batch = WriteBatch::default();
    let mut used = BTreeSet::new();
    for row in db.iterator(IteratorMode::Start) { let (k,v) = row?; if k.starts_with(&[1,0]) { used.insert(v.to_vec()); } }
    for (oid, r, raw) in &planned {
        let key = mapping(c, *oid)?; let (did, new) = match db.get(&key)? { Some(d) => (d, false), None => {
            let mut d = Uuid::new_v4().as_bytes()[..8].to_vec(); while used.contains(&d) { d = Uuid::new_v4().as_bytes()[..8].to_vec(); }
            used.insert(d.clone()); batch.put(key, &d); (d, true)
        }};
        let p = doc_prefix(&did)?; let mut clock = 0u32; let incoming;
        let mut known: BTreeSet<Vec<u8>> = if new { BTreeSet::new() }
            else { unpack(current.get(&oid.to_string()).context("Local collaboration missing")?)?.into_iter().collect() };
        if new {
            batch.put([p.as_slice(), &[0]].concat(), &raw[0]); incoming = &raw[1..];
            if !r.opaque { batch.put([p.as_slice(), &[1]].concat(), decode(raw)?.transact().state_vector().encode_v1()); }
            if let Some((k,v)) = &r.extra { batch.put(hex::decode(k)?, B64.decode(v)?); }
        } else {
            let update_prefix = [p.as_slice(), &[2]].concat(); incoming = raw.as_slice();
            for row in db.iterator(IteratorMode::From(&update_prefix, Direction::Forward)) {
                let (k, _) = row?; if !k.starts_with(&update_prefix) { break; }
                ensure!(k.len()==16, "Unknown update-clock schema");
                clock = clock.max(u32::from_be_bytes(k[11..15].try_into()?).checked_add(1).context("Update clock exhausted")?);
            }
        }
        for b in incoming {
            if !r.opaque && !known.insert(b.clone()) { continue; }
            ensure!(clock < u32::MAX, "Update clock exhausted");
            batch.put([p.as_slice(), &[2], &clock.to_be_bytes(), &[0]].concat(), b); clock += 1;
        }
    }
    write(db, batch)?; Ok(json!({"objects_merged":planned.len()}))
}
fn command(input: &str) -> Result<Value> {
    let request: Value = serde_json::from_str(input)?; let c: Config = serde_json::from_value(request["config"].clone())?;
    let db = open(&c)?; let all = records(&c, &db)?;
    match request["action"].as_str().unwrap_or("") {
        "inspect" => Ok(json!({"objects":all.iter().map(|(k,r)| (k.clone(),json!({"hash":r.hash,"opaque":r.opaque}))).collect::<BTreeMap<_,_>>() })),
        "identity" => identity(&c, &db, &all),
        "export" => {
            let names: Vec<String> = serde_json::from_value(request["ids"].clone())?; let mut selected = BTreeMap::new();
            for id in names { selected.insert(id.clone(), all.get(&id).context("Unknown object for export")?.clone()); }
            Ok(json!({"objects":selected}))
        },
        "import" => import(&c, &db, &all, serde_json::from_value(request["objects"].clone())?),
        _ => bail!("Unknown adapter action"),
    }
}

#[no_mangle]
pub extern "C" fn afdrive_json(input: *const c_char) -> *mut c_char {
    let result = std::panic::catch_unwind(|| {
        if input.is_null() { return Err(anyhow!("Missing adapter request")); }
        command(unsafe { CStr::from_ptr(input) }.to_str()?)
    });
    let value = match result { Ok(Ok(value)) => json!({"ok":true,"result":value}), Ok(Err(e)) => json!({"ok":false,"error":e.to_string()}), Err(_) => json!({"ok":false,"error":"The collaboration adapter stopped safely"}) };
    CString::new(value.to_string()).unwrap().into_raw()
}
#[no_mangle]
pub extern "C" fn afdrive_free(value: *mut c_char) { if !value.is_null() { drop(unsafe { CString::from_raw(value) }); } }

#[cfg(any(target_os = "android", target_os = "linux"))]
fn offline() -> bool {
    #[repr(C)] struct Filter { code:u16, jt:u8, jf:u8, k:u32 }
    #[repr(C)] struct Program { len:u16, filter:*const Filter }
    // Refuse internet sockets and connects in every editor thread. The sync
    // activity lives in another Android process and never installs this filter.
    let f = [
        Filter{code:0x20,jt:0,jf:0,k:0},
        Filter{code:0x15,jt:0,jf:1,k:libc::SYS_connect as u32},
        Filter{code:0x06,jt:0,jf:0,k:0x00050000 | libc::EPERM as u32},
        Filter{code:0x15,jt:0,jf:4,k:libc::SYS_socket as u32},
        Filter{code:0x20,jt:0,jf:0,k:16},
        Filter{code:0x15,jt:1,jf:0,k:libc::AF_INET as u32},
        Filter{code:0x15,jt:0,jf:1,k:libc::AF_INET6 as u32},
        Filter{code:0x06,jt:0,jf:0,k:0x00050000 | libc::EPERM as u32},
        Filter{code:0x06,jt:0,jf:0,k:0x7fff0000},
    ];
    let p = Program {len:f.len() as u16,filter:f.as_ptr()};
    unsafe { libc::prctl(38,1,0,0,0)==0 && libc::syscall(libc::SYS_seccomp,1,1,&p)==0 }
}
#[no_mangle]
pub extern "system" fn Java_io_appflowy_appflowy_MainActivity_enforceOffline(_env:*mut c_void,_class:*mut c_void) -> u8 {
    #[cfg(any(target_os="android",target_os="linux"))] { return u8::from(offline()); }
    #[allow(unreachable_code)] 0
}
