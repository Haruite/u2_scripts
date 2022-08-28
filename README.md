# u2_scripts
**使用方法：安装 python，安装依赖，修改相关选项，运行**

## 安装 python
需要 python3.6+，Windows 系统前往官网下载安装包，linux 下输入 python3 --version，如果版本小于 3.6，请自行编译安装
### ubuntu/debian 系统下编译 python3.10
输入以下命令 
```
sudo apt -y install build-essential zlib1g zlib1g-dev libffi-dev libc6-dev libssl-dev libbz2-dev libncurses5-dev libgdbm-dev libgdbm-compat-dev liblzma-dev uuid-dev sqlite3 libsqlite3-dev libssl-dev tcl8.6-dev tk8.6-dev libreadline-dev zlib1g-dev  
wget https://www.python.org/ftp/python/3.10.6/Python-3.10.6.tgz  
tar zxvf Python-3.10.6.tgz && cd Python-3.10.6  
./configure --prefix=/usr/lib/python3.10 --enable-optimizations --enable-shared  
make -j$(nproc) # 注意查看是否有模块报错，如果有一般是缺少依赖项，网上搜一下  
sudo make install  
cp /usr/lib/python3.10/lib/libpython3.10.so.1.0 /usr/lib/libpython3.10.so.1.0  
sudo ln -s /usr/lib/python3.10/bin/python3.10 /usr/bin/python3.10  
sudo ln -s /usr/lib/python3.10/bin/pip3.10 /usr/bin/pip3.10  
```
然后就可以使用命令 python3.10 和 pip3.10，也可以将 python3.10 改成 python3 的默认版本，不过不建议这样做  
如果激活了虚拟环境，使用命令 python3 和 pip3 即可。windows 下的命令就是 python 和 pip  

## 安装依赖
先激活虚拟环境(当然，不用虚拟环境也行)
```
python3 -m venv venv0 # 或者 python3.x，取决于安装的版本
source venv0/bin/activate  
```
然后安装依赖  
```
pip3 install ... # 具体有哪些模块请查看脚本说明，如果有报错根据报错信息自行安装
```

## 修改选项和运行
选项一般在文件开头 import 语句之后，如果需要爬网页 cookie 是必填的，注意格式等号两边一定不能有空格，其他自行参考注释  
运行输入
```
python3 xxx.py  
```
