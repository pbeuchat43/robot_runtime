// Copyright (c) 2026 Paul N. Beuchat. MIT License.
#include "robot_runtime_recording/recording.hpp"
#include <rclcpp/rclcpp.hpp>
#include <rgbd_camera_interfaces/msg/frame.hpp>
#include <rgbd_object_detector_interfaces/msg/overlay_frame.hpp>
#include <rgbd_object_detector_interfaces/msg/combined_overlay_frame.hpp>
#include <iostream>
#include <limits>

namespace rr=robot_runtime_recording;
using Frame=rgbd_camera_interfaces::msg::Frame;
using Capture=rgbd_camera_interfaces::msg::CaptureReference;
using Detections=rgbd_object_detector_interfaces::msg::CaptureDetections;
using Overlay=rgbd_object_detector_interfaces::msg::OverlayFrame;
using Combined=rgbd_object_detector_interfaces::msg::CombinedOverlayFrame;
int64_t stamp_ns(const std_msgs::msg::Header& h) {return int64_t(h.stamp.sec)*1000000000LL+h.stamp.nanosec;}
int64_t mapped_stamp(int64_t acquired, int64_t offset) {
  rr::require(acquired > 0 && offset <= std::numeric_limits<int64_t>::max() - acquired,
      "invalid acquisition epoch mapping");
  return acquired + offset;
}
bool valid_image(const sensor_msgs::msg::Image& image,const std::string& encoding,int width,int height,int64_t stamp) {
  const auto channels=encoding=="bgr8"?3:2;
  return image.width==uint32_t(width)&&image.height==uint32_t(height)&&image.encoding==encoding&&!image.is_bigendian&&
    image.step==uint32_t(width*channels)&&image.data.size()==size_t(image.step)*image.height&&stamp_ns(image.header)==stamp;
}
rr::Json capture_json(const Capture& c) {
  rr::require(c.schema_version==1&&c.stream_id=="rgbd"&&c.source_id.size()<=256&&c.generation.size()<=128&&c.clock_domain.size()<=128,"invalid capture identity");
  return {{"schema_version",c.schema_version},{"source_id",c.source_id},{"generation",c.generation},{"stream_id",c.stream_id},
    {"sequence",std::to_string(c.sequence)},{"acquisition_ns",std::to_string(c.acquisition_ns)},{"received_ns",std::to_string(c.received_ns)},
    {"clock_domain",c.clock_domain},{"unix_offset_ns",std::to_string(c.unix_offset_ns)}};
}
rr::Json provenance(const Detections& d) {
  rr::require(d.schema_version==1&&d.model_id.size()<=128&&d.model_sha256.size()==64&&d.model_profile_sha256.size()==64&&d.engine_sha256.size()==64&&
    d.preprocessing.size()<=128&&d.inference_backend.size()<=128,"invalid detector provenance");
  return {{"model_id",d.model_id},{"model_sha256",d.model_sha256},{"model_profile_sha256",d.model_profile_sha256},{"engine_sha256",d.engine_sha256},
    {"preprocessing",d.preprocessing},{"inference_backend",d.inference_backend},{"model_width",d.model_width},{"model_height",d.model_height},
    {"model_to_source",{d.model_to_source_x,d.model_to_source_y,d.model_to_source_offset_x,d.model_to_source_offset_y}},
    {"score_threshold",d.score_threshold},{"iou_threshold",d.iou_threshold},{"completed_ns",std::to_string(d.completed_ns)}};
}
class Recorder : public rclcpp::Node {
 public:
  Recorder():Node("recording") {
    const auto file=declare_parameter<std::string>("config_file","");
    const auto run=declare_parameter<std::string>("run_id","");
    const auto status=declare_parameter<std::string>("status_file","");
    rr::require(!get_parameter("use_sim_time").as_bool(),"physical recording cannot use simulated time");
    rr::Settings settings(rr::read_json(file));
    session_=std::make_unique<rr::Session>(std::move(settings),run,status);
    const auto& s=session_->settings();const auto qos=rclcpp::SensorDataQoS().keep_last(1);
    camera_=create_subscription<Frame>(s.frame_topic,qos,[this](Frame::ConstSharedPtr frame){camera(std::move(frame));});
    if(std::find(s.outputs.begin(),s.outputs.end(),"annotated")!=s.outputs.end()) {
      if(s.overlay_kind=="combined")combined_=create_subscription<Combined>(s.overlay_topic,qos,[this](Combined::ConstSharedPtr m){
        try {rr::require(m->capture_detections[0].capture==m->capture_detections[1].capture&&m->capture_detections[0].calibration_id==m->capture_detections[1].calibration_id,"combined capture mismatch");
          overlay(m->capture_detections[0],std::shared_ptr<const sensor_msgs::msg::Image>(m,&m->image),rr::Json::array({provenance(m->capture_detections[0]),provenance(m->capture_detections[1])}));
        }catch(const std::exception& e){session_->reject("annotated",e.what());}});
      else overlay_=create_subscription<Overlay>(s.overlay_topic,qos,[this](Overlay::ConstSharedPtr m){
        try{overlay(m->capture_detections,std::shared_ptr<const sensor_msgs::msg::Image>(m,&m->image),rr::Json::array({provenance(m->capture_detections)}));}
        catch(const std::exception& e){session_->reject("annotated",e.what());}});
    }
    timer_=create_wall_timer(std::chrono::milliseconds(100),[this]{if(session_->stopping())rclcpp::shutdown();});
  }
  bool finish(){session_->stop();return session_->successful();}
 private:
  void camera(Frame::ConstSharedPtr f) {
    try {
      const auto& s=session_->settings();
      rr::require(valid_image(f->rgb,"bgr8",s.width,s.height,mapped_stamp(f->capture.acquisition_ns,f->capture.unix_offset_ns))&&
        valid_image(f->depth,"16UC1",s.width,s.height,mapped_stamp(f->depth_acquisition_ns,f->capture.unix_offset_ns))&&
        f->rgb_info.width==f->rgb.width&&f->rgb_info.height==f->rgb.height&&f->depth_info.k==f->rgb_info.k&&
        f->rgb_info.header==f->rgb.header&&f->depth_info.header==f->depth.header&&
        f->alignment=="aligned_to_rgb"&&f->depth_convention=="optical_z"&&f->depth_unit_m==.001&&f->invalid_depth_value==0&&
        f->calibration_id.size()==64&&f->rgb_info.d.size()<=16&&f->rgb.header.frame_id.size()<=256,"incompatible RGB-D profile");
      const auto c=capture_json(f->capture);
      rr::Json geometry={{"width",s.width},{"height",s.height},{"k",f->rgb_info.k},{"d",f->rgb_info.d},{"frame_id",f->rgb.header.frame_id}};
      rr::require(session_->source(c,f->calibration_id,geometry,f->depth_acquisition_ns),"source identity, geometry or time rejected");
      rr::Json metadata={{"capture",c},{"calibration_id",f->calibration_id},{"depth_sequence",std::to_string(f->depth_sequence)},
        {"depth_acquisition_ns",std::to_string(f->depth_acquisition_ns)},{"skew_ns",std::to_string(f->skew_ns)},{"backend",f->backend}};
      session_->admit("rgb",std::shared_ptr<const sensor_msgs::msg::Image>(f,&f->rgb),metadata,f->capture.acquisition_ns);
      session_->admit("depth",std::shared_ptr<const sensor_msgs::msg::Image>(f,&f->depth),std::move(metadata),f->depth_acquisition_ns);
    }catch(const std::exception& e){for(const auto& name:session_->settings().outputs)session_->reject(name,e.what());}
  }
  void overlay(const Detections& d,std::shared_ptr<const sensor_msgs::msg::Image> image,rr::Json models) {
    const auto& s=session_->settings();auto c=capture_json(d.capture);
    rr::require(d.source_width==uint32_t(s.width)&&d.source_height==uint32_t(s.height)&&
      valid_image(*image,"bgr8",s.width,s.height,mapped_stamp(d.capture.acquisition_ns,d.capture.unix_offset_ns))&&
      session_->matches(c,d.calibration_id),"overlay source incompatible or unavailable");
    session_->admit("annotated",std::move(image),{{"capture",c},{"calibration_id",d.calibration_id},{"models",models},
      {"depth_sequence",std::to_string(d.depth_sequence)},{"depth_acquisition_ns",std::to_string(d.depth_acquisition_ns)}},d.capture.acquisition_ns);
  }
  std::unique_ptr<rr::Session> session_;
  rclcpp::Subscription<Frame>::SharedPtr camera_;
  rclcpp::Subscription<Overlay>::SharedPtr overlay_;
  rclcpp::Subscription<Combined>::SharedPtr combined_;
  rclcpp::TimerBase::SharedPtr timer_;
};
int main(int argc,char** argv) {
  gst_init(nullptr,nullptr);rclcpp::init(argc,argv);
  try {
    auto node=std::make_shared<Recorder>();rclcpp::spin(node);
    // A blocked filesystem/driver teardown cannot hold an explicit Stop forever.
    // This guard belongs to the standalone executable, never the reusable core.
    std::mutex mutex;std::condition_variable changed;bool finished=false;
    std::thread guard([&]{std::unique_lock<std::mutex> lock(mutex);if(!changed.wait_for(lock,std::chrono::seconds(12),[&]{return finished;})){
      std::cerr<<"recorder finalization deadline exceeded; session remains interrupted"<<std::endl;std::_Exit(3);}});
    const bool success=node->finish();node.reset();{std::lock_guard<std::mutex> lock(mutex);finished=true;}changed.notify_one();guard.join();
    if(rclcpp::ok())rclcpp::shutdown();
    return success ? 0 : 2;
  }catch(const std::exception& error){std::cerr<<error.what()<<std::endl;if(rclcpp::ok())rclcpp::shutdown();return 2;}
}
