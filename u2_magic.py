"""
给下载中的种子放魔法，python3.7 及以上应该能运行
依赖：pip3 install requests bs4 lxml deluge-client loguru func-timeout pytz nest_asyncio aiohttp paramiko qbittorrent-api

支持客户端 deluge 和 qbittorrent，其它的客户端自己去写类吧
支持配置多个客户端，可以任意停止和重新运行
检查重复，检查 uc 使用量，尽可能减少爬网页的次数
放魔法区分新种和旧种，因为新种魔法使用量太多，支持自定义魔法规则
不支持对单个种子同时施加一个以上的上传或者下载魔法
可以根据 24h 和 72h 的 uc 使用量自动切换规则
根据客户端的种子信息调整放魔法的时间，最小化 uc 使用量
对下载的种子进行限速，防止上传失效

用法：
修改配置信息，填上必填信息，按注释说明修改其它信息
至少应该填上 uid 和 cookie，默认为只给旧种放魔法
如果要给所有下载的种子放 free，将 magic_new 改为 True，default_mode 改为 4
以及删掉多余的 min_download_reduced 和 max_uc_peer_gb_reduced
当然，也可以设置 min_tid 和 min_leecher_num 很大让所有种子都被判断为旧种
不提供配置检查，因为不是重点（其实是因为懒）
所有功能和绝大部分语句都已得到测试

已修复问题:
* 使用 hdd 因为上传速度太快而失联，导致限速无法运行，加入了一种比较暴力的方法，在失联的时候网卡限速
* 多线程同时使用同一个 deluge 对象连接时可能引起 segmentation fault，给第一个线程使用不同的 deluge 对象，就不存在共用了
* 各种语言、时间显示类型为发生或者过去、优惠显示类型加高亮或者图标或者标记或者没有，以及任意时区
* 如果因为某些原因导致下载种子页面的上传量没被统计，会使用 peer 列表的上传量(正常情况下超速也是会统计的，
已知有三种情况会使部分流量不被统计，第一种是被当作同时下载，第二种是清空 tracker，还有一种是汇报超时，似乎还有其它不明情况)
* 有可能部分 libtorrent 版本有问题，next_announce 显示时间与实际不符，会通过查询 peer 列表的空闲时间来计算。
这其实是软件的问题，不过我估计这种现象还挺普遍的，所以大致解决了这个麻烦

已知问题：
暂时没有大的问题
"""

import asyncio
import json
import os
import re
import sys

import aiohttp
import nest_asyncio
import pytz

from functools import reduce, lru_cache
from datetime import datetime
from collections import deque, UserList, UserDict
from time import time, sleep
from typing import List, Dict, Tuple, Union, Any, Optional

from loguru import logger
from bs4 import BeautifulSoup, Tag
from concurrent.futures import ThreadPoolExecutor, as_completed

# ***********************************************必填配置，不填不能运行*****************************************************
uid = 50096  # type: Union[int, str]
'获取下载种子列表的用户 id'
cookies = {'nexusphp_u2': ''}  # type: Dict[str, str]
'网站cookie'

# *************************************************重要配置，核心配置******************************************************
proxy = ''  # type: str
'网络代理'
magic = True  # type: Any
'魔法的总开关，为真不施加任何魔法，否则至少会给旧种施加魔法'
magic_new = True  # type: Any
'只有为真才会给新种施加魔法'
limit = True  # type: Any
'是否开启限速'
clients_info = ({'type': 'deluge',  # 'de', 'Deluge', 'deluge'
                 'host': '127.0.0.1',  # 主机 ip 地址
                 'port': 58846,  # daemon 端口
                 'username': '',  # 本地一般不用填用户名和密码，查找路径是 ~/.config/deluge/auth
                 'password': '',
                 'connect_interval': 1.5,  # 从客户端读取种子状态的时间间隔
                 'min_announce_interval': 300  # 默认值 300，如果安装了 ltconfig 会读取设置并以 ltconfig 为准
                 },
                {'type': 'qbittorrent',  # 'qb', 'QB', 'qbittorrent', 'qBittorrent'
                 'host': 'http://127.0.0.1',  # 主机，最好加上 http
                 'port': 8080,  # webui 端口
                 'username': '',  # web 用户名
                 'password': '',  # web 密码
                 'connect_interval': 1,  # 从客户端读取种子状态的时间间隔
                 'min_announce_interval': 300  # 默认值 300
                 },
                )  # type: Tuple[Dict[str, Union[str, int, float]], ...]
'客户端配置'
enable_clients = False  # type: Any
'客户端配置是否生效'
tc_info = ({'host': '127.0.0.1',  # 主机 ip 地址
            'root_pass': '',  # root 密码，用于远程执行命令，本地不需要，但是要用 root 权限运行
            'device': '',  # 网卡名
            'timeout': 30,  # 客户端响应超时秒数，超时后就进行网卡限速
            'initial_rate': 100,  # 初始限速值 (Mbps)，尽量一步到位
            'min_rate': 10,  # 最低限速值 (Mbps)，不要太低,会导致机器失联
            },
           )  # type: Tuple[Dict[str, Union[str, int, float]], ...]
'网卡限速配置，客户端失联后进行操作，主要针对 deluge1.3 + 机械硬盘 + 10G 带宽 ，10G 以下带宽不推荐使用，谨慎填写信息'
enable_tc = False  # type: Any
'限速配置是否生效'

# *****************************************************详细配置**********************************************************
interval = 60  # type: Union[int, float]
'获取下载页面的时间间隔，magic为真时就会按这个间隔爬下载页面'
auto_mode = False  # type: Any
'如果为真，新种放魔法自动切换魔法规则(请仔细检查魔法规则，已有配置会消耗巨量uc)'
default_mode = 3  # type: int
'如果 auto_mode 不为真，则此项为新种的魔法规则，这个数字，就是 modes 列表的序号(第一个为 0)'
default_hours = 24  # type: int
'如果魔法规则没有指定魔法时长，则默认魔法为此时长'
min_tid = 47586  # type: int
'''种子 id 超过这个值纳入新种的判断范围
这个参数存在的原因在于，下载种子页没有提供种子的发布时间信息，下载人数也没法判断(刚加入的时候可能下载数为0)
但我又不想每个种子都去查详情页(想象一下同时下载 1000 个种子)，所以决定将tid大于一定数值才去判断'''
min_leecher_num = 5  # type: int
'种子下载人数（网页显示的数值）超过这个值纳入新种的判断范围'
min_leecher_to_seeder_ratio = 0.1  # type: Union[int, float]
'''只有当
下载人数 / (做种人数 + 1) 
超过这个值才可能是新种，如果这个值比较大，则新种只包括未出种的种子'''
uc_24_max = 6000000  # type: int
'24h 内 uc 消耗量超过这个值，则不放魔法'
uc_72_max = 12000000  # type: int
'72h 内 uc 消耗量超过这个值，则不放魔法'
default_ratio = 3
'种子默认分享率，用于魔法规则估计上传量'
min_secs_before_announce = 20  # type: Union[int, float]
'''这个值是检查放魔法的时间用的，给自己放魔法的话，在距离汇报时间小于 20s 的时候'''
modes = [{'uc_limit': {'24_max': 1500000, '72_max': 4300000, '24_min': 0, '72_min': 0},
          'rules': [{'ur': 2.33, 'dr': 1, 'user': 'ALL', 'min_size': 16146493595, 'max_size': 107374182400,
                     'min_uploaded': 1073741824, 'ur_less_than': 2},
                    {'ur': 2.33, 'dr': 1, 'user': 'SELF', 'min_uploaded': 1073741824, 'min_upload_added': 57123065037,
                     'max_uc_peer_gb_added': 771},
                    {'ur': 1, 'dr': 0, 'user': 'ALL'}
                    ]
          },
         {'uc_limit': {'24_max': 2200000, '72_max': 5600000, '24_min': 1400000, '72_min': 4100000},
          'rules': [{'ur': 2.33, 'dr': 1, 'user': 'SELF', 'min_uploaded': 1073741824, 'min_upload_added': 57123065037,
                     'max_uc_peer_gb_added': 771},
                    {'ur': 1, 'dr': 0, 'user': 'ALL'}
                    ]
          },
         {'uc_limit': {'24_max': 3000000, '72_max': 7500000, '24_min': 2050000, '72_min': 5300000},
          'rules': [{'ur': 2.33, 'dr': 1, 'user': 'SELF', 'min_uploaded': 5368709120, 'min_upload_added': 85684597555,
                     'max_uc_peer_gb_added': 545},
                    {'ur': 1, 'dr': 0, 'user': 'ALL', 'min_size': 16146493595, 'max_size': 214748364800},
                    {'ur': 1, 'dr': 0, 'user': 'SELF'}
                    ]
          },
         {'uc_limit': {'24_max': 4500000, '72_max': 10000000, '24_min': 2900000, '72_min': 7000000},
          'rules': [{'ur': 2.33, 'dr': 1, 'user': 'SELF', 'min_uploaded': 16106127360, 'min_upload_added': 214211493888,
                     'max_uc_peer_gb_added': 545},
                    {'ur': 1, 'dr': 0, 'user': 'SELF'}
                    ]
          },
         {'uc_limit': {'24_max': 6000000, '72_max': 12000000, '24_min': 4200000, '72_min': 9400000},
          'rules': [
              {'ur': 1, 'dr': 0, 'user': 'SELF', 'min_download_reduced': 5368709120, 'max_uc_peer_gb_reduced': 4727}]
          }
         ]  # type: List[Dict[str, Union[Dict[str, Union[int, float]], List[Dict[str, Union[int, float, str]]]]]]
'''这是新种的魔法规则，这下面的子项我称之为”模式“，可以配置任意套模式，程序中用 mode 表示(其实是用序号代替这个模式)
每个子项包含 uc_limit 和 rules 两项

:uc_limit:
    uc 使用限制，一个字典，包含 24_max/72_max/24_min/72_min 四个键
    如果 24h uc 使用量超过 24_max 或者 72h uc 使用量超过 72_max，则 mode + 1
    如果最后一级还是超过 24_max 或者 72h uc，则新种不放魔法
    如果 24h uc 使用量小于 24_max 且 72h uc 使用量小于 72_max 且 mode > 0，则 mode -1
    注意对于相邻的两级，高一级的 24_min 要不大于于低一级的 24_max，且高一级的 72_min 要不大于于低一级的 72_max
:rules:
    一个列表或者元组，每项为一个字典对应一条魔法规则，规则可以配置任意条，如果检查规则通过，则可以生成一个魔法
    如果上传下载比率都不为 1 则会拆成一个上传和一个下载魔法(uc 使用量不受影响)
    每次只选择一个上传魔法和一个下载魔法
    具体优先级是，首先优先选择范围为所有人魔法，然后优先选择上传比率更高或者下载比率更低的魔法，最后优先选择时效最长的魔法
        对于每一条规则，首先必须有 ur(上传比率)、dr(下载比率)、user(有效用户)
        hours 为时长，24~360 之间的整数，可以不写，会采用 default_hours
        ur 可选的值：1.3~2.33或1，ur可选的值：0~0.8或1，两者不能同时为1
        user：给自己放填 SELF，所有人放填 ALL，给另一个人放填 OTHER
        如果要给另一个人放魔法，最好另外开一个脚本，另外也可以加上 comment
        其它一些键为程序制定的检查项，具体见 MagicAndLimit 类的 check_rule 函数
        如果没有其他选项，则不进行任何检查，对所有种子都施加这个魔法
'''
variable_announce_interval = True
'开启后会尝试调节完成前最后一次汇报时间'

