"""Optional private S3 archive retention for ephemeral app containers."""
import hashlib,json,os,re
from pathlib import Path
from urllib.parse import urlsplit


def _client():
    names=['BUCKET','ENDPOINT','ACCESS_KEY_ID','SECRET_ACCESS_KEY','REGION']
    values={key:os.getenv('BACKUP_S3_'+key,'') for key in names}
    if not any(values.values()): return None,None
    if not all(values.values()): raise RuntimeError('Complete the private backup storage configuration')
    url=urlsplit(values['ENDPOINT'])
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise RuntimeError('Backup storage requires a private credential and HTTPS endpoint')
    import boto3
    from botocore.config import Config
    client=boto3.client('s3',endpoint_url=values['ENDPOINT'],region_name=values['REGION'],
        aws_access_key_id=values['ACCESS_KEY_ID'],aws_secret_access_key=values['SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4',s3={'addressing_style':'path'},
            request_checksum_calculation='when_required',response_checksum_validation='when_required',
            connect_timeout=10,read_timeout=60,retries={'max_attempts':2}))
    return client,values['BUCKET']


def store_archive(archive,manifest=None):
    client,bucket=_client()
    if client is None:return None
    archive=Path(archive)
    if not re.fullmatch(r'business-[a-zA-Z0-9.-]+[.]dump',archive.name):raise RuntimeError('Invalid archive filename')
    if manifest is None:
        manifest=archive.with_suffix('.json')
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        fd=os.open(manifest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'sha256':digest,'archive':archive.name},f)
    try:
        for path in (archive,Path(manifest)):
            with path.open('rb') as body:
                client.put_object(Bucket=bucket,Key='archives/'+path.name,Body=body,ContentLength=path.stat().st_size)
            if client.head_object(Bucket=bucket,Key='archives/'+path.name)['ContentLength']!=path.stat().st_size:
                raise RuntimeError('Backup upload size did not match')
    except Exception:
        raise RuntimeError('Durable backup upload failed; no schema upgrade was attempted') from None
    return {'bucket':bucket,'key':'archives/'+archive.name}


def download_archive(filename,directory):
    if not re.fullmatch(r'business-[a-zA-Z0-9.-]+[.]dump',filename):raise RuntimeError('Use an archive basename')
    client,bucket=_client()
    if client is None:raise RuntimeError('Configure private backup storage first')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    paths=[]
    try:
        for name in (filename,filename[:-5]+'.json'):
            target=directory/name
            fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            paths.append(target)
            obj=client.get_object(Bucket=bucket,Key='archives/'+name)
            with os.fdopen(fd,'wb') as out:
                for chunk in obj['Body'].iter_chunks(chunk_size=1048576):out.write(chunk)
        expected=json.loads(paths[1].read_text())['sha256']
        if hashlib.sha256(paths[0].read_bytes()).hexdigest()!=expected:raise RuntimeError('checksum mismatch')
    except Exception:
        for path in paths:path.unlink(missing_ok=True)
        raise RuntimeError('Archive download or checksum verification failed') from None
    return paths[0]
