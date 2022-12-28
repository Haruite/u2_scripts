"""
deluge 删种脚本，优先保留体积大、下载人数多、上传速度高、做种时间少的种子。
用过一些删种工具，逻辑都比较粗暴，所以自己写了一个，
每个种子综合考虑各项情况，分配加权把各项加起来，从低到高删除直到剩余空间大于指定值。
40% 取当前速度和平均速度的平均值，20% 取做种中种子的上传速度按做种时间权重分配的值，
剩下的 40% 中其中一部分为取做种中种子的上传速度按下载上传人数权重分配的值，另一部分为按做种人数分配的值
比例取决于参数 KS，同时有考虑体积，体积越大加权越高（大约是 0.2 次方）
"""

from time import sleep
from deluge_client import LocalDelugeRPCClient, FailedToReconnectException
from loguru import logger
from ssl import SSLError
from collections import deque
from typing import Union, Any, Tuple, List
import os

MIN_FREE_SPACE = 3725  # type: Union[int, float]
'最小剩余空间(GiB)，当下载速度未超过临界值 MAX_DR 时小于这个值删种'
MIN_FREE_SPACE_LOWER = 3725 / 3  # type: Union[int, float]
'''当下载速度超过临界值 MAX_DR 时小于这个值删种。
硬盘空间足够的话建议两个值的差 1024(1TB) 以上'''
MAX_DR = 10 * 1024 ** 2  # type: Union[int, float]
'下载速度临界值'
MODE = 1  # type: Any
'为 1 时先删除做种中的种子，删完后再删下载中的种子；否则综合考虑一起删'
KS = 0.5  # type: Union[int, float]
'按做种人数分配的权重占 40% 的比例，取值范围 [0, 1]，为 0 代表不考虑做种人数，这个参数的目的在于延长孤种的保种时间'
INTERVAL = 600  # type: Union[int, float]
'删种的时间间隔'
MIN_DOWN_TIME = 3600  # type: Union[int, float]
'下载时间小于这个值不删'
S0 = 300  # type: Union[int, float]
LOG_PATH = ''  # type: str
EXCLUDE_LABELS = ['seed', 'public']  # type: Union[Tuple[Any, ...], List[str]]
'如果种子有这些标签，删种时会跳过'


class Deluge(LocalDelugeRPCClient):
    timeout = 10

    def __init__(self,
                 host: str = '127.0.0.1',
                 port: int = 58846,
                 username: str = '',
                 password: str = '',
                 decode_utf8: bool = True,
                 automatic_reconnect: bool = True,
                 ):
        super().__init__(host, port, username, password, decode_utf8, automatic_reconnect)

    def call(self, method, *args, **kwargs):
        if not self.connected and method != 'daemon.login':
            for i in range(5):
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
        except:
            raise


