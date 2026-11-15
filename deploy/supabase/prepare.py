"""Generate NEW private deployment settings. Never reads existing app secrets."""
import argparse,base64,hashlib,hmac,json,os,secrets,time
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if a.output.exists(): raise SystemExit('Refusing to replace existing credentials.')
config=json.loads((Path(__file__).parent/'deployment.json').read_text())
values={k:secrets.token_hex(32) for k in ['POSTGRES_PASSWORD','JWT_SECRET','APP_DB_PASSWORD','DASHBOARD_PASSWORD','SESSION_SECRET','SIGNUP_INVITE_CODE','HOST_OWNER_PASSWORD','HOST_SESSION_SECRET','LOGISTICS_API_TOKEN','PG_META_CRYPTO_KEY','S3_PROTOCOL_ACCESS_KEY_SECRET']}
values.update(SECRET_KEY_BASE=secrets.token_hex(64),REALTIME_DB_ENC_KEY=secrets.token_hex(8),VAULT_ENC_KEY=secrets.token_hex(16),S3_PROTOCOL_ACCESS_KEY_ID=secrets.token_hex(16),DASHBOARD_USERNAME='supabase',LOGISTICS_BUSINESS_KEY=secrets.token_hex(16),LOGISTICS_ACTOR_REF='hosted-owner')
b64=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
for role,key in [('anon','ANON_KEY'),('service_role','SERVICE_ROLE_KEY')]:
 data=b64(json.dumps({'alg':'HS256','typ':'JWT'},separators=(',',':')).encode())+'.'+b64(json.dumps({'role':role,'iss':'supabase','iat':int(time.time()),'exp':int(time.time())+10*365*86400},separators=(',',':')).encode())
 values[key]=data+'.'+b64(hmac.new(values['JWT_SECRET'].encode(),data.encode(),hashlib.sha256).digest())
port=58080 if config['kind']=='integrated' else 58180
gateway=58000 if config['kind']=='integrated' else 58100
values.update(APP_PORT=str(port),SUPABASE_GATEWAY_PORT=str(gateway),APP_ORIGIN=f'http://127.0.0.1:{port}',SUPABASE_PUBLIC_URL=f'http://127.0.0.1:{gateway}')
a.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
fd=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: f.write(''.join(k+'='+v+'\n' for k,v in sorted(values.items())))
print('New private settings created. No credential values displayed. Public origins must match the target environment.')
