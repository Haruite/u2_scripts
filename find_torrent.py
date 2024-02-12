"""
根据根目录名添加种子，适用于种子数据丢失或者辅种
"""

import json
import os
from time import sleep

from typing import Dict

import qbittorrentapi
import requests
from loguru import logger

rename = True  # 是否改名
always_add = False  # 检测到种子缺失文件，是否任然添加(校验后需要下载)
host = 'localhost'
port = 8080
username = ''
password = ''
token = '7'
uid = 50096
passkey = ''
src_path = 'G:\\BDMV'
proxies = {
    # 'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'
}
char_map = {
    '?': '？',
    '*': '٭',
    '<': '《',
    '>': '》',
    ':': '：',
    '"': "'",
    '/': '／',
    '\\': '／',
    '|': '￨'
}
os_rename = True

logger.add(sink=f'{os.getcwd()}\\logs\\find_torrent-{{time}}.log', level='DEBUG')
client = qbittorrentapi.Client(host=host, port=port, username=username, password=password)


def check_files(path: str, torrent_tree: Dict):
    it = True
    paths = [path]

    def _check_files(info: dict):
        nonlocal it
        for k, v in info.items():
            paths.append(k)
            if v['type'] == 'directory':
                _check_files(v["children"])
            else:
                path = '/'.join(paths)
                if not os.path.exists(path) or os.path.getsize(path) != v['length']:
                    it = False
                    return
            paths.pop(-1)

    _check_files(torrent_tree)
    return it


hashes = {torrent.hash for torrent in client.torrents_info(status_filter='completed')}


for fn in os.listdir(src_path):
    data = {'uid': uid, 'token': token, 'torrent_name': fn}
    _json = requests.post(
        'https://u2.kysdm.com/api/v1/search_torrent_name', data=json.dumps(data), proxies=proxies).json()
    torrents = _json['data']['torrents']
    if torrents:
        for torrent in torrents:
            if not torrent['torrent_tree']:
                continue
            if always_add or check_files(src_path, json.loads(torrent['torrent_tree'])):
                tid = torrent['torrent_id']
                _id = torrent['torrent_hash']
                name = torrent['torrent_name']
                logger.info(f'文件名 {fn} 搜索到对应的种子, id 为 {tid}')

                if _id in hashes:
                    logger.info(f'文件名 {fn} 对应的种子已在客户端')
                else:
                    dl_link = f'https://u2.dmhy.org/download.php?id={tid}&passkey={passkey}&https=1'
                    try:
                        content = requests.get(dl_link, proxies=proxies).content
                        client.torrents_add(torrent_files=content, save_path=src_path, is_paused=True)
                    except Exception as e:
                        logger.error(e)
                    logger.info(f'已添加种子, id 为 {tid}')
                    sleep(0.1)  # 如果不 sleep 可能报错没有这个种子

                if rename:
                    title = ''.join(char_map.get(char) or char for char in torrent['title'])
                    try:
                        client.torrents_rename(_id, title)
                        logger.info(f"成功重命名种子名称 {name} -> {title}")
                    except Exception as e:
                        logger.error(e)
                    if os.path.isdir(f'{src_path}/{name}'):
                        try:
                            if os_rename:
                                os.renames(f'{src_path}/{name}', f'{src_path}/{title}')
                            client.torrents_rename_folder(_id, name, title)
                            logger.info(f"成功重命名种子文件夹 {name} -> {title}")
                        except Exception as e:
                            logger.error(e)
                    else:
                        try:
                            os.rename(f'{src_path}/{name}', f'{src_path}/{title}')
                            client.torrents_rename_file(_id, 0, title)
                            logger.info(f"成功重命名种子文件 {name} -> {title}")
                        except Exception as e:
                            logger.error(e)
                break
        else:
            logger.warning(f'文件名 {fn} 缺少文件， 可能的种子 id 有 {tuple(torrent["torrent_id"] for torrent in torrents)}')
    else:
        logger.debug(f'文件名 {fn} 未搜索到对应的种子')