# ****************************************************可调节配置**********************************************************
log_path = f'{os.path.splitext(__file__)[0]}.log'  # type: str
'日志文件路径'
magic_info_path = f'{os.path.splitext(__file__)[0]}.magic_info'  # type: str
'魔法信息保存路径'
torrents_info_path = f'{os.path.splitext(__file__)[0]}.torrents_info'  # type: str
'种子信息保存路径'
enable_debug_output = True  # type: Any
'为真时会输出 debug 级别日志，否则只输出 info 级别日志'
local_hosts = '127.0.0.1', 'http://127.0.0.1',  # type: Tuple[str, ...]
'本地客户端 ip'
max_cache_size = 256  # type: int
'lru_cache 的 max_size'
check_peer_list = True  # type: Any
'客户端的 TorrentManger 第一次添加某个种子时，是否从 peerlist 获取上传量，这项操作可以保证上传量计算不出差错'

# **********************************************************************************************************************

use_client = bool((magic and magic_new or limit) and len(clients_info) > 0 and enable_clients)
use_limit = bool(limit and len(clients_info) > 0 and enable_clients)

if use_client:
    import subprocess
    import paramiko

    from abc import ABCMeta, abstractmethod
    from func_timeout import func_set_timeout, FunctionTimedOut

    clients = []
    clients_copy = []


    class BTClient(metaclass=ABCMeta):
        """BT 客户端基类"""
        instances = []
        local_clients = []
        status_keys = ['download_payload_rate', 'eta', 'max_download_speed', 'max_upload_speed',
                       'name', 'next_announce', 'num_seeds', 'total_done', 'total_uploaded',
                       'total_size', 'tracker', 'time_added', 'upload_payload_rate'
                       ]

        def __init__(self, host, port, min_announce_interval, connect_interval):
            self.host = host
            match = re.findall(r'https?://(.*)', self.host)
            self.ip = match[0] if match else self.host
            self.port = port
            self.min_announce_interval = min_announce_interval
            self.connect_interval = connect_interval
            self.enable_tc = False
            self.io_busy = False
            self.tc_limited = False

            for info in tc_info:
                if info['host'] == self.host and enable_tc:
                    self.enable_tc = True
                    self.device = info['device']
                    self.op_timeout = info['timeout']
                    self.initial_rate = info['initial_rate']
                    self.tc_rate = self.initial_rate
                    self.min_rate = info['min_rate']
                    self.passwd = info['root_pass']
                    self.sshd = paramiko.SSHClient()
                    self.sshd.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    self.run_cmd(f'tc qdisc del dev {self.device} root >> /dev/null 2>&1')

            self.instances.append(self)
            if host in local_hosts:
                self.local_clients.append(self)

        @classmethod
        def log_filter(cls, record):
            """客户端失联的时候，硬盘非常繁忙，不写入 log 文件"""
            return all(1 - local_client.io_busy for local_client in cls.local_clients)

        def call(self, method, *args, **kwargs):
            try:
                if self.enable_tc:
                    res = func_set_timeout(self.op_timeout)(self.call_retry)(method, *args, **kwargs)
                    if self.tc_limited:
                        self.run_cmd(f'tc qdisc del dev {self.device} root >> /dev/null 2>&1')
                        logger.info(f'Release tc limit for {self.device} on {self.host}')
                        self.tc_limited = False
                        self.tc_rate = self.initial_rate
                    return res
                else:
                    return self.call_retry(method, *args, **kwargs)
            except BaseException as e:
                if isinstance(e, TimeoutError):
                    logger.error(f'{e.__class__.__name__}: {e}')
                elif not self.enable_tc:
                    raise
                if self.enable_tc:
                    self.io_busy = True
                    if isinstance(e, FunctionTimedOut):
                        logger.error(f'{e.__module__}.{e.__class__.__name__}: {e.msg}')
                    return self.on_fail_call(method, *args, **kwargs)

        def on_fail_call(self, method, *args, **kwargs):
            for _ in range(20):
                try:
                    if self.tc_rate >= self.min_rate and not self.limit_on_host():
                        self.run_cmd(f'tc qdisc del dev {self.device} root >> /dev/null 2>&1')
                        cmd = f'tc qdisc add dev {self.device} root handle 1: tbf rate {self.tc_rate:.2f}mbit ' \
                              f'burst {self.tc_rate / 10:.2f}mbit latency 1s >> /dev/null 2>&1'
                        self.run_cmd(cmd)
                        self.tc_limited = True
                        if self.tc_rate < self.initial_rate:
                            self.io_busy = False
                        logger.warning(
                            f'Set the upload limit for {self.device} on {self.host} to {self.tc_rate:.2f}mbps')
                        self.tc_rate = self.tc_rate / 2
                    try:
                        res = self.call_retry(method, *args, **kwargs)
                        self.io_busy = False
                        return res
                    except:
                        logger.error(f'Still cannot access the deluge instance on {self.host}')
                except BaseException as e:
                    logger.exception(e)
            self.run_cmd(f'tc qdisc del dev {self.device} root >> /dev/null 2>&1')

        def limit_on_host(self):
            for client in self.instances:
                if self.host in local_hosts and client.host in local_hosts:
                    return True
                if self.ip == client.ip:
                    return True

        def run_cmd(self, cmd):
            if self.host in local_hosts:
                subprocess.Popen(cmd, shell=True)
            else:
                self.sshd.connect(hostname=self.ip, username='root', password=self.passwd)
                self.sshd.exec_command(cmd)

        @abstractmethod
        def call_retry(self, method, *args, **kwargs):
            """客户端连接失败后重连"""

        if use_limit:
            @abstractmethod
            def set_upload_limit(self, _id: str, rate: Union[int, float]):
                """设置上传限速
                :param _id: 种子 hash (小写)
                :param rate: 限速值 (单位: KiB/s)
                """

            @abstractmethod
            def set_download_limit(self, _id: str, rate: Union[int, float]):
                """设置下载限速
                :param _id: 种子 hash (小写)
                :param rate: 限速值 (单位: KiB/s)
                """

            @abstractmethod
            def re_announce(self, _id):
                """强制重新汇报"""

            @abstractmethod
            def torrent_status(self, _id: str, keys: List[str]) -> Dict[str, Any]:
                """单个种子信息
                脚本是以 deluge 为基础写的，如果要使用其他客户端编写自定义 BTClient 继承类，
                则每个 status_keys 对应的信息必须和 deluge 相同，否则脚本功能不能正常使用
                deluge 返回示例见 Deluge 类的方法实现
                """

        @abstractmethod
        def downloading_torrents_info(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
            """下载中的种子信息
            返回以种子 hash 为 key (小写), 种子信息(见 torrent_status 返回值)为值的字典
            """


    for client_info in clients_info:
        client_type = client_info['type']
        del client_info['type']
        if client_type in ['de', 'Deluge', 'deluge']:
            from deluge_client import LocalDelugeRPCClient, FailedToReconnectException


            class Deluge(BTClient, LocalDelugeRPCClient):  # 主要是把 call 重写了一下，因为 deluge 太容易失联了
                timeout = 10

                def __init__(self,
                             host: str = '127.0.0.1',
                             port: int = 58846,
                             username: str = '',
                             password: str = '',
                             decode_utf8: bool = True,
                             automatic_reconnect: bool = True,
                             min_announce_interval: int = 300,
                             connect_interval: int = 1.5
                             ):
                    BTClient.__init__(self, host, port, min_announce_interval, connect_interval)
                    LocalDelugeRPCClient.__init__(self, host, port, username, password,
                                                  decode_utf8, automatic_reconnect)
                    try:
                        min_announce_interval = self.ltconfig.get_settings()['min_announce_interval']
                        if min_announce_interval != self.min_announce_interval:
                            logger.warning(f'Min announce interval changed from '
                                           f'{self.min_announce_interval} to {min_announce_interval}')
                            self.min_announce_interval = min_announce_interval
                    except:
                        pass

                def call_retry(self, method, *args, **kwargs):
                    if not self.connected and method != 'daemon.login':
                        for i in range(5):
                            try:
                                self.reconnect()
                                logger.info(f'Connected to deluge client on {self.host}')
                                break
                            except Exception as e:
                                if isinstance(e, FailedToReconnectException):
                                    logger.error(f'Failed to reconnect to deluge client! Host  -------  {self.host}')
                                elif e.__class__.__name__ == 'BadLoginError':
                                    # 这个类是 deluge 里面的，要 import deluge，但是很多人用的的是 deluge1.3(python2)
                                    logger.error(
                                        f'Failed to connect to deluge client on {self.host}, Password does not match')
                                else:
                                    sleep(0.3 * 2 ** i)
                    return LocalDelugeRPCClient.call(self, method, *args, **kwargs)

                if use_limit:
                    def set_upload_limit(self, _id, rate):
                        return self.core.set_torrent_options([_id], {'max_upload_speed': rate})

                    def set_download_limit(self, _id, rate):
                        return self.core.set_torrent_options([_id], {'max_download_speed': rate})

                    def re_announce(self, _id):
                        return self.core.force_reannounce([_id])

                    def torrent_status(self, _id, keys):
                        """Deluge 返回示例
                        {'name': 'MEDAKA BOX ABNORMAL', 'total_size': 60299096372, 'download_payload_rate': 10176348,
                        'time_added': 1667313753, 'max_upload_speed': -1, 'upload_payload_rate': 429508,
                        'max_download_speed': -1, 'num_seeds': 2, 'total_done': 22196281185,
                        'tracker': 'https://daydream.dmhy.best/announce?secure=',
                        'next_announce': 928, 'eta': 3744, 'total_uploaded': 7951417344}
                        种子下载速度为 0 时 eta 为 0
                        """
                        return self.core.get_torrent_status(_id, keys)

                def downloading_torrents_info(self, keys):
                    return self.core.get_torrents_status({'state': 'Downloading'}, keys)


            clients.append(Deluge(**client_info))
            clients_copy.append(Deluge(**client_info))
        elif client_type in ['qb', 'QB', 'qbittorrent', 'qBittorrent']:
            import qbittorrentapi
            from qbittorrentapi import APIConnectionError, HTTPError


            class QBittorrent(BTClient, qbittorrentapi.Client):
                def __init__(self,
                             host: str = 'http://127.0.0.1',
                             port: int = 8080,
                             username: str = '',
                             password: str = '',
                             min_announce_interval: int = 300,
                             connect_interval: int = 1.5,
                             **kwargs
                             ):
                    BTClient.__init__(self, host, port, min_announce_interval, connect_interval)
                    qbittorrentapi.Client.__init__(
                        self, host=host, port=port, username=username, password=password,
                        REQUESTS_ARGS={'timeout': 20}, FORCE_SCHEME_FROM_HOST=True,
                        VERIFY_WEBUI_CERTIFICATE=True if 'verify' not in kwargs else kwargs['verify']
                    )

                    self.connected = False
                    self.status_funcs = self.create_status_funcs()

                def auth_log_in(self, username=None, password=None, **kwargs):
                    qbittorrentapi.Client.auth_log_in(self, username, password, **kwargs)
                    self.connected = True
                    logger.info(f'Connected to qbittorrent client on {self.host}')

                def create_status_funcs(self):
                    return {
                        'download_payload_rate': lambda to: to.dlspeed,
                        'eta': lambda to: 0 if to.eta == 8640000 else to.eta,
                        'max_download_speed': lambda to: -1 if to.dl_limit <= 0 else to.dl_limit / 1024,
                        'max_upload_speed': lambda to: -1 if to.up_limit <= 0 else to.up_limit / 1024,
                        'name': lambda to: to.name,
                        'next_announce': lambda to: self.call('torrents_properties', to.hash).reannounce,
                        'num_seeds': lambda to: to.num_complete,
                        'peers': lambda to: tuple(
                            {
                                'client': peer.client,
                                'country': peer.country,
                                'down_speed': peer.dl_speed,
                                'ip': ip_port,
                                'progress': peer.progress,
                                'seed': 0 if peer.progress < 1 else 1,
                                'up_speed': peer.up_speed,
                            }
                            for ip_port, peer in self.call('sync_torrent_peers', to.hash).peers.items()
                        ),
                        'state': lambda to: {
                                                'uploading': 'Seeding',
                                                'stalledUP': 'Seeding',
                                                'downloading': 'Downloading',
                                                'stalledDL': 'Downloading',
                                            }.get(to.state) or to.state,
                        'total_done': lambda to: to.total_size - to.amount_left,
                        'total_uploaded': lambda to: to.uploaded,
                        'total_size': lambda to: to.total_size,
                        'tracker': lambda to: to.tracker,
                        'time_added': lambda to: to.added_on,
                        'upload_payload_rate': lambda to: to.upspeed,
                    }

                def call_retry(self, method, *args, **kwargs):
                    try:
                        return self.__getattribute__(method)(*args, **kwargs, _retries=5)
                    except HTTPError as e:
                        logger.error(
                            f'Failed to connect to qbittorrent on {self.host}:{self.port} due to http error: {e}')
                    except APIConnectionError as e:
                        logger.error(f'Failed to connect to qbittorrent on {self.host}:{self.port} due to '
                                     f'qbittorrentapi.exceptions.APIConnectionError:  {e}')

                def create_torrent_status(self, torrent: qbittorrentapi._attrdict.AttrDict, keys: List[str]):
                    return {key: self.status_funcs[key](torrent) for key in keys}

                if use_limit:
                    def re_announce(self, _id):
                        self.call('torrents_reannounce', torrent_hashes=_id)

                    def torrent_status(self, _id, keys):
                        return self.create_torrent_status(self.call('torrents_info', torrent_hashes=_id)[0], keys)

                    def set_upload_limit(self, _id, rate):
                        up_limit = -1 if rate < 0 else int(rate * 1024)
                        self.call('torrents_set_upload_limit', limit=up_limit, torrent_hashes=_id)

                    def set_download_limit(self, _id, rate):
                        dl_limit = -1 if rate < 0 else int(rate * 1024)
                        self.call('torrents_set_download_limit', limit=dl_limit, torrent_hashes=_id)

                def downloading_torrents_info(self, keys):
                    torrents = self.call('torrents_info', status_filter='downloading')
                    if not torrents:
                        return {}
                    if set(keys) & {'peers', 'next_announce'}:
                        with ThreadPoolExecutor(max_workers=len(torrents)) as executor:
                            futures = {executor.submit(self.create_torrent_status, torrent, keys): torrent.hash
                                       for torrent in torrents}
                            return {futures[future]: future.result() for future in as_completed(futures)}
                    else:
                        return {torrent.hash: self.create_torrent_status(torrent, keys) for torrent in torrents}


            clients.append(QBittorrent(**client_info))
            clients_copy.append(QBittorrent(**client_info))

    use_client = bool(clients)
    use_limit = bool(limit and clients)  # 艹，一个基本配置逻辑这么绕


class TorrentDict(UserDict):
    """包含种子信息的字典
    item 方式书写不方便，可以通过属性访问字典的值
    """

    def __repr__(self):
        return f'{self.__class__.__name__}({self.data})'

    def __getattr__(self, item):
        if isinstance(item, str) and item.endswith('byte'):
            key = item[:-5]
            if key in self.data:
                return self.byte(self.data[key])
        else:
            return self.data.get(item)

    def __setattr__(self, key, value):
        if key == 'data':
            super(TorrentDict, self).__setattr__(key, value)
        else:
            self.data.__setitem__(key, value)

    def __delattr__(self, item):
        self.__delitem__(item)

    def update(self, obj, **kwargs):
        if isinstance(obj, TorrentWrapper):
            obj = obj.torrent_dict
        return super(TorrentDict, self).update(obj, **kwargs)

    @property
    def delta(self):
        return int(time() - self.ts(self.date, self.tz))

    @staticmethod
    @lru_cache(maxsize=max_cache_size)
    def ts(date: str, tz: str):
        dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        return pytz.timezone(tz).localize(dt).timestamp()

    @staticmethod
    @lru_cache(maxsize=max_cache_size)
    def byte(st: Union[str, int], flag: int = 0) -> int:
        """将表示体积的字符串转换为字节，考虑四舍五入
        网站显示的的数据都是四舍五入保留三位小数
        """
        if isinstance(st, int):
            return st
        else:
            [num, unit] = st.split(' ')
            _pow = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB',
                    '蚌', '氪', '喵', '寄', '烫', '皮',
                    'Б', 'KiБ', 'MiБ', 'GiБ', 'TiБ', 'PiБ'
                    ].index(unit) % 6
            flag = 0 if flag == 0 else flag / abs(flag)
            return int((float(num.replace(',', '.')) + 0.0005 * flag) * 1024 ** _pow)

    @property
    def is_new(self):
        if self.tid > min_tid or self.leecher_num > min_leecher_num:
            if self.leecher_num / (self.seeder_num + 1) > min_leecher_to_seeder_ratio:
                return True
            elif self.date and self.delta < 600:
                return True
        return False


