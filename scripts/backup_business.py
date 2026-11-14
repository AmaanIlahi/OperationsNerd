"""Back up a private business DB or rehearse restoring it to a fresh database."""
import argparse
from pathlib import Path
import sys
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'operations_nerd'))


def main():
    load_dotenv(ROOT/'.env',override=False)
    import json,os
    local=ROOT/'.local'/'database.json'
    if not os.getenv('OPERATIONS_DATABASE_URL') and local.exists():
        settings=json.loads(local.read_text())
        os.environ['OPERATIONS_DATABASE_URL']=settings['control_url']
        os.environ['OPERATIONS_ADMIN_DATABASE_URL']=settings['admin_url']
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner',type=int,required=True)
    parser.add_argument('--business',type=int,required=True)
    parser.add_argument('--out-dir')
    parser.add_argument('--restore-copy',type=Path)
    args=parser.parse_args()
    from umbrella.backup import BackupToolError,backup_business,restore_copy
    try:
        result=restore_copy(args.owner,args.business,args.restore_copy) if args.restore_copy else \
            backup_business(args.owner,args.business,args.out_dir)
        print(json.dumps(result,indent=2))
    except BackupToolError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit('Backup/recovery did not complete. The live database was not replaced. Check identity, tools and connectivity.') from None


if __name__=='__main__':
    main()
