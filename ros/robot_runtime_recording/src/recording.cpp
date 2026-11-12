// Copyright (c) 2026 Paul N. Beuchat. MIT License.
#include "robot_runtime_recording/recording.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <fcntl.h>
#include <regex>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace robot_runtime_recording {
namespace {
void increment(Json& counters, const char* key, uint64_t amount = 1) {
  counters[key] = std::to_string(std::stoull(counters.at(key).get<std::string>()) + amount);
}
void write_bytes(const fs::path& path, const std::string& bytes, bool append = false) {
  const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC | (append ? O_APPEND : O_EXCL), 0600);
  require(fd >= 0, "cannot open output: " + path.string());
  try {
    size_t offset = 0;
    while (offset < bytes.size()) {
      const auto count = ::write(fd, bytes.data()+offset, bytes.size()-offset);
      if (count < 0 && errno == EINTR) continue;
      require(count > 0, "output write failed: " + path.string()); offset += count;
    }
    require(::fsync(fd) == 0, "output flush failed: " + path.string());
  } catch (...) { ::close(fd); throw; }
  ::close(fd);
}
void sync_file(const fs::path& path) {
  int fd = ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  require(fd >= 0, "finalized video unavailable");
  const int result = ::fsync(fd); ::close(fd); require(result == 0, "video flush failed");
}
void sync_directory(const fs::path& path) {
  const int fd=::open(path.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC);
  require(fd>=0,"output directory unavailable");
  const int result=::fsync(fd);::close(fd);require(result==0,"directory flush failed");
}
int64_t integer(const Json& value) { return std::stoll(value.get<std::string>()); }
bool safe_text(const std::string& s, size_t maximum=256) {
  return !s.empty() && s.size() <= maximum && s.find('\0') == std::string::npos;
}
}
int64_t now_ns() { return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count(); }
void require(bool value, const std::string& reason) { if (!value) throw std::runtime_error(reason); }
std::string token() { std::ifstream f("/proc/sys/kernel/random/uuid"); std::string s; f >> s; require(s.size()==36,"UUID unavailable"); return s; }
Json read_json(const fs::path& path, size_t limit) {
  int fd = ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
  require(fd >= 0, "JSON input unavailable: " + path.string());
  struct stat st{};
  if (::fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_size < 0 || uint64_t(st.st_size)>limit) {
    ::close(fd); throw std::runtime_error("invalid JSON file");
  }
  std::string raw(size_t(st.st_size), '\0'); size_t at=0;
  while (at<raw.size()) { auto n=::read(fd,raw.data()+at,raw.size()-at); if(n<0&&errno==EINTR)continue;
    if(n<=0) {::close(fd);throw std::runtime_error("short JSON read");} at+=n; }
  ::close(fd); return Json::parse(raw);
}
void atomic_json(const fs::path& path, const Json& value) {
  auto temporary = path; temporary += ".tmp-" + token();
  try { write_bytes(temporary,value.dump()+"\n"); fs::rename(temporary,path);
    sync_directory(path.parent_path());
  } catch (...) { std::error_code ignored; fs::remove(temporary,ignored); throw; }
}
void append_json(const fs::path& path,const Json& value) {write_bytes(path,value.dump()+"\n",true);}

