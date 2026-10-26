# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Optional ROS Image subscriber. It never opens or configures a camera device."""
import os
import signal
from queue import Full
import time

from .cameras import OakCapture


def frame_age_seconds(stamp_ns, ros_now_ns, tolerance_ms):
    if stamp_ns <= 0 or ros_now_ns <= 0:
        raise ValueError('ROS image requires a valid acquisition stamp and running ROS clock')
    age = (ros_now_ns - stamp_ns) / 1e9
    if age < -tolerance_ms / 1000:
        raise ValueError('ROS image stamp is ahead of the subscriber clock; check clock/domain configuration')
    return max(0, age)


def frame_deadline(now, previous, fps):
    """Rate-limit conversion without resetting the phase on every arrival.

    A late arrival may use one frame of credit; missed intervals never queue up.
    Resetting to now + period drops alternate frames under ordinary source jitter.
    """
    return None if now < previous else max(previous + 1 / fps, now)


def image_to_nv12(message, width, height):
    import cv2
    import numpy as np
    channels = {'bgr8': 3, 'rgb8': 3, 'mono8': 1}.get(message.encoding)
    if channels is None:
        raise ValueError(f'Unsupported ROS image encoding: {message.encoding}; use bgr8, rgb8 or mono8')
    if not 0 < message.width <= 4096 or not 0 < message.height <= 2160 or not message.width * channels <= message.step <= 32768:
        raise ValueError('Invalid ROS image dimensions or row stride')
    if len(message.data) > 32 * 1024 * 1024 or len(message.data) != message.step * message.height:
        raise ValueError('ROS image data length does not match its row stride')
    rows = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
    image = rows[:, :message.width * channels].reshape(message.height, message.width, channels)
    if (message.width, message.height) != (width, height):
        # Keep robot-rendered boxes and the source geometry in proportion when
        # a square camera and a rectangular camera share one encoder profile.
        scale = min(width / message.width, height / message.height)
        resized_width = max(1, min(width, round(message.width * scale)))
        resized_height = max(1, min(height, round(message.height * scale)))
        resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
        image = np.zeros((height, width, channels), dtype=np.uint8)
        if channels == 1:
            resized = resized.reshape(resized_height, resized_width, 1)
        left, top = (width - resized_width) // 2, (height - resized_height) // 2
        image[top:top + resized_height, left:left + resized_width] = resized
    if message.encoding == 'mono8':
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    conversion = cv2.COLOR_RGB2YUV_I420 if message.encoding == 'rgb8' else cv2.COLOR_BGR2YUV_I420
    planar = cv2.cvtColor(image, conversion).reshape(-1)
    area = width * height
    result = np.empty(area * 3 // 2, dtype=np.uint8)
    result[:area] = planar[:area]
    chroma = result[area:].reshape(-1, 2)
    chroma[:, 0], chroma[:, 1] = planar[area:area * 5 // 4], planar[area * 5 // 4:]
    return result


def ros_worker(device, width, height, fps, pixels, stamp, sequence, lock, stopped, errors, ready):
    node = context = executor = None
    signalled = False
    def request_stop(*_):
        # Keep signal handling free of process-shared locks (see oak_worker).
        nonlocal signalled
        signalled = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        policy = device['policy']
        os.environ.setdefault('ROS_AUTOMATIC_DISCOVERY_RANGE', 'LOCALHOST')
        if policy['ros_domain_id'] is not None:
            os.environ['ROS_DOMAIN_ID'] = str(policy['ros_domain_id'])
        import cv2
        import numpy as np
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.parameter import Parameter
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
        from sensor_msgs.msg import Image
        cv2.setNumThreads(1)
        context = Context()
        # Runtime units do not inherit HOME. This private observer uses neither
        # rosout nor file logging and owns its own signal/shutdown lifecycle.
        context.init(args=[], initialize_logging=False)
        node = Node(f'robot_stream_rgb_{os.getpid()}', context=context, enable_rosout=False,
                    start_parameter_services=False,
                    parameter_overrides=[Parameter('use_sim_time', value=policy['ros_use_sim_time'])])
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        target = np.frombuffer(pixels, dtype=np.uint8)
        next_at = 0.0

        def receive(message):
            nonlocal next_at
            now = time.monotonic()
            deadline = frame_deadline(now, next_at, fps)
            if deadline is None:
                return  # Rate-limit before pixel conversion, resizing or copying.
            stamp_ns = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
            age = frame_age_seconds(stamp_ns, node.get_clock().now().nanoseconds,
                                    policy['ros_clock_tolerance_ms'])
            if age * 1000 > policy['max_source_age_ms']:
                return
            data = image_to_nv12(message, width, height)
            with lock:
                target[:] = data
                stamp.value = now - age
                sequence.value += 1
                ready.set()
            next_at = deadline

        node.create_subscription(Image, device['topic'], receive,
            QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1,
                       reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE))
        while not signalled and not stopped.is_set():
            executor.spin_once(timeout_sec=.1)
    except Exception as error:
        try:
            errors.put_nowait(f'ROS RGB subscription: {error}')
        except Full:
            pass
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=1)
        if node is not None:
            node.destroy_node()
        if context is not None and context.ok():
            context.shutdown()


class RosCapture(OakCapture):
    """Reuse the bounded latest-frame child transport, independently of DepthAI."""
    def __init__(self, device, width, height, fps, policy):
        self.mode = {'provider': 'ROS Image', 'topic': device['topic'], 'ownership': 'subscription'}
        self.timeout_message = f"No fresh RGB images on {device['topic']}; check publisher, ROS domain and acquisition stamps"
        super().__init__(device | {'policy': policy}, width, height, fps, worker=ros_worker)
