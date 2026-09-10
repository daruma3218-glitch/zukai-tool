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


def _pipeline_fixture(tmp_path, **kw):
    pipe = pipeline.DiagramPipeline('fixture', tmp_path / 'job', **kw)
    results = [{'index': 1, 'filename': '1.png', 'success': True, 'excerpt': 'x', 'section': 's'}]
    return pipe, results


def test_content_review_is_off_by_default(tmp_path):
    pipe, _ = _pipeline_fixture(tmp_path)
    assert pipe.content_review is False


def test_review_skipped_when_disabled_never_calls_verifier(tmp_path):
    pipe, results = _pipeline_fixture(tmp_path, content_review=False)
    with mock.patch.object(pipeline, 'verify_images') as verify:
        assert pipe.content_review is False
        # run() 内の分岐と同じ判定: オフなら verifier に触れない
        review = pipe._run_content_review(results) if pipe.content_review else None
    verify.assert_not_called()
    assert review is None
    assert not (tmp_path / 'job/content_review.json').exists()


def test_review_runs_when_enabled(tmp_path):
    pipe, results = _pipeline_fixture(tmp_path, content_review=True)
    report = {'counts': {'pass': 1, 'needs_fix': 0, 'unverified': 0}, 'items': []}
    with mock.patch.object(pipeline, 'verify_images', return_value=report) as verify:
        review = pipe._run_content_review(results)
    verify.assert_called_once()
    assert review == report
    assert (tmp_path / 'job/content_review.json').exists()


def test_review_failure_does_not_abort_pipeline(tmp_path):
    """照合が途中で落ちても例外を外に出さない（完了・DL を止めない）。"""
    pipe, results = _pipeline_fixture(tmp_path, content_review=True)
    logs = []
    pipe.log_callback = lambda cat, msg, detail='': logs.append((cat, msg, detail))
    with mock.patch.object(pipeline, 'verify_images', side_effect=RuntimeError('cli timeout')):
        review = pipe._run_content_review(results)
    assert review is None
    assert any(cat == 'review' and '中断' in msg for cat, msg, _ in logs)


def test_run_without_review_completes_at_phase3_and_writes_manifest(tmp_path, monkeypatch):
    """既定（照合オフ）で run() が Phase 3 で 100% になり、verifier を呼ばずに manifest を書く。"""
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy')
    progress = []
    pipe = pipeline.DiagramPipeline(
        'x' * 200, tmp_path / 'job', target_count=5,
        progress_callback=lambda ph, msg, pct: progress.append((ph, pct)),
    )
    prompts = [{'index': 1, 'excerpt': 'e', 'section': 's', 'prompt': 'p'}]
    results = [{'index': 1, 'filename': '1.png', 'success': True}]
    with mock.patch.object(pipeline, 'get_anthropic_client', return_value=None), \
         mock.patch.object(pipeline, 'analyze_manuscript', return_value={'title': 'T', 'sections': [], 'keywords': []}), \
         mock.patch.object(pipeline, 'extract_visual_points', return_value=[{'index': 1, 'excerpt': 'e', 'section': 's'}]), \
         mock.patch.object(pipeline, 'generate_all_prompts', return_value=prompts), \
         mock.patch.object(pipeline, 'run_parallel_generation', return_value=results), \
         mock.patch.object(pipeline, 'verify_images') as verify:
        manifest = pipe.run()
    verify.assert_not_called()
    assert manifest['content_review'] is None
    assert manifest['content_review_enabled'] is False
    assert progress[-1] == (3, 100)
    assert (tmp_path / 'job/manifest.json').exists()