class AutoDel:
    def __init__(self, client: Deluge):
        self.client = client
        self.sur = deque(maxlen=100)
        self.free_space = MIN_FREE_SPACE * 1024 ** 3
        self.torrent_status = {}
        self.ses_dr = 0
        self.torrent_keys = ['active_time', 'download_payload_rate', 'name', 'state',
                             'seeding_time', 'total_peers', 'total_seeds', 'total_size',
                             'total_done', 'total_uploaded', 'upload_payload_rate', 'label'
                             ]

    def update_session(self):
        self.free_space = self.client.core.get_free_space()
        if not isinstance(self.free_space, int):
            raise

        seed_ur = 0
        up_status = self.client.core.get_torrents_status({'state': 'Seeding'}, ['upload_payload_rate'])
        if not isinstance(up_status, dict):
            raise
        for _id, data in up_status.items():
            seed_ur += data['upload_payload_rate']
        self.sur.append(seed_ur)

        self.ses_dr = self.client.core.get_session_status(['download_rate'])['download_rate']

        if self.free_space < MIN_FREE_SPACE * 1024 ** 3:
            self.torrent_status = self.client.core.get_torrents_status({}, self.torrent_keys)
            if not isinstance(self.torrent_status, dict):
                raise

    def run(self):
        while True:
            try:
                while True:
                    try:
                        self.update_session()
                        break
                    except:
                        pass
                min_space = (MIN_FREE_SPACE if self.ses_dr < MAX_DR else MIN_FREE_SPACE_LOWER) * 1024 ** 3
                if self.free_space >= min_space:
                    logger.debug(f'There is free space {self.free_space / 1024 ** 3:.3f} GiB. '
                                 f'No need to del any torrents.')
                else:
                    indicator, info = self.weight()
                    while self.free_space < min_space:
                        if not indicator:
                            break
                        i = indicator.index(min(indicator))
                        state = 'Failed to delete'
                        try:
                            self.client.core.remove_torrent(info[i]['_id'], True)
                            state = 'Successfully deleted'
                        except TimeoutError as e:
                            # 正常操作，一般实际上是已经删了
                            logger.error(f'{e.__class__.__name__}: {e}')
                        except Exception as e:
                            if e.__class__.__name__ == 'InvalidTorrentError':
                                # 正常操作，基本上也是删了
                                logger.error(f"{e.__module__}.{e.__class__.__name__}: "
                                             f"Torrent_id {info[i]['_id']} not in session")
                            else:
                                logger.exception(e)
                        self.free_space += info[i]['done']
                        logger.warning(f"{state} {info[i]['state'].lower()} torrent {info[i]['_id']}, "
                                       f"name | {info[i]['name']}. ")
                        if state == 'Successfully deleted':
                            logger.info(f"{info[i]['done'] / 1024 ** 3:.3f} GiB space released. "
                                        f"Free space {self.free_space / 1024 ** 3:.3f} GiB.")
                        sleep(info[i]['done'] / 1024 ** 3 / 10)
                        del indicator[i]
                        del info[i]
            except Exception as e:
                logger.exception(e)
            finally:
                sleep(INTERVAL)

    @staticmethod
    def torrent_filter(state):
        return lambda tup: tup[1]['label'] not in EXCLUDE_LABELS and tup[1]['state'] == state

    def weight(self):
        total_peer_weight = 0
        total_time_weight = 0
        total_peers = 0
        num = 0
        indicator = []
        info = []
        e_m = 0.0
        av_ur = sum(self.sur) / len(self.sur)
        if av_ur == 0:
            av_ur = 1048576

        for _id, data in filter(self.torrent_filter('Seeding'), self.torrent_status.items()):
            total_peers += data['total_peers']
            num += 1

        av_peer_num = total_peers / num if num > 0 else 0

        for _id, data in filter(self.torrent_filter('Seeding'), self.torrent_status.items()):
            data['peer_weight'] = (data['total_peers'] * (1 - KS) + av_peer_num * KS) / (
                data['total_seeds'] if data['total_seeds'] > 0 else 1) * data['total_size']
            total_peer_weight += data['peer_weight']
            k_time = data['seeding_time'] / 3600
            k_size = data['total_size'] / (S0 * 1024 ** 3)
            data['time_weight'] = pow(1 + pow((k_time / k_size), 2), -0.5)
            total_time_weight += data['time_weight']

        if total_time_weight > 0:
            for _id, data in filter(self.torrent_filter('Seeding'), self.torrent_status.items()):
                ur_e = data['upload_payload_rate'] * 0.4
                ur_tm_p = av_ur * data['time_weight'] / total_time_weight
                if total_peer_weight > 0:
                    ur_pr_p = av_ur * data['peer_weight'] / total_peer_weight
                    ur_e += ur_pr_p * 0.4 + ur_tm_p * 0.2
                else:
                    ur_e += ur_tm_p * 0.6
                sz_e = data['total_done'] / 1024 ** 3
                e = ur_e * pow(sz_e, -0.8)
                indicator.append(e)
                info.append({'_id': _id, 'name': data['name'], 'done': data['total_done'], 'state': data['state']})
            if MODE == 1 or av_ur == 0:
                e_m = max(indicator) + 1

        for _id, data in filter(self.torrent_filter('Downloading'), self.torrent_status.items()):
            if data['active_time'] < MIN_DOWN_TIME:
                continue
            au = data['total_uploaded'] / (data['active_time'] + 1)
            ur = data['upload_payload_rate']
            ur_e = au * 0.5 + ur * 0.5
            sz_a = data['download_payload_rate'] * INTERVAL / 2
            if sz_a < data['total_size'] - data['total_done']:
                sz_e = data['total_size'] / 1024 ** 3
            else:
                sz_e = (sz_a + data['total_done']) / 1024 ** 3
            if sz_e == 0:
                continue
            e = ur_e * pow(sz_e, -0.8) + e_m
            indicator.append(e)
            info.append({'_id': _id, 'name': data['name'], 'done': data['total_done'], 'state': data['state']})

        return indicator, info


log_path = LOG_PATH or f'{os.path.splitext(__file__)[0]}.log'
logger.add(level='DEBUG', sink=log_path, encoding='utf-8', rotation="5 MB")

AutoDel(Deluge()).run()
