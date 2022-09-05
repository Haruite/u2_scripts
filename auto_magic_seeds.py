"""python3.7及以上
脚本有两个功能，一个是给自己有上传速度的种子放魔法，一个是给孤种放地图炮吸引别人下载
支持客户端 deluge, qbittorrent, transmission, rutorrent 和 utorrent
依赖：pip3 install PyYAML requests bs4 lxml deluge-client qbittorrent-api transmission-rpc loguru pytz
Azusa 大佬的 api，见 https://github.com/kysdm/u2_api，自动获取 token: https://greasyfork.org/zh-CN/scripts/428545
因为使用了异步，放魔法速度很快，不会有反应时间，请使用前仔细检查配置
"""

import asyncio
import gc
import json
import os
import random
import re
import sys

import aiohttp
import pytz
import requests
import qbittorrentapi
import transmission_rpc

from abc import abstractmethod, ABCMeta
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from ssl import SSLError
from datetime import datetime
from time import time, sleep
from typing import Union, Dict, List

from bs4 import BeautifulSoup
from loguru import logger

from deluge_client import FailedToReconnectException, LocalDelugeRPCClient
from qbittorrentapi.exceptions import APIConnectionError, HTTPError
from transmission_rpc.error import TransmissionTimeoutError, TransmissionConnectError

CONFIG = {  # 应该跟 json 差不多，放到 ide 里方便能看出错误
    'clients_info': [
        {
            'type': 'deluge',  # 'de', 'Deluge', 'deluge'
            'host': '127.0.0.1',  # IP
            'port': 58846,  # daemon 端口
            'username': '',  # 本地客户端可以不填用户名和密码
            'password': ''  # cat ~/.config/deluge/auth
        },  # 多个用逗号隔开
        {
            'type': 'qbittorrent',  # 'qb', 'QB', 'qbittorrent', 'qBittorrent'
            'host': 'http://127.0.0.1',  # host，最好带上 http 或者 https
            'port': 8080,  # webui 端口
            'username': '',  # web 用户名
            'password': '',  # web 密码
            # 'verify': True  # 验证 https 证书
        },
        {
            'type': 'transmission',  # 'tr', 'Transmission', 'transmission'
            'host': '127.0.0.1',  # IP
            'port': 9091,  # webui 端口
            'username': '',  # web 用户名
            'password': ''  # web 密码
        },
        {
            'type': 'rutorrent',  # 'ru', 'Rutorrent', 'rutorrent'
            'url': 'https://127.0.0.1/rutorrent',  # rtinst 安装完是这样的
            'username': '',  # web 用户名
            'password': '',  # web 密码
            # 'verify': False  # 验证 https 证书
        },
        {
            'type': 'utorrent',  # 'ut', 'UT', 'utorrent', 'uTorrent', 'µTorrent'
            'host': 'http://127.0.0.1',  # host，最好带上 http 或者 https
            'port': 8080,  # webui 端口
            'username': '',  # web 用户名
            'password': '',  # web 密码
            # 'verify': True  # 验证 https 证书
        },
    ],
    'requests_args': {
        'cookies': {'nexusphp_u2': ''},  # 网站 cookie
        'headers': {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/104.0.5112.102 Safari/537.36 Edg/104.0.1293.70'
        },
        'proxy': '',  # 'http://127.0.0.1:10809'
        'timeout': 10
    },
    'magic_for_self': {  # 做种中的种子，上传速度大于一定值，给自己放 2.33x 魔法
        'enable': True,  # 是否开启
        'interval': 60,  # 检查的间隔
        'magic_downloading': True,  # 是否下载中的种子放 2.33x 魔法
        'min_rate': 1024,  # 最小上传速度(KiB/s)
        'min_size': 5,  # 最小体积(GiB)
        'min_d': 180,  # 种子最小生存天数
    },
    'magic_for_all': {  # 做种中的种子，做种人数小于一定值，放地图炮 free，吸引别人下载
        'enable': False,  # 是否开启
        'interval': 86400,  # 检查的间隔
        'torrent_num': 5,  # 一次放魔法的种子个数
        'max_seeder_num': 5,  # 做种人数最大值
        '233_all': True,  # 为真时给所有人放 2.33x↑0x↓，否则给所有人 0x↓，自己放 2.33x↑
        'hours': 24,  # 魔法持续时间
        'min_rm_hr': 0  # 2.33x 剩余时间小于这个值(小时)还是会放 2.33x 魔法
    },
    'uc_max': 30000,  # 单个魔法最大 uc 使用量
    'total_uc_max': 200000,  # 24h 内 uc 最大使用量
    'api_token': '',  # api 的 token，填了将默认使用 api 查询种子信息，不填就直接从 u2 网页获取信息
    'uid': 50096,  # 如果填了 api_token，则需要 uid
    'data_path': f'{os.path.splitext(__file__)[0]}.data.txt',  # 数据保存路径
    'log_path': f'{os.path.splitext(__file__)[0]}.log',  # 日志路径
}


