"""Authenticated requests stay within the selected job, including symlinks."""
import io
import json
import zipfile

import pytest
from PIL import Image
import app as web


@pytest.fixture
def saved(tmp_path, monkeypatch):
    root = tmp_path / 'output'
    job = root / 'old-job_01'
    (job / 'images').mkdir(parents=True)
    Image.new('RGB', (24, 16), 'blue').save(job / 'images' / 'diagram_001.png')
    (job / 'manifest.json').write_text(json.dumps({'title': 'Saved job', 'items': []}))
    (job / 'manuscript.txt').write_text('original manuscript')
    (tmp_path / 'canary.txt').write_text('outside-job-canary')
    monkeypatch.setattr(web, 'OUTPUT_DIR', root)
    monkeypatch.setattr(web, 'APP_PASSWORD', 'synthetic')
    client = web.app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client, root, job, tmp_path


@pytest.mark.parametrize('job_id', ['%2e%2e', '%2e', '..%5c', 'C%3a', 'bad%00id'])
@pytest.mark.parametrize('route', ['/results/{}/canary.txt', '/download/{}', '/api/manifest/{}'])
def test_bad_job_ids_are_rejected(saved, job_id, route):
    client, *_ = saved
    response = client.get(route.format(job_id))
    assert response.status_code == 404
    assert b'outside-job-canary' not in response.data


def test_normal_saved_results_and_zip_are_unchanged(saved):
    client, _, job, _ = saved
    response = client.get('/results/old-job_01/images/diagram_001.png')
    assert response.status_code == 200
    assert response.data == (job / 'images/diagram_001.png').read_bytes()
    response = client.get('/download/old-job_01')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.read('images/diagram_001.png') == (job / 'images/diagram_001.png').read_bytes()
        assert archive.read('manuscript.txt') == b'original manuscript'


@pytest.mark.parametrize('filename', ['%2e%2e/canary.txt', 'images/%2e%2e/%2e%2e/canary.txt', 'C%3a/canary.txt', '..%5ccanary.txt'])
def test_invalid_filename_is_refused(saved, filename):
    client, *_ = saved
    assert client.get('/results/old-job_01/' + filename).status_code == 404


def link_or_skip(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip('OS does not permit test symlinks: ' + type(exc).__name__)


def test_symlink_file_cannot_be_downloaded(saved):
    client, _, job, outside = saved
    link_or_skip(job / 'leak.txt', outside / 'canary.txt')
    assert client.get('/results/old-job_01/leak.txt').status_code == 404


def test_symlink_job_cannot_be_downloaded(saved):
    client, root, _, outside = saved
    link_or_skip(root / 'linked-job', outside, directory=True)
    assert client.get('/results/linked-job/canary.txt').status_code == 404


def test_zip_excludes_files_linked_outside_job(saved):
    client, _, job, outside = saved
    link_or_skip(job / 'images' / 'diagram_002.png', outside / 'canary.txt')
    (job / 'manuscript.txt').unlink()
    link_or_skip(job / 'manuscript.txt', outside / 'canary.txt')
    response = client.get('/download/old-job_01')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert 'images/diagram_001.png' in archive.namelist()
        assert all(b'outside-job-canary' not in archive.read(name) for name in archive.namelist())
