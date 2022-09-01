# python3.7及以上
# 依赖：pip3 install PyYAML requests bs4 deluge-client qbittorrent-api loguru pytz
# Azusa 大佬的 api，见 https://github.com/kysdm/u2_api，自动获取 token: https://greasyfork.org/zh-CN/scripts/428545

import asyncio
import gc
import json
import os
import re
import sys

import aiohttp
import yaml
import pytz
import qbittorrentapi

from abc import abstractmethod, ABCMeta
from collections import UserDict
from functools import wraps
from ssl import SSLError
from datetime import datetime
from time import time, sleep

from bs4 import BeautifulSoup
from loguru import logger
from concurrent.futures import ThreadPoolExecutor

from deluge_client import FailedToReconnectException, LocalDelugeRPCClient
from qbittorrentapi.exceptions import APIConnectionError, HTTPError

# *************************必填配置************************
clients_info = '''  # 按 yaml 语法填写客户端信息
-  # 可以填写多个客户端
    type: deluge  # de, deluge
    host: 127.0.0.1  # IP
    port: 58846  # daemon 端口
    username:   # 本地可以设置跳过用户名和密码
    password:   # cat ~/.config/deluge/auth
- 
    type: qbittorrent  # qb, qbittorrent
    host: http://127.0.0.1  # IP
    port: 8080  # webui 端口
    username:   # web 用户名
    password:   # web 密码
'''
requests_args = {'cookies': {'nexusphp_u2': ''},
                 'headers': {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                                           'Chrome/104.0.5112.102 Safari/537.36 Edg/104.0.1293.70'},
                 'proxy': '',  # 'http://127.0.0.1:10809'
                 'timeout': 10
                 }

# ************************可修改配置***********************
api_token = ''  # api 的 token，填了将默认使用 api 查询种子信息，不填就直接从 u2 网页获取信息
uid = 50096  # 如果填了 api_token，则需要 uid
magic_downloading = True  # 是否下载中的种子放 2.33x 魔法
min_rate = 360  # 最小上传速度(KiB/s)
min_size = 5  # 最小体积(GiB)
min_d = 180  # 种子最小生存天数
uc_max = 30000  # 单个魔法最大 uc 使用量
total_uc_max = 200000  # 24h 内 uc 最大使用量
interval = 60  # 检查的间隔

data_path = f'{os.path.splitext(__file__)[0]}.data.txt'  # 数据路径
log_path = f'{os.path.splitext(__file__)[0]}.log'  # 日志路径

# *************************END****************************


class CheckKeys:
    str_keys = ('name', 'tracker', 'state')
    int_keys = ('total_uploaded', 'upload_payload_rate')

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
                                 f'These are the all available keys: \n{args[0].all_keys}'
                                 )
            res = func(*args, **kwargs)
            if not isinstance(res, dict):
                raise TypeError(f'Return value of function {func.__name__} should be dict type')
            if res:
                _res = res
                if list(res.keys())[0] in keys:
                    _res = {'_': _res}

                for _id, data in _res.items():
                    for key, val in data.items():
                        if key in self.int_keys and not isinstance(val, int):
                            raise TypeError(f'The value of "{key}" should be int type')
                        elif key in self.str_keys and not isinstance(val, str):
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
                'tracker',  # str 类型，种子当前 tracker
                'upload_payload_rate',  # int 类型，上传速度 (B / s)
                )

    wrapped_class = []

    def __new__(cls, *args, **kwargs):
        ins = super(BtClient, cls).__new__(cls)
        subclass = ins.__class__

        if subclass not in cls.wrapped_class:
            setattr(subclass, 'active_torrents_info', check_keys(getattr(subclass, 'active_torrents_info')))
            cls.wrapped_class.append(subclass)

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


class Deluge(LocalDelugeRPCClient, BtClient):
    timeout = 10

    def __init__(self, host='127.0.0.1', port=58846, username='', password=''):
        super().__init__(host, port, username, password, decode_utf8=True, automatic_reconnect=True)

    def call(self, method, *args, **kwargs):
        if not self.connected and method != 'daemon.login':
            for i in range(6):
                try:
                    self.reconnect()
                    logger.debug(f'Connected to deluge client on {self.host}')
                    break
                except SSLError:
                    sleep(0.3 * 2 ** i)
        try:
            return super().call(method, *args, **kwargs)
        except FailedToReconnectException:
            logger.error(f'Failed to reconnect to deluge client on {self.host}')
        except TimeoutError:
            logger.error(f'Timeout when connecting to deluge client on {self.host}')
        except Exception:
            raise

    def active_torrents_info(self, keys):
        return self.call('core.get_torrents_status', {'state': 'Active'}, keys)


