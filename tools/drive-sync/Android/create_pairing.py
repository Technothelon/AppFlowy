# SPDX-License-Identifier: AGPL-3.0-only
"""Create an encrypted bootstrap for the separate Android experiment.

Transfer this bundle directly to the phone; never upload it to Drive.
"""
import base64
import configparser
import io
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'Sync'))
import drive_sync as desktop

def reveal(value):
    if value.startswith('GOCSPX-'):
        return value
    # Public obfuscation key: rclone v1.73.2 fs/config/obscure/obscure.go.
    key = bytes.fromhex('9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638')
    data = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    return Cipher(algorithms.AES(key), modes.CTR(data[:16])).decryptor().update(data[16:]).decode()

def main():
    os.umask(0o077)
    config = desktop.load_config(desktop.EXPERIMENT / 'Sync/config.json')
    private = desktop.EXPERIMENT / 'Android/Pairing'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    with desktop.closed_profile(config):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config['rclone_config'])
        remote = parser[config['remote'].partition(':')[0]]
        token = json.loads(remote['token'])
        drive = {'client_id':remote['client_id'], 'client_secret':reveal(remote['client_secret']),
                 'refresh_token':token['refresh_token']}
        request = urllib.request.Request('https://oauth2.googleapis.com/token',
            data=urllib.parse.urlencode(dict(drive, grant_type='refresh_token')).encode())
        with urllib.request.urlopen(request, timeout=30) as response:
            access = json.load(response)['access_token']
        parent = config['remote'].rsplit('/', 1)[0]
        listing = subprocess.run(['/usr/bin/rclone','lsjson',parent,'--dirs-only',
            '--config',config['rclone_config']], check=True, capture_output=True, text=True)
        matches = [x for x in json.loads(listing.stdout) if x['Name'] == config['vault_id']]
        if len(matches) != 1:
            raise RuntimeError('The dedicated experimental Drive folder is not unique.')
        children = subprocess.run(['/usr/bin/rclone','lsjson',config['remote'],'--dirs-only',
            '--config',config['rclone_config']], check=True, capture_output=True, text=True)
        packs = [x for x in json.loads(children.stdout) if x['Name'] == 'packs']
        if len(packs) != 1:
            raise RuntimeError('The encrypted records subfolder is not unique.')
        drive['folder_id'] = packs[0]['ID']
        query = urllib.parse.urlencode({'q':f"'{drive['folder_id']}' in parents and trashed=false",'fields':'files(id)','pageSize':1})
        request = urllib.request.Request('https://www.googleapis.com/drive/v3/files?'+query,
            headers={'Authorization':'Bearer '+access})
        with urllib.request.urlopen(request, timeout=30) as response:
            json.load(response)
        db = desktop.open_db(config)
        try:
            objects = desktop.object_records(config, db)
        finally:
            db.close()
        media = desktop.media_inventory(config)
        metadata = {key:config[key] for key in ('protocol','vault_id','workspace_id','uid')}
        metadata.update(key=base64.b64encode(Path(config['key_file']).read_bytes()).decode(),
            media=config['media'], drive=drive,
            aliases={str(config['_profile']/relative):group for group,relative in config['media'].items()},
            canonical_assets=str(config['_profile']/config['media']['Assets']),
            initial_sync_state={'protocol':1,'vault_id':config['vault_id'],'seen':[],
                'published_objects':{k:v['hash'] for k,v in objects.items()},
                'published_media':{k:v['hash'] for k,v in media.items()}})
        # Migrated documents retain these original desktop paths. The Linux
        # sandbox redirects them; Android resolves them inside its private vault.
        for original_path, group in config.get('media_aliases', {}).items():
            if group not in config['media']:
                raise ValueError('Media alias refers to an unknown group.')
            metadata['aliases'][original_path] = group
        buffer = io.BytesIO()
        store = config['_profile'] / 'Data/AppFlowy/data_beta.appflowy.cloud'
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr('pairing.json',json.dumps(metadata, separators=(',',':')))
            for file in sorted(store.rglob('*')):
                if file.is_symlink():
                    raise RuntimeError('Unexpected symlink in the experimental store.')
                if not file.is_file() or file.name.startswith('log') or file.name == 'LOCK':
                    continue
                if 'runtime' in file.relative_to(store).parts:
                    continue
                archive.write(file, 'Data/AppFlowy/data_beta.appflowy.cloud/'+file.relative_to(store).as_posix())
        code = secrets.token_hex(16)
        salt, nonce = os.urandom(16), os.urandom(12)
        key = PBKDF2HMAC(algorithm=hashes.SHA256(),length=32,salt=salt,iterations=310000).derive(code.encode())
        magic = b'AFPAIR1\n'
        sealed = magic+salt+nonce+AESGCM(key).encrypt(nonce,buffer.getvalue(),magic)
        if len(sealed) > 256*1024*1024:
            raise RuntimeError('Bootstrap exceeds the Android size limit.')
        (private/'AppFlowy-Drive-Experiment.afpair').write_bytes(sealed)
        (private/'pairing-code.txt').write_text(code+'\n')
        desktop.atomic_json(private/'verification.json',{'encrypted':True,'oauth_refresh_verified':True,
            'dedicated_drive_folder_verified':True,'objects':len(objects),'media':len(media),
            'bytes':len(sealed),'uploaded_to_drive':False})
        print('Private encrypted Android pairing bundle created; credentials and pairing code were not printed.')

if __name__ == '__main__':
    main()