class TorrentManager(UserDict):
    """存放每个 Run 实例的所有种子信息
    对应第一个实例: 键为种子 id, 值为包含种子信息的 TorrentDict
                 字典主要是 get_info_from_web 获取的信息
    对应第其他实例: 键为种子 hash (脚本中一般用 _id 表示), 值为包含种子信息的 TorrentDict
                 字典为 torrent_status 返回值，以及 get_info_from_web 信息合并以及其他一些信息
    当使用 __getitem__ 或 values 或 items 访问值时会生成 TorrentWrapper 对象
    """
    instances = []

    def __init__(self, dic=None, client=None, accurate_next_announce=True):
        for instance in self.instances:
            if instance.client and client:
                if instance.client.host == client.host and instance.client.port == client.port:
                    raise ValueError('TorrentManager instance for a client can only be created once ')

        self.instances.append(self)
        super(TorrentManager, self).__init__(dic)
        self.client = client
        self.ana = accurate_next_announce
        self.ana_updated = False
        self.last_connect = 0
        self.session = None
        self.deque_length = None

    def __repr__(self):
        return f'{self.__class__.__name__}({self.data}, accurate_next_announce={self.ana})'

    def __str__(self):
        return object.__repr__(self)

    def __getitem__(self, item):
        if item in self.data:
            return TorrentWrapper(self.data[item], self)

    @classmethod
    def save_data(cls):
        with open(torrents_info_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join([instance.__repr__() for instance in cls.instances]))

    class RequestManager:
        requests_args = {
            'headers': {'user-agent': 'U2-Auto-Magic'},
            'cookies': cookies, 'proxy': proxy
        }
        valid_cookie = None
        un_debug_urls = f'https://u2.dmhy.org/getusertorrentlistajax.php?userid={uid}&type=leeching',

        async def request(self: Any, url: str, method: str = 'get',
                          timeout: Union[int, float] = 10, retries: int = 5, **kw) -> Union[str, None]:
            """网页请求"""
            if use_client:
                if BTClient.local_clients and any(local_client.tc_limited for local_client in BTClient.local_clients):
                    # 限速爬不动
                    raise Exception('Waiting for release tc limit')
            cls = TorrentManager.RequestManager

            for i in range(retries + 1):
                try:
                    async with self.session.request(method, url, **cls.requests_args, timeout=timeout, **kw) as resp:
                        if resp.status < 300:
                            text = await resp.text()
                            if method == 'get':
                                if url not in cls.un_debug_urls:
                                    logger.debug(f'Downloaded page: {url}')
                                if not cls.valid_cookie:
                                    if '<title>Access Point :: U2</title>' in text or 'Access Denied' in text:
                                        logger.error('Your cookie is wrong')
                                        cls.valid_cookie = False
                                    else:
                                        cls.valid_cookie = True
                            return text
                        elif i == retries - 1:
                            raise Exception(f'Failed to request... method: {method}, url: {url}, kw: {kw}'
                                            f' ------ status code: {resp.status}')
                except Exception as e:
                    if i == retries - 1:
                        raise
                    elif isinstance(e, asyncio.TimeoutError):
                        timeout += 20

    async def request(self, *args, **kwargs):
        return await self.RequestManager.request(self, *args, **kwargs)

    if use_client:

        def dl_to_info(self, keys=None):
            if keys:
                return self.client.downloading_torrents_info(keys)
            else:
                return self.client.downloading_torrents_info(self.client.status_keys)


