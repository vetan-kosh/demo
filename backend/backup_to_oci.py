"""Off-VM backup for VetanKosh. Schedule daily with systemd timer/cron.
Uploads a DB backup plus private archive tarball to OCI Object Storage.
"""
import os, subprocess, tempfile, tarfile, shutil
from datetime import datetime, timezone
from urllib.parse import urlparse
import oci

def client_and_target():
    cfg=oci.config.from_file(os.getenv('OCI_CONFIG_FILE',oci.config.DEFAULT_LOCATION),os.getenv('OCI_CONFIG_PROFILE','DEFAULT'))
    c=oci.object_storage.ObjectStorageClient(cfg)
    ns=os.getenv('OCI_NAMESPACE') or c.get_namespace().data
    bucket=os.environ['OCI_BACKUP_BUCKET']
    return c,ns,bucket

def db_backup(dst):
    url=os.getenv('DATABASE_URL','sqlite:///./form16_database.db')
    if url.startswith('sqlite'):
        src=url.split('///',1)[-1]; shutil.copy2(src,dst); return
    clean=url.replace('postgresql+psycopg://','postgresql://',1)
    subprocess.run(['pg_dump','--no-owner','--no-privileges','--format=custom','--file',dst,clean],check=True,timeout=600)

def upload(c,ns,bucket,key,path):
    with open(path,'rb') as f: c.put_object(ns,bucket,key,f)

def main():
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); prefix=os.getenv('OCI_BACKUP_PREFIX','vetankosh')
    c,ns,bucket=client_and_target()
    with tempfile.TemporaryDirectory() as td:
        dbfile=os.path.join(td,'database.backup'); db_backup(dbfile); upload(c,ns,bucket,f'{prefix}/db/{stamp}.backup',dbfile)
        root=os.path.abspath(os.getenv('FORM16_ARCHIVE_DIR','./private_form16_archive'))
        if os.path.isdir(root):
            tar=os.path.join(td,'archive.tar.gz')
            with tarfile.open(tar,'w:gz') as tf: tf.add(root,arcname='private_form16_archive')
            upload(c,ns,bucket,f'{prefix}/archive/{stamp}.tar.gz',tar)
    print('OCI backup completed:',stamp)
if __name__=='__main__': main()