class Qbittorrent(qbittorrentapi.Client, BtClient):
    de_key_to_qb = {'name': 'name', 'tracker': 'tracker', 'total_size': 'size',
                    'upload_payload_rate': 'upspeed', 'state': 'state'}

    def __init__(self, host='http://127.0.0.1', port=8080, username='', password=''):
        super().__init__(host=host, port=port, username=username, password=password,
                         REQUESTS_ARGS={'timeout': 10}, FORCE_SCHEME_FROM_HOST=True)

    def call(self, method, *args, **kwargs):
        try:
            return self.__getattribute__(method)(*args, **kwargs, _retries=5)
        except HTTPError as e:
            logger.error(f'Failed to connect to qbittorrent on {self.host} due to http error: {e}')
        except APIConnectionError as e:
            logger.error(f'Failed to connect to qbittorrent on {self.host} due to '
                         f'qbittorrentapi.exceptions.APIConnectionError:  {e}')

    def active_torrents_info(self, keys):
        torrents_info = {}
        for torrent in self.call('torrents_info', status_filter=['active']):
            _id = torrent['hash']
            torrents_info[_id] = {}
            for key in keys:
                torrents_info[_id][key] = torrent.get(self.de_key_to_qb[key])
        return torrents_info


class MagicInfo(UserDict):
    def __setitem__(self, key, value):
        super(MagicInfo, self).__setitem__(key, value)
        self.save()

    def __set__(self, instance, value):
        self.data = value

    def del_unused(self):
        for _id in list(self.data.keys()):
            if int(time()) - self.data[_id]['ts'] > 86400:
                del self.data[_id]
                self.save()

    def cost(self):
        uc_cost = 0
        for _id, val in self.data.items():
            uc_cost += val.get('uc') or 0
        return uc_cost

    def save(self):
        with open(data_path, 'w', encoding='utf-8') as fp:
            json.dump(self.data, fp)