Settings::Settings(Json value) : json(std::move(value)) {
  require(json.at("schema_version")==1,"unsupported recording settings");
  root=json.at("root").get<std::string>();
  require(root.is_absolute() && root==root.lexically_normal() && root!=root.root_path(),"recording root must be an absolute managed directory");
  const auto& source=json.at("source");
  backend=source.at("backend"); source_id=source.at("source_id"); frame_topic=source.at("frame_topic");
  overlay_topic=source.value("overlay_topic",""); overlay_kind=source.value("overlay_kind","");
  width=source.at("width");height=source.at("height");
  require((width==1280&&height==720)||(width==640&&(height==360||height==480||height==640)),"unsupported recording dimensions");
  require(backend=="oak"||backend=="realsense","unsupported recording backend");
  require(source_id.size()<=256&&(source_id.empty()||source_id.rfind(backend+":",0)==0),"source identity/backend mismatch");
  require(std::regex_match(frame_topic,std::regex("/[A-Za-z0-9_/]{1,255}")),"invalid camera topic");
  outputs=json.at("outputs").get<std::vector<std::string>>();
  std::sort(outputs.begin(),outputs.end());
  require(!outputs.empty()&&outputs.size()<=3&&std::adjacent_find(outputs.begin(),outputs.end())==outputs.end(),"invalid output selection");
  for(const auto& output:outputs) require(output=="rgb"||output=="depth"||output=="annotated","unknown output");
  if(std::find(outputs.begin(),outputs.end(),"annotated")!=outputs.end())
    require((overlay_kind=="single"||overlay_kind=="combined")&&std::regex_match(overlay_topic,std::regex("/[A-Za-z0-9_/]{1,255}")),"annotated source unavailable");
  fps=json.value("fps",30); segment_seconds=json.value("segment_seconds",30); frame_index=json.value("frame_index",true);
  require(fps>=1&&fps<=30&&segment_seconds>=2&&segment_seconds<=60,"invalid recording timing");
  budget_bytes=json.value("budget_bytes",uint64_t(10000000000));reserve_bytes=json.value("reserve_bytes",uint64_t(1000000000));
  require(budget_bytes>=1000000&&budget_bytes<=1000000000000ULL&&reserve_bytes>=1000000&&reserve_bytes<=1000000000000ULL,"invalid storage limits");
  min_depth_m=json.value("min_depth_m",0.25);max_depth_m=json.value("max_depth_m",15.0);
  require(std::isfinite(min_depth_m)&&std::isfinite(max_depth_m)&&min_depth_m>0&&max_depth_m>min_depth_m&&max_depth_m<=65.535,"invalid depth display range");
  for(const auto& output:outputs) {const auto bitrate=json.at("bitrates").at(output).get<int>();require(bitrate>=100000&&bitrate<=20000000,"invalid bitrate");}
}

std::vector<uint8_t> colorize(const sensor_msgs::msg::Image& image,double minimum,double maximum) {
  require(image.encoding=="16UC1"&&!image.is_bigendian&&image.step>=image.width*2&&image.data.size()==size_t(image.step)*image.height,"invalid depth image");
  // Fixed blue -> cyan -> yellow -> red palette. Zero/invalid is black;
  // positive depths outside the fixed range saturate at its endpoints.
  std::array<std::array<uint8_t,3>,65536> palette{};
  const double low=minimum*1000,scale=1/(maximum*1000-low);
  for(size_t raw=1;raw<palette.size();++raw) {
    const double t=std::clamp((raw-low)*scale,0.0,1.0)*3;
    const double r=std::clamp(t-1,0.0,1.0),g=std::min(std::clamp(t,0.0,1.0),std::clamp(3-t,0.0,1.0));
    const double b=std::clamp(2-t,0.0,1.0);
    palette[raw]={uint8_t(std::lround(b*255)),uint8_t(std::lround(g*255)),uint8_t(std::lround(r*255))};
  }
  std::vector<uint8_t> result(size_t(image.width)*image.height*3);
  for(uint32_t y=0;y<image.height;++y)for(uint32_t x=0;x<image.width;++x) {
    const auto p=size_t(y)*image.step+x*2; const auto raw=uint16_t(image.data[p])|(uint16_t(image.data[p+1])<<8);
    std::copy(palette[raw].begin(),palette[raw].end(),result.begin()+(size_t(y)*image.width+x)*3);
  }
  return result;
}

Track::Track(const Settings& settings,fs::path session,std::string name)
    :settings_(settings),session_(std::move(session)),name_(std::move(name)) {thread_=std::thread([this]{run();});}
