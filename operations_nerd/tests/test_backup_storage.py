"""Durable storage failures must stop backup completion without hiding local archives."""
import hashlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from umbrella import backup_storage as storage

class Body(io.BytesIO):
    def iter_chunks(self,chunk_size):
        while data:=self.read(chunk_size):yield data

class Bucket:
    def __init__(self):self.objects={}
    def put_object(self,**kw):self.objects[kw['Key']]=kw['Body'].read()
    def head_object(self,**kw):return {'ContentLength':len(self.objects[kw['Key']])}
    def get_object(self,**kw):return {'Body':Body(self.objects[kw['Key']])}

class StorageTests(unittest.TestCase):
    def test_archive_round_trip_and_private_files(self):
        client=Bucket()
        with tempfile.TemporaryDirectory() as folder,patch.object(storage,'_client',return_value=(client,'private-test')):
            archive=Path(folder)/'business-1-test.dump';archive.write_bytes(b'fictional archive')
            result=storage.store_archive(archive)
            self.assertEqual(result['bucket'],'private-test')
            copy=storage.download_archive(archive.name,Path(folder)/'download')
            self.assertEqual(copy.read_bytes(),archive.read_bytes())
            self.assertEqual(copy.stat().st_mode&0o777,0o600)
            client.objects['archives/'+archive.name]=b'corrupted'
            with self.assertRaisesRegex(RuntimeError,'checksum'):
                storage.download_archive(archive.name,Path(folder)/'corrupted')
            self.assertFalse((Path(folder)/'corrupted'/archive.name).exists())

    def test_upload_failure_is_fatal_and_retains_local_archive(self):
        client=Bucket()
        with tempfile.TemporaryDirectory() as folder,patch.object(storage,'_client',return_value=(client,'private-test')):
            archive=Path(folder)/'business-1-test.dump';archive.write_bytes(b'fictional archive')
            with patch.object(client,'put_object',side_effect=RuntimeError('secret must not appear')):
                with self.assertRaisesRegex(RuntimeError,'Durable backup upload failed') as caught:storage.store_archive(archive)
                self.assertNotIn('secret',str(caught.exception))
            self.assertTrue(archive.exists())

    def test_disabled_partial_config_and_invalid_key(self):
        with patch.dict(os.environ,{k:'' for k in ['BACKUP_S3_BUCKET','BACKUP_S3_ENDPOINT','BACKUP_S3_ACCESS_KEY_ID','BACKUP_S3_SECRET_ACCESS_KEY','BACKUP_S3_REGION']}):
            self.assertEqual(storage._client(),(None,None))
            with patch.dict(os.environ,{'BACKUP_S3_BUCKET':'configured'}):
                with self.assertRaises(RuntimeError):storage._client()
        with self.assertRaises(RuntimeError):storage.download_archive('../outside.dump','/tmp')

if __name__=='__main__':unittest.main()
