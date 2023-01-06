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
