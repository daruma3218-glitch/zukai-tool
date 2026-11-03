"""Repeat real SDK/HTTP/Pillow edits with synthetic images; no network or API key.

Run in a fresh process before/after a memory change. Only TemporaryDirectory
receives images. The mock transport rejects any request outside /images/edits.
"""
import argparse
import base64
import gc
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import tracemalloc

import httpx
import openai
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generator


def rss_bytes():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        import os
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=32)
    parser.add_argument('--check-stable', action='store_true')
    args = parser.parse_args()
    rng = random.Random(731)
    with io.BytesIO() as buf, Image.frombytes('RGB', (1536, 1024), rng.randbytes(1536 * 1024 * 3)) as image:
        image.save(buf, 'PNG')
        encoded = base64.b64encode(buf.getvalue()).decode('ascii')
    calls = 0

    def respond(request):
        nonlocal calls
        assert request.url.host == 'offline.invalid' and request.url.path == '/v1/images/edits'
        calls += 1
        return httpx.Response(200, json={'created': 1, 'data': [{'b64_json': encoded}]})

    samples, hashes = [], set()
    with tempfile.TemporaryDirectory(prefix='zukai-edit-memory-') as temporary:
        root = Path(temporary)
        source, output = root / 'source.png', root / 'edited.png'
        with Image.new('RGB', (320, 180), 'white') as image:
            image.save(source)
        gc.collect()
        tracemalloc.start()
        baseline = rss_bytes()
        # Mirror production: one new thread/client for each edit, same worker.
        for index in range(args.count):
            failures = []

            def edit():
                try:
                    with openai.OpenAI(api_key='offline-test', base_url='https://offline.invalid/v1',
                                       max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
                        ok, error = generator._sync_edit_image_openai(client, source, 'synthetic edit', output)
                        assert ok, error
                except Exception as exc:
                    failures.append(str(exc))

            thread = threading.Thread(target=edit)
            thread.start()
            thread.join(30)
            if thread.is_alive() or failures:
                raise RuntimeError(f'Offline edit failed: {failures}')
            hashes.add(hashlib.sha256(output.read_bytes()).hexdigest())
            current, peak = tracemalloc.get_traced_memory()
            samples.append({'edit': index + 1, 'rss_bytes': rss_bytes(),
                            'python_bytes': current, 'python_peak_bytes': peak})
        print(json.dumps({'count': args.count, 'api_calls': 0, 'mock_calls': calls,
                          'sdk': openai.__version__, 'baseline_rss_bytes': baseline,
                          'output_hashes': sorted(hashes), 'samples': samples}))
        if args.check_stable:
            assert len(hashes) == 1 and calls == args.count
            assert samples[-1]['python_bytes'] - samples[0]['python_bytes'] < 4 * 1024 * 1024
            assert samples[-1]['rss_bytes'] - samples[0]['rss_bytes'] < 16 * 1024 * 1024


if __name__ == '__main__':
    main()
