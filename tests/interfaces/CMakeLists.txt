# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
cmake_minimum_required(VERSION 3.8)
project(robot_runtime_test_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ConeDetection.msg" "msg/ConeDetections.msg"
  "msg/FiducialDetection.msg" "msg/FiducialDetections.msg"
  DEPENDENCIES std_msgs geometry_msgs)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
