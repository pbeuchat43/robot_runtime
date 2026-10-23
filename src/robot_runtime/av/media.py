# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Small, dependency-free policies shared by capture and measurements."""


def stamp_bytes(milliseconds):
    value = int(milliseconds) & 0xffffffff
    data = [0xa5, *value.to_bytes(4, 'big')]
    data.append(data[0] ^ data[1] ^ data[2] ^ data[3] ^ data[4])
    return data


def stamp_nv12(data, width, height, milliseconds):
    """Stamp the Y plane before encoding; chroma in the strip becomes neutral.

    Geometry is normalized so the browser can read it after adaptive resizing.
    This timestamp describes host receipt, not sensor exposure or optical latency.
    """
    if width < 192 or width % 2 or height < 48 or height % 2:
        raise ValueError('NV12 stamp needs even dimensions of at least 192x48')
    if len(data) != width * height * 3 // 2:
        raise ValueError('Unexpected NV12 frame size')
    values = stamp_bytes(milliseconds)
    top = height - 32
    data[top * width:height * width] = 128
    data[width * height + top // 2 * width:] = 128
    luma = data[:width * height].reshape(height, width)
    for index in range(48):
        left = round((8 + index * 624 / 48) * width / 640)
        right = round((8 + (index + 1) * 624 / 48) * width / 640)
        luma[top + 4:height - 4, left:right] = (
            235 if values[index // 8] & (1 << (7 - index % 8)) else 16)