Track::~Track(){stop();join();}
void Track::reject(const std::string& reason){std::lock_guard<std::mutex> lock(mutex_);increment(counters_,"received");increment(counters_,"rejected");last_rejection_=reason;}
void Track::admit(Input input) {
  std::lock_guard<std::mutex> lock(mutex_);increment(counters_,"received");
  if(stopping_||!failure_.empty()){increment(counters_,"rejected");return;}
  last_received_=now_ns();
  if(input.acquired_ns<=last_acquired_||input.acquired_ns<input.origin_ns){increment(counters_,"rejected");last_rejection_="timestamp_regressed";return;}
  last_acquired_=input.acquired_ns;
  if(input.acquired_ns<next_admission_){increment(counters_,"rate_dropped");return;}
  const int64_t period=1000000000LL/settings_.fps;
  if(!next_admission_)next_admission_=input.acquired_ns;
  next_admission_+=((input.acquired_ns-next_admission_)/period+1)*period;
  if(queue_.size()>=2){increment(counters_,"overload_dropped");return;}
  desired_epoch_=input.epoch;queue_.push_back(std::move(input));increment(counters_,"admitted");changed_.notify_one();
}
void Track::rotate(uint64_t epoch){std::lock_guard<std::mutex> lock(mutex_);desired_epoch_=epoch;increment(counters_,"discarded_on_gap",queue_.size());queue_.clear();changed_.notify_one();}
void Track::stop(){std::lock_guard<std::mutex> lock(mutex_);stopping_=true;changed_.notify_one();}
void Track::join(){if(thread_.joinable())thread_.join();}
Json Track::status() const {std::lock_guard<std::mutex> lock(mutex_);
  Json recent=Json::array();for(size_t i=completed_.size()>32?completed_.size()-32:0;i<completed_.size();++i)recent.push_back(completed_[i]);
  return Json{{"state",state_},{"failure",failure_},{"last_rejection",last_rejection_},
  {"counters",counters_},{"queue_depth",queue_.size()},{"last_received_ns",std::to_string(last_received_)},
  {"finalizing_since_ns",std::to_string(finalizing_since_)},{"segments",recent},{"completed_segments",std::to_string(completed_.size())},{"done",done_}};}
void Track::fail(const std::string& failure){std::lock_guard<std::mutex> lock(mutex_);if(failure_.empty())failure_=failure.substr(0,512);state_="failed";}

