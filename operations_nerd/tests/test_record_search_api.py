"""Owner-facing search uses current visible spec and real SQLite via HTTP."""
import os, sys, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
_temp = tempfile.TemporaryDirectory()
os.environ['DATABASE_PATH'] = os.path.join(_temp.name, 'search.sqlite')
os.environ['LLM_PROVIDER'] = 'stub'
os.environ['SIGNUP_INVITE_CODE'] = 'search-test'
os.environ['RATE_LIMIT_ENABLED'] = '0'
from fastapi.testclient import TestClient
from main import app
from db import db as d
from agentic import store

def run():
    d.init_db(reset=True)
    a,b = TestClient(app),TestClient(app)
    for i,c in enumerate((a,b)):
        assert c.post('/api/auth/signup',json={'email':f'search{i}@example.com','password':'correct horse battery','invite_code':'search-test'}).status_code == 201
    biz=a.post('/api/businesses',json={'name':'Search shop','template':'health_club'}).json()
    ent=next(e for e in biz['spec']['entities'] if e['label']=='Member')
    key=ent['key']; name=next(f['key'] for f in ent['fields'] if f['label']=='Full name')
    url=f"/api/businesses/{biz['id']}/records/{key}"
    ids=[]
    for text in ['Straße toys','１００%_Ready','Other toy']:
        r=a.post(url,json={'values':{name:text}});assert r.status_code==201,r.text;ids.append(r.json()['id'])
    checks=0
    def check(ok):
        nonlocal checks
        assert ok;checks+=1
    check(a.get(url).json()['total']==3)
    check(a.get(url,params={'q':'STRASSE'}).json()['records'][0]['id']==ids[0])
    check(a.get(url,params={'q':'100%_'}).json()['total']==1)
    check(a.get(url,params={'q':"' OR 1=1 --"}).json()['total']==0)
    check(a.get(url,params={'q':'toy','limit':1,'offset':1}).json()['records'][0]['id']==ids[2])
    check(a.get(url,params={'q':'toy','limit':1}).json()['total']==2)
    check(a.get(url,params={'equals':json.dumps({name:'Other toy'})}).json()['total']==1)
    check(a.get(url,params={'q':'Straße','equals':json.dumps({name:'Other toy'})}).json()['total']==0)
    check(a.get(url,params={'equals':'[]'}).status_code==422)
    check(a.get(url,params={'equals':'{"x":NaN}'}).status_code==422)
    check(a.get(url,params={'equals':'{broken'}).status_code==422)
    check(a.get(url,params={'equals':json.dumps({'not_a_field':'x'})}).status_code==422)
    check(a.get(url,params={'q':'x'*201}).status_code==422)
    check(b.get(url,params={'q':'toy'}).status_code==404)
    check(TestClient(app).get(url).status_code==401)
    check(a.get(url,params={'offset':999}).json()['records']==[])
    # Archived attributes must never be a hidden search or count side channel.
    with d.get_conn() as c:
        spec=json.loads(c.execute('SELECT spec_json FROM spec_versions WHERE business_id=? AND version=?',(biz['id'],biz['current_version'])).fetchone()[0])
        hidden={'key':'f_search_hidden','label':'Hidden','type':'text','required':False,'archived':True}
        next(e for e in spec['entities'] if e['key']==key)['fields'].append(hidden)
        c.execute('UPDATE spec_versions SET spec_json=? WHERE business_id=? AND version=?',(json.dumps(spec),biz['id'],biz['current_version']))
        d.set_entity_attributes(c,key,ids[0],{hidden['key']:'secret-needle'})
    check(a.get(url,params={'q':'secret-needle'}).json()['total']==0)
    check(a.get(url,params={'equals':json.dumps({hidden['key']:'secret-needle'})}).status_code==422)
    check(a.get(url).json()['total']==3)
    print(f'record search HTTP: {checks} checks passed')

if __name__=='__main__':run()
