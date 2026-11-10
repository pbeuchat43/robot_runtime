// Copyright (c) 2026 Paul N. Beuchat. MIT License.
// Pure storage/visualization contracts; encoder and ROS acceptance are separate.
#include "robot_runtime_recording/recording.hpp"
#include <iostream>
using namespace robot_runtime_recording;
int main(){try{
  sensor_msgs::msg::Image depth;depth.width=3;depth.height=1;depth.step=8;depth.encoding="16UC1";
  depth.data={0,0,250,0,0x98,0x3a,255,255};
  auto rgb=colorize(depth,.25,15);
  require(rgb==std::vector<uint8_t>({0,0,0,255,0,0,0,0,255}),"fixed palette/invalid/stride contract failed");
  auto directory=fs::temp_directory_path()/("recording-contract-"+token());fs::create_directory(directory);
  Json value={{"sequence","18446744073709551615"},{"time","1790000000000000001"}};
  atomic_json(directory/"state.json",value);require(read_json(directory/"state.json")==value,"lossless JSON failed");
  fs::create_symlink(directory/"state.json",directory/"link");bool rejected=false;
  try{read_json(directory/"link");}catch(...){rejected=true;}require(rejected,"symlink input accepted");
  fs::remove(directory/"link");fs::remove(directory/"state.json");
  Json config={{"schema_version",1},{"root",(directory/"history").string()},{"outputs",{"rgb"}},
      {"bitrates",{{"rgb",1000000}}},{"source",{{"backend","oak"},{"source_id","oak:fixture"},
      {"width",640},{"height",640},{"frame_topic","/fixture/frames"}}}};
  std::ifstream boot_file("/proc/sys/kernel/random/boot_id");std::string boot;boot_file>>boot;
  {
    Session session(Settings(config),std::string(32,'c'),directory/"source-state.json");
    uint64_t sequence=0;
    auto source=[&](const std::string& generation,const std::string& calibration,double focal,bool duplicate=false) {
      const auto acquired=now_ns()-1000000;
      Json capture={{"source_id","oak:fixture"},{"generation",generation},{"clock_domain","linux_monotonic:"+boot},
        {"stream_id","rgbd"},{"sequence",std::to_string(duplicate?sequence:++sequence)},
        {"acquisition_ns",std::to_string(acquired)},{"received_ns",std::to_string(acquired+1000)},{"unix_offset_ns","0"}};
      return session.source(capture,calibration,{{"width",640},{"height",640},{"frame_id","fixture"},
        {"k",{focal,0.,320.,0.,focal,320.,0.,0.,1.}}},acquired);
    };
    require(source("first","calibration-a",600.),"initial source rejected");
    require(!source("first","calibration-a",600.,true),"duplicate capture identity admitted");
    require(!source("first","calibration-b",601.),"calibration changed within generation");
    require(source("second","calibration-b",601.),"new session calibration rejected");
    require(!source("first","calibration-a",600.),"retired generation admitted");
    session.stop();
    auto state=read_json(directory/"source-state.json");std::ifstream events(fs::path(state["output_directory"].get<std::string>())/"events.jsonl");
    std::string line;size_t generations=0;
    while(std::getline(events,line)){auto event=Json::parse(line);if(event["event"]=="generation"){
      require(event.contains("geometry")&&event.contains("calibration_id"),"generation provenance missing");++generations;}}
    require(generations==2,"Stop lost pending generation events");
  }
  fs::remove_all(directory);
  std::cout<<"recording contracts passed (software only)\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
