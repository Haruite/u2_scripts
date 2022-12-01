# u2_scripts
**使用方法：安装 python，安装模块，修改相关选项，运行**

## 安装 python
需要 python3.6+，部分脚本需要 python3.7+，Windows 系统前往官网下载安装包(选择添加到 PATH，把能勾的都选上)，
linux 下输入 python3 --version，如果版本小于 3.6，请自行编译安装
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
然后就可以使用命令 python3.11 和 pip3.11，不建议覆盖系统的 python3   
如果激活了虚拟环境，使用命令 python3 和 pip3 即可。windows 下的命令就是 python 和 pip  
在某些很老的系统上可能因为相关依赖版本太低而报错，也需要编译，示例如下(选择合适版本)  
```
# ------------编译 gcc---------------
wget https://ftp.gnu.org/gnu/gcc/gcc-8.5.0/gcc-8.5.0.tar.gz
tar -zxvf gcc-8.5.0.tar.gz && cd gcc-8.5.0
./contrib/download_prerequisites
mkdir build && cd build
../configure --enable-checking=release --enable-languages=c,c++ --disable-multilib
make -j$(nproc) && sudo make install
# ------------编译 openssl---------------
wget https://www.openssl.org/source/old/1.1.1/openssl-1.1.1l.tar.gz
tar zxvf openssl-1.1.1l.tar.gz && cd openssl-1.1.1l
./config && make && sudo make install
sudo echo "export LD_LIBRARY_PATH=/usr/local/lib">>/etc/profile
sudo source /etc/profile
openssl version  # OpenSSL 1.1.1l  24 Aug 2021
# ------------编译 zlib---------------
wget https://www.zlib.net/zlib-1.2.11.tar.gz && tar zxvf zlib-1.2.11.tar.gz && cd zlib-1.2.11
./configure && make && make install
sudo ln -s /usr/local/lib/libz.so /usr/lib/x86_64-linux-gnu/libz.so
sudo ln -s /usr/local/lib/libz.so.1.2.11 /usr/lib/x86_64-linux-gnu/libz.so.1.2.11
sudo ln -s /usr/local/lib/libz.a /usr/lib/x86_64-linux-gnu/libz.a
```

## 安装脚本需要的第三方模块
先激活虚拟环境(当然，不用虚拟环境也行)
```
python3 -m venv venv0 # 或者 python3.x，取决于安装的版本
source venv0/bin/activate  
```
然后安装模块
```
pip3 install ... # 具体有哪些模块请查看脚本说明，多个用空格分开，如果有报错根据报错信息自行安装
```

## 修改选项和运行
选项一般在文件开头 import 语句之后，由一组赋值语句给定，如果需要爬网页 cookie 是必填的，其他自行参考注释  
运行输入(长时间远程运行使用 screen)  
```
python3 xxx.py  
```
