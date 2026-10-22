"""Decode only the raster formats used by studio images and web references."""
from PIL import Image

ALLOWED_FORMATS = ('PNG', 'JPEG', 'WEBP', 'GIF')


def open_image(source):
    # Select decoders before parsing; a renamed TIFF/JP2 must not reach its decoder.
    return Image.open(source, formats=ALLOWED_FORMATS)