class CheckKeys:
    str_keys = ('name', 'tracker', 'state')
    int_keys = ('total_uploaded', 'upload_payload_rate', 'total_seeds')

    __slots__ = ()

    def __call__(self, func):
        """检查 keys 参数是否受支持，以及返回值类型是否符合
        用于 BT 客户端获取种子信息的函数，自定义客户端只有满足这些要求才能正确运行"""

        @wraps(func)
        def wrapper(*args, **kwargs):
            keys = []
            if len(args) > 1:
                keys = args[len(args) - 1]
            elif 'keys' in kwargs:
                keys = kwargs['keys']

            unsupported_keys = [key for key in keys if key not in args[0].all_keys]
            if unsupported_keys:
                raise ValueError(f'{unsupported_keys} not supported. '
                                 f'These are the all available keys: \n{args[0].all_keys}')
            res = func(*args, **kwargs)
            if not isinstance(res, dict) and res is not None:
                raise TypeError(f'Return value of function {func.__name__} should be dict type')
            if res:
                _res = res
                if list(res.keys())[0] in keys:
                    _res = {'_': _res}

                for _id, data in _res.items():
                    for key, val in data.items():
                        if key in self.int_keys and not isinstance(val, int):
                            raise TypeError(f'The value of "{key}" should be int type')
                        elif key in self.str_keys and not isinstance(val, str) and not (key == 'tracker' and not val):
                            raise TypeError(f'The value of "{key}" should be str type')
                        if key not in keys:
                            raise TypeError(f'Key "{key}" is not supported. Check return value {res}')

            return res

        return wrapper


check_keys = CheckKeys()


class BtClient(metaclass=ABCMeta):
    """BT 客户端基类"""
    all_keys = ('name',  # str 类型，文件名 (外层文件夹名)
                'peers',  # 可迭代对象，每项为一个字典，字典为单个 peer 的信息，其中必须包含 progress 项，代表进度，类型为 float(0~1)
                'total_size',  # int 类型，种子体积 (B)
                'state',  # str 类型，种子当前状态
                'total_seeds',  # int 类型，种子当前做种数
                'tracker',  # str 类型，种子当前 tracker
                'upload_payload_rate',  # int 类型，上传速度 (B / s)
                )

    wrapped_classes = []

    def __new__(cls, *args, **kwargs):
        ins = super(BtClient, cls).__new__(cls)
        subclass = ins.__class__

        if subclass not in cls.wrapped_classes:
            for function in ('seeding_torrents_info', 'active_torrents_info'):
                setattr(subclass, function, check_keys(getattr(subclass, function)))
            cls.wrapped_classes.append(subclass)
        return ins

    @abstractmethod
    def call(self, method, *args, **kwargs):
        """
        :param method: 方法
        """
        pass

    @abstractmethod
    def active_torrents_info(self, keys):
        """获取所有活动的种子信息

        :param keys: 包含种子信息相关键的列表，取值在 all_keys 中
        :return: 以种子 hash 为键，种子信息（一个字典，键为 keys 中的值）为值的字典。
        如果使用 deluge 以外客户端，需要按照 all_keys 中的说明返回指定类型数据
        """
        pass

    @abstractmethod
    def seeding_torrents_info(self, keys):
        """获取所有做种中的种子信息"""
        pass


