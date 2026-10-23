# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""V4L2 native mode enumeration and deterministic, bounded capture selection."""
import errno
from fractions import Fraction
import math
import os
import re
import struct

GST_FORMATS = {'YUYV': 'YUY2', 'UYVY': 'UYVY', 'NV12': 'NV12', 'RGB3': 'RGB', 'BGR3': 'BGR'}
FORMATS = (*GST_FORMATS, 'MJPG', 'JPEG')


def _fraction(numerator, denominator):
    if not numerator or not denominator:
        raise ValueError('Camera advertised a zero frame interval')
    return Fraction(numerator, denominator)


def _intervals(descriptor, fourcc, width, height, ioctl):
    result = []
    for index in range(256):
        value = bytearray(52)  # struct v4l2_frmivalenum, Linux UAPI
        struct.pack_into('IIII', value, 0, index, int.from_bytes(fourcc.encode(), 'little'), width, height)
        try:
            ioctl(descriptor, 0xc034564b, value)  # VIDIOC_ENUM_FRAMEINTERVALS
        except OSError as error:
            if error.errno == errno.EINVAL:
                break
            raise
        kind = struct.unpack_from('I', value, 16)[0]
        fields = struct.unpack_from('IIIIII', value, 20)
        if kind == 1:
            interval = _fraction(*fields[:2])
            result.append({'kind': 'discrete', 'num': interval.numerator, 'den': interval.denominator})
        elif kind in (2, 3):
            minimum, maximum = _fraction(*fields[:2]), _fraction(*fields[2:4])
            step = Fraction(0) if kind == 2 else _fraction(*fields[4:6])
            result.append({'kind': 'continuous' if kind == 2 else 'stepwise',
                           'min': [minimum.numerator, minimum.denominator],
                           'max': [maximum.numerator, maximum.denominator],
                           'step': [step.numerator, step.denominator]})
            break
        else:
            raise ValueError('Unknown V4L2 frame interval type')
    return result


def intervals_for_size(path, fourcc, width, height):
    import fcntl
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return _intervals(descriptor, fourcc, width, height, fcntl.ioctl)
    finally:
        os.close(descriptor)


def enumerate_modes(path, formats):
    import fcntl
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    result = []
    try:
        for fourcc in sorted(set(formats) & set(FORMATS)):
            for index in range(256):
                value = bytearray(44)  # struct v4l2_frmsizeenum, Linux UAPI
                struct.pack_into('II', value, 0, index, int.from_bytes(fourcc.encode(), 'little'))
                try:
                    fcntl.ioctl(descriptor, 0xc02c564a, value)  # VIDIOC_ENUM_FRAMESIZES
                except OSError as error:
                    if error.errno == errno.EINVAL:
                        break
                    raise
                kind = struct.unpack_from('I', value, 8)[0]
                fields = struct.unpack_from('IIIIII', value, 12)
                if kind == 1:
                    width, height = fields[:2]
                    result.append({'format': fourcc, 'size': {'kind': 'discrete', 'width': width, 'height': height},
                                   'intervals': _intervals(descriptor, fourcc, width, height, fcntl.ioctl)})
                elif kind in (2, 3):
                    result.append({'format': fourcc, 'size': {
                        'kind': 'continuous' if kind == 2 else 'stepwise',
                        'min_width': fields[0], 'max_width': fields[1], 'step_width': max(1, fields[2]),
                        'min_height': fields[3], 'max_height': fields[4], 'step_height': max(1, fields[5])}})
                    break  # Query intervals for an exact chosen size when opening.
                else:
                    raise ValueError('Unknown V4L2 frame size type')
        return result
    finally:
        os.close(descriptor)


