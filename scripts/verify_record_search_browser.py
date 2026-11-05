"""Disposable real-database browser acceptance checks. Install playwright and its Chromium first.
Uses only synthetic local fixtures; never a hosted account or real model.
"""
import os,sys,tempfile,subprocess,time,json,socket
from pathlib import Path
from playwright.sync_api import sync_playwright
REPO=Path(__file__).resolve().parents[1]; PY=sys.executable; OUT=Path(os.environ.get('BROWSER_EVIDENCE_DIR', str(Path(__file__).resolve().parents[1]/'.browser-evidence')));OUT.mkdir(exist_ok=True)

def wait(url):
 import urllib.request
 for _ in range(120):
  try:
   with urllib.request.urlopen(url,timeout=1) as r: return
  except Exception: time.sleep(.1)
 raise RuntimeError('server did not start '+url)

def ops():
 import httpx
 with tempfile.TemporaryDirectory() as temp:
  env={**os.environ,'DATABASE_PATH':temp+'/ops.sqlite','LLM_PROVIDER':'stub','SIGNUP_INVITE_CODE':'fixture','RATE_LIMIT_ENABLED':'0'}
  log=open(OUT/'ops-server.log','w');p=subprocess.Popen([PY,'-m','uvicorn','main:app','--host','127.0.0.1','--port','8131'],cwd=REPO/'operations_nerd',env=env,stdout=log,stderr=log)
  try:
   wait('http://127.0.0.1:8131/healthz')
   c=httpx.Client(base_url='http://127.0.0.1:8131');c.post('/api/auth/signup',json={'email':'browser@example.com','password':'correct horse battery','invite_code':'fixture'}).raise_for_status()
   biz=c.post('/api/businesses',json={'name':'Toy shop - synthetic','template':'health_club'}).json()
   ent=next(e for e in biz['spec']['entities'] if e['label']=='Member'); name=next(f['key'] for f in ent['fields'] if f['label']=='Full name');url=f"/api/businesses/{biz['id']}/records/{ent['key']}"
   select=next(f for f in ent['fields'] if f['type']=='select')
   for text in ['Straße toys','Other toys','<img src=x onerror=alert(1)>']:
    c.post(url,json={'values':{name:text,select['key']:select['options'][0]}}).raise_for_status()
   with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True,args=['--no-sandbox']);context=browser.new_context(viewport={'width':1440,'height':950});context.add_cookies([{'name':k,'value':v,'url':'http://127.0.0.1:8131'} for k,v in c.cookies.items()]);page=context.new_page();page.goto('http://127.0.0.1:8131/app/');page.get_by_role('button',name='Member',exact=True).click();page.get_by_role('searchbox',name='Search records').wait_for();page.get_by_role('searchbox',name='Search records').fill('STRASSE');page.get_by_role('button',name='Search',exact=True).click();page.get_by_text('1 record(s)',exact=True).wait_for();assert page.get_by_text('Straße toys',exact=True).count()==1
    page.screenshot(path=str(OUT/'operations-search-desktop.png'),full_page=True)
    page.get_by_role('button',name='Clear',exact=True).click();page.get_by_text('3 record(s)',exact=True).wait_for();page.get_by_label('Filter field').select_option(select['key']);
    with page.expect_response(lambda r: '/records/' in r.url and 'equals=' in r.url):
     page.get_by_role('button',name='Search',exact=True).click()
    page.get_by_text('3 record(s)',exact=True).wait_for()
    page.get_by_role('searchbox',name='Search records').fill('notfound');page.get_by_role('button',name='Search',exact=True).click();page.get_by_text('No matching records.',exact=False).wait_for()
    page.get_by_role('button',name='Clear',exact=True).click();page.get_by_text('<img src=x onerror=alert(1)>',exact=True).wait_for();assert page.locator('td img').count()==0
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(OUT/'operations-search-mobile.png'),full_page=True)
    browser.close()
   print('Operations browser: real signup/template/record create/search/clear/filter/empty/XSS paths passed',flush=True)
  finally:p.terminate();p.wait();log.close()


if __name__=='__main__': ops()
