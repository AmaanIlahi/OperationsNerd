import ast, hashlib, hmac, json, os, subprocess, sys
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]/'operations_nerd/insights/intake.py'
BASE='ba79e4f1000f3e5249fd6e284215c4285567fd08'
valid=hmac.new(b'secret',b'body',hashlib.sha256).hexdigest()
CASES=[(valid,True),('sha256='+valid,True),(' '+valid+' ',True),(valid.upper(),True),('é'*64,False),('g'*64,False),('a'*63,False),('',False),('0'*64,False)]
if len(sys.argv)>1:
 s=subprocess.check_output(['git','show',BASE+':operations_nerd/insights/intake.py'],text=True) if sys.argv[1]=='before' else SOURCE.read_text()
 n=next(n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name=='verify_signature');ns={'hashlib':hashlib,'hmac':hmac,'os':os};exec(compile(ast.Module(body=[n],type_ignores=[]),str(SOURCE),'exec'),ns)
 out=[]
 for signature,expected in CASES:
  try:
   value=ns['verify_signature'](b'body',signature,'secret');out.append({'pass':value is expected,'value':value})
  except Exception as e:out.append({'pass':False,'error':type(e).__name__})
 print(json.dumps(out))
else:
 print('| Run | Before cases | After cases | Before non-ASCII | After non-ASCII |\n| --- | --- | --- | --- | --- |')
 for i in range(1,21):
  b=json.loads(subprocess.check_output([sys.executable,__file__,'before'],text=True));a=json.loads(subprocess.check_output([sys.executable,__file__,'after'],text=True));assert sum(x['pass'] for x in b)==7 and all(x['pass'] for x in a)
  print(f"| {i} | 7/9 | 9/9 | {b[4]['error']} | False |")
