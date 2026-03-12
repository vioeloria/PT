# 这些脚本需要搭配vertex使用 自带更新vertex cookies 部分是可采用明文密码写入配置部分需要自己手动格式化为md5

## 主要实现功能是 
- hetzner cloud定时删除服务器/ 达量重建服务器 自动更新vertex下载器ip
- 监测netcup服务器限速情况 自动禁用/恢复 vertex下载器 （适用于新旧款VPS和RSG11 G12系列 G9.5系列通常直接限到200Mbps）
- 监控Hostdzire的服务器流量情况 禁用下载器流量重置后启用 防止用超扣钱（虽然商家现在似乎达量后给把机器暂停 以防万一）
- 基于guoshifu的autobrr负载均衡改 通过填写vertex对应下载器id 更方便去负载
- u2的免费种（基于网页）和魔法种抓取 转换为rss xml读取
- vertex-configedit 批量修改vertex rss/downloader参数