class TorrentWrapper:
    def __init__(self, torrent_dict: TorrentDict, manager: TorrentManager):
        self.torrent_dict = torrent_dict
        self.manager = manager

    def __getattr__(self, item):
        try:
            return self.torrent_dict.__getattribute__(item)
        except AttributeError:
            return self.torrent_dict.__getattr__(item)

    def __setattr__(self, key, value):
        if key in ('torrent_dict', 'manager'):
            super(TorrentWrapper, self).__setattr__(key, value)
        else:
            self.torrent_dict.__setitem__(key, value)

    def __delattr__(self, item):
        self.torrent_dict.__delitem__(item)

    def __iter__(self):
        return self.torrent_dict.__iter__()

    def __contains__(self, key):
        return key in self.torrent_dict.data

    def __getitem__(self, item):
        return self.torrent_dict[item]

    def __setitem__(self, key, value):
        self.torrent_dict.__setitem__(key, value)

    def __delitem__(self, key):
        self.torrent_dict.__delitem__(key)

    def __str__(self):
        return f'{self.__class__.__name__}({self.torrent_dict}, {self.manager})'

    if use_client:

        @property
        def announce_interval(self) -> int:
            """当前种子汇报间隔"""
            dt = self.delta
            if dt < 86400 * 7:
                return max(1800, self.manager.client.min_announce_interval)
            elif dt < 86400 * 30:
                return max(2700, self.manager.client.min_announce_interval)
            else:
                return max(3600, self.manager.client.min_announce_interval)

        @property
        def min_time(self) -> Union[int, float]:
            li = min(
                max(time() - (self.manager.last_connect or time()), self.manager.client.connect_interval),
                6 * self.manager.client.connect_interval
            )
            return min_secs_before_announce / self.manager.client.connect_interval * li

        @property
        def this_up(self) -> int:
            """当前种子自上次汇报的上传量"""
            _before = self.byte(self.uploaded_before, 1)
            _now = self.byte(self.true_uploaded or self.uploaded, -1)
            return self.total_uploaded - _now + _before

        @property
        def this_time(self) -> int:
            """当前种子距离上次汇报的时间"""
            this_time = self.announce_interval - self.next_announce - 1
            return 0 if this_time < 0 else this_time

        @property
        def next_announce(self):
            next_announce = self.torrent_dict.next_announce
            if next_announce > self.announce_interval:
                next_announce = self.announce_interval

            if not self.manager.ana_updated:  # 不确定 next_announce 是否有问题，继续观察
                if self.tid and self.date:
                    if time() - self.time_added < self.announce_interval:
                        delta = time() - self.time_added + next_announce - self.announce_interval
                        if abs(delta) <= 5:  # next_announce 没有问题
                            self.manager.ana = True
                            self.manager.ana_updated = True
                        elif delta < -600:  # next_announce 疑似异常
                            if not self.last_announce_time and not self.next_announce_is_true:
                                asyncio.run(self.find_last_announce())
                                if abs(self.last_announce_time + 900 - time() - next_announce) < 5:
                                    # 这是强制汇报引起的，所以还不能确定
                                    del self.last_announce_time
                                    self.next_announce_is_true = True
                                else:  # next_announce 确定有问题
                                    self.manager.ana = False
                                    self.manager.ana_updated = True

            if not self.manager.ana and not self.last_announce_time:
                asyncio.run(self.find_last_announce())

            if self.last_announce_time:
                return int(self.last_announce_time + self.announce_interval - time()) + 1
            else:
                return next_announce

        async def find_last_announce(self):
            self.last_announce_time = time()
            async with aiohttp.ClientSession() as self.manager.session:
                await self.info_from_peer_list()

        async def info_from_peer_list(self):
            try:
                peer_list = await self.manager.request(f'https://u2.dmhy.org/viewpeerlist.php?id={self.tid}')
                tables = BeautifulSoup(peer_list.replace('\n', ' '), 'lxml').find_all('table')
            except Exception as e:
                logger.error(e)
                return

            for table in tables or []:
                for tr in filter(lambda _tr: 'nowrap' in str(_tr), table):
                    if tr.get('bgcolor'):

                        if self.true_uploaded:
                            self.true_uploaded = tr.contents[1].string
                            self.true_downloaded = tr.contents[4].string
                            actual_uploaded_byte = self.true_uploaded_byte + self.uploaded_before_byte
                            if abs(actual_uploaded_byte - self.uploaded_byte) < 1024 ** 3:
                                del self.true_uploaded
                                del self.true_downloaded
                            elif actual_uploaded_byte > self.uploaded_byte:
                                logger.debug(f'Some upload of torrent {self.tid} was not calculated by tracker')

                                def show_size(byte):
                                    units = (('B', 0), ('KiB', 1), ('MiB', 2), ('GiB', 3), ('TiB', 6), ('PiB', 9))
                                    for unit, digits in units:
                                        if byte >= 1024 - 0.5 * 10 ** (-digits):
                                            byte /= 1024
                                        else:
                                            return f'{round(byte if byte >= 1 else 1.0, digits)} {unit}'

                                logger.debug(
                                    f'Actual upload of torrent {self.tid} is {show_size(actual_uploaded_byte)}')

                        if self.last_announce_time:
                            idle = reduce(lambda a, b: a * 60 + b, map(int, tr.contents[10].string.split(':')))
                            self.last_announce_time = time() - idle

                        break

    if use_limit:

        def set_upload_limit(self, rate):
            self.manager.client.set_upload_limit(self._id, rate)
            self.max_upload_speed = rate

        def set_download_limit(self, rate):
            self.manager.client.set_download_limit(self._id, rate)
            self.max_download_speed = rate

        def re_announce(self):
            self.manager.client.re_announce(self._id)
            logger.info(f'Re-announce of torrent {self.tid}')
            self.lft = time()
            if self.last_announce_time:
                self.last_announce_time = time()
            self.about_to_re_announce = False

        def torrent_status(self, keys):
            return self.manager.client.torrent_status(self._id, keys)


