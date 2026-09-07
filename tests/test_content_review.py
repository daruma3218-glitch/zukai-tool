import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verifier
import pipeline


def inputs(tmp_path,count=1):
    root=tmp_path/'images'
    root.mkdir()
    rows=[]
    for i in range(1,count+1):
        (root/f'{i}.png').write_bytes(b'image fixture')
        rows.append({'index':i,'filename':f'{i}.png','success':True,'excerpt':'100から80へ減少',
                     'section':'貿易','manuscript':'MUST_NOT_SEND_FULL_MANUSCRIPT','prompt':'MUST_NOT_SEND_PROMPT'})
    return root,rows


def test_only_approved_excerpt_and_image_go_to_codex(tmp_path):
    root,rows=inputs(tmp_path)
    answer=json.dumps([{'index':1,'status':'pass','reason':'一致'}])
    with mock.patch.object(verifier.runtime,'generate',return_value=(answer,{})) as call:
        report=verifier.verify_images(rows,root)
    assert report['counts']['pass']==1
    query=call.call_args.args[1]
    assert '貿易' in query and '100から80' in query
    assert 'MUST_NOT_SEND' not in query
    assert call.call_args.kwargs['primary']=='codex'
    assert call.call_args.kwargs['allow_fallback'] is False
    assert call.call_args.kwargs['model']=='gpt-6-astra'
    assert len(call.call_args.kwargs['attachments'])==1


def test_duplicate_and_missing_verdicts_remain_unverified(tmp_path):
    root,rows=inputs(tmp_path,2)
    answer=json.dumps([{'index':1,'status':'pass','reason':'x'}]*2+[{'index':99,'status':'pass','reason':'x'}])
    with mock.patch.object(verifier.runtime,'generate',return_value=(answer,{})):
        report=verifier.verify_images(rows,root)
    assert report['counts']['unverified']==2


def test_quota_failure_stops_later_batches_without_api_or_regeneration(tmp_path):
    root,rows=inputs(tmp_path,5)
    with mock.patch.object(verifier.runtime,'generate',side_effect=verifier.runtime.SubscriptionUnavailable('usage_limit')) as call:
        report=verifier.verify_images(rows,root)
    assert call.call_count==1
    assert report['counts']['unverified']==5


def test_image_path_outside_output_is_not_sent(tmp_path):
    root,rows=inputs(tmp_path)
    outside=tmp_path/'outside.png'
    outside.write_bytes(b'private')
    rows[0]['filename']='../outside.png'
    with mock.patch.object(verifier.runtime,'generate') as call:
        report=verifier.verify_images(rows,root)
    call.assert_not_called()
    assert report['counts']['unverified']==1


def test_review_persists_to_progress_snapshot(tmp_path):
    pipe=pipeline.DiagramPipeline('fixture',tmp_path/'job')
    review={'status':'needs_fix','reason':'数値が逆'}
    pipe._on_item_event({'index':1,'status':'ok','content_review':review})
    saved=json.loads((tmp_path/'job/images_progress.json').read_text(encoding='utf-8'))
    assert saved['items'][0]['content_review']==review