gchar* Track::location(GstElement*,guint number,GstSample* sample,gpointer user) {
  auto& self=*static_cast<Track*>(user);
  auto* buffer=sample?gst_sample_get_buffer(sample):nullptr;
  if(!buffer||!GST_BUFFER_PTS_IS_VALID(buffer)){self.fail("segment has no presentation timestamp");return nullptr;}
  const auto file=self.name_+"-"+std::to_string(self.part_)+"-"+std::to_string(number)+".mp4";
  {std::lock_guard<std::mutex> lock(self.mutex_);self.segments_.push_back({file,GST_BUFFER_PTS(buffer)});}
  return g_strdup((self.session_/file).c_str());
}
GstPadProbeReturn Track::encoded(GstPad*,GstPadProbeInfo* info,gpointer user) {
  if(!(GST_PAD_PROBE_INFO_TYPE(info)&GST_PAD_PROBE_TYPE_BUFFER))return GST_PAD_PROBE_OK;
  auto& self=*static_cast<Track*>(user);auto* buffer=GST_PAD_PROBE_INFO_BUFFER(info);
  if(!GST_BUFFER_PTS_IS_VALID(buffer))return GST_PAD_PROBE_OK; // codec header only
  std::lock_guard<std::mutex> lock(self.mutex_);const auto pts=GST_BUFFER_PTS(buffer);
  auto found=self.pending_.find(pts);
  if(found==self.pending_.end()) {self.failure_="encoder timestamp has no admitted capture";return GST_PAD_PROBE_OK;}
  found->second["pts_ns"]=std::to_string(pts);self.encoded_.emplace(pts,std::move(found->second));self.pending_.erase(found);
  increment(self.counters_,"encoded");return GST_PAD_PROBE_OK;
}
void Track::open(const Input& input) {
  ++part_;epoch_=input.epoch;eos_=false;
  const auto bitrate=settings_.json.at("bitrates").at(name_).get<int>();
  const std::string pipeline="appsrc name=source is-live=true format=time do-timestamp=false block=false max-buffers=2 emit-signals=false "
      "! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! videoconvert ! video/x-raw,format=BGRx "
      "! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc bitrate="+std::to_string(bitrate)+
      " control-rate=1 preset-level=1 num-B-Frames=0 iframeinterval="+std::to_string(settings_.fps)+" idrinterval="+std::to_string(settings_.fps)+
      " insert-sps-pps=true ! h264parse name=encoded config-interval=-1 ! video/x-h264,stream-format=avc,alignment=au "
      "! splitmuxsink name=segments muxer-factory=mp4mux async-finalize=false max-size-bytes=0 max-size-time="+std::to_string(uint64_t(settings_.segment_seconds)*GST_SECOND);
  GError* error=nullptr;pipeline_=gst_parse_launch(pipeline.c_str(),&error);
  if(error){const std::string detail=error->message;g_error_free(error);throw std::runtime_error(detail);}
  require(pipeline_!=nullptr,"encoder pipeline unavailable");
  source_=GST_APP_SRC(gst_bin_get_by_name(GST_BIN(pipeline_),"source"));
  auto* caps=gst_caps_new_simple("video/x-raw","format",G_TYPE_STRING,"BGR","width",G_TYPE_INT,settings_.width,
      "height",G_TYPE_INT,settings_.height,"framerate",GST_TYPE_FRACTION,settings_.fps,1,nullptr);
  gst_app_src_set_caps(source_,caps);gst_caps_unref(caps);
  auto* parser=gst_bin_get_by_name(GST_BIN(pipeline_),"encoded");auto* pad=gst_element_get_static_pad(parser,"src");
  gst_pad_add_probe(pad,GST_PAD_PROBE_TYPE_BUFFER,&Track::encoded,this,nullptr);gst_object_unref(pad);gst_object_unref(parser);
  auto* sink=gst_bin_get_by_name(GST_BIN(pipeline_),"segments");
  g_signal_connect(sink,"format-location-full",G_CALLBACK(&Track::location),this);gst_object_unref(sink);
  bus_=gst_element_get_bus(pipeline_);
  require(gst_element_set_state(pipeline_,GST_STATE_PLAYING)!=GST_STATE_CHANGE_FAILURE,"encoder refused PLAYING");
  {std::lock_guard<std::mutex> lock(mutex_);state_="recording";}
  append_json(session_/"events.jsonl",{{"event","track_started"},{"track",name_},{"part",std::to_string(part_)},
    {"epoch",std::to_string(input.epoch)},{"first_acquisition_ns",std::to_string(input.acquired_ns)}});
}
void Track::push(Input input) {
  if(now_ns()-input.acquired_ns>1000000000LL || gst_app_src_get_current_level_buffers(source_)>=2) {
    std::lock_guard<std::mutex> lock(mutex_);increment(counters_,"overload_dropped");return;
  }
  const auto& image=*input.image;
  require(image.width==uint32_t(settings_.width)&&image.height==uint32_t(settings_.height),"image dimensions changed");
  std::shared_ptr<const std::vector<uint8_t>> pixels;
  if(name_=="depth")pixels=std::make_shared<std::vector<uint8_t>>(colorize(image,settings_.min_depth_m,settings_.max_depth_m));
  else {
    require(image.encoding=="bgr8"&&image.step==image.width*3&&image.data.size()==size_t(image.step)*image.height,"invalid packed BGR image");
    pixels=std::shared_ptr<const std::vector<uint8_t>>(input.image,&image.data);
  }
  auto* owner=new std::shared_ptr<const std::vector<uint8_t>>(pixels);
  auto* buffer=gst_buffer_new_wrapped_full(GST_MEMORY_FLAG_READONLY,const_cast<uint8_t*>(pixels->data()),pixels->size(),0,pixels->size(),owner,
      [](gpointer data){delete static_cast<std::shared_ptr<const std::vector<uint8_t>>*>(data);});
  const uint64_t pts=input.acquired_ns-input.origin_ns;
  GST_BUFFER_PTS(buffer)=pts;GST_BUFFER_DTS(buffer)=GST_CLOCK_TIME_NONE;GST_BUFFER_DURATION(buffer)=GST_SECOND/settings_.fps;
  {std::lock_guard<std::mutex> lock(mutex_);
    require(pending_.size()+encoded_.size()<size_t((settings_.segment_seconds+5)*settings_.fps*2),"encoder/index progress stalled");
    pending_.emplace(pts,std::move(input.metadata));}
  require(gst_app_src_push_buffer(source_,buffer)==GST_FLOW_OK,"encoder input failed");
}
void Track::bus() {
  while(auto* message=gst_bus_pop(bus_)) {
    if(GST_MESSAGE_TYPE(message)==GST_MESSAGE_EOS)eos_=true;
    if(GST_MESSAGE_TYPE(message)==GST_MESSAGE_ERROR) {
      GError* error=nullptr;gchar* debug=nullptr;gst_message_parse_error(message,&error,&debug);
      const std::string detail=error?error->message:"GStreamer failure";g_clear_error(&error);g_free(debug);gst_message_unref(message);throw std::runtime_error(detail);
    }
    if(GST_MESSAGE_TYPE(message)==GST_MESSAGE_ELEMENT) {
      const auto* structure=gst_message_get_structure(message);
      if(gst_structure_has_name(structure,"splitmuxsink-fragment-closed")) {
        const auto* file=gst_structure_get_string(structure,"location");
        std::lock_guard<std::mutex> lock(mutex_);
        for(auto& segment:segments_)if(file&&(session_/segment.file)==fs::path(file))segment.closed=true;
      }
    }
    gst_message_unref(message);
  }
  finish_segments(false);
}
void Track::finish_segments(bool final) {
  struct Write {std::string file,records;Json descriptor;uint64_t count;};std::vector<Write> writes;
  {std::lock_guard<std::mutex> lock(mutex_);
    for(size_t i=0;i<segments_.size();++i) {
      auto& s=segments_[i];if(s.indexed||!s.closed||(i+1==segments_.size()&&!final))continue;
      const auto end=i+1<segments_.size()?segments_[i+1].start:UINT64_MAX;
      Write out{s.file,"",Json{{"file",s.file},{"start_pts_ns",std::to_string(s.start)},{"frames","0"}},0};
      auto it=encoded_.lower_bound(s.start);
      while(it!=encoded_.end()&&it->first<end) {
        it->second["segment"]=s.file;it->second["segment_frame"]=std::to_string(out.count++);
        it->second["segment_pts_ns"]=std::to_string(it->first-s.start);
        if(settings_.frame_index)out.records+=it->second.dump()+"\n";
        out.descriptor["last_pts_ns"]=std::to_string(it->first);it=encoded_.erase(it);
      }
      out.descriptor["frames"]=std::to_string(out.count);s.indexed=true;writes.push_back(std::move(out));
    }
  }
  for(auto& out:writes) {
    sync_file(session_/out.file);
    if(settings_.frame_index&&!out.records.empty())write_bytes(session_/(name_+".jsonl"),out.records,true);
    out.descriptor["bytes"]=std::to_string(fs::file_size(session_/out.file));
    append_json(session_/(name_+"-segments.jsonl"),out.descriptor);
    sync_directory(session_); // Make video/index directory entries durable too.
    std::lock_guard<std::mutex> lock(mutex_);require(completed_.size()<4096,"segment history limit reached");
    completed_.push_back(std::move(out.descriptor));increment(counters_,"finalized",out.count);
  }
}
void Track::close() {
  if(!pipeline_)return;
  {std::lock_guard<std::mutex> lock(mutex_);finalizing_since_=now_ns();if(failure_.empty())state_="finalizing";}
  try {
    gst_app_src_end_of_stream(source_);const auto deadline=now_ns()+5000000000LL;
    while(!eos_&&now_ns()<deadline) {
      bus();std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    bus();
  } catch(const std::exception& error) { fail(error.what()); }
  if(!eos_)fail("encoder finalization failed or exceeded five seconds");
  gst_element_set_state(pipeline_,GST_STATE_NULL);
  try {finish_segments(true);} catch(const std::exception& error) {fail(error.what());}
  gst_object_unref(source_);gst_object_unref(bus_);gst_object_unref(pipeline_);source_=nullptr;bus_=nullptr;pipeline_=nullptr;
  std::lock_guard<std::mutex> lock(mutex_);
  if(!pending_.empty()||!encoded_.empty()) {if(failure_.empty())failure_="encoded or admitted frames were not finalized";}
  pending_.clear();encoded_.clear();segments_.clear();finalizing_since_=0;state_=failure_.empty()?"waiting_for_source":"failed";
}
void Track::run() {
  try {
    while(true) {
      Input input;bool have=false,rotate=false,stop=false;
      {std::unique_lock<std::mutex> lock(mutex_);changed_.wait_for(lock,std::chrono::milliseconds(30),[&]{return stopping_||!queue_.empty();});
        stop=stopping_;rotate=pipeline_&&(desired_epoch_!=epoch_||(last_received_&&now_ns()-last_received_>2000000000LL));
        if(!failure_.empty())throw std::runtime_error(failure_);
        if(!queue_.empty()){input=std::move(queue_.front());queue_.pop_front();have=true;}}
      if(rotate) {
        {std::lock_guard<std::mutex> lock(mutex_);increment(counters_,"gaps");}
        append_json(session_/"events.jsonl",{{"event","track_gap"},{"track",name_},{"part",std::to_string(part_)},{"observed_ns",std::to_string(now_ns())}});
        close();
      }
      if(have){if(!pipeline_)open(input);push(std::move(input));}
      if(pipeline_)bus();
      if(stop&&!have)break;
    }
    close();
  }catch(const std::exception& error){fail(error.what());try{close();}catch(const std::exception& secondary){fail(secondary.what());}}
  std::lock_guard<std::mutex> lock(mutex_);state_=failure_.empty()?"completed":"failed";done_=true;
}

Session::Session(Settings settings,std::string run,fs::path status)
    :settings_(std::move(settings)),run_id_(std::move(run)),status_file_(std::move(status)) {
  require(std::regex_match(run_id_,std::regex("[a-f0-9]{32}")),"invalid recorder run identity");
  std::ifstream boot("/proc/sys/kernel/random/boot_id");boot>>boot_;require(boot_.size()==36,"boot identity unavailable");
  fs::create_directories(settings_.root);require(fs::canonical(settings_.root)==settings_.root,"recording root traverses a symlink");
  lock_=::open((settings_.root/".recording.lock").c_str(),O_CREAT|O_RDWR|O_NOFOLLOW|O_CLOEXEC,0600);
  require(lock_>=0&&::flock(lock_,LOCK_EX|LOCK_NB)==0,"recording root already owned");
  session_id_="session-"+std::to_string(std::time(nullptr))+"-"+run_id_;session_=settings_.root/session_id_;
  require(fs::create_directory(session_),"recording session already exists");
  fs::permissions(session_,fs::perms::owner_all);
  sync_directory(settings_.root);sync_directory(settings_.root.parent_path());
  fs::create_directories(status_file_.parent_path());
  manifest_={{"schema_version",1},{"owner","robot_runtime_recording"},{"session_id",session_id_},{"run_id",run_id_},{"boot_id",boot_},
    {"settings",settings_.json},{"state","starting"},{"finalization","pending"},{"depth_palette","blue-cyan-yellow-red-v1; invalid=black; endpoints=saturated"},
    {"frame_index",settings_.frame_index},{"exact_frame_reconstruction",settings_.frame_index},{"timeline","source monotonic acquisition; independent track indices"}};
  atomic_json(session_/"manifest.json",manifest_);
  for(const auto& output:settings_.outputs)tracks_[output]=std::make_unique<Track>(settings_,session_,output);
  monitor_=std::thread([this]{monitor();});
}
Session::~Session(){stop();if(lock_>=0)::close(lock_);}
void Session::stop(){stopping_=true;if(monitor_.joinable())monitor_.join();}
bool Session::source(const Json& capture,const std::string& calibration,const Json& geometry,int64_t depth_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  const std::string source=capture.at("source_id"),generation=capture.at("generation"),clock=capture.at("clock_domain");
  const auto acquired=integer(capture.at("acquisition_ns")),received=integer(capture.at("received_ns"));
  if(!safe_text(source)||!safe_text(generation)||clock!="linux_monotonic:"+boot_||source.rfind(settings_.backend+":",0)!=0||
      (!settings_.source_id.empty()&&source!=settings_.source_id)||acquired<=0||depth_ns<=0||received<acquired||received<depth_ns||
      received>now_ns()||now_ns()-std::min(acquired,depth_ns)>1000000000LL||acquired<=last_acquired_)return false;
  const bool changed=capture_.is_null()||generation!=capture_["generation"];
  if(!capture_.is_null()) {
    if(source!=capture_["source_id"])return false;
    // A returning device can report a new calibration for its new session.
    // Keep configured geometry/frame semantics fixed, and rotate before admitting
    // that generation; never change calibration underneath a live generation.
    for(const auto* key:{"width","height","frame_id"})if(geometry.at(key)!=geometry_.at(key))return false;
    if(!changed&&(calibration!=calibration_||geometry!=geometry_||
        std::stoull(capture.at("sequence").get<std::string>())<=std::stoull(capture_.at("sequence").get<std::string>())))return false;
  }
  if(changed) {
    if(std::find(generations_.begin(),generations_.end(),generation)!=generations_.end()||generations_.size()>=256)return false;
    generations_.push_back(generation);
    if(!capture_.is_null()){++epoch_;for(auto& [name,track]:tracks_)track->rotate(epoch_);}
  } else if(capture.at("unix_offset_ns")!=capture_.at("unix_offset_ns"))return false;
  if(!origin_ns_)origin_ns_=std::min(acquired,depth_ns);
  if(changed||waiting_)events_.push_back({{"event",changed?"generation":"source_resumed"},{"epoch",std::to_string(epoch_)},
    {"capture",capture},{"calibration_id",calibration},{"geometry",geometry},{"observed_ns",std::to_string(now_ns())}});
  capture_=capture;calibration_=calibration;geometry_=geometry;last_acquired_=acquired;last_source_=now_ns();waiting_=false;
  return true;
}
bool Session::matches(const Json& capture,const std::string& calibration) {
  std::lock_guard<std::mutex> lock(mutex_);
  if(capture_.is_null()||waiting_||calibration!=calibration_)return false;
  for(const auto* key:{"source_id","generation","stream_id","clock_domain","unix_offset_ns"})if(capture.at(key)!=capture_.at(key))return false;
  const auto acquired=integer(capture.at("acquisition_ns")),received=integer(capture.at("received_ns"));
  return acquired>0&&acquired<=received&&received<=now_ns()&&now_ns()-acquired<=1000000000LL;
}
void Session::admit(const std::string& name,std::shared_ptr<const sensor_msgs::msg::Image> image,Json metadata,int64_t acquired) {
  auto track=tracks_.find(name);if(track==tracks_.end())return;
  std::lock_guard<std::mutex> lock(mutex_);
  if(waiting_||stopping_)return;
  track->second->admit({std::move(image),std::move(metadata),acquired,origin_ns_,epoch_});
}
void Session::reject(const std::string& name,const std::string& reason){auto it=tracks_.find(name);if(it!=tracks_.end())it->second->reject(reason);}
Json Session::snapshot() {
  std::lock_guard<std::mutex> lock(mutex_);Json tracks=Json::object();bool failed=!failure_.empty(),complete=true,files_complete=true;
  for(const auto& [name,track]:tracks_){tracks[name]=track->status();const bool track_failed=!tracks[name]["failure"].get<std::string>().empty();
    failed|=track_failed;files_complete&=!track_failed;complete&=tracks[name]["done"].get<bool>();}
  return {{"schema_version",1},{"run_id",run_id_},{"session_id",session_id_},{"boot_id",boot_},{"heartbeat_ns",std::to_string(now_ns())},
      {"state",complete?(failed?"failed":"completed"):stopping_?"finalizing":waiting_?"waiting_for_source":"recording"},
      {"finalization",complete?(files_complete?"complete":"incomplete"):"pending"},{"failure",failure_},{"output_directory",session_.string()},
      {"bytes",std::to_string(bytes_)},{"managed_bytes",std::to_string(managed_bytes_)},{"source",capture_},{"calibration_id",calibration_},{"geometry",geometry_},
      {"timeline_origin_ns",std::to_string(origin_ns_)},{"outputs",tracks},{"requested",settings_.json}};
}
void Session::storage() {
  struct Candidate {fs::path path;std::vector<fs::path> files;uint64_t bytes;};std::vector<Candidate> completed;
  uint64_t used=0;size_t entries=0;
  const std::regex leaf("(manifest\\.json|state\\.json|events\\.jsonl|(rgb|depth|annotated)(-segments)?\\.jsonl|(rgb|depth|annotated)-[0-9]+-[0-9]+\\.mp4)");
  for(const auto& entry:fs::directory_iterator(settings_.root)) {
    require(++entries<=4096,"recording history entry limit reached");
    if(entry.is_symlink()||!entry.is_directory()||entry.path().filename().string().rfind("session-",0)!=0)continue;
    Json marker;try{marker=read_json(entry.path()/"manifest.json",1048576);}catch(...){continue;}
    if(!marker.is_object()||!marker.contains("owner")||marker["owner"]!="robot_runtime_recording"||
       !marker.contains("session_id")||marker["session_id"]!=entry.path().filename().string())continue;
    Candidate candidate{entry.path(),{},0};bool safe=true;size_t files=0;
    for(const auto& file:fs::directory_iterator(entry.path())) {
      require(++files<=16384,"session file limit reached");
      if(file.is_symlink()||!file.is_regular_file()){safe=false;continue;}
      candidate.bytes+=file.file_size();
      if(!std::regex_match(file.path().filename().string(),leaf))safe=false;
      candidate.files.push_back(file.path());
    }
    used+=candidate.bytes;
    if(entry.path()==session_)bytes_=candidate.bytes;
    if(safe&&entry.path()!=session_&&marker.value("state","")=="completed"&&marker.value("finalization","")=="complete")completed.push_back(std::move(candidate));
  }
  std::sort(completed.begin(),completed.end(),[](const auto& a,const auto& b){return a.path.filename()<b.path.filename();});
  auto available=fs::space(settings_.root).available;
  // Leave one second of maximum admitted encoded data plus metadata before the
  // next check. This is a reserve, not an encoder rate guarantee.
  uint64_t headroom=2000000;for(const auto& name:settings_.outputs)headroom+=settings_.json["bitrates"][name].get<uint64_t>()/8;
  for(auto& old:completed) {
    if(used+headroom<=settings_.budget_bytes&&available>=settings_.reserve_bytes+headroom)break;
    require(fs::canonical(old.path).parent_path()==settings_.root,"unsafe reclamation path");
    for(const auto& file:old.files)require(!fs::is_symlink(file)&&fs::is_regular_file(file),"session changed during reclamation");
    for(const auto& file:old.files)require(fs::remove(file),"cannot reclaim completed recording");
    require(fs::remove(old.path),"completed recording directory changed");used-=old.bytes;available=fs::space(settings_.root).available;
    append_json(session_/"events.jsonl",{{"event","reclaimed"},{"session_id",old.path.filename().string()},{"bytes",std::to_string(old.bytes)}});
  }
  managed_bytes_=used;
  require(used+headroom<=settings_.budget_bytes&&available>=settings_.reserve_bytes+headroom,"storage_stop: managed budget or filesystem reserve exhausted");
}
void Session::monitor() {
  try {
    while(!stopping_) {
      std::deque<Json> events;
      {std::lock_guard<std::mutex> lock(mutex_);
        if(last_source_&&!waiting_&&now_ns()-last_source_>2000000000LL){waiting_=true;++epoch_;for(auto& [name,track]:tracks_)track->rotate(epoch_);
          events_.push_back({{"event","source_gap"},{"observed_ns",std::to_string(now_ns())},{"epoch",std::to_string(epoch_)}});}
        events.swap(events_);}
      for(const auto& event:events)append_json(session_/"events.jsonl",event);
      storage();auto state=snapshot();atomic_json(session_/"state.json",state);atomic_json(status_file_,state);
      bool all_done=true;
      for(const auto& output:state["outputs"]) {
        all_done&=output["done"].get<bool>();
        const auto since=integer(output["finalizing_since_ns"]);
        require(!since||now_ns()-since<8000000000LL,"finalization_stalled: process shutdown required");
      }
      if(all_done)stopping_=true;
      for(int i=0;i<10&&!stopping_;++i)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }catch(const std::exception& error){std::lock_guard<std::mutex> lock(mutex_);failure_=error.what();stopping_=true;}
  for(auto& [name,track]:tracks_)track->stop();
  try{atomic_json(status_file_,snapshot());}catch(...){}
  for(auto& [name,track]:tracks_)track->join();
  auto state=snapshot();
  try {
    std::deque<Json> remaining;
    {std::lock_guard<std::mutex> lock(mutex_);remaining.swap(events_);}
    for(const auto& event:remaining)append_json(session_/"events.jsonl",event);
    manifest_["state"]=state["state"];manifest_["finalization"]=state["finalization"];manifest_["result"]=state;
    atomic_json(session_/"manifest.json",manifest_);atomic_json(session_/"state.json",state);atomic_json(status_file_,state);
  }catch(...) { /* The last durable state remains nonterminal/interrupted. */ }
  done_=true;
}
}  // namespace robot_runtime_recording