class Deluge(LocalDelugeRPCClient, BtClient):
    timeout = 10

    def __init__(self, host='127.0.0.1', port=58846, username='', password=''):
        super().__init__(host, port, username, password, decode_utf8=True, automatic_reconnect=True)

    def call(self, method, *args, **kwargs):
        if not self.connected and method != 'daemon.login':
            for i in range(6):
                try:
                    self.reconnect()
                    break
                except SSLError:
                    sleep(0.3 * 2 ** i)
        try:
            return super().call(method, *args, **kwargs)
        except FailedToReconnectException:
            logger.error(f'Failed to reconnect to deluge client on {self.host}:{self.port}')
        except TimeoutError:
            logger.error(f'Timeout when connecting to deluge client on {self.host}:{self.port}')
        except Exception as e:
            if e.__class__.__name__ == 'BadLoginError':
                logger.error(f'Failed to connect to deluge client on {self.host}:{self.port}, Password does not match')
            else:
                raise

    def active_torrents_info(self, keys):
        return self.call('core.get_torrents_status', {'state': 'Active'}, keys)

    def seeding_torrents_info(self, keys):
        return self.call('core.get_torrents_status', {'state': 'Seeding'}, keys)


class Qbittorrent(qbittorrentapi.Client, BtClient):
    de_key_to_qb = {'name': 'name', 'tracker': 'tracker', 'total_size': 'size',
                    'upload_payload_rate': 'upspeed', 'state': 'state', 'total_seeds': 'num_complete'}

    def __init__(self, host='http://127.0.0.1', port=8080, username='', password='', **kwargs):
        super().__init__(host=host, port=port, username=username, password=password,
                         REQUESTS_ARGS={'timeout': 10}, FORCE_SCHEME_FROM_HOST=True,
                         VERIFY_WEBUI_CERTIFICATE=True if 'verify' not in kwargs else kwargs['verify'])

    def call(self, method, *args, **kwargs):
        try:
            return self.__getattribute__(method)(*args, **kwargs, _retries=5)
        except HTTPError as e:
            logger.error(f'Failed to connect to qbittorrent on {self.host}:{self.port} due to http error: {e}')
        except APIConnectionError as e:
            logger.error(f'Failed to connect to qbittorrent on {self.host}:{self.port} due to '
                         f'qbittorrentapi.exceptions.APIConnectionError:  {e}')

    def fix_return_value(self, lst, keys):
        torrents_info = {}
        for torrent in lst:
            _id = torrent['hash']
            torrents_info[_id] = {}
            for key in keys:
                torrents_info[_id][key] = torrent.get(self.de_key_to_qb[key])
        return torrents_info

    def active_torrents_info(self, keys):
        return self.fix_return_value(self.call('torrents_info', status_filter=['active']), keys)

    def seeding_torrents_info(self, keys):
        return self.fix_return_value(self.call('torrents_info', status_filter=['seeding']), keys)


class Transmission(transmission_rpc.Client, BtClient):
    de_key_to_tr = {'name': 'name', 'total_size': 'total_size',
                    'upload_payload_rate': 'rateUpload', 'state': 'status'}

    def __init__(self, host='http://127.0.0.1', port=9091, username='', password=''):
        super().__init__(host=host, port=port, username=username, password=password, timeout=10)
        self.host = host
        self.port = port

    def call(self, method, *args, **kwargs):
        try:
            return self.__getattribute__(method)(*args, **kwargs)
            # 这个 rpc 模块自动尝试 10 次不好改
        except (TransmissionTimeoutError, TransmissionConnectError) as e:
            logger.error(f'Error when connect to transmission client on {self.host}:{self.port} | {e}')

    def keys_to_dict(self, keys, torrent):
        res = {key: torrent.__getattribute__(self.de_key_to_tr[key]) for key in keys if key in self.de_key_to_tr}
        if 'tracker' in keys:
            res['tracker'] = torrent.trackers[0]['announce'] if torrent.trackers else None
        if 'total_seeds' in keys:
            res['total_seeds'] = torrent.trackerStats[0]['seederCount'] if torrent.trackerStats else 99999
        return res

    def active_torrents_info(self, keys):
        return {torrent.hashString: self.keys_to_dict(keys, torrent)
                for torrent in self.call('get_torrents') if torrent.rateUpload > 0}

    def seeding_torrents_info(self, keys):
        return {torrent.hashString: self.keys_to_dict(keys, torrent)
                for torrent in self.call('get_torrents') if torrent.status == 'seeding'}


