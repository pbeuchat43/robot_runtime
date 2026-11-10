// Copyright (c) 2026 Paul N. Beuchat. MIT License.
#pragma once
#include <nlohmann/json.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <thread>
#include <gst/app/gstappsrc.h>

namespace robot_runtime_recording {
using Json = nlohmann::json;
namespace fs = std::filesystem;
int64_t now_ns();
void require(bool, const std::string&);
Json read_json(const fs::path&, size_t limit = 262144);
void atomic_json(const fs::path&, const Json&);
void append_json(const fs::path&, const Json&);
std::string token();

struct Settings {
  Json json;
  fs::path root;
  std::string backend, source_id, frame_topic, overlay_topic, overlay_kind;
  std::vector<std::string> outputs;
  int width, height, fps, segment_seconds;
  bool frame_index;
  uint64_t budget_bytes, reserve_bytes;
  double min_depth_m, max_depth_m;
  explicit Settings(Json);
};

// A raw image and its original capture metadata. Ownership may alias an atomic
// camera/overlay message; no callback copies its pixel buffer or writes to disk.
struct Input {
  std::shared_ptr<const sensor_msgs::msg::Image> image;
  Json metadata;
  int64_t acquired_ns = 0, origin_ns = 0;
  uint64_t epoch = 0;
};
std::vector<uint8_t> colorize(const sensor_msgs::msg::Image&, double minimum, double maximum);

class Track {
 public:
  Track(const Settings&, fs::path session, std::string name);
  ~Track();
  void admit(Input);
  void rotate(uint64_t epoch);
  void reject(const std::string& reason);
  void stop();
  void join();
  Json status() const;
 private:
  void run();
  void open(const Input&);
  void push(Input);
  void close();
  void bus();
  void finish_segments(bool final);
  void fail(const std::string&);
  static GstPadProbeReturn encoded(GstPad*, GstPadProbeInfo*, gpointer);
  static gchar* location(GstElement*, guint, GstSample*, gpointer);
  const Settings& settings_;
  fs::path session_;
  std::string name_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<Input> queue_;
  std::thread thread_;
  bool stopping_ = false, done_ = false;
  uint64_t desired_epoch_ = 0, epoch_ = 0, part_ = 0;
  int64_t last_acquired_ = 0, next_admission_ = 0, last_received_ = 0;
  Json counters_ = {{"received","0"},{"admitted","0"},{"encoded","0"},{"finalized","0"},
      {"rate_dropped","0"},{"overload_dropped","0"},{"rejected","0"},{"discarded_on_gap","0"},{"gaps","0"}};
  std::string state_ = "waiting_for_source", failure_, last_rejection_;
  GstElement* pipeline_ = nullptr;
  GstAppSrc* source_ = nullptr;
  GstBus* bus_ = nullptr;
  bool eos_ = false;
  int64_t finalizing_since_ = 0;
  std::map<uint64_t, Json> pending_, encoded_;
  struct Segment { std::string file; uint64_t start; bool closed = false, indexed = false; };
  std::vector<Segment> segments_;
  Json completed_ = Json::array();
};

class Session {
 public:
  Session(Settings, std::string run_id, fs::path status_file);
  ~Session();
  // source() binds/validates the camera identity and calibration before any track
  // can admit frames. The adapter calls it even for annotated-only sessions.
  bool source(const Json& capture, const std::string& calibration, const Json& geometry, int64_t depth_ns);
  bool matches(const Json& capture, const std::string& calibration);
  void admit(const std::string& track, std::shared_ptr<const sensor_msgs::msg::Image>, Json metadata, int64_t acquired_ns);
  void reject(const std::string& track, const std::string& reason);
  void stop();
  const Settings& settings() const { return settings_; }
  bool stopped() const { return done_.load(); }
  bool stopping() const { return stopping_.load(); }
  bool successful() { return snapshot()["state"] == "completed"; }
 private:
  void monitor();
  Json snapshot();
  void storage();
  Settings settings_;
  std::string run_id_, boot_, session_id_;
  fs::path session_, status_file_;
  int lock_ = -1;
  std::map<std::string, std::unique_ptr<Track>> tracks_;
  std::mutex mutex_;
  Json manifest_, capture_, geometry_;
  std::string calibration_, failure_;
  std::vector<std::string> generations_;
  int64_t origin_ns_ = 0, last_source_ = 0, last_acquired_ = 0;
  uint64_t epoch_ = 1, bytes_ = 0, managed_bytes_ = 0;
  bool waiting_ = true;
  std::deque<Json> events_;
  std::atomic<bool> stopping_{false}, done_{false};
  std::thread monitor_;
};
}  // namespace robot_runtime_recording
