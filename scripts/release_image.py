"""Publish a readable alias for the tested digest; never overwrite a different image."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def version_tag(service: str, version: str, build: str) -> str:
    short={'orders-worker':'worker','orders-api':'api'}[service]
    tag=short+'-'+version.replace('+','-')+'-'+build
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',tag) or not re.fullmatch(r'[0-9]{1,20}',build):
        raise ValueError('Invalid version or build label')
    return tag


def publish_version_alias(service: str, image: str, *, run=subprocess.run) -> str:
    root=Path(__file__).resolve().parents[1]
    try:tree=ast.parse((root/'src/orders/runtime/release.py').read_text())
    except FileNotFoundError:tree=ast.parse('')
    version=next((n.value.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in n.targets) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str) and re.fullmatch(r'[A-Za-z0-9_.+-]{1,64}',n.value.value)),os.environ.get('GITHUB_SHA','')[:12])
    tag=version_tag(service,version,os.environ['GITHUB_RUN_NUMBER'])
    digest=image.rsplit('@',1)[-1];repository='aws-eks-istio-lab-dev-'+service
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',digest):raise ValueError('Tested digest required')
    def aws(*args):return run(['aws','--region','us-east-1','ecr',*args,'--output','json'],capture_output=True,text=True,check=False,timeout=30)
    existing=aws('describe-images','--repository-name',repository,'--image-ids','imageTag='+tag)
    if existing.returncode==0:
        if json.loads(existing.stdout)['imageDetails'][0]['imageDigest']!=digest:raise RuntimeError('Version label already belongs to a different digest')
        return tag
    if '(ImageNotFoundException)' not in existing.stderr:raise RuntimeError('Cannot verify image label; request blocked')
    response=aws('batch-get-image','--repository-name',repository,'--image-ids','imageDigest='+digest)
    if response.returncode:raise RuntimeError('Cannot read tested image manifest')
    value=json.loads(response.stdout)
    images=value.get('images',[])
    # ECR may return one identical manifest per existing tag for a digest lookup.
    # Accept duplicates only when every digest and the exact manifest bytes agree.
    if (value.get('failures') or not images
            or any(item.get('imageId',{}).get('imageDigest')!=digest for item in images)
            or not isinstance(images[0].get('imageManifest'),str)
            or any(item.get('imageManifest')!=images[0]['imageManifest'] for item in images)):
        raise RuntimeError('Manifest digest mismatch')
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8') as f:
        f.write(value['images'][0]['imageManifest']);f.flush()
        result=aws('put-image','--repository-name',repository,'--image-tag',tag,'--image-manifest','file://'+f.name)
    if result.returncode or json.loads(result.stdout)['image']['imageId']['imageDigest']!=digest:raise RuntimeError('Could not prove version alias for tested digest')
    return tag