class Rutorrent(BtClient):
    tr_keys = {'tracker', 'total_seeds'}

    def __init__(self, url, username, password, **kwargs):
        self.url = url
        self.auth = (username, password)
        self.verify = False if 'verify' not in kwargs else kwargs['verify']

    def call(self, method, *args, **kwargs):
        data = {'mode': method}
        data.update(kwargs)
        res = ''
        for i in range(6):
            try:
                res = requests.post(f"{self.url.rstrip('/')}/plugins/httprpc/action.php",
                                    auth=self.auth, data=data, verify=self.verify).text
                return json.loads(res)
            except json.JSONDecodeError as e:
                if 'Authorization Required' in res:
                    logger.error(f'Failed to connect to rutorrent instance via {self.url}, '
                                 f'check your username and password.')
                    return
                else:
                    logger.error(e)
                sleep(0.3 * 2 ** i)

    @staticmethod
    def info_from_list(keys, lst):
        res = {}
        if 'name' in keys:
            res['name'] = lst[4]
        if 'total_size' in keys:
            res['total_size'] = int(lst[5])
        if 'state' in keys:
            res['state'] = 'seeding' if int(lst[19]) == 0 else 'downloading'
        if 'upload_payload_rate' in keys:
            res['upload_payload_rate'] = int(lst[11])
        return res

    def update_tracker_info(self, info, lst):
        for _id, data in self.call('trkall').items():
            if _id.lower() in info:
                update = ({'tracker': None, 'total_seeds': 99999} if not data
                          else {'tracker': data[0][0], 'total_seeds': int(data[0][4])})
                info[_id.lower()].update({key: update[key] for key in lst})

    def torrents_info(self, status, keys):
        res = {}
        for _id, data in self.call('list')['t'].items():
            if status == 'active' and int(data[11]) <= 0:
                continue
            if status == 'seeding' and int(data[19]) != 0:
                continue
            res[_id.lower()] = self.info_from_list(keys, data)
        if set(keys) & self.tr_keys:
            self.update_tracker_info(res, set(keys) & self.tr_keys)
        return res

    def active_torrents_info(self, keys):
        return self.torrents_info('active', keys)

    def seeding_torrents_info(self, keys):
        return self.torrents_info('seeding', keys)


class UTorrent(BtClient):
    key_to_index = {'name': 2, 'total_size': 3, 'total_seeds': 15, 'upload_payload_rate': 8}

    def __init__(self, host='127.0.0.1', port=8080, username='', password='', **kwargs):
        if not any(host.startswith(pre) for pre in ('http://', 'https://')):
            host = f'http://{host}'
        self.url = f'{host}:{port}/gui'
        self.verify = True if 'verif' not in kwargs else kwargs['verify']
        self.auth = (username, password)
        self.token, self.cookies = None, None
        self.err_msg = f'Failed to get utorrent web-api token via {self.url}, Check your username and password'
        self.get_token()

    def get_token(self):
        resp = requests.get(f'{self.url}/token.html', auth=self.auth, verify=self.verify)
        if resp:
            self.token = BeautifulSoup(resp.text, 'lxml').div.text
            self.cookies = {'GUID': resp.cookies['GUID']}
        else:
            logger.error(self.err_msg)

    def call(self, method, *args, **kwargs):
        if method == 'list':
            url = f'{self.url}/?list=1'
        else:
            url = f'{self.url}/?action={method}'
        params = {'token': self.token}
        params.update(kwargs)
        for i in range(6):
            resp = ''
            try:
                resp = requests.get(url, auth=self.auth, cookies=self.cookies, params=params, verify=self.verify).text
                if not resp:
                    logger.error(self.err_msg)
                    return
                else:
                    return json.loads(resp)
            except Exception as e:
                if 'invalid request' in resp:
                    self.get_token()
                else:
                    logger.error(e)
                    sleep(0.3 * 2 ** i)

    def get_state(self, num, rem):
        """状态有很多种，这里敷衍一下和 rutorrent 一样"""
        if num % 2 == 0:
            return 'paused'
        return 'seeding' if rem == 0 else 'downloading'

    def get_tracker(self, info):
        for torrent in self.call('getprops', hash=list(info.keys()))['props']:
            info[torrent['hash'].lower()]['tracker'] = torrent['trackers'].split('\r\n')[0]

    def active_torrents_info(self, keys):
        return self.torrents_info('active', keys)

    def seeding_torrents_info(self, keys):
        return self.torrents_info('seeding', keys)

    def torrents_info(self, status, keys):
        res = {}
        for torrent in self.call('list')['torrents']:
            if status == 'active' and torrent[8] <= 0:
                continue
            if status == 'seeding' and self.get_state(torrent[1], torrent[18]) != 'seeding':
                continue
            res[torrent[0].lower()] = {}
            for key in keys:
                if key in self.key_to_index:
                    res[torrent[0].lower()][key] = torrent[self.key_to_index[key]]
            if 'state' in keys:
                res[torrent[0].lower()]['state'] = self.get_state(torrent[1], torrent[18])
        if 'tracker' in keys:
            self.get_tracker(res)
        return res


