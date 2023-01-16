"""删除赚 UC 效率不高的种子

Notes:
    1. 客户端只支持 qb
    2. 只删不加
    3. 种子必须处于做种状态，不然统计不到做种人数
    4. 需要给要删的种子加标签
    5. 可以根据体积，秒收，效率来设定指标
    6. 先不要删太多，分几次来
    7. 不要在站免时运行, 否则误差会很大
    8. 谨慎操作, 如果出错重新运行
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import time
from datetime import datetime
from typing import Union, Dict, Tuple

import pytz
import requests
import qbittorrentapi

# **********************填写配置***********************
tag = 'tmp'  # 标签，不是这个标签的种子不删
host = '127.0.0.1'
port = 8080
username = ''
password = ''
uid = 50096
token = ''  # u2-api token --> https://greasyfork.org/zh-CN/scripts/428545
proxies = {'http': '', 'https': ''}  # 代理
max_seeder = 3  # 做种人数小于或等于这个值不删
an_hour = False  # 做种是否满一小时
free_days = 4.5  # 估计平均一个月站免的天数, 之前算过一年半内是 5.2, 懒得写从优惠历史计算了, 反正这个值是不稳定的

# 以下参数见 /mpseed.php, s0 和 sd0 tracker 在计算时是实时统计值(网页上的数字可能一天更新一次),
# 相对来说 s0 比较稳定, sd0 有一定的历史因素. 其他值基本不变，除非 sysop 手动修改
b = 14.5
s0 = 34.106
d = 0.3
e = 0.001
sd0 = 35.149
l0 = 1096
# ************************END*************************


class DeleteTorrents:
    def __init__(self):
        self.client = qbittorrentapi.Client(host, port, username, password)

        self.info_file = f'{os.path.splitext(__file__)[0]}.torrents_info'
        info_t = Dict[str, Dict[str, Union[Tuple[float, float], str, int, float]]]
        self.torrents_info: info_t = {}
        if not os.path.exists(self.info_file):
            with open(self.info_file, 'a'):
                pass
            self.get_info_from_client()
        else:
            with open(self.info_file, 'r') as fp:
                self.torrents_info: info_t = json.load(fp)

        self.count = 0
        self.unhandled_hashes = []

    def get_info_from_client(self):
        for torrent in self.client.torrents_info():
            if 'daydream.dmhy.best' in torrent.magnet_uri and tag in torrent.tags:
                self.torrents_info[torrent.hash] = {
                    'name': torrent.name,
                    'total_size': torrent.total_size,
                    'total_seeds': torrent.num_complete if an_hour else torrent.num_complete + 1,
                }
        self.save_info()

    def save_info(self):
        with open(self.info_file, 'w') as fp:
            json.dump(self.torrents_info, fp)

    def update_info(self):
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(self.search_id, _id): _id
                for _id in self.torrents_info if 'tid' not in self.torrents_info[_id]
            }
            for future in as_completed(futures):
                future.result()
        self.save_info()
        if self.unhandled_hashes:
            hash_name = '\n'.join([f"{_id} | {self.torrents_info[_id]['name']}" for _id in self.unhandled_hashes])
            print(f'以下 hash 种子未被找到\n{hash_name}')

    def search_id(self, _id):
        _params = {'uid': uid, 'token': token}
        history_json = requests.get(
            'https://u2.kysdm.com/api/v1/history',
            params={**_params, 'hash': _id}, proxies=proxies
        ).json()
        if history_json['data']['history']:
            data = history_json['data']['history'][0]
            tid = data['torrent_id']

            pro_json = requests.get(
                'https://u2.kysdm.com/api/v1/promotion_super',
                params={**_params, 'torrent_id': tid}, proxies=proxies
            ).json()
            pro = list(map(float, pro_json['data']['promotion_super'][0]['public_ratio'].split(' / ')))

            info = {
                'tid': tid, 'name': data['torrent_name'], 'cat': data['category'],
                'date': data['uploaded_at'].replace('T', ' '), 'pro': pro
            }
            print(f'Hash 值 {_id} 已找到相关信息: {info}')
            self.torrents_info[_id].update(info)

            self.count += 1
            if self.count % 100 == 0:
                self.save_info()
        else:
            self.unhandled_hashes.append(_id)
            print(f'Hash 值 {_id} 未找到相关信息')

    def sort_torrents(self):
        for _id, data in self.torrents_info.items():
            if 'pro' in data:
                s = data['total_size'] / 1024 ** 3
                sd = data['total_seeds']

                if data['cat'] in ['BDMV', 'DVDISO', 'Lossless Music']:
                    p = 1
                elif data['pro'][0] >= 2 or data['pro'][1] <= 0.5:
                    p = 0.5
                else:
                    r = free_days / 30
                    p = r * 0.5 + (1 - r) * max(0.5, max(2 - data['pro'][0], 0) * min(data['pro'][1], 1))

                dt = datetime.strptime(data['date'], '%Y-%m-%d %H:%M:%S')
                ttl = int(time()) - pytz.timezone('Asia/Shanghai').localize(dt).timestamp()
                if ttl < 86400 * 60:
                    l = 0
                else:
                    l = 1096 if ttl >= 86400 * l0 else ttl / 86400

                data['x'] = d * s0 / b / s + p * (1 + e * sd0 * l * s0 / b / s / sd)
                '''考虑体积、数量和保种的种子单位体积一小时内获得的 uc, 相对于体积和做种人数无穷大的原盘的倍数'''
            else:
                data['x'] = 99999
        self.torrents_info = dict(sorted(self.torrents_info.items(), key=lambda tup: tup[1]['x']))
        self.save_info()

    def main(self, test=False, target_size=None, target_speed=None, min_x=None):
        size = 0
        num = 0
        ms = 0

        for _id, data in self.torrents_info.items():
            if data['x'] != 99999:
                size += data['total_size']
                num += 1
                ms += data['x'] * data['total_size'] / 1024 ** 3 * b / s0 / 3600

        if target_size or target_speed or min_x:
            size1 = size
            num1 = num
            ms1 = ms
            delete_hashes = []
            for _id, data in self.torrents_info.items():
                if data['total_seeds'] > max_seeder and data['x'] != 99999:
                    if (
                            target_size is not None and size1 > target_size
                            or target_speed is not None and ms1 > target_speed
                            or min_x is not None and data['x'] < min_x
                    ):
                        delete_hashes.append(_id)
                        size1 -= data['total_size']
                        num1 -= 1
                        ms1 -= data['x'] * data['total_size'] / 1024 ** 3 * b / s0 / 3600
                    else:
                        break
            avg = ms1 * 3600 * s0 / b * 1024 ** 3 / size1
            if test:
                print(
                    f'预计删除 {len(delete_hashes)} 个种子, 删除后剩余 {num1} 个种子, '
                    f'总计大小 {size1}({self.show_size(size1)}), '
                    f'预计秒收 {ms1:.3f} UCoin, 平均效率 {avg:.3f}'
                )
            else:
                self.client.torrents_delete(delete_files=True, delete_hashes=delete_hashes)
                fn = f"{os.path.splitext(__file__)[0]}.delete_hashes.{datetime.now().__str__().replace(':', '-')}.txt"
                with open(fn, 'a') as fp:
                    json.dump(delete_hashes, fp)
                for _id in delete_hashes:
                    del self.torrents_info[_id]
                self.save_info()
                print(f'成功删除 {len(delete_hashes)} 个种子, 删除的种子 hash 保存在 {fn}')
        elif test:
            avg = ms * 3600 * s0 / b * 1024 ** 3 / size
            print(
                f'总共 {num} 个种子, 总计大小 {size}({self.show_size(size)}), '
                f'估计秒收 {ms:.3f} UCoin, 平均效率 {avg:.3f}'
            )

    @staticmethod
    def show_size(byte):
        units = {'B': 0, 'KiB': 1, 'MiB': 2, 'GiB': 3, 'TiB': 6, 'PiB': 9}
        for unit, digits in units.items():
            if byte >= 1024:
                byte /= 1024
            else:
                return f'{round(byte, digits)} {unit}'

    @staticmethod
    def str_to_byte(st):
        try:
            return int(float(st))
        except:
            try:
                num, unit = st.split(' ')
                units = ['b', 'kb', 'mb', 'gb', 'tb', 'pb', 'b', 'kib', 'mib', 'gib', 'tib', 'pib']
                return int(float(num) * 1024 ** (units.index(unit.lower()) % 6))
            except:
                pass

    def run(self):
        input(f'{__doc__}\n输入任意键继续:\n')
        self.update_info()
        self.sort_torrents()

        if not os.path.exists(f'{self.info_file}.bak'):
            with open(f'{self.info_file}.bak', 'w') as fp:
                json.dump(self.torrents_info, fp)

        self.main(test=True)
        while True:
            _ = input('输入操作: 0.退出 1.根据指定体积删种 2.根据指定秒收删种 3.根据效率删种\n')
            if _ == '0':
                exit()
            if _ == '1':
                while True:
                    size = self.str_to_byte(
                        input(
                            '输入删种后目标体积, 字节数或者数字和单位用空格分开e.g. '
                            '1278399 | 1.3 tb | 578 GiB\n'
                        ).strip()
                    )
                    if size:
                        self.main(test=True, target_size=size)
                        if input('按 y 继续\n').lower() == 'y':
                            self.main(target_size=size)
                        break
            if _ == '2':
                speed = float(input('输入秒收\n').strip())
                self.main(test=True, target_speed=speed)
                if input('按 y 继续\n').lower() == 'y':
                    self.main(target_speed=speed)
            if _ == '3':
                x = float(input('输入最低效率\n').strip())
                self.main(test=True, min_x=x)
                if input('按 y 继续\n').lower() == 'y':
                    self.main(min_x=x)


if __name__ == '__main__':
    DeleteTorrents().run()
