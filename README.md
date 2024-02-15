#### auto_del.py
deluge 客户端自动删种
#### auto_magic_seeds.py
给有上传速度的种子放魔法(支持各种 bt 客户端)
#### catch_magic.py
追魔/搭桥(不限客户端)
#### download_new_torrents.py
自动下载新种
#### find_torrent.py
根据文件名反查种子并添加到 qb 客户端
#### give_sugar.py
发糖
#### my_bencoder.py
自用 bencode 格式编码与解码
#### qb_del.py
从 qb 客户端删除赚分效率不高的种子
#### rename_torrents.py
按种子标题重命名种子文件(仅支持 qb)
#### u2_auxseed.py
u2辅种(仅支持 qb)
#### u2_magic.py
放魔法/限速(支持客户端 qb 和 de)

### ubuntu/debian 系统下编译安装 python3.11
输入以下命令 
```
sudo apt -y install build-essential zlib1g zlib1g-dev libffi-dev libc6-dev libssl-dev libbz2-dev libncurses5-dev libgdbm-dev libgdbm-compat-dev liblzma-dev uuid-dev sqlite3 libsqlite3-dev libssl-dev tcl8.6-dev tk8.6-dev libreadline-dev zlib1g-dev   
wget https://www.python.org/ftp/python/3.11.0/Python-3.11.0.tgz  
tar zxvf Python-3.11.0.tgz && cd Python-3.11.0  
./configure --prefix=/usr/local/python3.11 --enable-optimizations --enable-shared  # 不指定安装目录，会覆盖系统的 python3  
make -j$(nproc) # 注意查看是否有模块报错，如果有一般是缺少依赖项，网上搜一下   
sudo make install  
sudo ln -s /usr/local/python3.11/lib/libpython3.11.so.1.0 /usr/lib/libpython3.11.so.1.0  
sudo ln -s /usr/local/python3.11/bin/python3.11 /usr/bin/python3.11  
sudo ln -s /usr/local/python3.11/bin/pip3.11 /usr/bin/pip3.11  
```
然后就可以使用命令 python3.11 和 pip3.11
