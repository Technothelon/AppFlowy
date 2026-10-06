# SPDX-License-Identifier: AGPL-3.0-only
"""Public test vectors for Python/Dart encryption interoperability."""
import io
import json
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

root = Path(__file__).resolve().parent
key = bytes(range(32))
vault = '22222222-2222-4222-8222-222222222222'
content = b'Encrypted interoperability test, including a private-looking page name.'
nonce = bytes(range(12))
magic = b'AFDRIVE1\n'
(root / 'python.afsync').write_bytes(magic + nonce + AESGCM(key).encrypt(nonce, content, magic + vault.encode()))
(root / 'vectors.json').write_text(json.dumps({'key':list(key),'vault_id':vault,'content':content.decode(),'password':'test-pair-code'}))
metadata = {'protocol':1,'vault_id':vault,'workspace_id':'11111111-1111-4111-8111-111111111111',
            'uid':'123','device_id':'33333333-3333-4333-8333-333333333333','key':__import__('base64').b64encode(key).decode(),
            'media':{'Assets':'Media/Assets'},'aliases':{'/desktop/Assets':'Assets'},'canonical_assets':'/desktop/Assets'}
buffer = io.BytesIO()
with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('pairing.json',json.dumps(metadata))
    archive.writestr('Data/AppFlowy/data_beta.appflowy.cloud/123/collab_db/CURRENT','test-only')
salt = bytes(range(16)); nonce = bytes(range(12)); magic = b'AFPAIR1\n'
derived = PBKDF2HMAC(algorithm=hashes.SHA256(),length=32,salt=salt,iterations=310000).derive(b'test-pair-code')
(root / 'python.afpair').write_bytes(magic+salt+nonce+AESGCM(derived).encrypt(nonce,buffer.getvalue(),magic))
print('Prepared public interoperability vectors')
