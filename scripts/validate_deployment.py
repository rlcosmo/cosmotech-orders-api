"""Validate the application's complete DEV delivery configuration."""
import json
from pathlib import Path

def load(application, directory):
    names = {'deployment':'Deployment','configmap':'ConfigMap','service':'Service','hpa':'HorizontalPodAutoscaler','ingress':'Ingress'}
    values = {}
    if {p.name for p in directory.iterdir()} != {n+'.json' for n in names}:
        raise ValueError('Exactly the five versioned application resources are required')
    for name,kind in names.items():
        value=json.loads((directory/(name+'.json')).read_text())
        if value['kind'] != kind or value['metadata']['namespace'] != application or value['metadata']['name'] != ({'configmap':'orders-config','ingress':application+'-dev'}.get(name,application)):
            raise ValueError('Unexpected resource kind, name or namespace')
        values[name+'.json']=value
    deployment=values['deployment.json']['spec']['template']['spec']
    if not deployment['containers'][0]['resources']['requests'].get('cpu'):
        raise ValueError('CPU requests are required for HPA')
    hpa=values['hpa.json']['spec']
    if hpa['scaleTargetRef']['name'] != application or not 1 <= hpa['minReplicas'] <= hpa['maxReplicas'] <= 4:
        raise ValueError('Invalid application HPA')
    rule=values['ingress.json']['spec']['rules'][0]
    if rule['host'] != application+'-dev.cosmotechsolutions.com.br' or rule['http']['paths'][0]['backend']['service']['name'] != application:
        raise ValueError('Invalid application host/backend')
    return values

if __name__ == '__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--application',required=True);p.add_argument('--image');p.add_argument('--sha');p.add_argument('--output',type=Path)
    args=p.parse_args(); values=load(args.application,Path('deploy/dev'))
    if args.output:
        image,digest=args.image.split('@')
        values['deployment.json']['spec']['template']['spec']['containers'][0]['image']=image+':sha-'+args.sha+'@'+digest
        args.output.write_text(json.dumps({'application':args.application,'source_sha':args.sha,'source_repository':'rlcosmo/cosmotech-'+args.application,'resources':values},sort_keys=True))
    print('Validated application configuration, HPA and host')