class MagicInfo(UserDict):
    def __init__(self, dic=None, **kwargs):
        super(MagicInfo, self).__init__(dic, **kwargs)
        self.c = False

    def __setitem__(self, key, value):
        if key in self.data:
            if 'uc' in value and 'uc' in self.data[key]:
                value['uc'] += self.data[key]['uc']
            self.data[key].update(value)
        else:
            self.data[key] = value
        self.c = True

    def __set__(self, instance, value):
        self.data = value

    def del_unused(self):
        for _id in list(self.data.keys()):
            if 'ts' not in self.data[_id] or int(time()) - self.data[_id]['ts'] > 86400:
                del self.data[_id]
                self.c = True

    def cost(self):
        uc_cost = 0
        for _id, val in self.data.items():
            uc_cost += val.get('uc') or 0
        return uc_cost

    def save(self):
        if self.c:
            with open(CONFIG['data_path'], 'w', encoding='utf-8') as fp:
                json.dump(self.data, fp)
            self.c = False

    def min_secs(self):
        total = self.cost()
        for _id, data in self.data.items():
            total -= data.get('uc') or 0
            if total <= CONFIG['total_uc_max']:
                if 'ts' in data:
                    return data['ts'] + 86400 - int(time())


class Request:
    def __init__(self):
        self.session = None
        self.u2_args = CONFIG['requests_args']
        self.api_args = {'timeout': CONFIG['requests_args'].get('timeout'),
                         'proxy': CONFIG['requests_args'].get('proxy')}

    async def request(self, url, method='get', retries=5, **kwargs) -> (
            Union[str, Dict[str, Union[str, Dict[str, List[Dict[str, Union[str, int, None]]]]]]]
    ):
        if url.startswith('https://u2.dmhy.org'):
            [kwargs.setdefault(key, val) for key, val in self.u2_args.items()]
        else:
            [kwargs.setdefault(key, val) for key, val in self.api_args.items()]
        kwargs.setdefault('timeout', 10)

        if self.session is None:
            self.session = aiohttp.ClientSession()

        for i in range(retries + 1):
            try:
                async with self.session.request(method, url, **kwargs) as resp:
                    if resp.status < 300:
                        if url.startswith('https://u2.dmhy.org') and method == 'get':
                            logger.debug(f'Downloaded page: {url}')
                        return await (resp.text() if url.startswith('https://u2.dmhy.org') else resp.json())
                    else:
                        logger.error(f'Incorrect status code <{resp.status}> | {url}')
                        await asyncio.sleep(3)
            except Exception as e:
                if i == retries:
                    logger.error(e)
                elif isinstance(e, asyncio.TimeoutError):
                    kwargs['timeout'] += 20

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()


