// Copyright (c) 2026 Paul N. Beuchat. MIT License.
// Synthetic private filesystem fixtures. No camera or encoder is opened.
#include "robot_runtime_recording/recording.hpp"
#include <iostream>
using namespace robot_runtime_recording;
void fixture(const fs::path& root,const std::string& name,const std::string& state,size_t bytes) {
  auto path=root/name;fs::create_directory(path);
  atomic_json(path/"manifest.json",{{"owner","robot_runtime_recording"},{"session_id",name},{"state",state},{"finalization","complete"}});
  std::ofstream file(path/"rgb-1-0.mp4");file.seekp(bytes-1);file.put('x');
}
void wait_status(const fs::path& path) {
  const auto deadline=now_ns()+3000000000LL;
  while(!fs::exists(path)&&now_ns()<deadline)std::this_thread::sleep_for(std::chrono::milliseconds(10));
  require(fs::exists(path),"status deadline exceeded");
}
int main(){try {
  const auto directory=fs::temp_directory_path()/("recording-retention-"+token());
  fs::create_directory(directory);auto root=directory/"history";fs::create_directory(root);
  Json config={{"schema_version",1},{"root",root.string()},{"outputs",{"rgb"}},{"budget_bytes",8000000},{"reserve_bytes",1000000},
    {"bitrates",{{"rgb",1000000}}},{"source",{{"backend","oak"},{"source_id","oak:fixture"},{"width",640},{"height",640},{"frame_topic","/fixture/frames"}}}};
  fixture(root,"session-1000000000-old","completed",6000000);
  fixture(root,"session-1000000001-active","recording",4000000);
  std::ofstream(root/"unrelated.txt")<<"keep me";
  fixture(root,"session-1000000002-linked","completed",1);
  fs::create_symlink(root/"unrelated.txt",root/"session-1000000002-linked/depth-1-0.mp4");
  {
    Session session(Settings(config),std::string(32,'a'),directory/"first.json");wait_status(directory/"first.json");session.stop();
    require(!fs::exists(root/"session-1000000000-old"),"old completed session not reclaimed");
    require(fs::exists(root/"session-1000000001-active/rgb-1-0.mp4")&&fs::exists(root/"unrelated.txt")&&fs::is_symlink(root/"session-1000000002-linked/depth-1-0.mp4"),"protected content changed");
    require(read_json(directory/"first.json")["state"]=="completed","reclamation failed");
  }
  config["budget_bytes"]=3000000;
  {
    Session session(Settings(config),std::string(32,'b'),directory/"full.json");wait_status(directory/"full.json");session.stop();
    auto result=read_json(directory/"full.json");
    require(result["state"]=="failed"&&result["failure"].get<std::string>().find("storage_stop")!=std::string::npos,"quota exhaustion not explicit");
    require(result["finalization"]=="complete","storage failure erased successful file finalization");
    require(fs::exists(root/"session-1000000001-active/rgb-1-0.mp4"),"active session evicted");
  }
  fs::remove_all(directory);std::cout<<"recording retention contracts passed (synthetic)\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
