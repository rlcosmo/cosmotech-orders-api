import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

APP='orders-api'
PREFIX='api'

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('image_alias_under_test',ROOT/'scripts/release_image.py')
image_alias=importlib.util.module_from_spec(spec);spec.loader.exec_module(image_alias)


def response(value=None,code=0,error=''):
    return SimpleNamespace(returncode=code,stdout=json.dumps(value),stderr=error)


def test_alias_existing_same_digest_is_idempotent(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_NUMBER','17');digest='sha256:'+'a'*64
    run=Mock(return_value=response({'imageDetails':[{'imageDigest':digest}]}))
    tag=image_alias.publish_version_alias(APP,'registry@'+digest,run=run)
    assert tag.startswith(PREFIX+'-') and tag.endswith('-17') and run.call_count==1


def test_alias_cannot_overwrite_a_different_digest(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_NUMBER','17');run=Mock(return_value=response({'imageDetails':[{'imageDigest':'sha256:'+'b'*64}]}))
    with pytest.raises(RuntimeError,match='different digest'):image_alias.publish_version_alias(APP,'registry@sha256:'+'a'*64,run=run)
    assert run.call_count==1


def test_access_denied_is_not_treated_as_missing_alias(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_NUMBER','17');run=Mock(return_value=response(code=1,error='(AccessDeniedException)'))
    with pytest.raises(RuntimeError,match='blocked'):image_alias.publish_version_alias(APP,'registry@sha256:'+'a'*64,run=run)
    assert run.call_count==1


def test_publish_alias_reuses_exact_manifest_and_digest(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_NUMBER','17');digest='sha256:'+'a'*64
    manifest=json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json'})
    def call(command,**kwargs):
        if 'describe-images' in command:return response(code=1,error='(ImageNotFoundException)')
        if 'batch-get-image' in command:return response({'images':[{'imageId':{'imageDigest':digest},'imageManifest':manifest}]})
        assert 'put-image' in command
        path=Path(command[command.index('--image-manifest')+1].removeprefix('file://'))
        assert path.read_text()==manifest and path.stat().st_mode&0o077==0
        return response({'image':{'imageId':{'imageDigest':digest}}})
    run=Mock(side_effect=call)
    assert image_alias.publish_version_alias(APP,'registry@'+digest,run=run).endswith('-17') and run.call_count==3


@pytest.mark.parametrize('second_manifest,second_digest,allowed', [
    ('same', 'same', True), ('different', 'same', False), ('same', 'different', False),
])
def test_digest_lookup_can_repeat_only_identical_tagged_manifests(monkeypatch, second_manifest, second_digest, allowed):
    monkeypatch.setenv('GITHUB_RUN_NUMBER','18')
    digest='sha256:'+'a'*64
    manifest=json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.index.v1+json'})
    images=[{'imageId':{'imageDigest':digest,'imageTag':'sha-commit'},'imageManifest':manifest},
            {'imageId':{'imageDigest':digest if second_digest=='same' else 'sha256:'+'b'*64,
                        'imageTag':'existing-readable-alias'},
             'imageManifest':manifest if second_manifest=='same' else manifest+' '}]
    run=Mock(side_effect=[response(code=1,error='(ImageNotFoundException)'),
                          response({'images':images,'failures':[]}),
                          response({'image':{'imageId':{'imageDigest':digest}}})])
    if allowed:
        assert image_alias.publish_version_alias(APP,'registry@'+digest,run=run).endswith('-18')
        assert run.call_count==3
    else:
        with pytest.raises(RuntimeError,match='Manifest digest mismatch'):
            image_alias.publish_version_alias(APP,'registry@'+digest,run=run)
        assert run.call_count==2
