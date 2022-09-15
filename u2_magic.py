"""
给下载中的种子放魔法，python3.6 及以上应该能运行
依赖：pip3 install PyYAML requests bs4 lxml deluge-client loguru func-timeout

支持客户端 deluge，其它的客户端自己去写类吧
支持配置多个客户端，可以任意停止和重新运行
检查重复，检查 uc 使用量，尽可能减少爬网页的次数
放魔法区分新种和旧种，因为新种魔法使用量太多，支持自定义魔法规则
不支持对单个种子同时施加一个以上的上传或者下载魔法
可以根据 24h 和 72h 的 uc 使用量自动切换规则
根据客户端的种子信息调整放魔法的时间，最小化 uc 使用量
对下载的种子进行限速，防止上传失效

用法：
按 yaml 语法修改 config，填上必填信息，按注释说明修改其它信息
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


import os
import sys
import yaml
import subprocess
import paramiko
import pytz

from functools import reduce
from copy import deepcopy
from datetime import datetime
from collections import deque
from time import time, sleep
from typing import List, Dict, Tuple, Union, Any
from requests import request, Response, ReadTimeout
from loguru import logger
from bs4 import BeautifulSoup, Tag
from abc import ABCMeta, abstractmethod
from func_timeout import func_set_timeout, FunctionTimedOut
from deluge_client import LocalDelugeRPCClient, FailedToReconnectException
from concurrent.futures import ThreadPoolExecutor, as_completed

config = '''
uid:    
# uid 必填，可以是自己的或者别人的

proxies:  
# 代理
    # http: http://127.0.0.1:10809
    # https: http://127.0.0.1:10809

headers:  
# http 请求头
    cookie: nexusphp_u2=  
    # cookie 必填，注意等号两边不能有空格

    user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4814.0 Safari/537.36 Edg/99.0.1135.6

magic:
    enable: True  
    # 魔法的总开关，为 False 不施加任何魔法，为 True 则至少会给旧种施加魔法

    magic_new: False  
    # 只有为 True 才会给新种施加魔法

    interval: 180  
    # 获取下载页面的时间间隔，魔法一旦开启就会按这个间隔爬下载页面

    auto_mode: False  
    # 如果为真，新种放魔法自动切换魔法规则（请仔细检查魔法规则，已有配置会消耗巨量 uc）

    default_mode: 0  
    # 如果 auto_mode 不为真，则此项为新种的魔法规则，这个数字，就是 modes 列表的序号（第一个为 0）

    default_hours: 24  
    # 如果魔法规则没有指定魔法时长，则默认魔法为此时长

    min_tid: 47586  
    # 种子 id 超过这个值纳入新种的判断范围
    # 这个参数存在的原因在于，下载种子页没有提供种子的发布时间信息，下载人数也没法判断（刚加入的时候可能下载数为 0）
    # 但我又不想每个种子都去查详情页（想象一下同时下载 1000 个种子），所以决定将 tid 大于一定数值才去判断

    min_leecher_num: 5  
    # 种子下载人数（网页显示的数值）超过这个值纳入新种的判断范围

    min_leecher_to_seeder_ratio: 0.1  
    # 只有 下载人数 / (做种人数+1) 超过这个值才可能是新种，如果这个值比较大，则新种只包括未出种的种子

    uc_24_max: 6000000  
    # 24h 内 uc 消耗量超过这个值，则不放魔法

    uc_72_max: 12000000  
    # 72h 内 uc 消耗量超过这个值，则不放魔法

    default_ratio: 3  
    # 种子默认分享率，用于魔法规则估计上传量

    min_connect_times_before_announce: 3.6  
    # 这个值是检查放魔法的时间用的，比如说客户端连接时间 5s，
    # 给自己放魔法的话，在距离汇报时间小于 3.6 × 5s 的时候

    modes:  
    # 这是新种的魔法规则，这下面的子项我称之为”模式“，可以配置任意套模式，程序中用 mode 表示（其实是用序号代替这个模式）
        -
            uc_limit:  
            # uc 使用限制，四个参数都要填
            # 如果 24h uc 使用量超过 24_max 或者 72h uc 使用量超过 72_max，则 mode +1
            # 如果最后一级还是超过 24_max 或者 72h uc，则新种不放魔法
            # 如果 24h uc 使用量小于 24_max 且 72h uc 使用量小于 72_max 且 mode > 0，则 mode -1
            # 注意对于相邻的两级，高一级的 24_min 要不大于于低一级的 24_max，且高一级的 72_min 要不大于于低一级的 72_max
            # 否则在程序计算 mode 时可能会陷入死循环

                24_max: 1500000
                72_max: 4300000
                24_min: 0
                72_min: 0

            rules:
            # 规则可以配置任意条，如果检查规则通过，则可以生成一个魔法
            # 如果上传下载比率都不为 1 则会拆成一个上传和一个下载魔法（uc 使用量不受影响）
            # 每次只选择一个上传魔法和一个下载魔法
            # 具体优先级是，首先优先选择范围为所有人魔法，然后优先选择上传比率更高或者下载比率更低的魔法，最后优先选择时效最长的魔法

                -
                # 首先必须有 ur（上传比率）、dr（下载比率）、user（有效用户）
                # hours 为时长，24~360 之间的整数，可以不写，会采用 default_hours
                # ur 可选的值：1.3~2.33 或 1，ur 可选的值：0~0.8 或 1，两者不能同时为 1
                # user：给自己放填 SELF，所有人放填 ALL，给另一个人放填 OTHER
                # 如果要给另一个人放魔法，最好另外开一个脚本
                # 另外也可以加上 comment
                # 其它一些键为程序制定的检查项，具体见 MagicAndLimit 类的 check_rule 函数
                # 如果没有其他选项，则不进行任何检查，对所有种子都施加这个魔法

                    ur: 2.33
                    dr: 1
                    user: ALL
                    min_size: 16146493595
                    max_size: 107374182400
                    min_uploaded: 1073741824
                    ur_less_than: 2   
                -
                    ur: 2.33
                    dr: 1
                    user: SELF
                    min_uploaded: 1073741824
                    min_upload_added: 57123065037
                    max_uc_peer_gb_added: 771
                -
                    ur: 1
                    dr: 0
                    user: ALL                 
        -
            uc_limit:
                24_max: 2200000
                72_max: 5600000
                24_min: 1400000
                72_min: 4100000
            rules:  
                -
                    ur: 2.33
                    dr: 1
                    user: SELF
                    min_uploaded: 1073741824
                    min_upload_added: 57123065037
                    max_uc_peer_gb_added: 771
                -
                    ur: 1
                    dr: 0
                    user: ALL                     
        -
            uc_limit:
                24_max: 3000000
                72_max: 7500000
                24_min: 2050000
                72_min: 5300000
            rules:
                -
                    ur: 2.33
                    dr: 1
                    user: SELF
                    min_uploaded: 5368709120
                    min_upload_added: 85684597555
                    max_uc_peer_gb_added: 545
                -
                    ur: 1
                    dr: 0
                    user: ALL 
                    min_size: 16146493595
                    max_size: 214748364800
                -
                    ur: 1
                    dr: 0
                    user: SELF 
        -
            uc_limit:
                24_max: 4500000
                72_max: 10000000
                24_min: 2900000
                72_min: 7000000
            rules:
                -
                    ur: 2.33
                    dr: 1
                    user: SELF
                    min_uploaded: 16106127360
                    min_upload_added: 214211493888
                    max_uc_peer_gb_added: 545
                -
                    ur: 1
                    dr: 0
                    user: SELF 
        -
            uc_limit:
                24_max: 6000000
                72_max: 12000000
                24_min: 4200000
                72_min: 9400000
            rules:
                -
                    ur: 1
                    dr: 0
                    user: SELF 
                    min_download_reduced: 5368709120
                    max_uc_peer_gb_reduced: 4727

enable_clients: False  
# 为了防止有人不会删，加上这个好了，如果有下载客户端配置完记得改为 True

clients:  
    # 可以配置任意个下载客户端，也可以不配置

    - 
        type: de  
        # 目前只支持 de

        host: 127.0.0.1
        # IP

        port: 58846  
        # deamon 端口

        username:   
        # 用户名，本地客户端不用填

        password:   
        # 密码，本地客户端不用填

        connect_interval: 5  
        # 读取客户端状态的间隔，根据经验设为 5s 一般加入种子 8s 内可以放完魔法（如果马上就要放的话）

        min_announce_interval: 300  
        # libtorrent 默认值是 300

        tc:  
        # 失联时对网卡限速，10G 以下带宽不推荐使用，谨慎填写信息
        
            enable: False
            # 是否开启

            device: eno1
            # 网卡名

            initial_rate: 100
            # 初始限速值(Mbps)，尽量一步到位

            min_rate: 10
            # 最低限速值(Mbps)，不要太低，会导致机器失联

            timeout: 30
            # deluge 响应超时(s)

            root_pass: 
            # root 密码，用于远程执行命令，本地不需要，但是要用 root 权限运行

limit:  
# 懒得配置参数了，要调节自己改代码

    enable: True  
    # 是否开启限速处理
    
    variable_announce_interval: False
    # 开启后会尝试调节完成前最后一次汇报时间

log_path:  
# 日志路径（完整文件名），不填则使用默认值

data_path:  
# 程序数据保存路径（完整文件名），不填则使用默认值

enable_debug_output: True
# 为真时会输出 debug 信息
'''

conf = yaml.load(config, yaml.FullLoader)


class ConfigError(Exception):
    pass


class BtClient(metaclass=ABCMeta):  # 这个基类规定了 BT 客户端必须实现的功能

    @abstractmethod
    def call(self, method, *args, **kwargs):
        pass

    @abstractmethod
    def set_upload_limit(self, _id: str, rate: int):
        pass

    @abstractmethod
    def set_download_limit(self, _id: str, rate: int):
        pass

    @abstractmethod
    def reannounce(self, _id):
        pass

    @abstractmethod
    def downloading_torrents_info(self, keys: list):
        pass

    @abstractmethod
    def torrent_status(self, _id: str, keys: list):
        pass


class Deluge(LocalDelugeRPCClient, BtClient):  # 主要是把 call 重写了一下，因为 deluge 太容易失联了
    timeout = 10

    def __init__(self,
                 host: str = '127.0.0.1',
                 port: int = 58846,
                 username: str = '',
                 password: str = '',
                 decode_utf8: bool = True,
                 automatic_reconnect: bool = True,
                 min_announce_interval: int = 300,
                 connect_interval: int = 5,
                 tc: dict = None
                 ):
        super(Deluge, self).__init__(host, port, username, password, decode_utf8, automatic_reconnect)
        self.min_announce_interval = min_announce_interval
        self.connect_interval = connect_interval
        self.enable_tc = tc['enable']
        self.io_busy = False
        self.tc_limited = False
        self.device = tc['device']
        self.op_timeout = tc['timeout']
        self.initial_rate = tc['initial_rate']
        self.tc_rate = self.initial_rate
        self.min_rate = tc['min_rate']
        self.passwd = tc['root_pass']
        self.ssh_hd = paramiko.SSHClient()
        self.ssh_hd.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if self.enable_tc:
            self.run_cmd(f'tc qdisc del dev {self.device} root >> /dev/null 2>&1')

    def log_filter(self, record):  # 失联的时候，硬盘非常繁忙，不要写 log，会消耗大量时间
        return 1 - self.io_busy

    def call_retry(self, method, *args, **kwargs):
        if not self.connected and method != 'daemon.login':
            for i in range(5):
                try:
                    self.reconnect()
                    logger.info(f'Connected to deluge client on {self.host}')
                    break
                except:
                    sleep(0.3 * 2 ** i)
        return super(Deluge, self).call(method, *args, **kwargs)

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
            if isinstance(e, FailedToReconnectException):
                logger.error(f'Failed to reconnect to deluge client! Host  -------  {self.host}')
            elif isinstance(e, TimeoutError):
                logger.error(f'{e.__class__.__name__}: {e}')
            elif e.__class__.__name__ == 'BadLoginError':
                logger.error(f'Failed to connect to deluge client on {self.host}, Password does not match')
            elif not self.enable_tc:
                raise
            if self.enable_tc:
                self.io_busy = True
                if isinstance(e, FunctionTimedOut):
                    logger.error(f'{e.__module__}.{e.__class__.__name__}: {e.msg}')
                return self.call_on_fail(method, *args, **kwargs)

    def call_on_fail(self, method, *args, **kwargs):
        while True:
            try:
                if self.tc_rate >= self.min_rate:
                    self.run_cmd(f'tc qdisc del dev {self.device} root >> /dev/null 2>&1')
                    cmd = f'tc qdisc add dev {self.device} root handle 1: tbf rate {self.tc_rate:.2f}mbit ' \
                          f'burst {self.tc_rate / 10:.2f}mbit latency 1s >> /dev/null 2>&1'
                    self.run_cmd(cmd)
                    self.tc_limited = True
                    if self.tc_rate < self.initial_rate:
                        self.io_busy = False
                    logger.warning(f'Set the upload limit for {self.device} on {self.host} to {self.tc_rate:.2f}mbps')
                    self.tc_rate = self.tc_rate / 2
                try:
                    self.reconnect()
                    res = super(Deluge, self).call(method, *args, **kwargs)
                    self.io_busy = False
                    return res
                except:
                    logger.error(f'Still cannot access the deluge instance on {self.host}')
            except BaseException as e:
                logger.exception(e)

    def run_cmd(self, cmd):
        if self.host == '127.0.0.1':
            subprocess.Popen(cmd, shell=True)
        else:
            self.ssh_hd.connect(hostname=self.host, username='root', password=self.passwd)
            self.ssh_hd.exec_command(cmd)

    def set_upload_limit(self, _id, rate):
        return self.core.set_torrent_options([_id], {'max_upload_speed': rate})

    def set_download_limit(self, _id, rate):
        return self.core.set_torrent_options([_id], {'max_download_speed': rate})

    def reannounce(self, _id):
        return self.core.force_reannounce([_id])

    def downloading_torrents_info(self, keys):
        return self.core.get_torrents_status({'state': 'Downloading'}, keys)

    def torrent_status(self, _id, keys):
        return self.core.get_torrent_status(_id, keys)


"""
import qbittorrentapi
class Qbittorrent(qbittorrentapi.Client,BtClient):
    pass
