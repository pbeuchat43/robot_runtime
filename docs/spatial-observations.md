# Spatial and IMU observations

The reference browser renders declared cone, laser-scan, and fiducial projections in a shared metric 2D/3D view. IMU and diagnostic capabilities use the same bounded observation path. Start each provider explicitly and select its observations; neither viewing nor enabling a layer launches a sensor.

Every spatial capability declares its source/display frames, units, acquisition clock, freshness, and size/rate limits. Laser and marker projections apply only declared rigid transforms. Unsupported frames, malformed geometry, and invalid timestamps produce explicit unavailable/invalid states. A fresh empty observation clears old geometry; stale data is not relabeled as fresh when another stream updates.

Cone projections expose configured position, color, confidence, and acquisition metadata. Scan projections bound the number of returned points, reject invalid ranges, and report truncation/selection semantics. Fiducials carry dictionary, ID, physical size, pose validity, and reprojection error; repeated IDs remain separate observations. The view does not claim persistent tracking or an obstacle-complete scene.

IMU data retains its declared frame, provider accuracy, orientation/motion values, and acquisition/receipt freshness. Invalid or unavailable orientation is not presented as a valid attitude. Provider-specific accuracy semantics belong to the integration contract.

Consumers share one admitted upstream subscription per capability. Hiding, pausing, or removing the final consumer releases optional demand. Camera consumers must use a declared shared acquisition path or explicit mutually exclusive provider configurations; a visualization does not independently open hardware.

Profiles and public ROS type mappings are described in the [ROS guide](ros-operator-guide.md) and [cone projection contract](cone-observations.md). Independent synthetic observation, IMU, lidar, and fiducial fixtures are documented in the [test runbook](../tests/README.md#spatial-observations). Their messages are test data, not the proposed camera/detector API in the [research architecture](research-platform-architecture.md).