def parse_override(value):
    match = re.fullmatch(r'([A-Z0-9]{4}):(\d+)x(\d+)@(\d+(?:/\d+|\.\d+)?)', value or '')
    if not match or match[1] not in FORMATS:
        raise ValueError('Use FOURCC:WIDTHxHEIGHT@FPS, e.g. YUYV:640x360@15')
    try:
        fps = Fraction(match[4])
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError('Invalid native frame rate') from error
    width, height = int(match[2]), int(match[3])
    if not 1 <= width <= 16384 or not 1 <= height <= 16384 or not 0 < fps <= 1000:
        raise ValueError('Invalid native dimensions or frame rate')
    return {'format': match[1], 'width': width, 'height': height,
            'fps_num': fps.numerator, 'fps_den': fps.denominator}


def _size_supported(size, width, height):
    if size['kind'] == 'discrete':
        return (width, height) == (size['width'], size['height'])
    return all(size['min_' + name] <= value <= size['max_' + name] and
               (size['kind'] == 'continuous' or (value - size['min_' + name]) % size['step_' + name] == 0)
               for name, value in [('width', width), ('height', height)])


def _rates(intervals, requested, exact=False):
    target = 1 / requested
    for row in intervals:
        if row['kind'] == 'discrete':
            interval = Fraction(row['num'], row['den'])
        else:
            minimum, maximum, step = (Fraction(*row[key]) for key in ('min', 'max', 'step'))
            interval = target if exact else min(maximum, target)
            if interval < minimum or interval > maximum:
                continue
            if step:
                count = (interval - minimum) / step
                if exact and count.denominator != 1:
                    continue
                interval = minimum + math.floor(count) * step
        rate = 1 / interval
        if (exact and rate == requested) or (not exact and rate >= requested):
            yield rate


def choose_mode(modes, width, height, fps, preference, override=None, query_intervals=None):
    explicit = parse_override(override) if override else None
    requested = Fraction(str(fps))
    candidates = []
    for row in modes:
        size, fourcc = row['size'], row['format']
        if explicit:
            w, h = explicit['width'], explicit['height']
            if fourcc != explicit['format'] or not _size_supported(size, w, h):
                continue
        elif size['kind'] == 'discrete':
            w, h = size['width'], size['height']
        else:
            dimensions = []
            for name, target in [('width', width), ('height', height)]:
                low, high = size['min_' + name], size['max_' + name]
                step = 1 if size['kind'] == 'continuous' else size['step_' + name]
                value = low + math.ceil(max(0, target - low) / step) * step
                dimensions.append(min(low + (high - low) // step * step, value))
            w, h = dimensions
        intervals = row.get('intervals')
        if intervals is None and query_intervals:
            intervals = query_intervals(fourcc, w, h)
        exact_rate = Fraction(explicit['fps_num'], explicit['fps_den']) if explicit else requested
        for rate in _rates(intervals or [], exact_rate, exact=bool(explicit)):
            if rate < requested:
                continue
            mode = dict(format=fourcc, width=w, height=h, fps_num=rate.numerator, fps_den=rate.denominator)
            # Avoid upscaling first, then minimize source pixel rate. Formats break
            # ties, so an MJPEG-only high-rate mode can still be selected.
            rank = (w < width or h < height, w * h * rate,
                    abs(Fraction(w, h) - Fraction(width, height)),
                    preference.index(fourcc) if fourcc in preference else len(preference), w, h, rate)
            candidates.append((rank, mode))
    if not candidates:
        if explicit and not modes and Fraction(explicit['fps_num'], explicit['fps_den']) >= requested:
            return explicit | {'advertised': False}  # Still requires exact GStreamer negotiation.
        raise ValueError('No advertised native mode can supply this output; refresh cameras or set a supported capture_mode')
    return min(candidates, key=lambda item: item[0])[1] | {'advertised': True}


def source_caps(mode):
    media = 'image/jpeg' if mode['format'] in ('MJPG', 'JPEG') else f"video/x-raw,format={GST_FORMATS[mode['format']]}"
    return (f"{media},width={mode['width']},height={mode['height']},"
            f"framerate={mode['fps_num']}/{mode['fps_den']}")