"""


class MagicAndLimit:
    mode = 0
    magic_info: List[Dict] = []
    coefficient = 1.549161

    @classmethod
    def init(cls):
        with open(data_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('mode = '):
                    cls.mode = eval(line.lstrip('mode = '))
                if line.startswith('magic_info = '):
                    cls.magic_info = eval(line.lstrip('magic_info = '))
                if line.startswith('coefficient = '):
                    cls.coefficient = eval(line.lstrip('coefficient = '))

    def __init__(self, client: Union[Deluge, None]):
        self.client = client
        self.torrents_info = []
        self.m_conf = conf['magic']
        self.l_conf = conf['limit']
        self.to = {}
        self.last_connect = time()
        self.request_args = {'headers': conf['headers'], 'proxies': conf['proxies']}
        self.status_keys = ['download_payload_rate', 'eta', 'max_download_speed', 'max_upload_speed',
                            'name', 'next_announce', 'num_seeds', 'total_done', 'total_uploaded',
                            'total_size', 'tracker', 'time_added', 'upload_payload_rate']
        self.clients = []

    def run(self):
        if self.client is not None:
            while True:
                try:
                    self.torrents_info = self.get_info_from_client()
                    if self.m_conf['enable'] or self.l_conf['enable']:
                        self.fix_next_announce()
                    if self.m_conf['enable']:  # 顺序不能颠倒
                        self.magic()
                    if self.l_conf['enable']:
                        self.limit_speed()
                except Exception as e:
                    logger.exception(e)
                finally:
                    sleep(self.client.connect_interval)
        else:
            while True:
                sleep(1)
                if not any(not c.client.connected for i, c in enumerate(t_client) if i > 0):
                    logger.info('All clients connected')
                    sleep(10)
                    break
            while True:
                try:
                    if self.m_conf['enable']:
                        torrents = self.get_info_from_web()
                        self.torrents_info = self.locate_client(torrents)
                        self.magic()
                except Exception as e:
                    logger.exception(e)
                finally:
                    sleep(self.m_conf['interval'])

    def rq(self, method: str, url: str, timeout: Union[int, float] = 10, retries: int = 5, **kw) \
            -> Union[Response, None]:  # 网页请求
        if local_client and local_client.tc_limited:  # 限速爬不动
            raise Exception('Waiting for release tc limit')
        for i in range(retries):
            try:
                html = request(method, url=url, **self.request_args, timeout=timeout, **kw)
                code = html.status_code
                if code < 400:
                    if method == 'get':
                        if url != f'https://u2.dmhy.org/getusertorrentlistajax.php?userid={conf["uid"]}&type=leeching':
                            logger.debug(f'Downloaded page: {url}')
                        else:
                            logger.trace(f'Downloaded page: {url}')
                        if '<title>Access Point :: U2</title>' in html.text or 'Access Denied' in html.text:
                            logger.error('Your cookie is wrong')
                    return html
                elif i == retries - 1:
                    raise Exception(f'Failed to request... '
                                    f'method: {method}, url: {url}, kw: {kw}'
                                    f' ------ status code: {code}')
                elif code in [502, 503]:
                    delay = int(html.headers.get('Retry-After') or '30')
                    logger.error(f'Will attempt to request {url} after {delay}s')
                    sleep(delay)
            except Exception as e:
                if i == retries - 1:
                    raise
                elif isinstance(e, ReadTimeout):
                    timeout += 20

    def get_info_from_web(self) -> List[Dict[str, Any]]:
        torrents: List[Dict] = []  # 用来存放种子信息
        _info: List[Dict] = []  # 用来存放客户端已有种子信息

        # ********** 第一步，下载网页分析
        page = self.rq('get', f'https://u2.dmhy.org/getusertorrentlistajax.php?userid={conf["uid"]}&type=leeching').text
        table = BeautifulSoup(page.replace('\n', ''), 'lxml').table
        if table:
            for tr in table.contents[1:]:
                torrent = {}
                conts = tr.contents
                torrent['tid'] = tid = int(conts[1].a['href'][15:-6])
                torrent['category'] = int(conts[0].a['href'][26:])
                torrent['title'] = conts[1].a.b.text
                torrent['size'] = conts[2].get_text(' ')
                torrent['seeder_num'] = int(conts[3].string)
                torrent['leecher_num'] = int(conts[4].string)
                torrent['uploaded'] = conts[6].get_text(' ')
                torrent['downloaded'] = conts[7].get_text(' ')
                torrent['promotion'] = self.get_pro(tr)

                # ************ 第二步，和 torrent_info 已有信息合并
                for _torrent in self.torrents_info:
                    if torrent['tid'] == _torrent['tid']:
                        if (_torrent.get('pro_end_time') or 0) > time():
                            torrent['promotion'] = _torrent['promotion']
                        _torrent.update(torrent)
                        torrent.update(_torrent)
                        break

                if tid > self.m_conf['min_tid'] or torrent['leecher_num'] > self.m_conf['min_leecher_num']:
                    # 旧种子不需要知道 hash，因为不需要在客户端的线程放魔法

                    # ********** 第三步，已有信息查不到 hash，获取种子详细页
                    # 这一步是将种子 tid 与 _id 联系起来的入口
                    if '_id' not in torrent:
                        detail_page = self.rq('get', f'https://u2.dmhy.org/details.php?id={tid}&hit=1').text
                        soup1 = BeautifulSoup(detail_page.replace('\n', ''), 'lxml')
                        torrent['tz'] = self.get_tz(soup1)
                        table1 = soup1.find('table', {'width': '90%'})
                        torrent['date'] = table1.time.attrs.get('title') or table1.time.text
                        for tr1 in table1:
                            if tr1.td.text in ['种子信息', '種子訊息', 'Torrent Info', 'Информация о торренте',
                                               'Torrent Info', 'Информация о торренте']:
                                torrent['_id'] = tr1.tr.contents[-2].contents[1].strip()

                torrent['last_get_time'] = time()
                torrents.append(torrent)

        self.torrents_info = torrents
        return torrents

    def locate_client(self, torrents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect whether a new torrent is in BT client"""
        _info: Dict[str, Dict[str, Any]] = {}  # 存放客户端获取的当前种子信息
        _ids: set = set({})  # 存放所有需要知道是否在客户端的种子 hash
        [_ids.add(torrent['_id']) for torrent in torrents if '_id' in torrent and 'in_client' not in torrent]
        all_connected = True

        if len(_ids) > 0 and len(t_client) > 1:

            # 由于可能出现不可预料的延迟，采用线程任务
            with ThreadPoolExecutor(max_workers=len(t_client) - 1) as executor:
                """修复 Segmentation fault
                发现 deepcopy 不能解决问题，单独给第一个线程创建对象算了，如果我的想法是对的那么问题已经解决了
                如果还是异常退出，可以用 monitor.py，检测到脚本退出后自动运行脚本"""
                futures = [executor.submit(cl.downloading_torrents_info, self.status_keys) for cl in self.clients]
                for future in as_completed(futures):
                    try:
                        _info.update(future.result())
                    except Exception as e:
                        logger.exception(e)
                        all_connected = False
                    else:
                        for _id in list(_ids):
                            for hash_id, data in _info.items():
                                if hash_id == _id:
                                    _ids.remove(_id)
                                    [to.update({'in_client': True}) for to in torrents if to.get('_id') == _id]

                        if len(_ids) == 0:
                            executor._threads.clear()
                            break

        if all_connected:  # 如果有些客户端连接不上，可能有些种子不能确定是否客户端
            [to.update({'in_client': False}) for to in torrents if '_id' in to and 'in_client' not in to]
        return torrents

    def get_info_from_client(self) -> List[Dict[str, Any]]:
        """
        读取客户端种子的状态，并且与已知信息合并
        由于客户端只有种子的 hash 信息，而放魔法需要知道种子 id
        当然可以直接在网站搜索 hash，但只有新种才需要，为了避免浪费服务器资源
        采用对比的方式合并种子信息，旧种子的 id 将会被设置为 -1
        """
        # ********** 第一步，从 BT 客户端获取当前下载的种子的状态
        info = self.client.downloading_torrents_info(self.status_keys)
        if info is None:
            return self.torrents_info

        torrents: List[Dict] = []  # 存放种子信息
        _info: List[Dict] = []  # 用来存放网页获取的种子信息
        f1 = 0  # 用来标志是否访问了下载页面，此函数内最多访问一次

        for _id, data in info.items():
            if data['tracker'] and 'daydream.dmhy.best' in data['tracker']:
                del data['tracker']
                data['_id'] = _id

                # ********** 第二步，更新之前的 torrent_info 信息
                for torrent in self.torrents_info:
                    if _id == torrent['_id']:
                        if data['total_done'] > 0 and 'first_seed_time' not in torrent:
                            torrent['first_seed_time'] = time()
                        torrent.update(data)
                        data.update(torrent)
                        # 等价于 [data.setdefault(key, val) for key, val in torrent.items()]
                        break

                # ********** 第三步，更新网页获取的种子信息，这一步也是必做，因为要更新上传下载量
                for _torrent in t_client[0].torrents_info:
                    if _id == _torrent.get('_id') or data.get('tid') == _torrent['tid']:
                        if '_id' not in _torrent:
                            _torrent['_id'] = _id
                            _torrent['in_client'] = True
                        data.update(_torrent)
                        '''但是这会导致另一个潜在的 bug，如果单独限速，t_client[0] 是不工作的
                        更新 uploaded 时需要更新 t_client[0] 的 torrents_info 的对应信息,
                        否则到了这里 uploaded 会变为原来的值'''
                        break

                # ********** 第四步，已有信息都查不到，获取下载页面分析
                if 'tid' not in data:
                    if f1 == 0:
                        try:
                            t_client[0].get_info_from_web()
                            '''没有用 locate_client，是为了避免多线程同时使用同一个 deluge 对象'''
                            for to in t_client[0].torrents_info:
                                if to.get('_id') == data['_id']:
                                    to['in_client'] = True
                                    data.update(to)
                            f1 = 1
                        except Exception as e:
                            logger.exception(e)

                    # ********** 第五步，更新网页后还是查不到，标记 tid 为 -1，
                    # 之后客户端的线程不会对这个种子放魔法，这个种子的魔法会由爬网页的线程施加
                    if 'tid' not in data:
                        data['tid'] = -1

                torrents.append(data)

        if f1 == 1 and self.m_conf['enable']:
            t_client[0].magic()

        self.last_connect = time()
        return torrents

    @staticmethod
    def get_pro(tr: Tag) -> List[Union[int, float]]:
        """
        tr: 兼容三种 tr: 种子页每行 tr，下载页每行 tr，详情页显示优惠信息的行 tr
        返回上传下载比率，如果控制面板关掉了优惠显示，返回的结果可能与实际不符，会在检查魔法是否重复的时候修正
        """
        pro = {'ur': 1.0, 'dr': 1.0}
        pro_dict = {'free': {'dr': 0.0}, 'twoup': {'ur': 2.0}, 'halfdown': {'dr': 0.5}, 'thirtypercent': {'dr': 0.3}}
        if tr.get('class'):  # 高亮显示
            [pro.update(data) for key, data in pro_dict.items() if key in tr['class'][0]]
        td = tr.tr and tr.select('tr')[1].td or tr.select('td')[1]
        pro_dict_1 = {'free': {'dr': 0.0}, '2up': {'ur': 2.0}, '50pct': {'dr': 0.5}, '30pct': {'dr': 0.3}, 'custom': {}}
        for img in td.select('img') or []:  # 图标显示
            if not [pro.update(data) for key, data in pro_dict_1.items() if key in img['class'][0]]:
                pro[{'arrowup': 'ur', 'arrowdown': 'dr'}[img['class'][0]]] = float(img.next.text[:-1].replace(',', '.'))
        for span in td.select('span') or []:  # 标记显示
            [pro.update(data) for key, data in pro_dict.items() if
             key in (span.get('class') and span['class'][0] or '')]
        return list(pro.values())

    @classmethod
    def write_info(cls):  # 文件中写入程序数据，最小化程序运行中断带来的影响
        with open(data_path, 'r', encoding='utf-8') as f1, \
                open(f'{data_path}.bak', 'w', encoding='utf-8') as f2:
            to_info = {i: c.torrents_info for i, c in enumerate(t_client)}
            syntax_map = {'mode = ': cls.mode,
                          'magic_info = ': cls.magic_info,
                          'coefficient = ': cls.coefficient,
                          'torrents_info = ': to_info
                          }
            for line in f1:
                tmp = [_begin for _begin in list(syntax_map.keys()) if line.startswith(_begin)]
                if tmp:
                    f2.write(f'{tmp[0]}{syntax_map[tmp[0]]}\n')
                    del syntax_map[tmp[0]]
                else:
                    f2.write(line)
            for _begin, var in syntax_map.items():
                f2.write(f'{_begin}{var}\n')
        os.remove(data_path)
        os.rename(f'{data_path}.bak', data_path)

    @staticmethod
    def byte(st: str, flag: int) -> int:
        """
        将表示体积的字符串转换为字节，考虑四舍五入
        网站显示的的数据都是四舍五入保留三位小数
        """
        [num, unit] = st.split(' ')
        _pow = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB',
                '蚌', '氪', '喵', '寄', '烫', '皮',
                'Б', 'KiБ', 'MiБ', 'GiБ', 'TiБ', 'PiБ'
                ].index(unit) % 6
        flag = 0 if flag == 0 else flag / abs(flag)
        return int((float(num.replace(',', '.')) + 0.0005 * flag) * 1024 ** _pow)

    @property
    def deta(self) -> int:  # 返回种子发布时间与当前的时间差
        dt = datetime.strptime(self.to['date'], '%Y-%m-%d %H:%M:%S')
        return int(time() - pytz.timezone(self.to['tz']).localize(dt).timestamp())

    @staticmethod
    def get_tz(soup: Tag) -> str:
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        return [tz_info[len(pre):-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]

    def magic(self):
        for self.to in self.torrents_info:
            if self.to['tid'] == -1:
                continue
            if self.client is None and '_id' in self.to:
                if self.to.get('in_client'):
                    continue
            if self.is_new:
                if self.m_conf['magic_new']:
                    self.magic_new()
            else:
                self.magic_old()

    def magic_old(self):
        if self.change_mode() != -1:
            if self.to['promotion'][1] > 0:
                data = {'ur': 1, 'dr': 0, 'user': 'SELF', 'hours': 24}
                if self.to['seeder_num'] > 0:  # 当然也可以用 check_time，不过我觉得没必要
                    self.print(f"torrent {self.to['tid']} - Seeder-num > 0, passed")
                    if not self.check_duplicate(data):
                        self.send_magic(data)
                else:
                    self.print(f"torrent {self.to['tid']} - No seeder, wait")
            else:
                self.print(f"torrent {self.to['tid']} - Is free")

    def magic_new(self):
        # ********** 根据 uc 使用量选取相应的规则
        mode = self.change_mode()
        if mode in [-1, len(self.m_conf['modes'])]:
            return
        rules = self.m_conf['modes'][mode]['rules']
        raw_data = []
        up_data = {}
        down_data = {}

        # ********** 计算放魔法的时长
        hours = 24
        if 'first_seed_time' in self.to and 'time_added' in self.to:
            add_time = time() - self.to['time_added']
            seed_time = time() - self.to['first_seed_time']
            if add_time > 86400 and seed_time > 3600:
                # 这个情况一般是做种者上传速度很慢需要几天，所以最好一次性放完节约成本
                if 'total_done' in self.to:
                    progress = self.to['total_done'] / self.to['total_size']
                else:
                    progress = self.byte(self.to['downloaded'], 0) / self.byte(self.to['size'], 0)
                progress = 1 if progress > 1 else 0.01 if progress < 0.01 else progress
                hours = int((1 - progress) / progress * seed_time / 3600) + 1
                hours = min(max(hours, 24), 360)

        # ********** 检查每个规则，符合就生成魔法
        # ********** 把上传的魔法和下载的魔法拆开
        # ********** 时长、范围相同的情况下，上传和下载的魔法可以分开放也可以合并，uc 使用量是一样的
        # ********** 具体是否合并取决于时间检查
        for rule in rules:
            _rule = deepcopy(rule)
            data = self.check_rule(_rule)
            if isinstance(data, dict):
                data.setdefault('hours', hours)
                self.print(f"torrent {self.to['tid']} | rule {rule} - Passed. "
                           f"Will send a magic: {data}")
                if data['dr'] < 1 < data['ur']:
                    ls = [data, data]
                    ls[0]['dr'] = 1
                    ls[1]['ur'] = 1
                    raw_data.extend(ls)
                else:
                    raw_data.append(data)
            elif isinstance(data, str):
                self.print(f"torrent {self.to['tid']} | rule {rule} - Failed. "
                           f"Reason: {data}")

        # ********** 合并由规则生成的一系列魔法
        # ********** 其实是支持给另一个人放魔法的，但问题是网页显示的是自己的优惠，如果先给自己放了魔法的话可能就不会给另一个人放了
        # ********** 解决的办法是直接查种子的优惠历史，而且只能查一次，反正我是不打算写这个...
        # ********** 至于多个魔法嘛，没有这样的设计，不仅耗费 uc，而且会使程序变得很复杂和让人迷惑
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
                    if not self.check_duplicate(magic_data):
                        self.send_magic(magic_data)
                    return
            if not self.check_duplicate(magic_data):
                self.send_magic(magic_data)
        if down_data != {} and self.check_time(down_data):
            magic_data = down_data
            if not self.check_duplicate(magic_data):
                self.send_magic(magic_data)

    def check_rule(self, rule: Dict[str, Any]) -> Union[str, Dict[str, Any]]:
        """
        检查魔法规则，如果通过则返回魔法数据
        如果返回 dict，则是检查通过，返回值是魔法信息
        如果返回 str，则是检查失败，返回值是失败的原因
        """
        ur = 1 if rule['ur'] <= self.to['promotion'][0] else rule['ur']
        dr = 1 if rule['dr'] >= self.to['promotion'][1] else rule['dr']
        if ur == dr == 1:
            return 'magic already existed'
        if ur != 1 and not 1.3 <= ur <= 2.33:
            return 'invalid upload rate'
        if dr != 1 and not 0 <= dr <= 0.8:
            return 'invalid download rate'

        if 'min_size' in rule:
            if 'total_size' in self.to:
                if self.to['total_size'] < rule['min_size']:
                    return "check for 'min_size' failed"
            elif self.byte(self.to['size'], 0) < rule['min_size']:
                return "check for 'min_size' failed"
            del rule['min_size']

        if 'max_size' in rule:
            if 'total_size' in self.to:
                if self.to['total_size'] > rule['max_size']:
                    return "check for 'max_size' failed"
            elif self.byte(self.to['size'], 0) > rule['max_size']:
                return "check for 'max_size' failed"
            del rule['max_size']

        if 'ur_less_than' in rule:
            if self.to['promotion'][0] >= rule['ur_less_than']:
                return "check for 'ur_less_than' failed"
            del rule['ur_less_than']

        if 'dr_more_than' in rule:
            if self.to['promotion'][1] <= rule['dr_more_than']:
                return "check for 'dr_more_than' failed"
            del rule['dr_more_than']

        if 'min_uploaded' in rule:
            if 'total_uploaded' in self.to:
                if self.to['total_uploaded'] < rule['min_uploaded']:
                    return "check for 'min_uploaded' failed"
            elif self.byte(self.to['uploaded'], 0) < rule['min_uploaded']:
                return "check for 'min_uploaded' failed"
            del rule['min_uploaded']

        if 'min_downloaded' in rule:
            if 'total_done' in self.to:
                if self.to['total_done'] < rule['min_downloaded']:
                    return "check for 'min_downloaded' failed"
            elif self.byte(self.to['downloaded'], 0) < rule['min_downloaded']:
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

    def expected_add(self, rule: Dict[str, Any]) -> Union[int, float]:  # 期望的上传量增加值
        urr = rule['ur'] - self.to['promotion'][0]
        if 'total_uploaded' in self.to:
            e_up = self.to['total_uploaded'] / (self.to['total_done'] + 1024) * self.to['total_size']
            e_add = (e_up - self.byte(self.to.get('true_uploaded') or self.to['uploaded'], 0)) * urr
        else:
            uploaded = self.byte(self.to['uploaded'], 0)
            downloaded = self.byte(self.to.get('true_downloaded') or self.to['downloaded'], 0)
            size = self.byte(self.to['size'], 0)
            if downloaded < 1024 ** 2:
                e_add = self.m_conf['default_ratio'] * size * urr
            else:
                e_add = (size * uploaded / (downloaded + 1024) - uploaded) * urr
        return e_add

    def expected_reduce(self, rule: Dict[str, Any]) -> Union[int, float]:  # 期望的下载量减少值
        if 'total_size' in self.to:
            size = self.to['total_size']
        else:
            size = self.byte(self.to['size'], 0)
        return (size - self.byte(self.to.get('true_downloaded') or self.to['downloaded'], 0)) * (1 - rule['dr'])

    def expected_cost(self, rule: Dict[str, Any]) -> float:  # 估计 uc 消耗量
        c = self.coefficient
        m = {'SELF': 350, 'OTHER': 500, 'ALL': 1200}[rule['user']]
        if 'total_size' in self.to:
            s = int(self.to['total_size'] / 1024 ** 3) + 1
        else:
            [num, unit] = self.to['size'].split(' ')[0]
            s = 1 if unit in ['MiB', '喵', 'MiБ'] else (
                    int(float(num) * 1024 if unit in ['TiB', '烫', 'TiБ'] else float(num)) + 1)
        ttl = self.deta / 2592000
        ttl = 1 if ttl < 1 else ttl
        ur, dr = float(rule['ur']), float(rule['dr'])
        h = float(rule.get('hours') or self.m_conf['default_hours'])
        e_cost = m * c * pow(s, 0.5) * (pow(2 * ur - 2, 1.5) + pow(2 - 2 * dr, 2)) * pow(ttl, -0.8) * pow(h, 0.5)
        return e_cost

    def check_time(self, data: Dict[str, Any]) -> Union[bool, None]:
        """优化放魔法时间，如果到了放魔法的时间则返回 True"""
        _begin = f"torrent {self.to['tid']} | magic {data}: "
        if self.to.get('about_to_reannounce'):
            self.print(f"{_begin}is about to re-announce, passed")
            return True
        if 'total_size' not in self.to:
            if 'in_client' not in self.to and self.is_new:
                '''新种，本该交给客户端的线程放魔法，但客户端失联，希望等待恢复
                但如果一直失联超过一定时间，就必须放魔法了，时间是估算的，如果是 10G 带宽还要把时间估小一点'''
                if self.deta > self.byte(self.to['size'], 0) / 55 / 1024 ** 2:
                    return True
                return
            '''除此之外，就是已确定不在客户端端下载的种子'''
            if self.to['seeder_num'] > 0:
                self.print(f'{_begin}Seeder-num > 0, passed')
                return True
            else:
                self.print(f'{_begin}No seeder, wait')
        elif self.to['total_size'] < 1.5 * self.client.connect_interval * 110 * 1024 ** 2:
            # 体积小于一定值，马上放魔法，防止过快下载导致来不及放魔法
            self.print(f'{_begin}Small size, passed')
            return True
        elif data['dr'] == 1 and self.to['total_uploaded'] == 0:
            # 上传量为 0 则不放上传的魔法
            self.print(f'{_begin}No upload for up-magic, wait for seeding...')
            return
        elif data['ur'] == 1 and self.to['total_done'] == 0:
            # 下载量为 0 则不放下载的魔法
            self.print(f'{_begin}No download for down-magic, wait for seeding...')
            return
        elif self.to['next_announce'] <= self.min_time:
            # 快要到汇报时间则放魔法
            self.print(f"{_begin}Will announce in {int(self.min_time)}s, passed")
            return True
        elif data['user'] == 'SELF':
            if self.to['max_download_speed'] == -1:
                if 0 < self.to['eta'] <= self.min_time:
                    # 因为有 limit 函数，所以不能根据 eta 直接判断。另外，eta=0 是下载速度为 0
                    if self.this_time > 1 and self.this_up / self.this_time < 52428800:
                        self.print(f"{_begin}About to complete, passed")
                        return True
                    else:
                        self.print(f"{_begin}Wait for limit download speed")
                else:
                    self.print(f"{_begin}Just wait...")
            elif self.this_up / (self.this_time + self.min_time) < 52428800:
                self.print(f"{_begin}About to release download limit and complete, passed")
                return True
        elif 0 < self.to['eta'] <= self.min_time:
            # 给所有人放魔法，如果自己快要下完则放魔法
            self.print(f"{_begin}About to complete, passed")
            return True
        elif self.to['max_download_speed'] != -1:
            self.print(f"{_begin}Others are about to complete, passed")
            return True
        elif self.deta > 1800 - self.min_time:
            # 给所有人放魔法，如果快要到 30 分钟则放魔法
            self.print(f"{_begin}Others are about to announce, passed")
            return True
        elif data['ur'] == 1:
            if self.to['total_size'] > 15 * 1024 ** 3 and self.deta < 120:
                self.print(f"{_begin}Wait for a while, if anyone going to magic")
                # 其实是给其他放魔法留出一点时间，但总体上来说，free 放得越早越好，不管对自己还是其他人来说
                # if time.time() - self.to['first_seed_time'] < 120:
                return
            if self.to['total_size'] > 200 * 1024 ** 3:
                self.print(f"{_begin}Large size. Wait...")
                # 体积太大了，再等等吧-_-}\
                return
            self.print(f"{_begin}Passed")
            return True
        else:
            self.print(f"{_begin}Just wait...")

    def print(self, st: str):  # 只输出一次信息，避免频繁输出
        if 'statement' not in self.to:
            self.to['statement'] = []
        if st not in self.to['statement']:
            function = sys._getframe(1).f_code.co_name
            line = sys._getframe(1).f_lineno
            _logger = logger.patch(lambda record: record.update({'function': function, 'line': line}))
            _logger.debug(st)
            self.to['statement'].append(st)

    def check_duplicate(self, data: Dict[str, Any]) -> Union[bool, None]:
        """
        放魔法前检查是否重复施加魔法，先检查已有魔法，再查看网页种子的优惠信息是否改变
        第一步是未了避免不可预料的错误，比如网页结构改变导致优惠判断失效，或者网页的种子出现重复，或者给别人放魔法也需要检查
        第二步是因为客户端放魔法（循环间隔就是客户端的连接间隔）和爬网页更新种子优惠不是同步的
        """
        for _info in self.magic_info:
            if self.to['tid'] == _info['tid']:
                if time() - _info['ts'] < _info['hours'] * 3600:
                    if data['ur'] <= _info['ur'] and data['dr'] >= _info['dr']:
                        return True
        if 'last_get_time' in self.to and time() - self.to['last_get_time'] < 0.01 or not self.is_new:
            return
        try:
            page = self.rq('get', f'https://u2.dmhy.org/details.php?id={self.to["tid"]}&hit=1').text
            soup = BeautifulSoup(page.replace('\n', ''), 'lxml')
            table = soup.find('table', {'width': '90%'})
            if table:
                for tr in table:
                    if tr.td.text in ['流量优惠', '流量優惠', 'Promotion', 'Тип раздачи (Бонусы)']:
                        pro = self.get_pro(tr)
                        if pro != self.to['promotion']:
                            self.to['promotion'] = pro
                            if tr.time:
                                dt = datetime.strptime(tr.time.get('title') or tr.time.text, '%Y-%m-%d %H:%M:%S')
                                pro_end_time = pytz.timezone(self.get_tz(soup)).localize(dt).timestamp()
                            else:
                                pro_end_time = time() + 86400
                            [_torrent.update({'promotion': self.to['promotion'], 'pro_end_time': pro_end_time}) 
                             for _torrent in t_client[0].torrents_info if _torrent['tid'] == self.to['tid']]
                            logger.warning(f'Magic for torrent {self.to["tid"]} already existed')
                            return True
            else:
                logger.error(f"Torrent {self.to['tid']} was not found")
                self.to['tid'] = -1
                return True
        except Exception as e:
            logger.error(e)

    @property
    def is_new(self) -> bool:  # 是否为新种
        if self.to['tid'] > self.m_conf['min_tid'] or self.to['leecher_num'] > self.m_conf['min_leecher_num']:
            if self.to['leecher_num'] / (self.to['seeder_num'] + 1) > self.m_conf['min_leecher_to_seeder_ratio']:
                return True
        return False

    @property
    def min_time(self) -> Union[int, float]:
        last_interval = time() - self.last_connect
        li = min(max(last_interval, self.client.connect_interval), 6 * self.client.connect_interval)
        return self.m_conf['min_connect_times_before_announce'] * li

    @property
    def this_up(self) -> int:  # 当前种子自上次汇报的上传量
        if 'uploaded_before' in self.to:
            _before = self.byte(self.to['uploaded_before'], 1)
        else:
            _before = 0
        _now = self.byte(self.to.get('true_uploaded') or self.to['uploaded'], -1)
        return self.to['total_uploaded'] - _now + _before

    @property
    def this_time(self) -> int:  # 当前种子距离上次汇报的时间
        return self.announce_interval - self.to['next_announce'] - 1

    @property
    def announce_interval(self) -> int:  # 当前种子汇报间隔
        dt = self.deta
        if dt < 86400 * 7:
            return max(1800, self.client.min_announce_interval)
        elif dt < 86400 * 30:
            return max(2700, self.client.min_announce_interval)
        else:
            return max(3600, self.client.min_announce_interval)

    def send_magic(self, _data: Dict[str, Union[int, float, str]]):
        tid = self.to['tid']
        url = f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}'

        try:
            soup = BeautifulSoup(self.rq('get', url).text, 'lxml')
            data = {h['name']: h['value'] for h in soup.find_all('input', {'type': 'hidden'})}
            data.update({'user_other': conf['uid'], 'start': 0, 'promotion': 8, 'comment': ''})
            data.update(_data)
            response = self.rq('post', 'https://u2.dmhy.org/promotion.php?test=1', data=data).json()
            if response['status'] == 'operational':
                uc = int(float(BeautifulSoup(response['price'], 'lxml').span['title'].replace(',', '')))
                url = f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}'
                _post = self.rq('post', url, retries=1, data=data)
                if _post.status_code == 200:
                    self.magic_info.append({**_data, **{'tid': tid, 'ts': int(time()), 'uc': uc}})
                    self.write_info()
                    user = data['user_other'] if data['user'] == 'OTHER' else data["user"].lower()
                    logger.warning(f'Sent a {data["ur"]}x upload and {data["dr"]}x download magic to torrent {tid}, '
                                   f'user {user}, duration {data["hours"]}h, ucoin cost {uc}')
                    uc_24, uc_72 = self.total_uc_cost()
                    logger.info(f'Mode: ------ {self.mode}, 24h uc cost: ------ {uc_24}, 72h uc cost: ------ {uc_72}')
                    # _to = deepcopy(self.to)
                    # del _to['statement']
                    # logger.debug(f'torrent info | {_to}')  # debug 用，感觉输出不是很好看
                    if uc > 30000 and 'date' in self.to:
                        co = uc / self.expected_cost(data) * self.coefficient
                        self.__class__.coefficient = co
                        logger.info(f'divergence / sqrt(S0): {co:.6f}')
                else:
                    logger.error(f'Failed to send magic to torrent {tid} ------ status code: {_post.status_code}'
                                 f' ------ data: {data}')
        except Exception as e:
            logger.exception(e)

    @classmethod
    def total_uc_cost(cls) -> Tuple[int, int]:  # 计算 24h 和 72h uc 使用量之和
        uc_24 = 0
        uc_72 = 0
        tmp = []
        for info in cls.magic_info:
            dt = int(time()) - info['ts']
            if dt < 259200:
                tmp.append(info)
                uc_72 += info['uc']
                if dt < 86400:
                    uc_24 += info['uc']
        cls.magic_info = tmp
        return uc_24, uc_72

    @classmethod
    def change_mode(cls) -> int:
        """根据 uc 使用量选取规则。

        为什么要动态规则呢，可能是因为我有选择困难症，不知道怎么放魔法好。
        其实可以优化 uc 使用，使用量少就多放些魔法，否则就少放些魔法。

        新种大部分有地图炮魔法的时候，魔法系数稳步增长，也就是说同样情况下魔法越来越贵。
        这是因为全站虚拟分享率在增长(看看公式里的 divergence 系数)，
        没有 free 的时候魔法系数就会下跌，也就是说放魔法还起到调节魔法价格的作用，
        这也是为什么我不希望总是全部放 free 的原因"""
        m_conf = conf['magic']
        old_mode = cls.mode
        uc_24, uc_72 = cls.total_uc_cost()

        if uc_24 > m_conf['uc_24_max'] or uc_72 > m_conf['uc_72_max']:
            cls.mode = -1

        elif m_conf['magic_new']:
            if not m_conf['auto_mode']:
                cls.mode = m_conf['default_mode']

            else:
                if cls.mode < 0:
                    cls.mode = 0
                mode_max = len(m_conf['modes'])
                if cls.mode >= mode_max:
                    cls.mode = mode_max - 1

                # ********** 注意了这里有坑，配置不当会导致死循环
                while True:
                    uc_limit = m_conf['modes'][cls.mode]['uc_limit']
                    if uc_24 > uc_limit['24_max'] or uc_72 > uc_limit['72_max']:
                        cls.mode += 1
                        if cls.mode == mode_max:
                            break
                    elif uc_24 < uc_limit['24_min'] and uc_72 < uc_limit['72_min']:
                        if cls.mode > 0:
                            cls.mode -= 1
                        if cls.mode == 0:
                            break
                    else:
                        break

        if cls.mode != old_mode:
            logger.warning(f'Mode for new torrents change from {old_mode} to {cls.mode}')
            cls.write_info()
        return cls.mode

    def limit_speed(self):
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
        for self.to in self.torrents_info:

            if self.to['tid'] == -1:
                '''旧种子默认不限速，因为没有查详情页不知道 id，不知道上传汇报的上传量。
                但是当上传速度超过 50M/s 后就有超速可能，这时候就需要查找 id'''
                if self.to['upload_payload_rate'] > 52428800 and not self.to.get('404'):
                    logger.debug(f"Try to find tid of {self.to['_id']} --- ")
                    try:
                        self.update_tid()
                        self.update_upload()
                        continue
                    except:
                        pass
                else:
                    self.print(f"Will not limit speed of {self.to['_id']}")
                continue

            if 'date' not in self.to:  # 按理说是不会有这种情况的
                logger.error(f"Could not find 'date' of torrent {self.to['tid']}")
                continue
            if 'last_get_time' not in self.to:  # 按理说是不会有这种情况的
                logger.error(f"Could not find 'last_get_time' of torrent {self.to['tid']}")
                continue

            if time() - self.this_time + 2 > self.to['last_get_time'] and f1 == 0:
                # 刚汇报完，更新上次汇报的上传量
                if self.to['total_uploaded'] > 0:
                    try:
                        self.update_upload()
                        f1 = 1
                    except:
                        pass

            if self.l_conf['variable_announce_interval']:
                self.optimize_announce_time()

            self.limit_download_speed()

            if self.this_time < 0:  # 汇报后 tracker 还没有返回
                continue

            self.limit_upload_speed()

    def limit_download_speed(self):
        if self.to['max_download_speed'] == -1:
            if self.this_time > 2 and self.this_up / self.this_time > 52428800:
                ps = 0
                m_t = self.min_time
                if self.to['max_upload_speed'] != -1:
                    '''上传限速时，如果限速值很低，给其他 peer 上传速度低，
                    其他 peer 给自己的上传速度也会很低，所以会严重拖慢下载进度，eta 值会变大。
                    但是出种后其他 peer 变成做种状态，这时候的上传策略一般是根据下载者的下载速度，
                    跟下载者的上传速度没有关系，由于先前没有下载限速，所以这时候种子可能突然变成满速下载，
                    不仅下载时间短而且客户端可能变得很难连接，可能导致限速失败。
                    所以这里在上传限速时检查其他 peer 的进度，在其他 peer 完成前提前下载限速。'''
                    m_t = 2 * self.min_time
                    p0 = 1 - 1610612736 / self.to['total_size']
                    try:
                        for peer in self.client.torrent_status(self.to['_id'], ['peers'])['peers']:
                            if peer['progress'] > p0:
                                ps += 1
                    except:
                        pass
                if 0 < self.to['eta'] <= m_t or self.to['max_upload_speed'] != -1 and ps > 20:
                    # 平均速度超过 50M/s 并且快要完成，开始下载限速
                    max_download_speed = (self.to['total_size'] - self.to['total_done']) / (
                            self.this_up / 52428800 - self.this_time + 30) / 1024
                    self.client.set_download_limit(self.to['_id'], max_download_speed)
                    logger.warning(f'Begin to limit download speed of torrent {self.to["tid"]}.'
                                   f' Value ------- {max_download_speed:.2f}K')
        elif self.this_time > 0:
            if self.this_up / self.this_time >= 52428800:
                # 已有下载限速，调整限速值
                if self.to['download_payload_rate'] / 1024 < 2 * self.to['max_download_speed']:
                    max_download_speed = (self.to['total_size'] - self.to['total_done']) / (
                            self.this_up / 52428800 - self.this_time + 60) / 1024
                    max_download_speed = min(max_download_speed, 512000)
                    if max_download_speed > 1.5 * self.to['max_download_speed']:
                        max_download_speed = 1.5 * self.to['max_download_speed']
                        self.client.set_download_limit(self.to['_id'], max_download_speed)
                        logger.debug(f'Change the max download speed of torrent {self.to["tid"]} '
                                     f'to {max_download_speed:.2f}K')
                    elif max_download_speed < self.to['max_download_speed']:
                        max_download_speed = max_download_speed / 1.5
                        self.client.set_download_limit(self.to['_id'], max_download_speed)
                        logger.debug(f'Change the max download speed of torrent {self.to["tid"]} '
                                     f'to {max_download_speed:.2f}K')
            else:
                '''平均速度已降到 50M/s 以下，解除限速，之似乎发现 tracker 计算的时间精度比秒更精确？
                无论如何 next_announce 是个整数必须 +1s'''
                self.client.set_upload_limit(self.to['_id'], 51200)
                self.client.set_download_limit(self.to['_id'], -1)
                self.to['max_download_speed'] = -1
                logger.info(f'Removed download speed limit of torrent {self.to["tid"]}.')
                for _ in range(30):
                    sleep(1)
                    try:
                        if self.client.torrent_status(self.to['_id'], ['state'])['state'] == 'Seeding':
                            self.client.set_upload_limit(self.to['_id'], -1)
                            self.to['max_upload_speed'] = -1
                            return
                    except:
                        pass
                logger.error(f"Torrent {self.to['tid']} | failed to remove upload limit")

    def limit_upload_speed(self):
        if 10 < self.to['eta'] + 10 < self.to['next_announce']:
            eta = self.to['eta'] + 10
        else:
            eta = self.to['next_announce']
        '''eta 代表到下次汇报之前还可以正常上传的时间，
        如果完成时间在下次周期汇报之前，那么完成时就会汇报，到下次汇报的时间就是到完成的时间，
        虽然可能通过下载限速延长完成时间，但是在延长的那段时间由于已经出种并且下载速度有限制，
        通常并不能上传很多，所以可以正常上传的时间就按照完成时间计算'''

        if self.to['max_upload_speed'] == -1:
            res = 10 * self.to['upload_payload_rate']
            if self.this_up + res + 6291456 * eta > self.announce_interval * 52428800:
                '''上次汇报到现在的上传量即将超过一个汇报周期内允许的不超速的最大值，开始上传限速.
                限速值不要太低，太低会跟不上进度影响之后的上传'''
                self.client.set_upload_limit(self.to['_id'], 6144)
                logger.warning(f'Begin to limit upload speed of torrent {self.to["tid"]}. Value ------- {6144}K')
                self.to['_t'] = time()
        else:
            # 已经开始上传限速，调整限速值
            if self.to['max_upload_speed'] == 5120:
                # 在 optimize_announce_time 用到了这个，也可以手动限速到 5120k 等待汇报
                if self.this_up / self.this_time < 52428800 and self.this_time >= 900:
                    self.re_an()
                    self.client.set_upload_limit(self.to['_id'], -1)
                    logger.info('Average upload speed below 50MiB/s, remove 5120K up-limit')
            elif self.this_time < 120:  # 已经汇报完，解除上传限速
                self.client.set_upload_limit(self.to['_id'], -1)
                logger.info(f'Removed upload speed limit of torrent {self.to["tid"]}.')
            elif self.to['upload_payload_rate'] / 1024 < 2 * self.to['max_upload_speed']:
                max_upload_speed = (self.announce_interval * 52428800 - self.this_up) / (eta + 10) / 1024
                '''计算上传限速值。把 +10 变成 +1，甚至可以限速到 49.999，不过也很容易超（不知道下载用固态会不会好点）'''
                if max_upload_speed > 51200:
                    self.client.set_upload_limit(self.to['_id'], -1)
                    logger.info(f'Removed upload speed limit of torrent {self.to["tid"]}.')
                elif max_upload_speed < 0:  # 上传量超过了一个汇报间隔内不超速的最大值
                    if self.this_up / self.this_time < 209715200:
                        if self.this_time >= 900:
                            self.re_an()
                            logger.error(f'Failed to limit upload speed limit of torrent {self.to["tid"]} '
                                         f'because the upload exceeded')
                    else:
                        self.client.set_upload_limit(self.to['_id'], 1)
                elif 8192 < max_upload_speed < 51200 and eta > 180:
                    # 调整限速值减小余量，deluge 上传量一般比限速值低
                    self.client.set_upload_limit(self.to['_id'], 51200)
                    logger.info(f'Set 51200K upload limit for torrent {self.to["tid"]}')
                elif 8192 < max_upload_speed < 16384 and eta > 60:
                    self.client.set_upload_limit(self.to['_id'], 16384)
                    logger.info(f'Set 16384K upload limit for torrent {self.to["tid"]}')
                else:
                    if self.announce_interval * 52428800 - self.this_up > 94371840 and max_upload_speed < 3072:
                        max_upload_speed = 3072  # 这个速度下载还不会卡住
                    if self.announce_interval * 52428800 - self.this_up > 31457280 and max_upload_speed < 1024:
                        max_upload_speed = 1024  # 这个速度在出种前会卡死下载
                    if self.to['max_upload_speed'] != max_upload_speed:
                        if max_upload_speed == 5120:
                            max_upload_speed = 5119
                        self.client.set_upload_limit(self.to['_id'], max_upload_speed)
                        if max_upload_speed in [3072, 1024]:
                            logger.debug(f'Set {max_upload_speed}K upload limit to torrent {self.to["tid"]}')
                        elif '_t' not in self.to or '_t' in self.to and time() - self.to['_t'] > 120:
                            # 2 分钟输出一次，当然也可以直接输出(改成 > 0)，不过我觉得有点频繁
                            logger.debug(f'Change the max upload speed for torrent {self.to["tid"]} '
                                         f'to {max_upload_speed:.2f}K')
                            self.to['_t'] = time()

    def fix_next_announce(self):
        """目前已知 lt1.2.16/1.2.17/2.0.6/2.0.7 next_announce 可能与实际不和，
        通过查询 peerlist 计算上传汇报时间并得到实际值，可能存在一定误差"""
        for self.to in filter(lambda to: 'tid' in to and 'date' in to, self.torrents_info):
            if time() - self.to['time_added'] < self.announce_interval:
                if time() - self.to['time_added'] + self.to['next_announce'] - self.announce_interval < -600:
                    if 'last_announce_time' not in self.to and not self.to.get('next_announce_is_true'):
                        next_announce = self.to['next_announce']
                        if next_announce > 3:
                            logger.debug(f"Unexpected next announce time of torrent {self.to['tid']}")
                            self.to['last_announce_time'] = time()
                            self.info_from_peer_list()
                            if abs(self.to['last_announce_time'] + 900 - time() - next_announce) < 3:
                                logger.debug('Caused by manually re-announce')
                                del self.to['last_announce_time']
                                if 'true_downloaded' in self.to:
                                    del self.to['true_downloaded']
                                self.to['next_announce'] = next_announce
                                self.to['next_announce_is_true'] = True

            if 'last_announce_time' in self.to and 'date' in self.to:
                self.to['next_announce'] = int(self.to['last_announce_time'] + self.announce_interval - time()) + 1
                while self.to['next_announce'] < 0:
                    self.to['next_announce'] += self.announce_interval

            if self.to['tid'] != -1 and 'date' in self.to and 'uploaded_before' not in self.to:
                if abs(time() + self.to['next_announce'] - self.announce_interval - self.to['time_added']) < 180:
                    self.to['uploaded_before'] = self.to['uploaded']
                else:
                    self.to['uploaded_before'] = '0 B'

    def optimize_announce_time(self):
        """尽量把完成前最后一次汇报时间调整到最合适的点，粗略计算，没有严格讨论问题。

        解释一下，假设一个种子的下载时间超过汇报时长，并且这个种子每次汇报前都经过限速并且两次汇报间的平均速度接近 50M/s，
        那么可以把这个种子到完成时的平均速度按 50M/s 计算，要获得尽可能多的上传量则需要使完成时间尽可能延后。
        假设这个种子不限速时上传速度是一个稳定的数值，那么最后一次汇报时间有一个点能使完成时间延长最多。

        但实际并非总是如人意，比如最后一次定期汇报时间刚好在完成时，就没有任何可以延长下载时间的余地。
        这个函数就是解决这个问题，在合适的时间强制汇报来调整完成前最后一次汇报时间。"""
        i = int(300 / self.client.connect_interval) + 1
        if 'detail_progress' not in self.to:
            self.to['detail_progress'] = deque(maxlen=i)
        self.to['detail_progress'].append((self.to['total_uploaded'], self.to['total_done'], time()))
        if len(self.to['detail_progress']) != i or self.this_time < 30 or self.to['max_upload_speed'] == 5120:
            return
        _list = self.to['detail_progress']
        '''计算 5 分钟内平均下载速度和平均上传速度'''
        upspeed = (_list[i - 1][0] - _list[0][0]) / (_list[i - 1][2] - _list[0][2])
        dlspeed = (_list[i - 1][1] - _list[0][1]) / (_list[i - 1][2] - _list[0][2])
        if upspeed > 52428800 and dlspeed > 0 and _list[0][1] != 0:
            '''complete_time 是估计的完成时间，
            perfect_time 是估计的最佳的最后一次汇报时间，
            earliest 是计算的最早能强制汇报且不超速的时间。
            
            如果最佳汇报时间可以强制汇报并且不超速，直接汇报就行，实际并非总是如此。
            有可能最早能汇报的时间在最佳时间点之后，这时候就需要比较在最早能汇报的时间汇报和不强制汇报'''
            complete_time = (self.to['total_size'] - self.to['total_done']) / dlspeed + time()
            perfect_time = complete_time - self.announce_interval * 52428800 / upspeed
            if self.this_up / self.this_time > 52428800:
                earliest = (self.this_up - 52428800 * self.this_time) / 45 / 1024 ** 2 + time()
            else:
                earliest = time()
            if earliest - (time() - self.this_time) < 900:
                return
            if earliest > perfect_time:
                if time() >= earliest:
                    if (self.this_up + upspeed * 20) / self.this_time > 52428800:
                        self.re_an()
                        logger.info(f"Re-announce torrent {self.to['tid']}")
                    return
                if earliest < perfect_time + 60:
                    self.client.set_upload_limit(self.to['_id'], 5120)
                    self.to['max_upload_speed'] = 5120
                    logger.info(f"Set 5120K upload limit for torrent {self.to['tid']}, waiting for re-announce")
                else:
                    if time() - self.this_time > perfect_time:
                        return
                    _eta1 = complete_time - earliest
                    if _eta1 < 120:
                        return
                    earliest_up = (earliest - time() + self.this_time) * 5248800 + _eta1 * upspeed
                    default_up = self.announce_interval * 52428800
                    _eta2 = complete_time - (time() + self.to['next_announce'])
                    if _eta2 > 0:
                        default_up += _eta2 * upspeed
                    if earliest_up > default_up:
                        self.client.set_upload_limit(self.to['_id'], 5120)
                        self.to['max_upload_speed'] = 5120
                        logger.info(f"Set 5120K upload limit for torrent {self.to['tid']}, waiting for re-announce")

    def re_an(self):
        if not ('lft' in self.to and time() - self.to['lft'] < 900):
            self.to['about_to_reannounce'] = True
            _to = self.to
            if self.m_conf['enable']:
                self.magic()
            self.to = _to
            sleep(1)
            self.client.reannounce(self.to['_id'])
            self.to['lft'] = time()
            if 'last_announce_time' in self.to:
                self.to['last_announce_time'] = time()
            self.to['about_to_reannounce'] = False

    def update_tid(self):
        """根据 hash 搜索种子 id"""
        url = f'https://u2.dmhy.org/torrents.php?incldead=0&spstate=0' \
              f'&inclbookmarked=0&search={self.to["_id"]}&search_area=5&search_mode=0'
        try:
            soup = BeautifulSoup(self.rq('get', url).text.replace('\n', ''), 'lxml')
            table = soup.select('table.torrents')
            if table:
                self.to['tid'] = int(table[0].contents[1].contents[1].a['href'][15:-6])
                date = table[0].contents[1].contents[3].time
                self.to['date'] = date.get('title') or date.get_text(' ')
                self.to['tz'] = self.get_tz(soup)
                logger.debug(f"{self.to['_id']} --> {self.to['tid']}")
            else:
                self.to['404'] = True
                logger.info(f"{self.to['_id']} was not found in u2")
        except Exception as e:
            logger.error(e)

    def update_upload(self):
        tmp_to = self.to
        try:
            page = self.rq('get',
                           f'https://u2.dmhy.org/getusertorrentlistajax.php?userid={conf["uid"]}&type=leeching').text
            table = BeautifulSoup(page.replace('\n', ''), 'lxml').table
            if not table:
                return
            tmp_info = []
            for tr in table.contents[1:]:
                tid = int(tr.contents[1].a['href'][15:-6])
                for self.to in self.torrents_info:
                    if self.to['tid'] != tid:
                        continue
                    data = {'uploaded': tr.contents[6].get_text(' '), 'last_get_time': time()}

                    if 'date' in self.to and 'last_get_time' in self.to:
                        if time() - self.this_time + 10 > self.to['last_get_time']:
                            if 'true_uploaded' in self.to or 'last_announce_time' in self.to:
                                tmp_info.append(self.to)
                            if self.to['total_uploaded'] - self.byte(data['uploaded'], 1) > \
                                    300 * 1024 ** 2 * (self.this_time + 2):
                                self.to['true_uploaded'] = data['uploaded']
                                tmp_info.append(self.to)
                            if data['uploaded'].split(' ')[0] != '0':
                                self.print(f"Last announce upload of torrent {tid} is {data['uploaded']}")

                    self.to.update(data)
                    [_torrent.update(data) for _torrent in t_client[0].torrents_info if _torrent['tid'] == tid]

            for self.to in tmp_info:
                self.info_from_peer_list()
        except Exception as e:
            logger.exception(e)
        finally:
            self.to = tmp_to

    def info_from_peer_list(self):
        """Fix incorrect upload and next announce"""
        try:
            peer_list = self.rq('get', f"https://u2.dmhy.org/viewpeerlist.php?id={self.to['tid']}").text
            tables = BeautifulSoup(peer_list.replace('\n', ' '), 'lxml').find_all('table')
        except Exception as e:
            logger.error(e)
            return

        for table in tables or []:
            for tr in filter(lambda _tr: 'nowrap' in str(_tr), table):
                if tr.get('bgcolor'):

                    if 'true_uploaded' in self.to:
                        self.to['true_uploaded'] = tr.contents[1].string
                        self.to['true_downloaded'] = tr.contents[4].string
                        if self.to['true_uploaded'] == self.to['uploaded']:
                            del self.to['true_uploaded']
                            del self.to['true_downloaded']
                        else:
                            self.print(f"Some upload of torrent {self.to['tid']} was not calculated by tracker")
                            self.print(f"Actual upload of torrent {self.to['tid']} is {self.to['true_uploaded']}")

                    if 'last_announce_time' in self.to:
                        idle = reduce(lambda a, b: a * 60 + b, map(int, tr.contents[10].string.split(':')))
                        self.to['last_announce_time'] = time() - idle
                        self.to['next_announce'] = self.announce_interval - idle + 1
                        if self.to['next_announce'] < 0:
                            self.to['next_announce'] = 0


