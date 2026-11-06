"""Request Kestra promotion using the existing OIDC role; never write GitOps."""
import argparse
import json
import os
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', choices=['orders-api','orders-worker'], required=True)
    parser.add_argument('--image', required=True)
    args=parser.parse_args()
    sha=os.environ['GITHUB_SHA'];run=os.environ['GITHUB_RUN_ID'];attempt=os.environ['GITHUB_RUN_ATTEMPT']
    if not re.fullmatch('[a-f0-9]{40}',sha) or not re.fullmatch('[0-9]{1,20}',run) or not re.fullmatch('[0-9]{1,4}',attempt):
        raise SystemExit('Invalid immutable source execution')
    pattern=r'[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/aws-eks-istio-lab-dev-'+re.escape(args.service)+r'@(sha256:[a-f0-9]{64})'
    match=re.fullmatch(pattern,args.image)
    if not match:raise SystemExit('Immutable application digest required')
    label={'orders-api':'API','orders-worker':'Worker'}[args.service]
    parameters={'Commit':[sha],'Digest':[match[1]],'RunId':[run],'RunAttempt':[attempt]}
    command=['aws','ssm','send-command','--region','us-east-1','--document-name','CosmoTech-Request-Orders-'+label+'-Promotion','--document-version','$LATEST','--instance-ids','i-0def4e806c49cf60e','--parameters',json.dumps(parameters),'--query','Command.CommandId','--output','text']
    result=subprocess.run(command,check=False,capture_output=True,text=True,timeout=30)
    if result.returncode:raise SystemExit('Kestra request failed; no fallback publisher')
    command_id=result.stdout.strip()
    for _ in range(60):
        response=subprocess.run(['aws','ssm','get-command-invocation','--region','us-east-1',
            '--command-id',command_id,'--instance-id','i-0def4e806c49cf60e','--output','json'],
            check=False,capture_output=True,text=True,timeout=20)
        if response.returncode==0:
            invocation=json.loads(response.stdout)
            if invocation['Status']=='Success':
                accepted=json.loads(invocation['StandardOutputContent'])
                if not accepted.get('kestra_execution_id'):
                    raise SystemExit('The trusted request did not return an execution reference')
                print(json.dumps({'state':'requested','kestra_execution_id':accepted['kestra_execution_id'],
                                  'promotion_validated':False}))
                return
            if invocation['Status'] in {'Failed','Cancelled','TimedOut'}:
                raise SystemExit('Trusted Kestra request failed; inspect '+command_id+'; no fallback publisher')
        time.sleep(2)
    raise SystemExit('Ambiguous request outcome; reconcile '+command_id+' before retry')

if __name__=='__main__':
    main()