class MagicSeed(Request):
    magic_info = MagicInfo({})
    instances = []

    def __new__(cls, *args, **kwargs):
        _instance = super().__new__(cls)
        if cls == MagicSeed:
            cls.instances.append(_instance)
        return _instance

    def __init__(self, client):
        super(MagicSeed, self).__init__()
        self.client = client

    async def main(self):
        tasks = []

        for _id, data in self.client.active_torrents_info(
                ['name', 'tracker', 'total_size', 'upload_payload_rate', 'state']).items():

            if (_id not in self.magic_info  # 魔法还在有效期内则不加入
                    or 'ts' in self.magic_info[_id] and int(time()) - self.magic_info[_id]['ts'] >= 86400):
                if data['tracker'] and ('daydream.dmhy.best' in data['tracker']
                                        or 'tracker.dmhy.org' in data['tracker']):  # 过滤不是 U2 的种子
                    magic_downloading = CONFIG['magic_for_self']['magic_downloading']
                    if magic_downloading or data['state'] not in ['Downloading', 'downloading']:  # 过滤下载中的种子
                        if data['upload_payload_rate'] >= CONFIG['magic_for_self']['min_rate'] * 1024:
                            if data['total_size'] >= CONFIG['magic_for_self']['min_size'] * 1024 ** 3:
                                tasks.append(self.check_torrent(_id, data['name']))

        res = await asyncio.gather(*tasks)
        self.magic_info.del_unused()

        magic_tasks = [self.send_magic(__id, _tid, {'user': 'SELF', 'hours': 24, 'ur': 2.33, 'dr': 1})
                       for __id, _tid, ur_233 in res if _tid and __id not in self.magic_info]
        await asyncio.gather(*magic_tasks)
        self.magic_info.save()

    async def check_torrent(self, _id, name):
        if not CONFIG['api_token']:
            return await self.info_from_u2(_id, name)
        else:
            try:
                return await self.info_from_api(_id, name)
            except Exception as e:
                logger.exception(e)
                return await self.info_from_u2(_id, name)

    async def info_from_u2(self, _id, name):
        url = f'https://u2.dmhy.org/torrents.php'
        params = {'search': _id, 'search_area': 5}
        page = await self.request(url, params=params)
        soup = BeautifulSoup(page.replace('\n', ''), 'lxml')

        '''获取时区'''
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        tz = [tz_info[len(pre):-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]
        timezone = pytz.timezone(tz)

        table = soup.select('table.torrents')
        tid = None
        if table:
            cont = table[0].contents[1].contents
            tid = int(cont[1].a['href'][15:-6])

            '''判断种子是否已有 2.33x 优惠'''
            for img in cont[1].select('tr')[1].td.select('img') or []:
                if img.get('class') == ['arrowup'] and float(img.next_element.text[:-1].replace(',', '.')) >= 2.33:
                    logger.info(f'Torrent {_id}, id: {tid}: 2.33x upload magic existed!')
                    time_tag = cont[1].time
                    if time_tag:
                        magicst = self.ts(time_tag.get('title') or time_tag.text, timezone) - 86400
                        self.magic_info[_id] = {'ts': magicst}
                    else:
                        self.magic_info[_id] = {'ts': int(time()) + 86400 * 30}
                    return _id, tid, True

            '''判断种子时间是否小于最小天数'''
            delta = time() - self.ts(cont[3].time.get('title') or cont[3].time.get_text(' '), timezone)
            if delta < CONFIG['magic_for_self']['min_d'] * 86400:
                self.magic_info[_id] = {'ts': int(time())}
                return _id, tid, False

        else:
            logger.error(f'Torrent {_id} , name: {name} was not found in u2...')
            self.magic_info[_id] = {'ts': int(time()) + 86400 * 3}
        return _id, tid, False

    async def info_from_api(self, _id, name):
        _param = {'uid': CONFIG['uid'], 'token': CONFIG['api_token']}

        history_data = await self.request('https://u2.kysdm.com/api/v1/history',
                                          params={**_param, 'hash': _id})
        tid = None
        if history_data['data']['history']:
            tid = history_data['data']['history'][0]['torrent_id']

            res = await self.request('https://u2.kysdm.com/api/v1/promotion_super',
                                     params={**_param, 'torrent_id': tid})
            if float(res['data']['promotion_super'][0]['private_ratio'].split(' / ')[0]) >= 2.33:
                logger.info(f'Torrent {_id}, id: {tid}: 2.33x upload magic existed!')

                res = await self.request('https://u2.kysdm.com/api/v1/promotion_specific',
                                         params={**_param, 'torrent_id': tid})
                pro_list = res['data']['promotion']

                pro_end_time = time()
                for pro_data in pro_list:
                    if float(pro_data['ratio'].split(' / ')[0]) >= 2.33:
                        if not pro_data['for_user_id'] or pro_data['for_user_id'] == CONFIG['uid']:
                            if not pro_data['expiration_time']:
                                self.magic_info[_id] = {'ts': int(time()) + 86400 * 30}
                                break
                            else:
                                end_time = self.ts(pro_data['expiration_time'].replace('T', ' '))
                                if self.ts(pro_data['from_time'].replace('T', ' ')) < time() < end_time:
                                    if end_time > pro_end_time:
                                        pro_end_time = end_time
                self.magic_info[_id] = {'ts': int(pro_end_time) - 86400}
                return _id, tid, True

            upload_date = history_data['data']['history'][0]['uploaded_at']
            if time() - self.ts(upload_date.replace('T', ' ')) < CONFIG['magic_for_self']['min_d'] * 86400:
                self.magic_info[_id] = {'ts': int(time())}
                return _id, tid, False

        else:
            logger.error(f'Torrent {_id} , name: {name} was not found...')
            self.magic_info[_id] = {'ts': int(time()) + 86400 * 3}
        return _id, tid, False

    @staticmethod
    def ts(date, tz=pytz.timezone('Asia/Shanghai')):
        dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        return tz.localize(dt).timestamp()

    async def send_magic(self, _id, tid, _data: Dict):
        page = await self.request(f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}')
        soup = BeautifulSoup(page, 'lxml')
        hidden = soup.find_all('input', {'type': 'hidden'})
        if not hidden:
            logger.error(f'Torrent {_id}, tid: {tid} was not found in site...')
            self.magic_info[_id] = {'ts': int(time()) + 86400 * 3}
            return
        data = {h['name']: h['value'] for h in hidden}
        data.update({'user_other': '', 'start': 0, 'promotion': 8, 'comment': ''})
        data.update({'user': 'SELF', 'hours': 24, 'ur': 2.33, 'dr': 1})
        data.update(_data)

        try:
            p1 = await self.request('https://u2.dmhy.org/promotion.php?test=1', method='post', data=data)
            res_json = json.loads(p1)
            if res_json['status'] == 'operational':
                uc = int(float(BeautifulSoup(res_json['price'], 'lxml').span['title'].replace(',', '')))

                if uc > CONFIG['uc_max']:
                    logger.warning(f'Torrent id: {tid} cost {uc}uc, too expensive | data: {data}')
                    self.magic_info[_id] = {'ts': int(time())}
                    return

                if self.magic_info.cost() > CONFIG['total_uc_max']:
                    secs = min(self.magic_info.min_secs(), 1800)
                    logger.warning(f'24h ucoin usage exceeded, Waiting for {secs}s ------ | data: {data}')
                    await asyncio.sleep(secs)
                    return
                self.magic_info[_id] = {'uc': uc}

                url = f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}'
                p2 = await self.request(url, method='post', retries=0, data=data)
                if re.match(r'^<script.+<\/script>$', p2):
                    logger.info(f"Sent a {data['ur']}x upload and {data['dr']}x download "
                                f"magic to torrent {_id}, tid: {tid}, user {data['user'].lower()}, "
                                f"duration {data['hours']}h, uc usage {uc}, 24h total {self.magic_info.cost()}")
                    self.magic_info[_id] = {'ts': int(time())}
                else:
                    logger.error(f'Failed to send magic to torrent {_id}, id: {tid} | data: {data}')
                    self.magic_info[_id] = {'uc': -uc}
                if self.magic_info.cost() > CONFIG['total_uc_max']:
                    self.magic_info.save()

        except Exception as e:
            logger.exception(e)

    def magic_for_self(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            try:
                loop.run_until_complete(self.main())
            except Exception as e:
                logger.exception(e)
            finally:
                gc.collect()
                sleep(CONFIG['magic_for_self']['interval'])


class Run(MagicSeed):
    def __init__(self):
        super(Run, self).__init__(None)
        with open(CONFIG['data_path'], 'a', encoding='utf-8'):
            pass
        with open(CONFIG['data_path'], 'r', encoding='utf-8') as fp:
            try:
                self.magic_info = json.load(fp)
            except json.JSONDecodeError:
                pass
        self.clients = []  # 多线程调用同一个 deluge 就 segfault，没找到好的解决办法

    async def main(self):
        info = {}
        for client in self.clients:
            info.update(client.seeding_torrents_info(['name', 'total_seeds', 'tracker']))

        _id_list = []
        for _id, data in info.items():
            if data['tracker'] and 'daydream.dmhy.best' in data['tracker'] or 'tracker.dmhy.org' in data['tracker']:
                if data['total_seeds'] <= CONFIG['magic_for_all']['max_seeder_num']:
                    _id_list.append(_id)

        num = CONFIG['magic_for_all']['torrent_num']
        if len(_id_list) >= num:
            _id_list = random.sample(_id_list, num)
            logger.info(f'Found {num} torrent which num of seeders < {num}  --> {_id_list}')
        else:
            logger.info(f'There are only {len(_id_list)} torrents which num of seeders < {num}  --> {_id_list}')

        tasks = [self.check_torrent(_id, info[_id]['name']) for _id in _id_list]
        res = await asyncio.gather(*tasks)
        self.magic_info.del_unused()

        magic_tasks = []
        hr = CONFIG['magic_for_all']['hours']
        for __id, _tid, ur_233 in res:
            if _tid:
                if ur_233 and not (
                        'ts' in self.magic_info[__id]
                        and self.magic_info[__id]['ts'] + 86400 - time() < CONFIG['magic_for_all']['min_rm_hr'] * 3600
                ):
                    magic_tasks.append(self.send_magic(__id, _tid, {'user': 'ALL', 'hours': hr, 'ur': 1, 'dr': 0}))
                elif CONFIG['magic_for_all']['233_all']:
                    magic_tasks.append(self.send_magic(__id, _tid, {'user': 'ALL', 'hours': hr, 'ur': 2.33, 'dr': 0}))
                else:
                    magic_tasks.append(self.send_magic(__id, _tid, {'user': 'ALL', 'hours': hr, 'ur': 1, 'dr': 0}))
                    magic_tasks.append(self.send_magic(__id, _tid, {'user': 'SELF', 'hours': hr, 'ur': 2.33, 'dr': 1}))

        await asyncio.gather(*magic_tasks)
        self.magic_info.save()

    def magic_for_all(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            try:
                loop.run_until_complete(self.main())
            except Exception as e:
                logger.exception(e)
            finally:
                gc.collect()
                sleep(CONFIG['magic_for_all']['interval'])

    def run(self):
        if CONFIG['magic_for_all']['enable'] and not CONFIG['magic_for_self']['enable']:
            self.magic_for_all()
        else:
            with ThreadPoolExecutor(max_workers=len(self.instances) + 1) as executor:
                if CONFIG['magic_for_all']['enable']:
                    executor.submit(self.magic_for_all)
                [executor.submit(instance.magic_for_self) for instance in self.instances]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not isinstance(exc_val, KeyboardInterrupt):
            logger.exception(exc_val)
        os._exit(0)


logger.add(level='DEBUG', sink=CONFIG['log_path'], rotation="5 MB")

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

class_to_name = {Deluge: ['de', 'Deluge', 'deluge'],
                 Qbittorrent: ['qb', 'QB', 'qbittorrent', 'qBittorrent'],
                 Transmission: ['tr', 'Transmission', 'transmission'],
                 Rutorrent: ['ru', 'Rutorrent', 'rutorrent'],
                 UTorrent: ['ut', 'UT', 'utorrent', 'uTorrent', 'µTorrent']}
name_to_class = {name: cls for cls, lst in class_to_name.items() for name in lst}

with Run() as r:
    for client_info in CONFIG['clients_info']:
        c_type = client_info['type']
        del client_info['type']
        MagicSeed(name_to_class[c_type](**client_info))
        r.clients.append(name_to_class[c_type](**client_info))
    r.run()
