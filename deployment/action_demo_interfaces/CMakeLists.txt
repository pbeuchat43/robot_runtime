# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
cmake_minimum_required(VERSION 3.8)
project(robot_runtime_demo_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME} "action/Count.action")
ament_export_dependencies(rosidl_default_runtime)
ament_package()
