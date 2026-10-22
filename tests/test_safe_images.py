import io

import pytest
from PIL import Image, UnidentifiedImageError
from safe_images import open_image
import generator


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP', 'GIF'])
def test_supported_images_still_decode(format):
    data = io.BytesIO()
    Image.new('RGB', (32, 18), 'blue').save(data, format=format)
    data.seek(0)
    with open_image(data) as result:
        result.load()
        assert result.size == (32, 18)


def test_renamed_tiff_is_refused_before_decoding(tmp_path, monkeypatch):
    from PIL import TiffImagePlugin
    path = tmp_path / 'reference.png'
    Image.new('RGB', (32, 18), 'blue').save(path, format='TIFF')
    def forbidden(*args, **kwargs):
        pytest.fail('TIFF decoder must not be called')
    monkeypatch.setattr(TiffImagePlugin.TiffImageFile, '_open', forbidden)
    with pytest.raises(UnidentifiedImageError):
        open_image(path)
    with pytest.raises(UnidentifiedImageError):
        generator._save_as_16_9(path.read_bytes(), tmp_path / 'output.png')
    assert not (tmp_path / 'output.png').exists()