if __name__ == '__main__':
    modes = conf['magic']['modes']
    if modes and len(modes) > 1:
        for i in range(len(modes) - 1):
            if modes[i]['uc_limit']['24_max'] < modes[i + 1]['uc_limit']['24_min']:
                raise ConfigError()
            if modes[i]['uc_limit']['72_max'] < modes[i + 1]['uc_limit']['72_min']:
                raise ConfigError()
    
    log_path = conf.get('log_path') or f'{os.path.splitext(__file__)[0]}.log'
    data_path = conf.get('data_path') or f'{os.path.splitext(__file__)[0]}.data.txt'
    logger.remove(handler_id=0)  # 默认有一个 sys.stderr handler 会输出 debug 信息，需要清除
    level = 'DEBUG' if conf['enable_debug_output'] else 'INFO'
    logger.add(sink=sys.stderr, level=level)
    torrents_info = {}
    with open(data_path, 'a', encoding='utf-8') as _f1:
        pass
    with open(data_path, 'r', encoding='utf-8') as _f2:
        for _line in _f2:
            if _line.startswith('torrents_info = '):
                torrents_info = eval(_line.lstrip('torrents_info = '))
    MagicAndLimit.init()

    local_client = None
    if conf['magic']['enable'] or conf['limit']['enable']:
        t_client: List[MagicAndLimit] = [MagicAndLimit(None)]
        if len(conf['clients']) > 0 and conf['enable_clients']:
            for _c in conf['clients']:
                _t = _c['type']
                del _c['type']
                if _t in ['de', 'Deluge', 'deluge']:
                    _c.setdefault('decode_utf8', True)
                    _c.setdefault('connect_interval', 5)
                    _c.setdefault('min_announce_interval', 300)
                    if _c['host'] == '127.0.0.1':
                        local_client = Deluge(**_c)
                        _client = MagicAndLimit(local_client)
                    else:
                        _client = MagicAndLimit(Deluge(**_c))
                        
                    try:
                        min_announce_interval = _client.client.ltconfig.get_settings()['min_announce_interval']
                    except:
                        min_announce_interval = 300
                    if _c['min_announce_interval'] != min_announce_interval:
                        raise ConfigError()
                        
                    t_client[0].clients.append(Deluge(**_c))
                    t_client.append(_client)

        if local_client is None:
            logger.add(sink=log_path, level=level, rotation='5 MB')
        else:
            logger.add(sink=log_path, level=level, rotation='5 MB', filter=local_client.log_filter)

        for _i, info_ in torrents_info.items():
            try:
                t_client[_i].torrents_info = info_
            except:
                pass

        try:
            with ThreadPoolExecutor(max_workers=len(t_client)) as _executor:
                _futures = {_executor.submit(_c.run): _c.client for _c in t_client}
                """因为 deluge 很容易失联，如果有多个客户端，要分配多个线程让各个客户端时间上不受牵制。
                第一个线程客户端是 None，这个线程的任务就是定期爬网页以及放魔法(对不在客户端的种子)，
                单独开限速时，这个线程什么也不做。之后的线程每个都对应有一个客户端，给在客户端的种子放魔法以及限速"""
                for _future in as_completed(_futures):
                    try:
                        _future.result()
                    except BaseException as _e:
                        _client = _futures[_future]
                        if _client is None:
                            logger.critical('Thread 0 terminated unexpectedly')
                        else:
                            logger.critical(f'Thread for deluge on {_client.host} terminated unexpectedly')
                        logger.exception(_e)
        except KeyboardInterrupt:
            logger.warning(f'This script: {__file__} has been manually terminated.')
            MagicAndLimit.write_info()
            os._exit(0)

    else:
        logger.error('The program will do nothing')
        os._exit(0)