class FunctionBase:
    instances = []

    def __init__(self, client):
        self.client = client
        self.instances.append(self)
        n = self.instances.index(self)
        m = len(TorrentManager.instances)
        if n >= m:
            self.torrent_manager: TorrentManager = TorrentManager(
                {}, self.client, accurate_next_announce=True
            )
        else:
            self.torrent_manager: TorrentManager = TorrentManager.instances[n]
            self.torrent_manager.client = self.client
        if self.client:
            self.torrent_manager.deque_length = int(300 / self.client.connect_interval) + 1
        self.to: TorrentWrapper = None
        self.clients = []
        self.magic_tasks = []
        self.session = None

    def print(self, st: str):
        """只输出一次信息，避免频繁输出"""
        if 'statement' not in self.to:
            self.to.statement = []
        if st not in self.to.statement:
            function = sys._getframe(1).f_code.co_name
            line = sys._getframe(1).f_lineno
            _logger = logger.patch(lambda record: record.update({'function': function, 'line': line}))
            _logger.debug(st)
            self.to.statement.append(st)

    async def request(self, *args, **kwargs):
        return await TorrentManager.RequestManager.request(self, *args, **kwargs)

    @staticmethod
    def get_tz(soup: Tag) -> str:
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        return [tz_info[len(pre):-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]

    class ProType:
        class_to_pro = {}
        known_keywords_map = [
            ('free', {'dr': 0.0}),
            ('twoup', {'ur': 2.0}), ('2up', {'ur': 2.0}),
            ('halfdown', {'dr': 0.5}), ('50pct', {'dr': 0.5}),
            ('thirtypercent', {'dr': 0.3}), ('30pct', {'dr': 0.3}),
        ]

        @classmethod
        def get_pro_by_class(cls, element_class: str) -> Optional[List[Union[int, float]]]:
            if element_class in cls.class_to_pro:
                return cls.class_to_pro[element_class]
            else:
                pro_dict = {'ur': 1.0, 'dr': 1.0}
                if tuple(
                        pro_dict.update(pro_data)
                        for pro_type, pro_data in cls.known_keywords_map if pro_type in element_class
                ):
                    pro = list(pro_dict.values())
                    cls.class_to_pro[element_class] = pro
                    return pro
                else:
                    cls.class_to_pro[element_class] = None

    @classmethod
    def get_pro(cls, tr: Tag) -> List[Union[int, float]]:
        """返回上传下载比率，如果控制面板关掉了优惠显示，返回的结果可能与实际不符，会在检查魔法是否重复的时候修正
        :param tr: 优惠信息的行元素，兼容三种 tr: 种子页每行 tr，下载页每行 tr，详情页显示优惠信息的行 tr(实际上魔法信息页的 tr 也行)
        """
        if tr.get('class'):  # 高亮显示或者魔法信息的行
            pro = cls.ProType.get_pro_by_class(tr['class'][0])
            if pro:
                return pro

        if tr.tr:  # 行里还有行，这是种子信息显示，优惠信息在下边
            td = tr.select('tr')[1].td
            imgs = td.select("img")  # 优惠图标
        else:
            td = None
            imgs = tr.select("img")

        if imgs:  # 图标显示或自定义优惠或种子详情页
            pro = [1.0, 1.0]
            for img in imgs:
                img_class = img['class'][0]
                _pro = cls.ProType.get_pro_by_class(img_class)
                if _pro:  # 图标显示或者种子详情页
                    return _pro
                elif img_class == 'arrowup':  # 自定义优惠上传比率
                    pro[0] = float(img.next.text[:-1].replace(',', '.'))
                elif img_class == 'arrowdown':  # 自定义优惠下载比率
                    pro[1] = float(img.next.text[:-1].replace(',', '.'))
            return pro

        if td:
            spans = td.select("span[class^='']")
            if spans:
                for span in spans:  # 这里 span 的 class 有很多，可能有 hot/tooltip/classic
                    pro = cls.ProType.get_pro_by_class(span['class'][0])
                    if pro:  # 标记显示
                        return pro

        return [1.0, 1.0]

    if use_client:

        def dl_to_info(self, keys=None):
            return self.torrent_manager.dl_to_info(keys)

        async def get_info_from_client(self):
            """读取客户端种子的状态，并且与已知信息合并
            由于客户端只有种子的 hash 信息，而放魔法需要知道种子 id
            当然可以直接在网站搜索 hash，但只有新种才需要，为了避免浪费服务器资源
            采用对比的方式合并种子信息，旧种子的 id 将会被设置为 -1
            """
            _id_td = {
                _id: TorrentDict(dic) for _id, dic in self.dl_to_info().items()
                if dic.get('tracker') and 'daydream.dmhy.best' in dic['tracker']
            }
            _id_tw_0 = {tw._id: tw for tw in self.instances[0].torrent_manager.values() if tw._id}
            checked = False  # 用来标志是否访问了下载页面，此函数内最多访问一次
            update_upload = False
            if (self.torrent_manager.last_connect < self.instances[0].torrent_manager.last_connect
                    or not self.torrent_manager.last_connect):
                update_upload = True

            for _id in list(self.torrent_manager):  # 上次连接客户端时的种子信息
                tw = self.torrent_manager[_id]
                if _id in _id_td:  # 本次连接种子还在下载
                    tw.update(_id_td[_id])
                    if not tw.first_seed_time and tw.total_done > 0:
                        tw.first_seed_time = time()
                    if update_upload:
                        if _id in _id_tw_0:
                            tw.update(_id_tw_0[_id])
                        elif tw.tid in self.instances[0].torrent_manager:
                            tw.update(self.instances[0].torrent_manager[tw.tid])
                    _id_td.pop(_id)
                else:  # 本次连接种子不在下载
                    self.torrent_manager.pop(_id)

            for _id, td in _id_td.items():  # 本次连接新加入的种子
                td._id = _id
                if _id in _id_tw_0:
                    td.update(_id_tw_0[_id])
                    _id_tw_0[_id].in_client = True
                else:
                    if not checked:
                        try:
                            await self.instances[0].get_info_from_web()  # 下载网页，查找种子 tid
                        except Exception as e:
                            logger.error(e)
                            # 为了保证准确性，get_info_from_web 不该加 try，这边捕获到异常直接返回
                            # 不 raise 保证后边的限速能运行
                            return
                        else:
                            checked = True
                            _id_tw_0 = {tw._id: tw for tw in self.instances[0].torrent_manager.values() if tw._id}
                            if _id in _id_tw_0:
                                td.update(_id_tw_0[_id])
                                _id_tw_0[_id].in_client = True
                    if not td.tid:
                        td.tid = -1

            self.torrent_manager.update(_id_td)
            # 修复 uploaded_before 的问题，因为有可能种子下载途中有汇报后运行脚本
            tasks = []
            for _id in _id_td:
                tw = self.torrent_manager[_id]
                if tw.tid != -1:
                    if not check_peer_list:
                        if time() - tw.time_added >= tw.announce_interval:
                            tw.uploaded_before = '0 B'
                            self.instances[0].torrent_manager[tw.tid].uploaded_before = '0 B'
                    else:
                        tw.uploaded_before = '0 B'
                        self.instances[0].torrent_manager[tw.tid].uploaded_before = '0 B'
                        tw.true_uploaded = tw.uploaded
                        tasks.append(tw.info_from_peer_list())
            if tasks:
                async with aiohttp.ClientSession() as self.torrent_manager.session:
                    await asyncio.gather(*tasks)

            self.torrent_manager.last_connect = time()
            if update_upload:
                self.instances[0].torrent_manager.last_connect = self.torrent_manager.last_connect

            if checked and magic and not self.instances[0].magic_tasks:
                await self.instances[0].magic()

    @classmethod
    def save_torrents_info(cls):
        TorrentManager.save_data()

    async def get_info_from_web(self):
        try:
            async with aiohttp.ClientSession() as self.session:
                page = await self.request(
                    f'https://u2.dmhy.org/getusertorrentlistajax.php?userid={uid}&type=leeching')
        except Exception as e:
            logger.exception(e)
        else:
            table = BeautifulSoup(page.replace('\n', ''), 'lxml').table
            tid_td = {}
            if table:
                for tr in table.contents[1:]:
                    contents = tr.contents
                    tid = int(contents[1].a['href'][15:-6])
                    tid_td[tid] = TorrentDict(
                        {
                            'tid': tid,
                            'category': int(contents[0].a['href'][26:]),
                            'title': contents[1].a.b.text,
                            'size': contents[2].get_text(' '),
                            'seeder_num': int(contents[3].string),
                            'leecher_num': int(contents[4].string),
                            'uploaded': contents[6].get_text(' '),
                            'downloaded': contents[7].get_text(' '),
                            'promotion': self.get_pro(tr)
                        }
                    )

            for tid in list(self.torrent_manager):
                tw = self.torrent_manager[tid]
                if tid in tid_td:
                    td = tid_td[tid]
                    if (td.get('pro_end_time') or 0) > time():
                        td.promotion = tw.promotion
                    tw.update(td)
                    tw.last_get_time = time()
                    tid_td.pop(tid)
                else:
                    self.torrent_manager.pop(tid)

            for tid, td in tid_td.items():  # 新种子
                td.uploaded_before = td.uploaded
                td.add_time = time()
                if use_client and (tid > min_tid or td.leecher_num > min_leecher_num):
                    async with aiohttp.ClientSession() as self.session:
                        detail_page = await self.request(f'https://u2.dmhy.org/details.php?id={tid}&hit=1')
                    soup = BeautifulSoup(detail_page.replace('\n', ''), 'lxml')
                    td.tz = self.get_tz(soup)
                    tab = soup.find('table', {'width': '90%'})
                    td.date = tab.time.attrs.get('title') or tab.time.text
                    for tr in tab:
                        if tr.td.text in [
                            '种子信息', '種子訊息', 'Torrent Info', 'Информация о торренте',
                            'Torrent Info', 'Информация о торренте'
                        ]:  # 这里的空格是 nbsp，一定不要搞错了
                            td['_id'] = tr.tr.contents[-2].contents[1].strip()
                td.last_get_time = time()

            self.torrent_manager.update(tid_td)
            self.torrent_manager.last_connect = time()


if magic:

    class MagicInfo(UserList):
        def __init__(self, lst=None, mode: int = 0, c: float = 1.549161):
            super(MagicInfo, self).__init__(lst)
            self.mode = mode
            self.c = c
            self.update_ts = int(time()) - 1
            self.uc_24, self.uc_72 = 0, 0
            self.get_mode()

        def __str__(self):
            return f'{self.__class__.__name__}({self.data}, mode={self.mode}, c={self.c})'

        def add_magic(self, to: TorrentWrapper, info):
            self.data.append(info)
            self.uc_24 += info['uc']
            self.uc_72 += info['uc']
            self.change_mode()
            if 86400 + info['ts'] < self.update_ts:
                self.update_ts = 86400 + info['ts']

            user = info['user_other'] if info['user'] == 'OTHER' else info["user"].lower()
            uc = info['uc']
            logger.warning(f"Sent a {info['ur']}x upload and {info['dr']}x download magic to torrent {info['tid']}, "
                           f"user {user}, duration {info['hours']}h, ucoin cost {uc}")
            logger.info(f'Mode: ------ {self.mode}, 24h uc cost: ------ {self.uc_24}, 72h uc cost: ------ {self.uc_72}')
            if uc > 30000 and 'date' in to:
                self.c = uc / self.expected_cost(to, info) * self.c
                logger.info(f'divergence / sqrt(S0): {self.c:.6f}')

        def get_mode(self) -> int:
            """计算 24h 和 72h uc 使用量之和"""
            t = int(time())
            if t >= self.update_ts:
                self.update_ts = t + 86400 * 15
                uc_24, uc_72 = 0, 0
                for info in list(self.data):
                    dt = t - info['ts']
                    if dt < 259200:
                        uc_72 += info['uc']
                        if dt < 86400:
                            uc_24 += info['uc']
                    else:
                        if info['ts'] + info['hours'] * 3600 < t:
                            self.data.remove(info)
                    for t0 in 86400, 259200, info['hours'] * 3600:
                        if t < t0 + info['ts'] < self.update_ts:
                            self.update_ts = t0 + info['ts']
                self.uc_24, self.uc_72 = uc_24, uc_72
                self.change_mode()
            return self.mode

        def change_mode(self):
            old_mode = self.mode
            if self.uc_24 > uc_24_max or self.uc_72 > uc_72_max:
                self.mode = -1
            elif magic_new:
                if not auto_mode:
                    self.mode = default_mode
                else:
                    if self.mode < 0:
                        self.mode = 0
                    mode_max = len(modes)
                    if self.mode >= mode_max:
                        self.mode = mode_max - 1
                    while True:
                        uc_limit = modes[self.mode]['uc_limit']
                        if self.uc_24 > uc_limit['24_max'] or self.uc_72 > uc_limit['72_max']:
                            self.mode += 1
                            if self.mode == mode_max:
                                break
                        elif self.uc_24 < uc_limit['24_min'] and self.uc_72 < uc_limit['72_min']:
                            if self.mode > 0:
                                self.mode -= 1
                            if self.mode == 0:
                                break
                        else:
                            break
            if self.mode != old_mode:
                logger.warning(f'Mode for new torrents change from {old_mode} to {self.mode}')
                self.save_data()

        if use_client:

            def expected_cost(self, to: TorrentWrapper, rule: Dict[str, Any]) -> float:
                """估计 uc 消耗量"""
                ttl = to.delta / 2592000
                ttl = 1 if ttl < 1 else ttl
                h = float(rule.get('hours') or default_hours)
                return self.cal_cost(
                    self.c, float(rule['ur']), float(rule['dr']), rule['user'].upper(), int(h),
                    ttl, to.size, to.total_size
                )

            @staticmethod
            @lru_cache(maxsize=max_cache_size)
            def cal_cost(c: float, ur: float, dr: float, user: str, h: int,
                         ttl: Union[int, float], size=None, total_size: int = None) -> float:
                m = {'SELF': 350, 'OTHER': 500, 'ALL': 1200}[user]
                if total_size:
                    s = total_size // 1024 ** 3 + 1
                else:
                    [num, unit] = size.split(' ')
                    s = 1 if unit in ['MiB', '喵', 'MiБ'] else (
                            int(float(num) * 1024 if unit in ['TiB', '烫', 'TiБ'] else float(num)) + 1
                    )
                return m * c * pow(s, 0.5) * (pow(2 * ur - 2, 1.5) + pow(2 - 2 * dr, 2)) * pow(ttl, -0.8) * pow(h, 0.5)

        def save_data(self):
            with open(magic_info_path, 'w', encoding='utf-8') as f:
                f.write(str(self))


    class Magic(FunctionBase):
        magic_info: MagicInfo = None

        @property
        def mode(self):
            return self.magic_info.get_mode()

        def save_magic_info(self):
            self.magic_info.save_data()

        async def magic(self):
            for self.to in self.torrent_manager.values():
                if self.to.tid == -1:
                    continue
                if self.client is None and '_id' in self.to:
                    if self.to.in_client:
                        continue
                if self.to.is_new:
                    if magic_new:
                        await self.magic_new()
                else:
                    await self.magic_old()
            if self.magic_tasks:
                async with aiohttp.ClientSession() as self.session:
                    await asyncio.gather(*self.magic_tasks)
                    self.save_magic_info()
                self.magic_tasks.clear()

        async def magic_old(self):
            if self.mode != -1:
                if self.to.promotion[1] > 0:
                    data = {'ur': 1, 'dr': 0, 'user': 'SELF', 'hours': 24}
                    if self.to.seeder_num > 0:  # 当然也可以用 check_time，不过我觉得没必要
                        self.print(f'torrent {self.to.tid} - Seeder-num > 0, passed')
                        if not await self.check_duplicate(data):
                            self.magic_tasks.append(self.send_magic(data, self.to))
                    else:
                        self.print(f'torrent {self.to.tid} - No seeder, wait')
                else:
                    self.print(f'torrent {self.to.tid} - Is free')

        if magic_new:

            async def magic_new(self):
                # 根据 uc 使用量选取相应的规则
                if self.mode in [-1, len(modes)]:
                    return
                rules = modes[self.mode]['rules']
                raw_data = []
                up_data = {}
                down_data = {}

                # 计算放魔法的时长
                hours = 24
                if self.to.first_seed_time and self.to.time_added:
                    add_time = time() - self.to.time_added
                    seed_time = time() - self.to.first_seed_time
                    if add_time > 86400 and seed_time > 3600:
                        # 这个情况一般是做种者上传速度很慢需要几天，所以最好一次性放完节约成本
                        if 'total_done' in self.to:
                            progress = self.to.total_done / self.to.total_size
                        else:
                            progress = self.to.downloaded_byte / self.to.size_byte
                        progress = 1 if progress > 1 else 0.01 if progress < 0.01 else progress
                        hours = int((1 - progress) / progress * seed_time / 3600) + 1
                        hours = min(max(hours, 24), 360)

                # 检查每个规则，符合就生成魔法
                # 把上传的魔法和下载的魔法拆开
                # 时长、范围相同的情况下，上传和下载的魔法可以分开放也可以合并，uc 使用量是一样的
                # 具体是否合并取决于时间检查
                for rule in rules:
                    data = self.check_rule(**rule)
                    if isinstance(data, dict):
                        data.setdefault('hours', hours)
                        self.print(f'torrent {self.to.tid} | rule {rule} - Passed. '
                                   f'Will send a magic: {data}')
                        if data['dr'] < 1 < data['ur']:
                            ls = [data, data]
                            ls[0]['dr'] = 1
                            ls[1]['ur'] = 1
                            raw_data.extend(ls)
                        else:
                            raw_data.append(data)
                    elif isinstance(data, str):
                        self.print(f'torrent {self.to.tid} | rule {rule} - Failed. '
                                   f'Reason: {data}')

                # 合并由规则生成的一系列魔法
                # 其实是支持给另一个人放魔法的，但问题是网页显示的是自己的优惠，如果先给自己放了魔法的话可能就不会给另一个人放了
                # 解决的办法是直接查种子的优惠历史，而且只能查一次，反正我是不打算写这个...
                # 至于多个魔法嘛，没有这样的设计，不仅耗费 uc，而且会使程序变得很复杂和让人迷惑
                for data in raw_data:
                    if data['dr'] == 1:
                        if up_data == {}:
                            up_data = data
                        elif data['user'] == 'ALL' and up_data['user'] != 'ALL':
                            up_data = data
                        elif data['ur'] > up_data['ur']:
                            up_data = data
                        elif data['hours'] > up_data['hours']:
                            up_data = data
                    if data['ur'] == 1:
                        if down_data == {}:
                            down_data = data
                        elif data['user'] == 'ALL' and down_data['user'] != 'ALL':
                            down_data = data
                        elif data['dr'] < down_data['dr']:
                            down_data = data
                        elif data['hours'] > down_data['hours']:
                            down_data = data

                # 合并上传和下载的魔法，如果时长范围一致，比如说 2.33x↑1x↓ 和 1x↑0x↓ 合并成 2.33x↑0x↓，以及检查是否重复施加魔法
                if up_data != {} and self.check_time(up_data):
                    magic_data = up_data
                    if down_data != {} and self.check_time(down_data):
                        if up_data['hours'] == down_data['hours'] and up_data['user'] == down_data['user']:
                            magic_data['dr'] = down_data['dr']
                            if not await self.check_duplicate(magic_data):
                                self.magic_tasks.append(self.send_magic(magic_data, self.to))
                            return
                    if not await self.check_duplicate(magic_data):
                        self.magic_tasks.append(self.send_magic(magic_data, self.to))
                if down_data != {} and self.check_time(down_data):
                    magic_data = down_data
                    if not await self.check_duplicate(magic_data):
                        self.magic_tasks.append(self.send_magic(magic_data, self.to))

            def locate_client(self):
                """Detect whether a new torrent is in BT client"""
                _id_tw = {tw._id: tw for tw in self.torrent_manager.values() if tw._id and 'in_client' not in tw}
                all_connected = True

                if _id_tw:
                    with ThreadPoolExecutor(max_workers=len(self.instances) - 1) as executor:
                        futures = [executor.submit(cl.downloading_torrents_info, cl.status_keys) for cl in self.clients]
                        for future in as_completed(futures):
                            try:
                                _id_dict = future.result()
                            except Exception as e:
                                logger.exception(e)
                                all_connected = False
                            else:
                                for _id in list(_id_tw):
                                    if _id in _id_dict:
                                        _id_tw[_id].in_client = True
                                        _id_tw.pop(_id)

                                if not _id_tw:
                                    executor._threads.clear()
                                    break

                if all_connected:  # 如果有些客户端连接不上，可能有些种子不能确定是否客户端
                    for tw in _id_tw.values():
                        tw.in_client = False

            def check_rule(self, **rule) -> Union[str, Dict[str, Any]]:
                """检查魔法规则，如果通过则返回魔法数据
                如果返回 dict，则是检查通过，返回值是魔法信息
                如果返回 str，则是检查失败，返回值是失败的原因
                """
                ur = 1 if rule['ur'] <= self.to.promotion[0] else rule['ur']
                dr = 1 if rule['dr'] >= self.to.promotion[1] else rule['dr']
                if ur == dr == 1:
                    return 'magic already existed'
                if ur != 1 and not 1.3 <= ur <= 2.33:
                    return 'invalid upload rate'
                if dr != 1 and not 0 <= dr <= 0.8:
                    return 'invalid download rate'

                if 'min_size' in rule:
                    if 'total_size' in self.to:
                        if self.to.total_size < rule['min_size']:
                            return "check for 'min_size' failed"
                    elif self.to.size_byte < rule['min_size']:
                        return "check for 'min_size' failed"
                    del rule['min_size']

                if 'max_size' in rule:
                    if 'total_size' in self.to:
                        if self.to.total_size > rule['max_size']:
                            return "check for 'max_size' failed"
                    elif self.to.size_byte > rule['max_size']:
                        return "check for 'max_size' failed"
                    del rule['max_size']

                if 'ur_less_than' in rule:
                    if self.to.promotion[0] >= rule['ur_less_than']:
                        return "check for 'ur_less_than' failed"
                    del rule['ur_less_than']

                if 'dr_more_than' in rule:
                    if self.to.promotion[1] <= rule['dr_more_than']:
                        return "check for 'dr_more_than' failed"
                    del rule['dr_more_than']

                if 'min_uploaded' in rule:
                    if 'total_uploaded' in self.to:
                        if self.to.total_uploaded < rule['min_uploaded']:
                            return "check for 'min_uploaded' failed"
                    elif self.to.uploaded_byte < rule['min_uploaded']:
                        return "check for 'min_uploaded' failed"
                    del rule['min_uploaded']

                if 'min_downloaded' in rule:
                    if 'total_done' in self.to:
                        if self.to.total_done < rule['min_downloaded']:
                            return "check for 'min_downloaded' failed"
                    elif self.to.downloaded_byte < rule['min_downloaded']:
                        return "check for 'min_downloaded' failed"
                    del rule['min_downloaded']

                if 'min_upload_added' in rule:
                    if self.expected_add(rule) < rule['min_upload_added']:
                        return "check for 'min_upload_added' failed"
                    del rule['min_upload_added']

                if 'min_download_reduced' in rule:
                    if self.expected_reduce(rule) < rule['min_download_reduced']:
                        return "check for 'min_download_added' failed"
                    del rule['min_download_reduced']

                if 'max_uc_peer_gb_added' in rule:
                    e_cost = self.expected_cost(rule)
                    e_gb = (self.expected_add(rule) + 1024) / 1024 ** 3
                    if e_cost / e_gb > rule['max_uc_peer_gb_added']:
                        return "check for 'max_uc_peer_gb_added' failed"
                    del rule['max_uc_peer_gb_added']

                if 'max_uc_peer_gb_reduced' in rule:
                    e_cost = self.expected_cost(rule)
                    e_gb = (self.expected_reduce(rule) + 1024) / 1024 ** 3
                    if e_cost / e_gb > rule['max_uc_peer_gb_reduced']:
                        return "check for 'max_uc_peer_gb_reduced' failed"
                    del rule['max_uc_peer_gb_reduced']

                return rule

            def expected_add(self, rule: Dict[str, Any]) -> Union[int, float]:
                """期望的上传量增加值"""
                urr = rule['ur'] - self.to.promotion[0]
                if 'total_uploaded' in self.to:
                    e_up = self.to.total_uploaded / (self.to.total_done + 1024) * self.to.total_size
                    e_add = (e_up - (self.to.true_uploaded_byte or self.to.uploaded_byte)) * urr
                else:
                    uploaded = self.to.true_uploaded_byte or self.to.uploaded_byte
                    downloaded = self.to.true_downloaded_byte or self.to.downloaded_byte
                    size = self.to.size_byte
                    if downloaded < 1024 ** 2:
                        e_add = default_ratio * size * urr
                    else:
                        e_add = (size * uploaded / (downloaded + 1024) - uploaded) * urr
                return e_add

            def expected_reduce(self, rule: Dict[str, Any]) -> Union[int, float]:
                """期望的下载量减少值"""
                if 'total_size' in self.to:
                    size = self.to.total_size
                else:
                    size = self.to.size_byte
                return (size - (self.to.true_downloaded_byte or self.to.downloaded_byte)) * (1 - rule['dr'])

            def expected_cost(self, rule: Dict[str, Any]) -> float:
                """估计 uc 消耗量"""
                return self.magic_info.expected_cost(self.to, rule)

            def check_time(self, data: Dict[str, Any]) -> Union[bool, None]:
                """优化放魔法时间，如果到了放魔法的时间则返回 True"""
                _begin = f'torrent {self.to.tid} | magic {data}: '
                if self.to.get('about_to_re_announce'):
                    self.print(f'{_begin}is about to re-announce, passed')
                    return True
                if 'total_size' not in self.to:
                    if use_client and 'in_client' not in self.to and self.to.is_new:
                        if time() - self.to.add_time > self.to.size_byte / 55 / 1024 ** 2:
                            return True
                        return
                    if self.to.seeder_num > 0:
                        self.print(f'{_begin}Seeder-num > 0, passed')
                        return True
                    else:
                        self.print(f'{_begin}No seeder, wait')
                elif self.to.total_size < 1.5 * self.client.connect_interval * 110 * 1024 ** 2:
                    self.print(f'{_begin}Small size, passed')
                    return True
                elif data['dr'] == 1 and self.to.total_uploaded == 0:
                    self.print(f'{_begin}No upload for up-magic, wait for seeding...')
                    return
                elif data['ur'] == 1 and self.to.total_done == 0:
                    self.print(f'{_begin}No download for down-magic, wait for seeding...')
                    return
                elif self.to.next_announce <= self.to.min_time:
                    self.print(f'{_begin}Will announce in {int(self.to.min_time)}s, passed')
                    return True
                elif data['user'] == 'SELF':
                    if self.to.max_download_speed == -1:
                        if 0 < self.to.eta <= self.to.min_time:
                            if self.to.this_time > 1 and self.to.this_up / self.to.this_time < 52428800:
                                self.print(f'{_begin}About to complete, passed')
                                return True
                            else:
                                self.print(f'{_begin}Wait for limit download speed')
                        else:
                            self.print(f'{_begin}Just wait...')
                    elif self.to.this_up / (self.to.this_time + self.to.min_time) < 52428800:
                        self.print(f'{_begin}About to release download limit and complete, passed')
                        return True
                elif 0 < self.to.eta <= self.to.min_time:
                    self.print(f'{_begin}About to complete, passed')
                    return True
                elif self.to.max_download_speed != -1:
                    self.print(f'{_begin}Others are about to complete, passed')
                    return True
                elif self.to.delta > 1800 - self.to.min_time:
                    self.print(f'{_begin}Others are about to announce, passed')
                    return True
                elif data['ur'] == 1:
                    if self.to.total_size > 15 * 1024 ** 3 and self.to.delta < 120:
                        self.print(f'{_begin}Wait for a while, if anyone going to magic')
                        return
                    if self.to.total_size > 200 * 1024 ** 3:
                        self.print(f'{_begin}Large size. Wait...')
                        return
                    self.print(f'{_begin}Passed')
                    return True
                else:
                    self.print(f'{_begin}Just wait...')

        async def check_duplicate(self, data: Dict[str, Any]) -> Union[bool, None]:
            """
            放魔法前检查是否重复施加魔法，先检查已有魔法，再查看网页种子的优惠信息是否改变
            第一步是未了避免不可预料的错误，比如网页结构改变导致优惠判断失效，或者网页的种子出现重复，或者给别人放魔法也需要检查
            第二步是因为客户端放魔法（循环间隔就是客户端的连接间隔）和爬网页更新种子优惠不是同步的
            """
            tid = self.to.tid
            for info in self.magic_info:
                if tid == info['tid']:
                    if time() - info['ts'] < info['hours'] * 3600:
                        if data['ur'] <= info['ur'] and data['dr'] >= info['dr']:
                            return True

            if 'last_get_time' in self.to and time() - self.to.last_get_time < 0.01 or not self.to.is_new:
                return

            try:
                async with aiohttp.ClientSession() as self.session:
                    page = await self.request(f'https://u2.dmhy.org/details.php?id={tid}&hit=1')
                soup = BeautifulSoup(page.replace('\n', ''), 'lxml')
                table = soup.find('table', {'width': '90%'})
                if table:
                    for tr in table:
                        if tr.td.text in ['流量优惠', '流量優惠', 'Promotion', 'Тип раздачи (Бонусы)']:
                            pro = self.get_pro(tr)
                            if pro != self.to.promotion:
                                self.to.promotion = pro
                                if tr.time:
                                    dt = datetime.strptime(tr.time.get('title') or tr.time.text, '%Y-%m-%d %H:%M:%S')
                                    pro_end_time = pytz.timezone(self.get_tz(soup)).localize(dt).timestamp()
                                else:
                                    pro_end_time = time() + 86400
                                if tid in self.instances[0].torrent_manager:
                                    self.instances[0].torrent_manager[tid].update(
                                        {'promotion': pro, 'pro_end_time': pro_end_time}
                                    )
                                logger.warning(f'Magic for torrent {self.to.tid} already existed')
                                return True
                else:
                    logger.error(f'Torrent {self.to.tid} was not found')
                    self.to.tid = -1
                    return True
            except Exception as e:
                logger.error(e)

        async def send_magic(self, _data: Dict[str, Union[int, float, str]], to: TorrentWrapper):
            tid = to.tid
            try:
                data = {'action': 'magic', 'divergence': '', 'base_everyone': '', 'base_self': '', 'base_other': '',
                        'torrent': tid, 'tsize': '', 'ttl': '', 'user_other': '', 'start': 0, 'promotion': 8,
                        'comment': ''}
                data.update(_data)
                response = await self.request('https://u2.dmhy.org/promotion.php?test=1', method='post', data=data)
                _json = json.loads(response)
                if _json['status'] == 'operational':
                    uc = int(float(BeautifulSoup(_json['price'], 'lxml').span['title'].replace(',', '')))
                    _post = await self.request('https://u2.dmhy.org/promotion.php', method='post', retries=0, data=data)
                    if re.match(r'^<script.+<\/script>$', _post):
                        self.magic_info.add_magic(to, {**_data, **{'tid': tid, 'ts': int(time()), 'uc': uc}})
                    else:
                        logger.error(f'Failed to send magic to torrent {tid} ------ data: {data}')
            except Exception as e:
                logger.exception(e)

if use_limit:

    class Limit(FunctionBase):
        async def limit_speed(self):
            """将两次汇报间的平均速度限制到 50M/s 以下

            解释一下什么是超速。tracker 并不知道种子的上传速度情况，因为种子每次汇报的只有上传量、下载量和剩余完成量，
            而 peer 列表的瞬时速度，是由最近两次汇报的上传量差/最近两次汇报的时间差计算的，
            只要这个值小于 50M/s，就会把两次汇报上传量的差值加到账号的实际上传，乘以种子优惠比率加到虚拟上传，否则的话就不计算。
            也就是说只要在相邻两次汇报之间的上传量不超过 (50M/s * 两次汇报的时间间隔) 就行。

            通常情况下种子会以固定的周期向 tracker 汇报，不超速情况下两次汇报间的最大上传量是固定的，
            只有在快要传满的时候限速就行。但问题是，种子完成时也会向 tracker 汇报，
            这个时间是未知的，如果种子下载时间小于 30 分钟而限速是按照 30 分钟汇报间隔计算，那么在完成汇报时就会超速。
            解决这个问题有两种方法，一种在完成前最后一次汇报后进行特殊处理，检测到平均速度即将超过 50M/s 就限速，
            这样一来不管什么时候完成都不会超速；另一种就是在快要完成时限速下载以延后完成时间，
            但无论如何到下一次定期汇报时间点也是要汇报的。这里使用的是第二种方法。"""
            f1 = 0
            for self.to in self.torrent_manager.values():

                if self.to.tid == -1:
                    # 旧种子默认不限速，因为没有查详情页不知道 id，不知道上传汇报的上传量。
                    # 但是当上传速度超过 50M/s 后就有超速可能，这时候就需要查找 id
                    if self.to.upload_payload_rate > 52428800 and not self.to.get('404'):
                        logger.debug(f'Try to find tid of {self.to._id} --- ')
                        try:
                            await self.update_tid()
                            await self.update_upload()
                            self.to.ex = True
                            continue
                        except:
                            pass
                    continue
                if self.to.upload_payload_rate > 52428800:
                    self.to.ex = True

                if not self.to.get('ex'):
                    continue

                if 'date' not in self.to:  # 按理说是不会有这种情况的
                    logger.error(f"Could not find 'date' of torrent {self.to.tid}")
                    continue
                if 'last_get_time' not in self.to:  # 按理说是不会有这种情况的
                    logger.error(f"Could not find 'last_get_time' of torrent {self.to.tid}")
                    continue

                if time() - self.to.this_time + 1 > self.to.last_get_time and f1 == 0:
                    # 刚汇报完，更新上次汇报的上传量
                    if self.to.total_uploaded > 0:
                        try:
                            await self.update_upload()
                            f1 = 1
                        except:
                            pass

                if variable_announce_interval:
                    await self.optimize_announce_time()

                self.limit_download_speed()

                if self.to.this_time < 0:  # 汇报后 tracker 还没有返回
                    continue

                await self.limit_upload_speed()

        def limit_download_speed(self):
            this_time = self.to.this_time
            this_up = self.to.this_up
            if self.to.max_download_speed == -1:
                if this_time > 2 and this_up / this_time > 52428800:
                    ps = 0
                    m_t = self.to.min_time
                    if self.to.max_upload_speed != -1:
                        # 上传限速时，如果限速值很低，给其他 peer 上传速度低，
                        # 其他 peer 给自己的上传速度也会很低，所以会严重拖慢下载进度，eta 值会变大。
                        # 但是出种后其他 peer 变成做种状态，这时候的上传策略一般是根据下载者的下载速度，
                        # 跟下载者的上传速度没有关系，由于先前没有下载限速，所以这时候种子可能突然变成满速下载，
                        # 不仅下载时间短而且客户端可能变得很难连接，可能导致限速失败。
                        # 所以这里在上传限速时检查其他 peer 的进度，在其他 peer 完成前提前下载限速。
                        m_t = 2 * self.to.min_time
                        p0 = 1 - 1610612736 / self.to.total_size
                        try:
                            for peer in self.to.torrent_status(['peers'])['peers']:
                                if peer['progress'] > p0:
                                    ps += 1
                        except:
                            pass
                    if 0 < self.to.eta <= m_t or self.to.max_upload_speed != -1 and ps > 20:
                        # 平均速度超过 50M/s 并且快要完成，开始下载限速
                        max_download_speed = (self.to.total_size - self.to.total_done) / (
                                this_up / 52428800 - this_time + 30) / 1024
                        self.to.set_download_limit(max_download_speed)
                        logger.warning(f'Begin to limit download speed of torrent {self.to.tid}. '
                                       f'Value ------- {max_download_speed:.2f}K')
            elif this_time > 0:
                if this_up / this_time >= 52428800:
                    # 已有下载限速，调整限速值
                    if self.to.download_payload_rate / 1024 < 2 * self.to.max_download_speed:
                        max_download_speed = (self.to.total_size - self.to.total_done) / (
                                this_up / 52428800 - this_time + 60) / 1024
                        max_download_speed = min(max_download_speed, 512000)
                        if max_download_speed > 1.5 * self.to.max_download_speed:
                            max_download_speed = 1.5 * self.to.max_download_speed
                            self.to.set_download_limit(max_download_speed)
                            logger.debug(f'Change the max download speed of torrent {self.to.tid} '
                                         f'to {max_download_speed:.2f}K')
                        elif max_download_speed < self.to.max_download_speed:
                            max_download_speed = max_download_speed / 1.5
                            self.to.set_download_limit(max_download_speed)
                            logger.debug(f'Change the max download speed of torrent {self.to.tid} '
                                         f'to {max_download_speed:.2f}K')
                else:
                    # 平均速度已降到 50M/s 以下，解除限速，之似乎发现 tracker 计算的时间精度比秒更精确？
                    # 无论如何 next_announce 是个整数必须 +1s
                    self.to.set_upload_limit(51200)
                    self.to.set_download_limit(-1)
                    logger.info(f'Removed download speed limit of torrent {self.to.tid}.')
                    for _ in range(30):
                        sleep(1)
                        try:
                            if self.to.torrent_status(['state'])['state'] == 'Seeding':
                                self.to.set_upload_limit(-1)
                                return
                        except:
                            pass
                    logger.error(f'Torrent {self.to.tid} | failed to remove upload limit')

        async def limit_upload_speed(self):
            this_time = self.to.this_time
            this_up = self.to.this_up
            announce_interval = self.to.announce_interval

            if 10 < self.to.eta + 10 < self.to.next_announce:
                eta = self.to.eta + 10
            else:
                eta = self.to.next_announce
            # eta 代表到下次汇报之前还可以正常上传的时间，
            # 如果完成时间在下次周期汇报之前，那么完成时就会汇报，到下次汇报的时间就是到完成的时间，
            # 虽然可能通过下载限速延长完成时间，但是在延长的那段时间由于已经出种并且下载速度有限制，
            # 通常并不能上传很多，所以可以正常上传的时间就按照完成时间计算

            if self.to.max_upload_speed == -1:
                res = 10 * self.to.upload_payload_rate
                if this_up + res + 6291456 * eta > announce_interval * 52428800:
                    # 上次汇报到现在的上传量即将超过一个汇报周期内允许的不超速的最大值，开始上传限速.
                    # 限速值不要太低，太低会跟不上进度影响之后的上传
                    self.to.set_upload_limit(6144)
                    logger.warning(f'Begin to limit upload speed of torrent {self.to.tid}. Value ------- {6144}K')
                    self.to._t = time()
            else:
                # 已经开始上传限速，调整限速值
                if self.to.max_upload_speed == 5120:
                    # 在 optimize_announce_time 用到了这个，也可以手动限速到 5120k 等待汇报
                    if this_up / this_time < 52428800 and this_time >= 900:
                        await self.re_an()
                        self.to.set_upload_limit(-1)
                        logger.info('Average upload speed below 50MiB/s, remove 5120K up-limit')
                elif this_time < 120:  # 已经汇报完，解除上传限速
                    self.to.set_upload_limit(-1)
                    logger.info(f'Removed upload speed limit of torrent {self.to.tid}.')
                elif self.to.upload_payload_rate / 1024 < 2 * self.to.max_upload_speed:
                    max_upload_speed = (announce_interval * 52428800 - this_up) / (eta + 10) / 1024
                    # 计算上传限速值。把 +10 变成 +1，甚至可以限速到 49.999，不过也很容易超（不知道下载用固态会不会好点）
                    if max_upload_speed > 51200:
                        self.to.set_upload_limit(-1)
                        logger.info(f'Removed upload speed limit of torrent {self.to.tid}.')
                    elif max_upload_speed < 0:  # 上传量超过了一个汇报间隔内不超速的最大值
                        if this_up / this_time < 209715200:
                            if await self.re_an():
                                logger.error(f'Failed to limit upload speed limit of torrent {self.to.tid} '
                                             f'because the upload exceeded')
                        else:
                            if self.to.max_upload_speed != 1:
                                self.to.set_upload_limit(1)
                    elif 8192 < max_upload_speed < 51200 and eta > 180:
                        # 调整限速值减小余量，deluge 上传量一般比限速值低
                        self.to.set_upload_limit(51200)
                        logger.info(f'Set 51200K upload limit for torrent {self.to.tid}')
                    elif 8192 < max_upload_speed < 16384 and eta > 60:
                        self.to.set_upload_limit(16384)
                        logger.info(f'Set 16384K upload limit for torrent {self.to.tid}')
                    else:
                        if announce_interval * 52428800 - this_up > 94371840 and max_upload_speed < 3072:
                            max_upload_speed = 3072  # 这个速度下载还不会卡住
                        if announce_interval * 52428800 - this_up > 31457280 and max_upload_speed < 1024:
                            max_upload_speed = 1024  # 这个速度在出种前会卡死下载
                        if self.to.max_upload_speed != max_upload_speed:
                            if max_upload_speed == 5120:
                                max_upload_speed = 5119
                            self.to.set_upload_limit(max_upload_speed)
                            if max_upload_speed in [3072, 1024]:
                                logger.debug(f'Set {max_upload_speed}K upload limit to torrent {self.to.tid}')
                            elif '_t' not in self.to or '_t' in self.to and time() - self.to._t > 120:
                                # 2 分钟输出一次，当然也可以直接输出(改成 > 0)，不过我觉得有点频繁
                                logger.debug(f'Change the max upload speed for torrent {self.to.tid} '
                                             f'to {max_upload_speed:.2f}K')
                                self.to._t = time()

        if variable_announce_interval:

            async def optimize_announce_time(self):
                """尽量把完成前最后一次汇报时间调整到最合适的点，粗略计算，没有严格讨论问题。

                解释一下，假设一个种子的下载时间超过汇报时长，并且这个种子每次汇报前都经过限速并且两次汇报间的平均速度接近 50M/s，
                那么可以把这个种子到完成时的平均速度按 50M/s 计算，要获得尽可能多的上传量则需要使完成时间尽可能延后。
                假设这个种子不限速时上传速度是一个稳定的数值，那么最后一次汇报时间有一个点能使完成时间延长最多。

                但实际并非总是如人意，比如最后一次定期汇报时间刚好在完成时，就没有任何可以延长下载时间的余地。
                这个函数就是解决这个问题，在合适的时间强制汇报来调整完成前最后一次汇报时间。"""
                this_time = self.to.this_time
                this_up = self.to.this_up
                announce_interval = self.to.announce_interval

                i = self.torrent_manager.deque_length
                if 'detail_progress' not in self.to:
                    self.to.detail_progress = deque(maxlen=i)
                self.to.detail_progress.append((self.to.total_uploaded, self.to.total_done, time()))
                if len(self.to.detail_progress) != i or this_time < 30 or self.to.max_upload_speed == 5120:
                    return
                _list = self.to.detail_progress
                # 计算 5 分钟内平均下载速度和平均上传速度
                upspeed = (_list[i - 1][0] - _list[0][0]) / (_list[i - 1][2] - _list[0][2])
                dlspeed = (_list[i - 1][1] - _list[0][1]) / (_list[i - 1][2] - _list[0][2])
                if upspeed > 52428800 and dlspeed > 0 and _list[0][1] != 0:
                    # complete_time 是估计的完成时间，
                    # perfect_time 是估计的最佳的最后一次汇报时间，
                    # earliest 是计算的最早能强制汇报且不超速的时间。
                    # 如果最佳汇报时间可以强制汇报并且不超速，直接汇报就行，实际并非总是如此。
                    # 有可能最早能汇报的时间在最佳时间点之后，这时候就需要比较在最早能汇报的时间汇报和不强制汇报
                    complete_time = (self.to.total_size - self.to.total_done) / dlspeed + time()
                    perfect_time = complete_time - announce_interval * 52428800 / upspeed
                    if this_up / this_time > 52428800:
                        earliest = (this_up - 52428800 * this_time) / 45 / 1024 ** 2 + time()
                    else:
                        earliest = time()
                    if earliest - (time() - this_time) < 900:
                        return
                    if earliest > perfect_time:
                        if time() >= earliest:
                            if (this_up + upspeed * 20) / this_time > 52428800:
                                await self.re_an()
                            return
                        if earliest < perfect_time + 60:
                            self.to.set_upload_limit(5120)
                            logger.info(f'Set 5120K upload limit for torrent {self.to.tid}, waiting for re-announce')
                        else:
                            if time() - this_time > perfect_time:
                                return
                            _eta1 = complete_time - earliest
                            if _eta1 < 120:
                                return
                            earliest_up = (earliest - time() + this_time) * 5248800 + _eta1 * upspeed
                            default_up = announce_interval * 52428800
                            _eta2 = complete_time - (time() + self.to.next_announce)
                            if _eta2 > 0:
                                default_up += _eta2 * upspeed
                            if earliest_up > default_up:
                                self.to.set_upload_limit(5120)
                                logger.info(f'Set 5120K upload limit for torrent {self.to.tid}, '
                                            f'waiting for re-announce')

        async def re_an(self):
            if not (self.to.lft and time() - self.to.lft < 900):
                self.to.about_to_re_announce = True
                _to = self.to
                if magic:
                    await self.magic()
                self.to = _to
                sleep(1)

                self.to.re_announce()
                return True

        async def update_tid(self):
            """根据 hash 搜索种子 id"""
            url = f'https://u2.dmhy.org/torrents.php?search={self.to._id}&search_area=5'
            try:
                async with aiohttp.ClientSession() as self.session:
                    text = await self.request(url)
                soup = BeautifulSoup(text.replace('\n', ''), 'lxml')
                table = soup.select('table.torrents')
                if table:
                    self.to.tid = int(table[0].contents[1].contents[1].a['href'][15:-6])
                    date = table[0].contents[1].contents[3].time
                    self.to.date = date.get('title') or date.get_text(' ')
                    self.to.tz = self.get_tz(soup)
                    if self.to.tid in self.instances[0].torrent_manager:
                        self.to.update(self.instances[0].torrent_manager[self.to.tid])
                    logger.debug(f'{self.to._id} --> {self.to.tid}')
                else:
                    self.to['404'] = True
                    logger.info(f'{self.to._id} was not found in u2')
            except Exception as e:
                logger.error(e)

        async def update_upload(self):
            try:
                async with aiohttp.ClientSession() as self.session:
                    page = await self.request(
                        f'https://u2.dmhy.org/getusertorrentlistajax.php?userid={uid}&type=leeching')
                table = BeautifulSoup(page.replace('\n', ''), 'lxml').table
                if not table:
                    return
                tmp_info = []
                tid_tw = {tw.tid: tw for tw in self.torrent_manager.values() if tw.tid not in [-1, None]}
                for tr in table.contents[1:]:
                    tid = int(tr.contents[1].a['href'][15:-6])
                    if tid in tid_tw:
                        tw: TorrentWrapper = tid_tw[tid]
                        data = {'uploaded': tr.contents[6].get_text(' '), 'last_get_time': time()}
                        if tw.date and tw.last_get_time:
                            if time() - tw.this_time + 1 > tw.last_get_time:
                                if (tw.total_uploaded + tw.byte(tw.uploaded_before, -1) - tw.byte(data['uploaded'], 1)
                                ) > 300 * 1024 ** 2 * (tw.this_time + 2):
                                    tw.true_uploaded = data['uploaded']
                                if tw.true_uploaded or tw.last_announce_time:
                                    tmp_info.append(tw)
                                if data['uploaded'].split(' ')[0] != '0':
                                    self.print(f"Last announce upload of torrent {tid} is {data['uploaded']}")
                        tw.update(data)
                tasks = [to.info_from_peer_list() for to in tmp_info]
                async with aiohttp.ClientSession() as self.torrent_manager.session:
                    await asyncio.gather(*tasks)
            except Exception as e:
                logger.exception(e)

if magic:
    if use_limit:
        Run = type('RunMagicLimit', (Magic, Limit), {})
    else:
        Run = type('RunMagic', (Magic,), {})
elif use_limit:
    Run = type('RunLimit', (Limit,), {})
else:
    Run = type('Run', (FunctionBase,), {})


async def run_job(self):
    if self.client is not None:
        while True:
            try:
                await self.get_info_from_client()
                if magic:  # 顺序不能颠倒
                    await self.magic()
                if limit:
                    await self.limit_speed()
            except Exception as e:
                logger.exception(e)
            finally:
                sleep(self.client.connect_interval)
    else:
        if use_client:
            while True:
                sleep(1)
                if all(instance.client.connected for instance in self.instances[1:]):
                    logger.info('All clients connected')
                    sleep(10)
                    break
        while True:
            try:
                if magic:
                    await self.get_info_from_web()
                    if magic_new and use_client:
                        self.locate_client()
                    await self.magic()
            except Exception as e:
                logger.exception(e)
            finally:
                sleep(interval)


Run.run = run_job


class Main:
    def __init__(self, cls=Run):
        self.cls = cls
        nest_asyncio.apply()

        if magic and magic_new and auto_mode and len(modes) > 1:
            for i in range(len(modes) - 1):
                if modes[i]['uc_limit']['24_max'] < modes[i + 1]['uc_limit']['24_min']:
                    raise ValueError(f"modes[{i}]['uc_limit']['24_max'] < modes[{i + 1}]['uc_limit']['24_min']")
                if modes[i]['uc_limit']['72_max'] < modes[i + 1]['uc_limit']['72_min']:
                    raise ValueError(f"modes[{i}]['uc_limit']['72_max'] < modes[{i + 1}]['uc_limit']['72_min']")

        if magic and os.path.exists(magic_info_path):
            with open(magic_info_path, 'r', encoding='utf-8') as f:
                try:
                    self.cls.magic_info = eval(f.read())
                except:
                    pass
        if magic and self.cls.magic_info is None:
            self.cls.magic_info = MagicInfo([])

        if os.path.exists(torrents_info_path):
            with open(torrents_info_path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        eval(line)
                    except:
                        pass

        logger.remove(handler_id=0)
        level = 'DEBUG' if enable_debug_output else 'INFO'
        logger.add(sink=sys.stderr, level=level)

        if magic or use_limit:
            ins = self.cls(None)
            if use_client:
                [self.cls(client) for client in clients]
                ins.clients = clients_copy
                logger.add(sink=log_path, level=level, rotation='5 MB', filter=BTClient.log_filter)
                return
        logger.add(sink=log_path, level=level, rotation='5 MB')

    def run(self):
        if self.cls.instances:
            try:
                with ThreadPoolExecutor(max_workers=len(self.cls.instances)) as executor:
                    futures = {executor.submit(asyncio.run, instance.run()): instance.client
                               for instance in self.cls.instances}
                    # 因为 deluge 很容易失联，如果有多个客户端，要分配多个线程让各个客户端时间上不受牵制。
                    # 第一个线程客户端是 None，这个线程的任务就是定期爬网页以及放魔法(对不在客户端的种子)，
                    # 单独开限速时，这个线程什么也不做。之后的线程每个都对应有一个客户端，给在客户端的种子放魔法以及限速
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except BaseException as er:
                            client = futures[future]
                            if client is None:
                                logger.critical('Thread 0 terminated unexpectedly')
                            else:
                                logger.critical(f'Thread for deluge on {client.host} terminated unexpectedly')
                            logger.exception(er)
            except (KeyboardInterrupt, SystemExit):
                self.cls.save_torrents_info()
                os._exit(0)
        else:
            logger.info('The program will do nothing')


if __name__ == '__main__':
    Main().run()