class Request:
    def __init__(self):
        self.session = None
        self.u2_args = requests_args
        self.api_args = {'timeout': requests_args.get('timeout'),
                         'proxy': requests_args.get('proxy')
                         }

    async def request(self, url, method='get', retries=5, **kwargs):
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
        self.to_ids = []

    async def main(self):
        self.to_ids = []
        tasks = []

        for _id, data in self.client.active_torrents_info(
                ['name', 'tracker', 'total_size', 'upload_payload_rate', 'state']).items():

            if _id in self.magic_info:
                if int(time()) - self.magic_info[_id]['ts'] < 86400:  # 魔法还在有效期内则不加入
                    continue
            if not data['tracker'] or 'daydream.dmhy.best' not in data['tracker'] and \
                    'tracker.dmhy.org' not in data['tracker']:  # 过滤不是 U2 的种子
                continue
            if not magic_downloading and data['state'] in ['Downloading', 'downloading']:  # 过滤下载中的种子
                continue
            if data['upload_payload_rate'] < min_rate * 1024:
                continue
            if data['total_size'] < min_size * 1024 ** 3:
                continue

            tasks.append(self.check_torrent(_id, data['name']))

        await asyncio.gather(*tasks)

        self.magic_info.del_unused()
        magic_tasks = [self.send_magic(_id, tid) for _id, tid in self.to_ids]
        await asyncio.gather(*magic_tasks)

    async def check_torrent(self, _id, name):
        if not api_token:
            await self.info_from_u2(_id, name)
        else:
            try:
                await self.info_from_api(_id, name)
            except Exception as e:
                logger.exception(e)
                await self.info_from_u2(_id, name)

    async def info_from_u2(self, _id, name):
        url = f'https://u2.dmhy.org/torrents.php?search={_id}'
        params = {'search': _id, 'search_area': 5}
        page = await self.request(url, params=params)
        soup = BeautifulSoup(page.replace('\n', ''), 'lxml')

        '''获取时区'''
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        tz = [tz_info[len(pre):][:-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]
        timezone = pytz.timezone(tz)

        table = soup.select('table.torrents')
        if table:
            cont = table[0].contents[1].contents

            '''判断种子时间是否小于最小天数'''
            delta = time() - self.ts(cont[3].time.get('title') or cont[3].time.get_text(' '), timezone)
            if delta < min_d * 86400:
                self.magic_info[_id] = {'ts': int(time())}
                return

            '''判断种子是否已有 2.33x 优惠'''
            tid = int(cont[1].a['href'][15:-6])
            for img in cont[1].select('tr')[1].td.select('img') or []:
                if img.get('class') == ['arrowup'] and float(img.next_element.text[:-1].replace(',', '.')) >= 2.33:
                    logger.info(f'Torrent {_id}, id: {tid}: 2.33x uploaded magic existed!')
                    time_tag = cont[1].time
                    if time_tag:
                        magicst = self.ts(time_tag.get('title') or time_tag.text, timezone) - 86400
                        self.magic_info[_id] = {'ts': magicst}
                    else:
                        self.magic_info[_id] = {'ts': int(time()) + 86400 * 30}
                    return

            self.to_ids.append((_id, tid))
        else:
            logger.error(f'Torrent {_id} , name: {name} was not found in u2...')
            self.magic_info[_id] = {'ts': int(time()) + 86400 * 3}

    async def info_from_api(self, _id, name):
        _param = {'uid': uid, 'token': api_token}

        history_data = await self.request('https://u2.kysdm.com/api/v1/history',
                                          params={**_param, 'hash': _id})
        if history_data['data']['history']:
            tid = history_data['data']['history'][0]['torrent_id']
            upload_date = history_data['data']['history'][0]['uploaded_at']
            if time() - self.ts(upload_date.replace('T', ' ')) < min_d * 86400:
                self.magic_info[_id] = {'ts': int(time())}
                return

            res = await self.request('https://u2.kysdm.com/api/v1/promotion_super',
                                     params={**_param, 'torrent_id': tid})
            if float(res['data']['promotion_super'][0]['private_ratio'].split(' / ')[0]) >= 2.33:
                logger.info(f'Torrent {_id}, id: {tid}: 2.33x uploaded magic existed!')

                res = await self.request('https://u2.kysdm.com/api/v1/promotion_specific',
                                         params={**_param, 'torrent_id': tid})
                pro_list = res['data']['promotion']

                pro_end_time = time()
                for pro_data in pro_list:
                    if float(pro_data['ratio'].split(' / ')[0]) >= 2.33:
                        if not pro_data['for_user_id'] or pro_data['for_user_id'] == uid:
                            if not pro_data['expiration_time']:
                                self.magic_info[_id] = {'ts': int(time()) + 86400 * 30}
                                break
                            else:
                                end_time = self.ts(pro_data['expiration_time'].replace('T', ' '))
                                if self.ts(pro_data['from_time'].replace('T', ' ')) < time() < end_time:
                                    if end_time > pro_end_time:
                                        pro_end_time = end_time
                self.magic_info[_id] = {'ts': int(pro_end_time) - 86400}
                return

            self.to_ids.append((_id, tid))
        else:
            logger.error(f'Torrent {_id} , name: {name} was not found...')
            self.magic_info[_id] = {'ts': int(time()) + 86400 * 3}

    @staticmethod
    def ts(date, tz=pytz.timezone('Asia/Shanghai')):
        dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        return tz.localize(dt).timestamp()

    async def send_magic(self, _id, tid):
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

        try:
            p1 = await self.request('https://u2.dmhy.org/promotion.php?test=1', method='post', data=data)
            res_json = json.loads(p1)
            if res_json['status'] == 'operational':
                uc = int(float(BeautifulSoup(res_json['price'], 'lxml').span['title'].replace(',', '')))

                if uc > uc_max:
                    logger.warning(f'Torrent id: {tid} cost {uc}uc, too expensive')
                    self.magic_info[_id] = {'ts': int(time())}
                    return

                if self.magic_info.cost() > total_uc_max:
                    logger.warning('24h ucoin usage exceeded, Waiting ------')
                    return

                url = f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}'
                p2 = await self.request(url, method='post', retries=0, data=data)
                if re.match(r'^<script.+<\/script>$', p2):
                    logger.info(f'Sent magic to torrent {_id}, tid: {tid}. Ucoin usage {uc}, '
                                f'24h total usage {self.magic_info.cost()}')
                    self.magic_info[_id] = {'ts': int(time()), 'uc': uc}
                else:
                    logger.error(f'Failed to send magic to torrent {_id}, id: {tid}')

        except Exception as e:
            logger.exception(e)

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            try:
                loop.run_until_complete(self.main())
            except Exception as e:
                logger.exception(e)
            finally:
                gc.collect()
                sleep(interval)


class Run(MagicSeed):
    def __init__(self):
        super(Run, self).__init__(None)
        with open(data_path, 'a', encoding='utf-8'):
            pass
        with open(data_path, 'r', encoding='utf-8') as fp:
            try:
                self.magic_info = json.load(fp)
            except json.JSONDecodeError:
                pass

    def run(self):
        with ThreadPoolExecutor(max_workers=len(self.instances)) as executor:
            [executor.submit(instance.run) for instance in self.instances]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logger.exception(exc_val)
        os._exit(0)


logger.add(level='DEBUG', sink=log_path, rotation="5 MB")

for client_info in yaml.load(clients_info, yaml.FullLoader):
    c_type = client_info['type']
    del client_info['type']
    if c_type in ['de', 'Deluge', 'deluge']:
        MagicSeed(Deluge(**client_info))
    elif c_type in ['qb', 'QB', 'qbittorrent', 'qBittorrent']:
        MagicSeed(Qbittorrent(**client_info))

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

with Run() as r:
    r.run()
