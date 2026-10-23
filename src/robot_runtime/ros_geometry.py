"""Fixed spatial transforms shared by bounded observation projections."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
import math

from .common import Fault, text


def validate_mount(value):
    from .ros_profile import number
    for name in ("source_frame_id", "frame_id"):
        text(value[name], 128)
    for name, size, bound in (("translation_m", 3, 100), ("quaternion_xyzw", 4, 1)):
        if not isinstance(value[name], list) or len(value[name]) != size:
            raise Fault("invalid_ros_profile", "Declare a complete fixed mounting transform")
        for item in value[name]: number(item, -bound, bound)
    if abs(math.hypot(*value["quaternion_xyzw"]) - 1) > 1e-6:
        raise Fault("invalid_ros_profile", "The fixed mounting quaternion must have unit norm")


def normalize(q):
    norm = math.hypot(*q)
    return tuple(value / norm for value in q)


def rotate(q, point):
    x, y, z = point
    qx, qy, qz, qw = normalize(q)
    return ((1 - 2*(qy*qy + qz*qz))*x + 2*(qx*qy - qz*qw)*y + 2*(qx*qz + qy*qw)*z,
            2*(qx*qy + qz*qw)*x + (1 - 2*(qx*qx + qz*qz))*y + 2*(qy*qz - qx*qw)*z,
            2*(qx*qz - qy*qw)*x + 2*(qy*qz + qx*qw)*y + (1 - 2*(qx*qx + qy*qy))*z)


def transform(point, mount):
    return tuple(a + b for a, b in zip(rotate(mount["quaternion_xyzw"], point), mount["translation_m"]))


def compose(a, b):
    """q_target_from_source * q_source_from_object, in ROS xyzw order."""
    ax, ay, az, aw = normalize(a)
    bx, by, bz, bw = normalize(b)
    return normalize((aw*bx + ax*bw + ay*bz - az*by,
                      aw*by - ax*bz + ay*bw + az*bx,
                      aw*bz + ax*by - ay*bx + az*bw,
                      aw*bw - ax*bx - ay*by - az*bz))
